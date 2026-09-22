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

import contextlib
import logging
import re
import threading
from datetime import datetime

from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

import hdb
from ura import search_property, price_trend, band_transactions
from rental import get_rental_by_band
from maps import get_nearby_info, geocode_building, resolve_postal_code
from district_search import DISTRICT_NAMES
from cache.cache_hdb import (
    get_hdb_resale_data,
    get_index_records,
    get_street_records,
    is_hdb_residential_block,
)
from cache import explore_cache
from cache.cache_ura import get_ura_data, get_project_index, oldest_contract_date
from cache.cache_rental import get_rental_data
from cache.onemap_mrt import build_mrt_cache
from storage import get_recent_searches
from utils import (
    FLAT_TYPES,
    SIZE_BANDS,
    get_mongo_db,
    haversine_m,
    hdb_block_key,
    parse_float,
    parse_mmyy_date,
    sqm_to_sqft,
    svy21_to_wgs84,
)

_POSTAL_RE = re.compile(r"^\d{6}$")

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(_app):
    """Warm the landing payload in the background at boot.

    The explore map is the landing state, so the first visitor after a deploy
    or restart is the one who pays for a cold `/api/developments`. Doing it
    here moves that cost to container start, where nobody is waiting, and the
    thread is daemonic so a slow or failing warm never holds up serving.
    """
    threading.Thread(target=_warm_caches, daemon=True).start()
    yield


app = FastAPI(title="Property Bot API", lifespan=_lifespan)
# /api/developments is ~540KB of JSON — gzip takes it to roughly a quarter of
# that, and every other response is small enough that the threshold skips it.
app.add_middleware(GZipMiddleware, minimum_size=1024)


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


# ── HDB payload shaping (the FLAT_TYPES parallel to the bands above) ─────────
#
# hdb.py is the single source of truth and its return shapes are passed through
# unchanged, exactly as ura.py's are — these helpers only put them in JSON's
# terms. HDB is grouped by flat type where private is grouped by size band, so
# order_by_flat_type is order_by_band's counterpart and for the same reason:
# JSON object order is what the frontend renders.

def order_by_flat_type(mapping: dict) -> dict:
    """Re-key a flat-type dict into FLAT_TYPES order (hdb builds them in
    encounter order). Anything unrecognised still gets through, at the end."""
    ordered = {ft: mapping[ft] for ft in FLAT_TYPES if ft in mapping}
    ordered.update({k: v for k, v in mapping.items() if k not in ordered})
    return ordered


def shape_flat_types(flat_types: dict) -> dict:
    """JSON-shape hdb's per-flat-type aggregate.

    _aggregate_by_flat_type carries `latest` as a whole normalised row, which
    holds a datetime (month_dt) and every raw field. Pick out what the frontend
    renders instead of serialising the row wholesale — the payload stays small
    and the response stops depending on hdb's internal row shape.
    """
    out = {}
    for ft, agg in order_by_flat_type(flat_types or {}).items():
        latest = agg.get("latest") or {}
        out[ft] = {
            "count": agg.get("count"),
            "median_price": agg.get("median_price"),
            "avg_psf": agg.get("avg_psf"),
            "typical_lease": agg.get("typical_lease"),
            "latest": {
                "price": latest.get("price"),
                "psf": latest.get("psf"),
                "area_sqft": round(latest["area_sqft"]) if latest.get("area_sqft") else None,
                "month": latest.get("month"),
                "storey_range": latest.get("storey_range"),
                "flat_model": latest.get("flat_model"),
                "lease_years": latest.get("lease_years"),
            } if latest else None,
        }
    return out


def build_hdb_block_payload(result: dict, coords: dict | None) -> dict:
    """One block's detail — the HDB answer to build_property_payload.

    `market` and `kind` are what the frontend branches on: one search box
    serves both markets (a postal code can resolve to either), so the response
    has to say which one came back rather than leaving it to be inferred.

    A block coordinate is an *address* geocode (block + street), not a name
    geocode, so it is exact in the way project_xy_coords is and the frontend
    passes it straight to /api/amenities — cf. the street-geocode caveat on
    project_xy_coords, which does not apply here.
    """
    street = result["street"]
    payload = {
        "market": "hdb",
        "kind": "block",
        "development": f"Block {result['block']} {street.title()}",
        "block": result["block"],
        "street": street,
        "town": result.get("town"),
        "flat_types": shape_flat_types(result.get("flat_types")),
        "total_txns": result.get("total_txns", 0),
        "lat": coords["lat"] if coords else None,
        "lng": coords["lng"] if coords else None,
    }
    if coords:
        payload["exact_coords"] = True
    return payload


