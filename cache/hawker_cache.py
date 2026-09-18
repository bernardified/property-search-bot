"""NEA hawker centres — the amenity that needed no Google Places call.

Google's Places API is what every other amenity here goes through, but it is
the wrong tool for this one: a `keyword=hawker centre` search answers with
coffee shops, kopitiams and mall food courts mixed in among the real ones, and
there is no type that separates them. NEA publishes the actual register on
data.gov.sg — 129 centres, point geometry, no key and no auth — so this is the
schools pattern instead: fetch the whole list once, keep it in Mongo, and
answer "nearest N" with a haversine loop.

Scope is worth stating plainly, because it is the thing a user could
misread: these are **government hawker centres**. Privately run food courts
and coffee shops are not in the dataset and will not appear, which is exactly
what makes the list trustworthy for the ones that are.
"""

import logging
import time

import requests

from utils import get_mongo_db, haversine_m

logger = logging.getLogger(__name__)

# The NEA "Hawker Centres" dataset. v1 hands back a short-lived signed S3 URL
# rather than the file itself, so a fetch is always two requests.
DATASET_ID = "d_4a086da0a5553be1d89383cd90d07ecd"
POLL_URL = f"https://api-open.data.gov.sg/v1/public/api/datasets/{DATASET_ID}/poll-download"

# The register moves a handful of times a year (a centre reopens after
# upgrading, a new one is handed over), so monthly is generous.
CACHE_MAX_AGE_DAYS = 30

# Centres that are still building have a coordinate and a name but nothing to
# eat at. Every other status — including "Interim Centre" and the "(new)" and
# "(replacement)" variants — is open for business.
CLOSED_STATUSES = {"Under Construction"}

# One process asks at most once an hour. Without this every amenity lookup
# re-ran the freshness check and, on a box with no Mongo, re-fetched — which
# data.gov.sg rate-limits without an API key, so the second lookup in a row
# came back empty and the amenity silently vanished. An hour still picks up a
# refresh the same day on a register that moves a few times a year.
MEMO_TTL_S = 3600
_memo: dict = {"at": 0.0, "hawkers": []}

# 2km, matching primary schools rather than the 1km used for supermarkets.
# Measured against the map's own dots: at 1km, 30% of HDB blocks and 27% of
# private developments would get an empty list rather than a far-but-real
# answer. At 2km that is 4% and 3%.
MAX_RADIUS_M = 2000


# ── Cache read/write ─────────────────────────────────────────────────────────

def _is_cache_fresh() -> bool:
    db = get_mongo_db()
    if db is None:
        return False
    try:
        doc = db["hawker_cache"].find_one({"_id": "meta"})
        if not doc:
            return False
        return (time.time() - doc.get("timestamp", 0)) / 86400 < CACHE_MAX_AGE_DAYS
    except Exception:
        return False


def _load_cache() -> list:
    db = get_mongo_db()
    if db is None:
        return []
    try:
        doc = db["hawker_cache"].find_one({"_id": "data"})
        return doc.get("hawkers", []) if doc else []
    except Exception as e:
        logger.error(f"[Hawker Cache] Load failed: {e}")
        return []


def _save_cache(hawkers: list) -> None:
    db = get_mongo_db()
    if db is None:
        return
    try:
        db["hawker_cache"].replace_one(
            {"_id": "meta"},
            {"_id": "meta", "timestamp": time.time(), "hawker_count": len(hawkers)},
            upsert=True,
        )
        db["hawker_cache"].replace_one(
            {"_id": "data"}, {"_id": "data", "hawkers": hawkers}, upsert=True
        )
        logger.info(f"[Hawker Cache] Saved {len(hawkers)} hawker centres to MongoDB")
    except Exception as e:
        logger.error(f"[Hawker Cache] Save failed: {e}")


# ── Parsing (pure — tested) ──────────────────────────────────────────────────

def parse_hawker_geojson(geojson: dict) -> list:
    """GeoJSON features → the flat rows this module stores and serves.

    GeoJSON is lng-first, which is the one thing here that silently produces
    plausible-looking nonsense if it is read the other way round: Singapore's
    coordinates swapped land in the Indian Ocean, and every distance would
    still compute.

    `stalls` rides along because it is the honest difference between a centre
    with 112 cooked-food stalls and one with 12 — a distinction the name alone
    never carries.
    """
    out = []
    for f in geojson.get("features", []):
        p = f.get("properties") or {}
        name = str(p.get("NAME") or "").strip()
        if not name or p.get("STATUS") in CLOSED_STATUSES:
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        try:
            stalls = int(p.get("NUMBER_OF_COOKED_FOOD_STALLS") or 0)
        except (TypeError, ValueError):
            stalls = 0
        out.append({
            "name": name,
            "lat": lat,
            "lng": lng,
            "address": str(p.get("ADDRESS_MYENV") or "").strip(),
            "stalls": stalls,
        })
    return out


# ── Fetch ────────────────────────────────────────────────────────────────────

def _fetch_hawkers() -> list:
    """Download and parse the register. [] on any failure — the caller falls
    back to whatever is cached rather than losing the amenity."""
    try:
        poll = requests.get(POLL_URL, timeout=20).json()
        url = (poll.get("data") or {}).get("url")
        if not url:
            logger.warning(f"[Hawker Cache] No download URL in poll response: {poll}")
            return []
        return parse_hawker_geojson(requests.get(url, timeout=30).json())
    except Exception as e:
        logger.error(f"[Hawker Cache] Fetch failed: {e}")
        return []


# ── Public API ───────────────────────────────────────────────────────────────

def get_hawker_cache(now=None) -> list:
    """Every open hawker centre, refreshing the cache when it is stale."""
    now = now if now is not None else time.time()
    if _memo["hawkers"] and now - _memo["at"] < MEMO_TTL_S:
        return _memo["hawkers"]

    hawkers = _load_cache() if _is_cache_fresh() else []
    if not hawkers:
        logger.info("[Hawker Cache] Stale or missing — fetching from data.gov.sg…")
        hawkers = _fetch_hawkers()
        if hawkers:
            _save_cache(hawkers)
        else:
            # A failed fetch is not an empty Singapore: serve what is stored
            # and try again next time rather than saving nothing over
            # something — and never memoize the empty answer.
            logger.warning("[Hawker Cache] Nothing fetched — using stale cache")
            return _load_cache()

    _memo.update(at=now, hawkers=hawkers)
    return hawkers


def find_nearest_hawkers(origin_lat: float, origin_lng: float, top_n: int = 3) -> list:
    """The `top_n` nearest open hawker centres within MAX_RADIUS_M, nearest
    first. `dist` is straight-line metres; the walking distance is added later
    by maps.get_nearby_info, which is the only caller."""
    nearby = []
    for h in get_hawker_cache():
        dist = haversine_m(origin_lat, origin_lng, h["lat"], h["lng"])
        if dist <= MAX_RADIUS_M:
            nearby.append({**h, "dist": dist})
    nearby.sort(key=lambda x: x["dist"])
    return nearby[:top_n]
