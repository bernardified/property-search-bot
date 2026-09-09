"""
FastAPI backend for the property webapp — a second frontend beside the
Telegram bot (bot.py is untouched; both call the same domain modules).

Run locally:  uvicorn api:app --reload --port 8000
Railway:      uvicorn api:app --host 0.0.0.0 --port $PORT
              (own service, independent of property-bot / ura-cache-refresh)

The API is a thin JSON wrapper: search_property / get_nearby_info /
get_rental_by_band already return plain dicts, so endpoints only combine
them — no domain logic lives here.

Amenity lookup is slow (sequential Google Places/Distance Matrix calls —
the reason the bot fetches amenities lazily per button tap), so it is a
separate endpoint: /api/property answers fast from the URA cache with a
cheap OneMap geocode for the map pin, and the frontend fetches
/api/amenities afterwards while showing a loading state.

CORS: none configured on purpose — the frontend is served same-origin by
this app (StaticFiles below). Revisit if the webapp ever moves to its own
domain for a client-facing version.
"""

import re
import threading
from datetime import datetime

from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from ura import search_property, price_trend
from rental import get_rental_by_band
from maps import get_nearby_info, geocode_building, resolve_postal_code
from cache.cache_hdb import is_hdb_residential_block
from cache.cache_ura import get_ura_data
from storage import get_recent_searches
from utils import (
    SIZE_BANDS,
    get_mongo_db,
    parse_float,
    parse_mmyy_date,
    sqm_to_sqft,
    svy21_to_wgs84,
)

_POSTAL_RE = re.compile(r"^\d{6}$")

app = FastAPI(title="Property Bot API")


# ── Pure payload shaping (no IO — tested in Test/test_api.py) ────────────────

def sale_prices_from_bands(bands: dict) -> dict:
    """Band → latest transacted price, the shape get_rental_by_band expects
    for yield. Mirrors bot.py's amenity_callback exactly (latest price per
    band, not the 12-month average)."""
    return {label: {"price": txn.get("price")} for label, txn in (bands or {}).items()}


def order_by_band(mapping: dict) -> dict:
    """Re-key a band→value dict into SIZE_BANDS order (search_property builds
    bands in encounter order; JSON object order is what the frontend renders)."""
    ordered = {b["label"]: mapping[b["label"]] for b in SIZE_BANDS if b["label"] in mapping}
    # Anything unexpected still gets through, at the end.
    ordered.update({k: v for k, v in mapping.items() if k not in ordered})
    return ordered


def project_xy_coords(project_dicts: list, project_name: str) -> dict | None:
    """URA's own x/y (SVY21 → WGS84) for one project, or None when it has none.

    This is the authoritative coordinate — the same source the explore map's
    dots use, validated at 0m median error vs OneMap across 2,376 projects.
    Geocoding the street name instead lands *somewhere along* the street, which
    for a long road is nowhere near the development (YIO CHU KANG ROAD put
    HUNDRED PALMS RESIDENCES ~5km off, and OneMap's and Google's guesses for
    it differed from each other by 1.4km — the pin visibly jumped when the
    amenity response snapped it from one to the other).
    """
    target = (project_name or "").strip().upper()
    for pd in project_dicts:
        if (pd.get("project") or "").strip().upper() != target:
            continue
        x, y = parse_float(pd.get("x")), parse_float(pd.get("y"))
        if not x or not y:
            return None
        lat, lng = svy21_to_wgs84(x, y)
        return {"lat": round(lat, 6), "lng": round(lng, 6)}
    return None


