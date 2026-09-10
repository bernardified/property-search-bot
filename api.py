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
from cache.cache_ura import get_ura_data
from cache.cache_rental import get_rental_data
from cache.onemap_mrt import build_mrt_cache
from storage import get_recent_searches
from utils import (
    FLAT_TYPES,
    SIZE_BANDS,
    get_mongo_db,
    haversine_m,
    parse_float,
    parse_mmyy_date,
    sqm_to_sqft,
    svy21_to_wgs84,
)

_POSTAL_RE = re.compile(r"^\d{6}$")

app = FastAPI(title="Property Bot API")
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


def _geocode_hdb_block(block: str, street: str) -> dict | None:
    """Resolve an HDB block to coordinates via OneMap: the raw street first,
    then its abbreviation-expanded form (ST → STREET), which is how the data
    spells streets that OneMap writes out in full.

    Deliberately a twin of bot.py's helper rather than an import: api.py never
    imports bot.py (that would pull in the Telegram application and its
    module-level setup), so the few lines are duplicated on purpose.
    """
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


# Memo keyed by the identity of the memoized transaction + rental lists from
# the cache modules — rebuilt only when a cache actually refreshes (new list
# object), which is also when the MRT coords are re-read.
_dev_memo = {"txns": None, "rentals": None, "payload": None}


def _mrt_coords() -> list:
    """Cached station coords, or [] — a missing MRT cache just drops mrt_m
    (and its filter) rather than failing the whole explore layer."""
    try:
        return station_coords(build_mrt_cache())
    except Exception:
        return []


@app.get("/api/developments")
def api_developments():
    """Every non-landed development with a coordinate — the explore-map layer.
    ~2.4k rows of {project, street, district, lat, lng, avg_psf, txns_12mo,
    yield_pct, tenure, mrt_m, last_txn}: the last four drive the colour
    metrics and the client-side filters, and cost no extra IO. Plus
    `districts`, the district -> estate-name table the dots label against."""
    with _search_lock:
        transactions, _pipeline = get_ura_data()
        rentals = get_rental_data()
    if (_dev_memo["txns"] is transactions and _dev_memo["rentals"] is rentals
            and _dev_memo["payload"] is not None):
        return _dev_memo["payload"]
    devs = build_developments(
        transactions, _load_fallback_coords(),
        rent_index=build_rent_index(rentals), mrt_coords=_mrt_coords(),
    )
    # District estate names ride along with the dots (28 short strings, and
    # the payload is already gzipped) rather than costing the frontend a
    # second request for a static table.
    payload = {"developments": devs, "count": len(devs),
               "districts": {f"{d:02d}": name for d, name in DISTRICT_NAMES.items()}}
    _dev_memo["txns"], _dev_memo["rentals"] = transactions, rentals
    _dev_memo["payload"] = payload
    return payload


def nearby_developments(devs: list, origin_name: str, radius_m: int = 1000,
                        limit: int = 40, origin: tuple | None = None) -> dict:
    """Developments within `radius_m` of a project, nearest first (pure).

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

    `origin` overrides the origin coordinate: the frontend passes the pin it
    is already showing (URA x/y, or an exact postal coordinate), so the
    circle is centred on the same point the user sees.
    """
    key = (origin_name or "").strip().upper()
    origin_row = next((d for d in devs if d["project"].strip().upper() == key), None)
    if origin is None:
        if origin_row is None:
            return {"error": f'Could not pinpoint "{origin_name}" on the map.'}
        origin = (origin_row["lat"], origin_row["lng"])
    lat, lng = origin

    rows = []
    for d in devs:
        if d["project"].strip().upper() == key:
            continue                                   # exclude the origin itself
        dist = haversine_m(lat, lng, d["lat"], d["lng"])
        if dist <= radius_m:
            rows.append({**d, "distance_m": round(dist)})
    rows.sort(key=lambda r: r["distance_m"])

    return {
        "origin": {
            "project": origin_row["project"] if origin_row else origin_name,
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
    lat: float | None = None,
    lng: float | None = None,
    radius_m: int = Query(1000, ge=100, le=5000),
    limit: int = Query(40, ge=1, le=200),
):
    """Neighbouring developments for the map's nearby view. Each row is an
    explore-map dot (same keys, so the popups render identically) plus
    `distance_m`; `total` says how many were inside the radius before `limit`."""
    devs = api_developments()["developments"]
    origin = (lat, lng) if lat is not None and lng is not None else None
    return nearby_developments(devs, q, radius_m=radius_m, limit=limit, origin=origin)


@app.get("/api/trend")
def api_trend(q: str = Query(..., min_length=1)):
    """Price trend (avg PSF over time, resale+sub-sale only). Called with the
    already-resolved development name, same re-query pattern as the bot's trend
    button. Returns price_trend's dict unchanged (error/ambiguous passthrough)."""
    with _search_lock:
        return price_trend(q)


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


@app.get("/api/hdb/streets")
def api_hdb_streets():
    """Every distinct HDB street, as stored and as spelled out.

    This exists so the single search box can route free text to the right
    market *before* asking, which a heuristic cannot do: "8 SAINT THOMAS" is a
    condo that opens with a number and "BISHAN ST 22" is an HDB street that
    does not. Routing private-first-and-fall-back-on-error does not work
    either — a fuzzy private search answers an HDB street name with condos
    that merely share a word ("BISHAN ST 22" -> BISHAN LOFT), so the fallback
    never fires.

    Both spellings ride along because the data abbreviates ("ANG MO KIO AVE 6")
    while users type either that or the full form; matching one string against
    both covers it without the frontend re-implementing STREET_ABBREV.
    """
    ts = _hdb_meta_ts()
    if _hdb_streets_memo["payload"] is not None and _hdb_streets_memo["ts"] == ts:
        return _hdb_streets_memo["payload"]

    # Derived from the index set, not the window: it already holds every
    # distinct street, at a fraction of the rows.
    streets = sorted({r["street"] for r in hdb._normalise_all(_hdb_index_records())})
    payload = {
        "streets": [{"s": s, "c": hdb.expand_street(s)} for s in streets],
        "count": len(streets),
    }
    # Only a real answer is worth keeping. An empty list here means the read
    # failed, not that Singapore has no HDB streets, and memoizing it would
    # poison routing for the life of the process — every HDB query would then
    # fall back to the shape heuristic and quietly land in the private market.
    if streets:
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
        return band_transactions(q, band)


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