def build_hdb_street_payload(result: dict) -> dict:
    """A whole street's aggregate, plus its blocks as follow-up targets.

    No coordinate: a street is an aggregate over blocks spread along its
    length, so there is no single point that honestly represents it (and the
    amenity distances measured from one would be wrong for most of its
    blocks). The frontend offers the blocks instead.
    """
    street = result["street"]
    return {
        "market": "hdb",
        "kind": "street",
        "development": street.title(),
        "street": street,
        "town": result.get("town"),
        "flat_types": shape_flat_types(result.get("flat_types")),
        "blocks": [{"block": b, "count": n} for b, n in result.get("blocks", [])],
        "total_txns": result.get("total_txns", 0),
        "window_months": result.get("window_months"),
        "lat": None,
        "lng": None,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/list")
def api_list():
    return {"searches": get_recent_searches(limit=10)}


# get_ura_data()/get_rental_data() refresh in-line when stale with no
# concurrency guard — fine for the bot (Telegram updates are serial), but
# concurrent web requests would each launch a full URA refresh. Serialize
# the cache-touching section here rather than modifying the cache modules.
#
# Every ura.py call from this module passes `from_index=True`, which is not an
# optimisation but the difference between answering and not: the full load is
# ~31MB over the wire and ~240MB of Python, and on a 512MB container
# /api/property simply stopped returning (>300s, while every small-read
# endpoint stayed at 0.2s). The index path reads ~1MB. The bot keeps the full
# load — it needs every project for browse-by-district anyway.
_search_lock = threading.Lock()


def _search_with_rental(query: str):
    """The locked search+rental section shared by name and postal searches.
    Returns either a passthrough dict (ambiguous/error) or (ura_result, rental)."""
    with _search_lock:
        ura_result = search_property(query, from_index=True)

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
    """The rows `project_xy_coords` scans: project name plus URA's x/y.

    The name index carries exactly those fields, so this is a ~250KB projected
    read rather than the whole ~31MB cache — which is the difference between a
    pin lookup and a second full load on the same request. Only a stale or
    missing index falls back to get_ura_data(), under the search's own lock
    because that call refreshes in-line."""
    index = get_project_index()
    if index is not None:
        return index
    with _search_lock:
        transactions, _pipeline = get_ura_data()
    return transactions


# HDB's own lock, not _search_lock: get_hdb_resale_data() refreshes in-line
# when stale exactly as the URA cache does, and that refresh is now a 60-month
# fetch measured in minutes. Sharing one lock would park every private search
# behind it for the duration; they touch different caches and share no state.
_hdb_lock = threading.Lock()


# Reads are scoped to what the question needs, which is why the cache is
# stored keyed by street. hdb.py's entry points all take `records` and filter
# it themselves, so a pre-filtered subset gives byte-identical answers — none
# of them had to change for this.
#
#   resolve a query   -> the (block, street) index set   ~9.6k rows,  ~3MB
#   a block or street -> that street's rows              ~few hundred, ~1MB
#   the full window   -> only the bot wants it            130k rows,  39MB
#
# The lock is HDB's own, not _search_lock: get_hdb_resale_data() refreshes
# in-line when stale exactly as the URA cache does, and that refresh is a
# 60-month fetch measured in minutes. Sharing one lock would park every
# private search behind it; they touch different caches and share no state.

def _hdb_records() -> list:
    """The whole window. Only for callers that genuinely need every row."""
    with _hdb_lock:
        return get_hdb_resale_data()


# The index set is the one thing every HDB request needs, so it is held rather
# than re-fetched: ~9.6k rows is ~17MB of Python against the window's 229MB,
# and it is keyed on the cache's meta timestamp so a refresh replaces it.
# Steady state, that leaves a request paying for one street read alone.
_hdb_index_memo: dict = {"ts": None, "records": None}


def _hdb_index_records() -> list:
    """One row per (block, street) — enough for hdb.resolve_query, and a
    fraction of the window."""
    ts = _hdb_meta_ts()
    if _hdb_index_memo["records"] is not None and _hdb_index_memo["ts"] == ts:
        return _hdb_index_memo["records"]
    with _hdb_lock:
        records = get_index_records()
    if records:
        _hdb_index_memo.update(ts=ts, records=records)
    return records


def _hdb_street_records(street: str) -> list:
    """Just this street's rows — one indexed lookup."""
    with _hdb_lock:
        return get_street_records(street)


def _warmed_block_coord(block: str, street: str) -> dict | None:
    """One indexed read of the warmed `hdb_block_coords` collection.

    The same coordinates the explore layer is built from, and they were
    validated when stored (OneMap's own BLK_NO and ROAD_NAME had to match what
    was asked), so this is the better answer as well as the faster one: a live
    geocode can simply fail, and a block search that loses its coordinate
    loses its pin, its amenities and its nearby button with it.
    """
    db = get_mongo_db()
    if db is None:
        return None
    try:
        doc = db["hdb_block_coords"].find_one(
            {"_id": hdb_block_key(block, street), "lat": {"$ne": None}})
        return {"lat": doc["lat"], "lng": doc["lng"]} if doc else None
    except Exception:
        return None


def _geocode_hdb_block(block: str, street: str) -> dict | None:
    """Resolve an HDB block to coordinates: the warmed collection first, then
    OneMap live — the raw street, then its abbreviation-expanded form
    (ST → STREET), which is how the data spells streets that OneMap writes out
    in full.

    Live geocoding stays as the fallback rather than being removed: the warm
    covers the blocks that were in the resale window when it last ran, and a
    block that has just entered it should still answer.

    Deliberately a twin of bot.py's helper rather than an import: api.py never
    imports bot.py (that would pull in the Telegram application and its
    module-level setup), so the few lines are duplicated on purpose.
    """
    warmed = _warmed_block_coord(block, street)
    if warmed:
        return warmed
    seen = set()
    for q in (f"{block} {street}", f"{block} {hdb.expand_street(street)}"):
        if q in seen:
            continue
        seen.add(q)
        try:
            loc = geocode_building(q)
        except Exception:
            loc = None
        if loc:
            return {"lat": loc["lat"], "lng": loc["lng"]}
    return None


def _hdb_block_response(block: str, street: str, records: list,
                        coords: dict | None = None) -> dict:
    """Block detail + its coordinate. `coords` skips geocoding — the postal
    path already holds the exact OneMap coordinate for the address.

    A block with no coordinate is still a usable answer (prices and the 5-year
    trend need none), so a geocode miss degrades to a payload without a pin
    rather than an error — the same call the bot makes.
    """
    result = hdb.block_detail(block, street, records)
    if "error" in result:
        return result
    return build_hdb_block_payload(
        result, coords or _geocode_hdb_block(block, result["street"])
    )


def _hdb_by_postal(postal: str, resolved: dict) -> dict:
    """An HDB block reached by postal code — mirrors bot.py's
    _hdb_search_by_postal, including its no-resale explanation."""
    block, road = resolved.get("block", ""), resolved.get("road", "")
    if not block or not road:
        return {"error": f"Postal code {postal} doesn't map to an HDB block."}

    resolution = hdb.resolve_query(f"{block} {road}", _hdb_index_records())
    if resolution.get("kind") == "block":
        payload = _hdb_block_response(
            resolution["block"], resolution["street"],
            _hdb_street_records(resolution["street"]),
            coords={"lat": resolved["lat"], "lng": resolved["lng"]},
        )
        payload["postal"] = resolved.get("postal")
        return payload

    # A real HDB block with no resale rows is almost always a newer development
    # still inside its 5-year MOP, so no flat in it *can* be sold yet. Saying
    # "not found" would read as a bug; this is the bot's wording.
    building = (resolved.get("building") or "").title()
    name = f"{building} (Block {block} {road.title()})" if building else f"Block {block} {road.title()}"
    return {"error": f"{name} is an HDB block, but has no resale transactions on record "
                     f"(postal {postal}). This usually means it hasn't reached its 5-year "
                     f"MOP yet, so none of its flats can be sold on the resale market."}


def _property_by_postal(postal: str) -> dict:
    """Postal-code search, mirroring bot.py's route_postal discriminator: the
    authoritative HDB Property Information dataset decides HDB vs private —
    NOT the OneMap building name (BTO blocks carry names just like condos).

    Market-agnostic, as in the bot: one code finds either market, so this is
    the one entry point that can return an HDB payload from /api/property."""
    resolved = resolve_postal_code(postal)
    if not resolved:
        return {"error": f'Couldn\'t find any address for postal code "{postal}".\nPlease double-check the 6-digit code.'}

    block, road = resolved.get("block"), resolved.get("road")
    if block and road and is_hdb_residential_block(block, road):
        return _hdb_by_postal(postal, resolved)
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


def sqft_midpoint(area_range: str):
    """URA rental areas are range strings ("1500-1600") — take the midpoint."""
    try:
        lo, hi = str(area_range).split("-")
        return (float(lo) + float(hi)) / 2
    except (ValueError, AttributeError):
        return None


def build_rent_index(rental_projects: list, now=None, min_contracts: int = 3) -> dict:
    """PROJECT (upper) → mean ANNUAL rent psf over the last 12 months.

    Divided by a sale PSF this gives gross yield. Projects with fewer than
    `min_contracts` recent contracts are omitted rather than shown noisily —
    a single lease is not a yield.
    """
    now = now or datetime.now()
    cutoff = now.replace(day=1) - relativedelta(months=12)
    index = {}

    for proj in rental_projects or []:
        psfs = []
        for c in proj.get("rental", []):
            dt = parse_mmyy_date(c.get("leaseDate", ""))
            if not dt or dt < cutoff:
                continue
            sqft = sqft_midpoint(c.get("areaSqft", ""))
            rent = parse_float(c.get("rent", 0))
            if sqft and rent:
                psfs.append(rent * 12 / sqft)
        if len(psfs) >= min_contracts:
            index[(proj.get("project") or "").strip().upper()] = sum(psfs) / len(psfs)
    return index


# Broad lease-tenure buckets for the explore filter. Keys are what the dot
# payload carries and what the frontend filters on; every non-empty URA tenure
# string lands in exactly one of them, so nothing is unreachable.
#   freehold  70% of developments   999  6%   99  24%   other  0.4%
# 999-/9999-year leases used to bucket as freehold (they are, in every way a
# buyer cares about) but that made 140 developments invisible to anyone
# looking for one, so they are their own category and the filter is
# multi-select — "freehold or 999" is still one gesture.
TENURE_FREEHOLD = "freehold"
TENURE_999 = "999"
TENURE_99 = "99"
TENURE_OTHER = "other"          # 60/70/85/93 and 100/101/102/103/110/115 yrs


def tenure_bucket(tenure: str) -> str | None:
    """One URA tenure string -> bucket key, or None when it is blank/unparseable."""
    tenure = (tenure or "").strip().lower()
    if not tenure:
        return None
    if tenure.startswith("freehold"):
        return TENURE_FREEHOLD
    years = re.match(r"(\d+)", tenure)
    if not years:
        return None
    years = int(years.group(1))
    if years >= 900:
        return TENURE_999
    if years == 99:
        return TENURE_99
    return TENURE_OTHER


def classify_tenure(txns: list) -> str | None:
    """A development's lease tenure -> bucket key ('freehold' | '999' | '99' |
    'other'), or None when no transaction carries a tenure.

    Projects mix tenures very rarely; the majority across transactions wins.
    """
    votes: dict[str, int] = {}
    for t in txns:
        bucket = tenure_bucket(t.get("tenure"))
        if bucket:
            votes[bucket] = votes.get(bucket, 0) + 1
    if not votes:
        return None
    # max() over (count, key) keeps ties deterministic rather than
    # insertion-ordered — a project split 1:1 must not flip between refreshes.
    return max(sorted(votes), key=lambda k: votes[k])


def station_coords(stations: dict) -> list:
    """MRT cache → [(lat, lng)]. Coords are already cached (123 stations), so
    distance-to-MRT costs a haversine loop, not an API call."""
    return [(s["lat"], s["lng"]) for s in (stations or {}).values()
            if s.get("lat") and s.get("lng")]


def nearest_mrt_m(lat: float, lng: float, coords: list):
    if not coords:
        return None
    return round(min(haversine_m(lat, lng, a, b) for a, b in coords))


def build_developments(project_dicts: list, fallback_coords: dict, rent_index=None,
                       mrt_coords=None, now=None) -> list:
    """One dot per non-landed development for the explore map (pure — tested).

    Coordinates come from URA's own x/y (SVY21 → WGS84) — authoritative and
    present on ~88% of projects. `fallback_coords` ({NAME: {lat, lng}}, the
    project_coords collection) covers the rest; a project with neither is
    skipped. Do NOT prefer the OneMap fallback over x/y: name-geocoding put
    same-named buildings 20km off (e.g. THE SUMMIT), while x/y matched OneMap
    to 0m median across 2,376 projects.

    Beyond position, each dot carries what the map colours and filters on:
    avg_psf and yield_pct (the two colour metrics), tenure, mrt_m and
    last_txn. All of it is derived from data already in memory — the rental
    index and MRT coords are injected, so this stays pure and does no IO.
    """
    now = now or datetime.now()
    cutoff = now.replace(day=1) - relativedelta(months=12)
    landed = ("detached", "terrace", "bungalow")
    rent_index = rent_index or {}
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
        latest = None
        for t in strata:
            dt = parse_mmyy_date(t.get("contractDate", ""))
            if not dt:
                continue
            if latest is None or dt > latest:
                latest = dt
            if dt < cutoff:
                continue
            area = parse_float(t.get("area", 0))
            price = parse_float(t.get("price", 0))
            if not area or area <= 0 or not price:
                continue
            psf_list.append(price / sqm_to_sqft(area))

        avg_psf = round(sum(psf_list) / len(psf_list)) if psf_list else None
        rent_psf = rent_index.get(name.upper())
        # Gross yield only means something against a real sale PSF; a dot with
        # no recent transaction gets no yield rather than a stale one.
        yield_pct = round(rent_psf / avg_psf * 100, 2) if (rent_psf and avg_psf) else None

        out.append({
            "project": name,
            "street": pd.get("street", ""),
            "district": strata[0].get("district", ""),
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "avg_psf": avg_psf,
            "txns_12mo": len(psf_list),
            "yield_pct": yield_pct,
            "tenure": classify_tenure(strata),
            "mrt_m": nearest_mrt_m(lat, lng, mrt_coords),
            "last_txn": latest.strftime("%b %Y") if latest else None,
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


class _DerivedLayer:
    """One persisted map layer: served from cache/explore_cache, rebuilt only
    when the caches it is derived from have actually moved.

    Both explore layers follow the same three-step protocol, and it is subtle
    enough that two copies of it would drift apart:

      1. the in-process memo, when its key is still current;
      2. otherwise the stored blob — and if that blob predates the caches it
         came from, it is still handed to this visitor while the rebuild runs
         behind them, because rebuilding means the multi-second full-cache
         load and these layers move twice a week at most;
      3. only an empty store makes a caller wait for a build.

    `key_fn` returning None means "do not use the store" — a cache is missing
    or stale, so the slow path (which is what refreshes it) has to run.
    """

    def __init__(self, name: str, doc_id: str, key_fn, build_fn):
        self.name = name              # log prefix
        self.doc_id = doc_id          # explore_cache document
        # Both are called, never stored as the target function itself: the
        # key functions are looked up on explore_cache at call time so a
        # patched module attribute is actually seen (a direct reference here
        # silently kept the original, and the layer answered from the live
        # caches while a test believed it was driving the store).
        self.key_fn = key_fn          # () -> key tuple | None
        self.build_fn = build_fn      # () -> payload dict (the slow path)
        # The payload and the key it was built for. That key is the same one
        # explore_cache stores against, so the memo, the stored blob and a
        # fresh build all agree on when the layer is current.
        self.state: dict = {"key": None, "payload": None}
        self.lock = threading.Lock()
        # Key a background rebuild has already been kicked off for, so a burst
        # of requests during one rebuild does not spawn a thread each.
        self.rebuilding_for = None

    def rebuild(self) -> dict:
        """The slow path: load the source cache(s) and derive the layer.

        Serialized, because two concurrent cold rebuilds would each drag the
        same tens of megabytes across the wire.
        """
        with self.lock:
            key = self.key_fn()
            if key is not None and self.state["key"] == key:
                return self.state["payload"]   # someone rebuilt while we queued
            payload = self.build_fn()
            # An empty layer is a failed load, not an empty city. Persisting
            # or memoizing one would serve a blank map as though it were
            # current — for a month, on the HDB layer — so it is returned to
            # this caller and nothing else. The next request retries.
            if not payload.get("count"):
                logger.warning(f"[{self.name}] Built an empty layer — not persisting")
                return payload
            if key is not None:
                explore_cache.save(payload, key, self.doc_id)
            self.state.update(key=key, payload=payload)
            return payload

    def rebuild_bg(self) -> None:
        """`rebuild` for the background thread: clears the in-flight marker
        whatever happens, so a failed rebuild is retried on the next miss
        rather than pinning the stale payload forever."""
        try:
            self.rebuild()
        except Exception as e:
            logger.warning(f"[{self.name}] Background rebuild failed: {e}")
        finally:
            self.rebuilding_for = None

    def payload(self) -> dict:
        key = self.key_fn()
        if key is not None and self.state["key"] == key:
            return self.state["payload"]

        stored, stored_key = explore_cache.load(self.doc_id)
        if stored is not None:
            self.state.update(key=stored_key, payload=stored)
            if key is not None and stored_key == key:
                return stored
            # The blob predates the caches it came from — a refresh has landed.
            # Hand this visitor the previous layer (at most one cycle old) and
            # rebuild behind them.
            if self.rebuilding_for != key:
                self.rebuilding_for = key
                threading.Thread(target=self.rebuild_bg, daemon=True).start()
            return stored

        # Nothing stored at all (first ever boot, or a wiped collection): there
        # is nothing to serve, so this caller waits for the build.
        return self.rebuild()


def _mrt_coords() -> list:
    """Cached station coords, or [] — a missing MRT cache just drops mrt_m
    (and its filter) rather than failing the whole explore layer."""
    try:
        return station_coords(build_mrt_cache())
    except Exception:
        return []


def _build_developments() -> dict:
    """Derive the private layer from the transaction and rental caches.

    Cold, that is ~31MB out of Mongo and ~9s — which is the entire reason the
    result is persisted.
    """
    with _search_lock:
        transactions, _pipeline = get_ura_data()
        rentals = get_rental_data()
    devs = build_developments(
        transactions, _load_fallback_coords(),
        rent_index=build_rent_index(rentals), mrt_coords=_mrt_coords(),
    )
    # District estate names ride along with the dots (28 short strings, and
    # the payload is already gzipped) rather than costing the frontend a
    # second request for a static table.
    return {"developments": devs, "count": len(devs),
            "districts": {f"{d:02d}": name for d, name in DISTRICT_NAMES.items()}}


developments_layer = _DerivedLayer(
    "Explore", explore_cache.DOC_ID,
    lambda: explore_cache.source_key(), _build_developments)


def developments_payload() -> dict:
    """Every non-landed development with a coordinate — the explore-map layer.
    ~2.4k rows of {project, street, district, lat, lng, avg_psf, txns_12mo,
    yield_pct, tenure, mrt_m, last_txn}: the last four drive the colour
    metrics and the client-side filters, and cost no extra IO. Plus
    `districts`, the district -> estate-name table the dots label against.

    Served from the persisted payload (cache/explore_cache.py) whenever it is
    current, so the landing page never touches the transaction cache.
    """
    return developments_layer.payload()


@app.get("/api/developments")
def api_developments():
    return developments_payload()


# ── HDB explore layer ───────────────────────────────────────────────────────
#
# The HDB half of the explore map: one dot per HDB block, the level at which a
# resale flat actually has an address, a price and a lease. Deliberately NOT
# folded into the private layer — the two are shown one market at a time,
# because a cluster bubble averaging a condo's PSF with a flat's says nothing
# about either.
#
# It lives beside the private layer rather than in the HDB section below
# because it is the same machinery: a _DerivedLayer over a persisted payload,
# with the same MRT-distance enrichment. The search endpoints are further down.

HDB_EXPLORE_WINDOW_MONTHS = 12


def _hdb_block_coords() -> dict:
    """The warmed `hdb_block_coords` collection: block key -> {lat, lng}.

    ~9.6k documents, warmed once by scripts/build_hdb_block_coords.py (HDB
    resale records carry no coordinates and neither does HDB Property
    Information). Misses are stored as lat=None markers and filtered out here;
    an empty collection means no HDB layer rather than a broken one.
    """
    db = get_mongo_db()
    if db is None:
        return {}
    try:
        return {d["_id"]: d for d in db["hdb_block_coords"].find({"lat": {"$ne": None}})}
    except Exception as e:
        logger.warning(f"[HDB Explore] Coordinate load failed: {e}")
        return {}


def build_hdb_blocks(records: list, coords: dict, mrt_coords=None,
                     window_months: int = HDB_EXPLORE_WINDOW_MONTHS,
                     now=None) -> list:
    """One dot per HDB block for the explore map (pure — tested).

    `records` is the raw resale window and `coords` the warmed block
    coordinates; a block with no coordinate is skipped, exactly as a project
    with no x/y is on the private side.

    Two things differ from `build_developments` because HDB data differs:

    *Remaining lease* is the dot's second colour metric, and it has to be
    read off the LATEST transaction and then aged to today. Within one block
    every flat shares a lease start, so the spread across the cached window is
    just the window itself — taking the max would quote a median 4.2 years too
    much lease, on the metric whose whole point is decay. Decay is exactly one
    year per year, so ageing the newest reading forward is not an estimate.

    *Flat types* come from the full window, not the 12-month one: which types
    a block contains is a property of the building, and a block that sold no
    4-rooms this year still has them.
    """
    now = now or datetime.now()
    cutoff = now.replace(day=1) - relativedelta(months=window_months)
    rows = hdb._normalise_all(records)

    groups: dict = {}
    for r in rows:
        if r["block"] and r["street"]:
            groups.setdefault((r["block"], r["street"]), []).append(r)

    out = []
    for (block, street), rs in groups.items():
        coord = coords.get(hdb_block_key(block, street))
        if not coord:
            continue

        recent = [r for r in rs if r["month_dt"] and r["month_dt"] >= cutoff]
        psfs = [r["psf"] for r in recent if r["psf"]]
        prices = sorted(r["price"] for r in recent if r["price"])
        dated = [r for r in rs if r["month_dt"]]
        latest = max(dated, key=lambda r: r["month_dt"]) if dated else None

        types = sorted({r["flat_type"] for r in rs if r["flat_type"]},
                       key=lambda t: FLAT_TYPES.index(t) if t in FLAT_TYPES else len(FLAT_TYPES))
        lat, lng = coord["lat"], coord["lng"]
        out.append({
            "block": block,
            "street": street,
            "town": rs[0]["town"],
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "avg_psf": round(sum(psfs) / len(psfs)) if psfs else None,
            "med_price": round(prices[len(prices) // 2]) if prices else None,
            "txns_12mo": len(recent),
            "lease_years": _lease_today(latest, now),
            "flat_types": types,
            "mrt_m": nearest_mrt_m(lat, lng, mrt_coords),
            "last_txn": latest["month_dt"].strftime("%b %Y") if latest else None,
        })

    out.sort(key=lambda d: (d["street"], d["block"]))
    return out


def _lease_today(latest: dict | None, now: datetime) -> float | None:
    """The block's remaining lease as of `now`, from its most recent sale.

    A lease reading is only true on the day it was recorded, and these run up
    to five years old; it decays a year per year, so the correction is the age
    of the reading. Returns None when the newest sale carried no lease (the
    pre-2017 era the cache deliberately excludes).
    """
    if not latest or latest.get("lease_years") is None or not latest.get("month_dt"):
        return None
    months = (now.year - latest["month_dt"].year) * 12 + (now.month - latest["month_dt"].month)
    return round(max(latest["lease_years"] - months / 12, 0), 1)


def _build_hdb_blocks() -> dict:
    """Derive the HDB layer from the full 60-month resale window.

    ~130k rows and ~8s cold — the same reason the private layer is persisted,
    and why this one is too. `_hdb_lock` because get_hdb_resale_data()
    refreshes in-line when stale.
    """
    with _hdb_lock:
        records = get_hdb_resale_data()
    blocks = build_hdb_blocks(records, _hdb_block_coords(), mrt_coords=_mrt_coords())
    return {"blocks": blocks, "count": len(blocks),
            "flat_types": FLAT_TYPES, "window_months": HDB_EXPLORE_WINDOW_MONTHS}


hdb_blocks_layer = _DerivedLayer(
    "HDB Explore", explore_cache.HDB_DOC_ID,
    lambda: explore_cache.hdb_source_key(), _build_hdb_blocks)


def hdb_blocks_payload() -> dict:
    """Every HDB block with a warmed coordinate — the HDB explore layer.

    ~9.6k rows of {block, street, town, lat, lng, avg_psf, med_price,
    txns_12mo, lease_years, flat_types, mrt_m, last_txn}. Bigger than the
    private layer (~216KB gzipped against ~77KB) because there are four times
    as many blocks as developments, which is why it is fetched only when the
    user actually switches markets — the landing state is still private.
    """
    return hdb_blocks_layer.payload()


@app.get("/api/hdb/blocks")
def api_hdb_blocks():
    return hdb_blocks_payload()


def _warm_caches() -> None:
    """Boot-time warm — see `_lifespan`.

    Three steps, in the order a visitor needs them. The private dots come
    first and are cheap (the persisted payload), then what a *search* reads:
    the project name index, the window anchor and the rental cache. The HDB
    layer comes last: nobody sees it until they switch markets, and warming it
    is a small read unless its blob is cold, in which case this thread is a
    better place to pay than a request.

    Note what is NOT warmed: the full transaction cache. A search no longer
    touches it (cache_ura's index + one chunk), and pulling all ~31MB in would
    put ~240MB of Python in this container's steady state for nothing — which
    is what made /api/property stop answering on a 512MB instance. The only
    thing that still wants the whole cache is the explore-layer rebuild, and
    that runs in its own thread twice a week at most.

    Every step swallows its errors: a failed warm only means the first request
    takes the slow path, as before.
    """
    try:
        developments_payload()
    except Exception as e:
        logger.warning(f"[Explore] Warm-up failed, first request will rebuild: {e}")
    try:
        with _search_lock:
            get_project_index()
            oldest_contract_date()
            get_rental_data()
    except Exception as e:
        logger.warning(f"[Cache] Warm-up failed, first search will load: {e}")
    try:
        hdb_blocks_payload()
    except Exception as e:
        logger.warning(f"[HDB Explore] Warm-up failed, first request will rebuild: {e}")

# Identity differs by market — a private development is its name, an HDB
# address is a block on a street — and that is the only thing the radius
# filter below needs to know about either.
def dev_key(d: dict) -> str:
    return str(d.get("project", "")).strip().upper()


def block_key(d: dict) -> str:
    return f"{d.get('block', '')} {d.get('street', '')}".strip().upper()


NEARBY_MARKETS = {
    "private": {"rows": lambda: developments_payload()["developments"], "key": dev_key},
    "hdb": {"rows": lambda: hdb_blocks_payload()["blocks"], "key": block_key},
}


def nearby_dots(dots: list, origin_name: str, key_of=dev_key, radius_m: int = 1000,
                limit: int = 40, origin: tuple | None = None) -> dict:
    """Dots within `radius_m` of a point, nearest first (pure).

    Deliberately NOT a wrapper over nearby.nearby_for_project. That module
    bounds candidates to the origin's own *district* — a tractability measure
    for the bot's 10-row text list — which on a map reads as a hard
    straight-line edge of missing dots: SANDY EIGHT (D15) has 171
    developments within 1 km but only 86 of them share its district, and
    PARK NATURA (D23) has 33 versus 3. It also returns name/street/distance
    only, whereas every map popup here needs the full dot payload
    (`build_developments`) the explore layer already computes and memoizes.
    So this is a radius filter over that payload — no extra IO, no domain
    logic of its own.

    It serves both markets off the same payloads the explore map draws, so a
    neighbour's popup is its explore popup whichever market it came from.
    `key_of` is the only difference: it names a row, so the origin can be
    excluded from its own results.

    `origin` overrides the origin coordinate: the frontend passes the pin it
    is already showing (URA x/y, or an exact postal coordinate), so the
    circle is centred on the same point the user sees. It is also what lets
    an origin search the *other* market, where by definition it is not in the
    list to be looked up.
    """
    key = (origin_name or "").strip().upper()
    origin_row = next((d for d in dots if key_of(d) == key), None)
    if origin is None:
        if origin_row is None:
            return {"error": f'Could not pinpoint "{origin_name}" on the map.'}
        origin = (origin_row["lat"], origin_row["lng"])
    lat, lng = origin

    rows = []
    for d in dots:
        if key_of(d) == key:
            continue                                   # exclude the origin itself
        dist = haversine_m(lat, lng, d["lat"], d["lng"])
        if dist <= radius_m:
            rows.append({**d, "distance_m": round(dist)})
    rows.sort(key=lambda r: r["distance_m"])

    return {
        "origin": {
            "name": key_of(origin_row) if origin_row else origin_name,
            "street": origin_row["street"] if origin_row else "",
            "lat": lat,
            "lng": lng,
        },
        "radius_m": radius_m,
        "total": len(rows),
        "results": rows[:limit],
    }


@app.get("/api/nearby")
def api_nearby(
    q: str = Query(..., min_length=1),
    market: str = Query("private", pattern="^(private|hdb)$"),
    lat: float | None = None,
    lng: float | None = None,
    radius_m: int = Query(1000, ge=100, le=5000),
    limit: int = Query(40, ge=1, le=200),
):
    """Neighbouring dots for the map's nearby view. Each row is an explore-map
    dot of the requested market (same keys, so the popups render identically)
    plus `distance_m`; `total` says how many were inside the radius before
    `limit`.

    One market per call, the same rule the explore map follows, and here for a
    measured reason as well as a conceptual one: an HDB block typically has
    150-270 HDB blocks within 1 km against 3-36 private developments (810A
    Choa Chu Kang Ave 7: 210 and 5; 109 Tampines St 11: 232 and 3), so a
    merged list would be 40 rows of the same estate with private buried under
    it. The frontend toggles between them instead, defaulting to the origin's
    own market.

    `q` is the origin's key in the requested market's own terms — a project
    name, or "<block> <street>" — and is used only to keep the origin out of
    its own results; the coordinate comes from `lat`/`lng`.
    """
    spec = NEARBY_MARKETS[market]
    origin = (lat, lng) if lat is not None and lng is not None else None
    out = nearby_dots(spec["rows"](), q, key_of=spec["key"],
                      radius_m=radius_m, limit=limit, origin=origin)
    if "error" not in out:
        out["market"] = market
    return out


@app.get("/api/trend")
def api_trend(q: str = Query(..., min_length=1)):
    """Price trend (avg PSF over time, resale+sub-sale only). Called with the
    already-resolved development name, same re-query pattern as the bot's trend
    button. Returns price_trend's dict unchanged (error/ambiguous passthrough)."""
    with _search_lock:
        return price_trend(q, from_index=True)


# The street list is the search box's routing table, so it is memoized on the
# cache's meta timestamp (a one-document read) rather than recomputed: deriving
# it costs a full cache load, while the result itself is ~579 short strings.
# Mirrors cache_ura's _meta_timestamp memo — and note what is held is the
# derived list, never the 130k rows it came from.
_hdb_streets_memo: dict = {"ts": None, "payload": None}


def _hdb_meta_ts():
    """The HDB cache's last-refresh timestamp, or None. One tiny read."""
    db = get_mongo_db()
    if db is None:
        return None
    try:
        doc = db["hdb_cache"].find_one({"_id": "meta"}, {"timestamp": 1})
        return doc.get("timestamp") if doc else None
    except Exception:
        return None


def build_hdb_street_index(records: list) -> dict:
    """The search box's HDB table: every street, both spellings, its blocks.

    Pure, so the endpoint below is only memoization around it.

    Two jobs ride on one payload. *Routing* needs the street names: the box
    has to pick a market before asking, which a heuristic cannot do ("8 SAINT
    THOMAS" is a condo opening with a number; "BISHAN ST 22" is an HDB street
    that does not), and asking private first does not work either, because a
    fuzzy private search answers an HDB street with condos that merely share a
    word ("BISHAN ST 22" -> BISHAN LOFT) so the fallback never fires.
    *Type-ahead* needs the blocks, since an HDB address is a block on a street
    and completing only the street stops one token short of the answer.

    The blocks are worth shipping because they are small: 9.6k of them across
    579 streets take the whole table to ~87KB of JSON, ~18KB once
    GZipMiddleware has it — under a quarter of the private dot layer, on a
    response the frontend already fetches once at boot. That keeps type-ahead what it is on the private
    side: a scan over strings already in the browser, with no endpoint, no
    request and no debounce behind each keystroke.

    Both spellings ride along because the data abbreviates ("ANG MO KIO AVE
    6") while users type either that or the full form; matching one string
    against both covers it without the frontend re-implementing STREET_ABBREV.
    """
    blocks: dict = {}
    for r in hdb._normalise_all(records):
        if r["street"]:
            blocks.setdefault(r["street"], set()).add(r["block"])

    streets = [
        {"s": s, "c": hdb.expand_street(s), "b": sorted(blocks[s] - {""})}
        for s in sorted(blocks)
    ]
    return {
        "streets": streets,
        "count": len(streets),
        "blocks": sum(len(s["b"]) for s in streets),
    }


@app.get("/api/hdb/streets")
def api_hdb_streets():
    """The HDB street + block index — see build_hdb_street_index."""
    ts = _hdb_meta_ts()
    if _hdb_streets_memo["payload"] is not None and _hdb_streets_memo["ts"] == ts:
        return _hdb_streets_memo["payload"]

    # Derived from the index set, not the window: it already holds every
    # distinct (block, street) pair, at a fraction of the rows.
    payload = build_hdb_street_index(_hdb_index_records())
    # Only a real answer is worth keeping. An empty list here means the read
    # failed, not that Singapore has no HDB streets, and memoizing it would
    # poison routing for the life of the process — every HDB query would then
    # fall back to the shape heuristic and quietly land in the private market.
    if payload["streets"]:
        _hdb_streets_memo.update(ts=ts, payload=payload)
    return payload


@app.get("/api/hdb")
def api_hdb(q: str = Query(..., min_length=1)):
    """HDB resale search: free-text block and/or street.

    Postal codes are NOT handled here — they stay with /api/property, which
    decides the market from the authoritative HDB block dataset and can return
    either payload. One code has to find both markets (bot convention), and
    splitting that decision across two endpoints would duplicate it.

    resolve_query's contract is passed through unchanged, error and ambiguous
    included, exactly as /api/property passes search_property's through. An
    ambiguous street carries `block` so the frontend can re-ask for the same
    block on whichever street the user picks.
    """
    q = q.strip()
    resolution = hdb.resolve_query(q, _hdb_index_records())

    if resolution.get("ambiguous"):
        return {
            "market": "hdb",
            "ambiguous": True,
            "block": resolution.get("block"),
            "candidates": resolution["candidates"],
        }
    if "error" in resolution:
        return {"market": "hdb", "error": resolution["error"]}

    # Resolution named a street, so from here only that street's rows matter.
    records = _hdb_street_records(resolution["street"])
    if resolution["kind"] == "block":
        return _hdb_block_response(resolution["block"], resolution["street"], records)

    summary = hdb.street_summary(resolution["street"], records)
    if "error" in summary:
        return {"market": "hdb", **summary}
    return build_hdb_street_payload(summary)


@app.get("/api/hdb/trend")
def api_hdb_trend(
    street: str = Query(..., min_length=1),
    block: str | None = None,
):
    """5-year resale PSF trend for one block, or for a whole street when no
    block is given.

    Its own route rather than a market flag on /api/trend: that one is keyed by
    development name, and an HDB block is identified by block + street. The
    *response* needs no such split — hdb.price_trend already returns
    ura.price_trend's shape, which is why the frontend draws both with the same
    chart. Re-queried at call time, the same pattern as /api/trend.
    """
    block = (block or "").strip() or None
    street = street.strip()
    return hdb.price_trend(block, street, _hdb_street_records(street))


@app.get("/api/transactions")
def api_transactions(
    q: str = Query(..., min_length=1),
    band: str = Query(..., min_length=1),
):
    """Every transaction in one size band, newest first.

    /api/property carries only the latest sale per band — the drill-down list
    is fetched on demand when the user taps a band, so the first payload (the
    one a phone waits on) stays small. Called with the already-resolved
    development name, same re-query pattern as /api/trend."""
    with _search_lock:
        return band_transactions(q, band, from_index=True)


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


class RevalidatingStaticFiles(StaticFiles):
    """Serve the frontend with revalidation instead of heuristic caching.

    StaticFiles sends ETag and Last-Modified but no Cache-Control, which leaves
    a browser free to invent its own freshness lifetime — and mobile Safari
    invents a generous one. A deploy then lands on the server while the phone
    keeps running the map.js it cached days ago, with no way for the user to
    tell that is what happened.

    `no-cache` does not mean "don't cache": the copy is kept, but it must be
    revalidated before use. The ETag already being sent answers that with a
    304 and an empty body whenever nothing changed, so the cost is one small
    conditional request per asset — cheap next to serving a stale app. Starlette
    carries Cache-Control through onto the 304, so the header survives the
    revalidation it triggers.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


# Mounted last so /api/* wins; html=True serves index.html at /.
app.mount("/", RevalidatingStaticFiles(directory="webapp", html=True), name="webapp")