def build_property_payload(ura_result: dict, rental_result: dict, coords: dict | None) -> dict:
    """Combine the three sources into the /api/property response.

    ura_result must already be a successful search (no error/ambiguous).
    coords is the map pin: URA's x/y when the project has one (exact — the
    frontend keeps it and feeds it to /api/amenities), else geocode_building()'s
    street-level OneMap guess, which /api/amenities later snaps to Google's.
    """
    return {
        "development": ura_result["development"],
        "street": ura_result["street"],
        "lat": coords["lat"] if coords else None,
        "lng": coords["lng"] if coords else None,
        "bands": order_by_band(ura_result["bands"]),
        "band_avg_psf": ura_result.get("band_avg_psf", {}),
        "overall_avg_psf": ura_result.get("overall_avg_psf"),
        "overall_psf_count": ura_result.get("overall_psf_count", 0),
        "total_units": ura_result.get("total_units"),
        "expected_top": ura_result.get("expected_top"),
        "under_construction": ura_result.get("under_construction", False),
        "fuzzy_match": ura_result.get("fuzzy_match"),
        "rental": rental_result,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/list")
def api_list():
    return {"searches": get_recent_searches(limit=10)}


# get_ura_data()/get_rental_data() refresh in-line when stale with no
# concurrency guard — fine for the bot (Telegram updates are serial), but
# concurrent web requests would each launch a full URA refresh. Serialize
# the cache-touching section here rather than modifying the cache modules.
_search_lock = threading.Lock()


def _search_with_rental(query: str):
    """The locked search+rental section shared by name and postal searches.
    Returns either a passthrough dict (ambiguous/error) or (ura_result, rental)."""
    with _search_lock:
        ura_result = search_property(query)

        if ura_result.get("ambiguous"):
            return {"ambiguous": True, "candidates": ura_result["candidates"]}
        if "error" in ura_result:
            return {"error": ura_result["error"]}

        # Under construction → no rental contracts of its own; any name match
        # would be stale/wrong (same gate as bot.py's rental button).
        if ura_result.get("under_construction"):
            rental_result = {"unavailable": "under_construction"}
        else:
            sale_prices = sale_prices_from_bands(ura_result.get("bands"))
            rental_result = get_rental_by_band(
                ura_result["development"], sale_prices, ura_result["street"]
            )
    return ura_result, rental_result


def _project_dicts() -> list:
    """The cached URA project list, read under the same lock as the search
    (get_ura_data() refreshes in-line when stale)."""
    with _search_lock:
        transactions, _pipeline = get_ura_data()
    return transactions


def _property_by_postal(postal: str) -> dict:
    """Postal-code search, mirroring bot.py's route_postal discriminator: the
    authoritative HDB Property Information dataset decides HDB vs private —
    NOT the OneMap building name (BTO blocks carry names just like condos)."""
    resolved = resolve_postal_code(postal)
    if not resolved:
        return {"error": f'Couldn\'t find any address for postal code "{postal}".\nPlease double-check the 6-digit code.'}

    block, road = resolved.get("block"), resolved.get("road")
    if block and road and is_hdb_residential_block(block, road):
        return {"error": f'{resolved.get("address") or "That address"} is an HDB block — the webapp covers private developments only for now. Use the Telegram bot for HDB searches.'}
    if not resolved.get("building"):
        return {"error": f'Postal code {postal} isn\'t a condo/apartment development (likely a landed home or commercial building).'}

    result = _search_with_rental(resolved["building"])
    if isinstance(result, dict):
        return result
    ura_result, rental_result = result

    # The postal coordinate is the exact address — use it for the pin AND tell
    # the frontend to feed it to /api/amenities so distances are measured from
    # the real address, not a street geocode (same convention as the bot).
    payload = build_property_payload(
        ura_result, rental_result, {"lat": resolved["lat"], "lng": resolved["lng"]}
    )
    payload["exact_coords"] = True
    payload["postal"] = resolved["postal"]
    return payload


@app.get("/api/property")
def api_property(q: str = Query(..., min_length=1)):
    q = q.strip()
    if _POSTAL_RE.match(q):
        return _property_by_postal(q)

    result = _search_with_rental(q)
    if isinstance(result, dict):
        return result
    ura_result, rental_result = result

    # Pin: URA's own x/y first (authoritative, 95% of searchable projects).
    # Only a project without one falls back to geocoding the street — street
    # only, never "project + street" (wrong-coordinate convention) — and that
    # guess is the one /api/amenities snaps to Google's origin.
    coords = project_xy_coords(_project_dicts(), ura_result["development"])
    exact = coords is not None
    if coords is None:
        try:
            coords = geocode_building(ura_result["street"])
        except Exception:
            coords = None

    payload = build_property_payload(ura_result, rental_result, coords)
    if exact:
        payload["exact_coords"] = True
    return payload


def build_developments(project_dicts: list, fallback_coords: dict, now=None) -> list:
    """One dot per non-landed development for the explore map (pure — tested).

    Coordinates come from URA's own x/y (SVY21 → WGS84) — authoritative and
    present on ~88% of projects. `fallback_coords` ({NAME: {lat, lng}}, the
    project_coords collection) covers the rest; a project with neither is
    skipped. Do NOT prefer the OneMap fallback over x/y: name-geocoding put
    same-named buildings 20km off (e.g. THE SUMMIT), while x/y matched OneMap
    to 0m median across 2,376 projects.
    """
    now = now or datetime.now()
    cutoff = now.replace(day=1) - relativedelta(months=12)
    landed = ("detached", "terrace", "bungalow")
    out = []

    for pd in project_dicts:
        name = (pd.get("project") or "").strip()
        if not name or name.upper() == "LANDED HOUSING DEVELOPMENT":
            continue
        txns = pd.get("transaction", [])
        strata = [t for t in txns
                  if not any(w in t.get("propertyType", "").lower() for w in landed)]
        if not strata:
            continue

        x, y = parse_float(pd.get("x")), parse_float(pd.get("y"))
        if x and y:
            lat, lng = svy21_to_wgs84(x, y)
        else:
            fb = fallback_coords.get(name.upper())
            if not fb:
                continue
            lat, lng = fb["lat"], fb["lng"]

        psf_list = []
        for t in strata:
            dt = parse_mmyy_date(t.get("contractDate", ""))
            if not dt or dt < cutoff:
                continue
            area = parse_float(t.get("area", 0))
            price = parse_float(t.get("price", 0))
            if not area or area <= 0 or not price:
                continue
            psf_list.append(price / sqm_to_sqft(area))

        out.append({
            "project": name,
            "street": pd.get("street", ""),
            "district": strata[0].get("district", ""),
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "avg_psf": round(sum(psf_list) / len(psf_list)) if psf_list else None,
            "txns_12mo": len(psf_list),
        })

    out.sort(key=lambda d: d["project"])
    return out


def _load_fallback_coords() -> dict:
    db = get_mongo_db()
    if db is None:
        return {}
    try:
        return {d["_id"]: d for d in db["project_coords"].find({"lat": {"$ne": None}})}
    except Exception:
        return {}


# Memo keyed by the identity of the memoized transactions list from cache_ura —
# rebuilt only when the URA cache actually refreshes (new list object).
_dev_memo = {"ref": None, "payload": None}


@app.get("/api/developments")
def api_developments():
    """Every non-landed development with a coordinate — the explore-map layer.
    ~2.4k rows of {project, street, district, lat, lng, avg_psf, txns_12mo}."""
    with _search_lock:
        transactions, _pipeline = get_ura_data()
    if _dev_memo["ref"] is transactions and _dev_memo["payload"] is not None:
        return _dev_memo["payload"]
    devs = build_developments(transactions, _load_fallback_coords())
    payload = {"developments": devs, "count": len(devs)}
    _dev_memo["ref"], _dev_memo["payload"] = transactions, payload
    return payload


@app.get("/api/trend")
def api_trend(q: str = Query(..., min_length=1)):
    """Price trend (avg PSF over time, resale+sub-sale only). Called with the
    already-resolved development name, same re-query pattern as the bot's trend
    button. Returns price_trend's dict unchanged (error/ambiguous passthrough)."""
    with _search_lock:
        return price_trend(q)


@app.get("/api/amenities")
def api_amenities(
    street: str = Query(..., min_length=1),
    lat: float | None = None,
    lng: float | None = None,
):
    """Slow bundle: MRT + malls + schools + supermarkets in one get_nearby_info
    call (each item already carries dest_lat/dest_lng/maps_link). Geocodes the
    street via Google unless lat/lng are passed."""
    return get_nearby_info(street, lat=lat, lng=lng)


# Mounted last so /api/* wins; html=True serves index.html at /.
app.mount("/", StaticFiles(directory="webapp", html=True), name="webapp")
