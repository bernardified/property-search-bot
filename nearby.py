"""Nearby-properties search: private developments within a radius of a project.

URA transaction records carry no coordinates, so we geocode project addresses
via OneMap (free, no Google cost) and cache the result permanently in the Mongo
`project_coords` collection (same survive-forever idea as `unit_counts`). When
Mongo is unavailable we still geocode live — we just don't persist.

Candidates are bounded to the origin's **district** (read straight from URA's
`district` field) and capped by transaction volume, to keep the first (cold)
search tractable; later searches in the same area are instant off the cache.

Pure helpers take their data injected for tests; `nearby_for_project` is the
only IO entry point.
"""

import logging

from utils import haversine_m, get_mongo_db, get_onemap_token
from ura import get_ura_data
from district_search import _normalize_district
from maps import search_onemap

logger = logging.getLogger(__name__)

DEFAULT_RADIUS_M = 1000
DEFAULT_LIMIT = 10
# Cap how many candidate projects we geocode on a cold cache, to bound OneMap
# calls (and latency) on the first search in a district. The most-transacted
# developments are kept first, so the cap drops only obscure projects.
MAX_CANDIDATES_TO_GEOCODE = 80
COORDS_COLLECTION = "project_coords"
LANDED_TYPES = ("detached", "terrace", "bungalow")  # matches district_search


def _is_landed(prop_type: str) -> bool:
    return any(t in (prop_type or "").lower() for t in LANDED_TYPES)


def find_origin(transactions: list, name: str) -> dict | None:
    """Return the URA project record exactly matching `name` (case-insensitive)."""
    target = name.strip().upper()
    for pd in transactions:
        if (pd.get("project") or "").strip().upper() == target:
            return pd
    return None


def project_district(record: dict) -> int | None:
    """The dominant district of a project record (mode of its txn districts)."""
    districts = [
        d for d in (_normalize_district(t.get("district"))
                    for t in record.get("transaction", []))
        if d
    ]
    if not districts:
        return None
    return max(set(districts), key=districts.count)


def candidate_projects(transactions: list, district: int) -> list[dict]:
    """Non-landed projects in `district`, sorted by transaction volume desc.

    Returns [{"project", "street", "txn_count"}], one row per project name.
    """
    bucket: dict[str, dict] = {}
    for pd in transactions:
        name = (pd.get("project") or "").strip()
        if not name:
            continue
        street = (pd.get("street") or "").strip()
        for txn in pd.get("transaction", []):
            if _normalize_district(txn.get("district")) != district:
                continue
            if _is_landed(txn.get("propertyType", "")):
                continue
            row = bucket.setdefault(
                name.upper(), {"project": name, "street": street, "txn_count": 0}
            )
            row["txn_count"] += 1
    rows = [r for r in bucket.values() if r["txn_count"] > 0]
    rows.sort(key=lambda r: r["txn_count"], reverse=True)
    return rows


# ── Geocoding + coords cache (IO) ───────────────────────────────────────────────

def _get_cached_coords(db, key: str):
    if db is None:
        return None
    try:
        doc = db[COORDS_COLLECTION].find_one({"_id": key})
    except Exception as e:
        logger.warning(f"[Nearby] coords lookup failed for {key}: {e}")
        return None
    if doc and doc.get("lat") is not None and doc.get("lng") is not None:
        return (doc["lat"], doc["lng"])
    return None


def _cache_coords(db, key: str, name: str, street: str, lat: float, lng: float):
    if db is None:
        return
    try:
        db[COORDS_COLLECTION].replace_one(
            {"_id": key},
            {"_id": key, "project": name, "street": street, "lat": lat, "lng": lng},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"[Nearby] coords cache write failed for {key}: {e}")


def _geocode(name: str, street: str, token: str):
    """Geocode a project via OneMap: try the project name, then the street.

    Returns (lat, lng) or None. OneMap building names usually match the URA
    project name; the street is a coarse fallback that still lands on the road.
    """
    for query in (name, street):
        if not query:
            continue
        for result in search_onemap(query, token):
            try:
                return float(result["LATITUDE"]), float(result["LONGITUDE"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _resolve_coords(db, token, key, name, street):
    """Cache-first coords: read Mongo, else geocode live and (if possible) persist."""
    coords = _get_cached_coords(db, key)
    if coords is not None:
        return coords
    coords = _geocode(name, street, token)
    if coords is not None:
        _cache_coords(db, key, name, street, *coords)
    return coords


def nearby_for_project(name: str, radius_m: int = DEFAULT_RADIUS_M,
                       limit: int = DEFAULT_LIMIT) -> dict:
    """Find private developments within `radius_m` of `name` (same district).

    Returns {"origin", "district", "radius_m", "results": [{project, street,
    distance_m}]} or {"error": ...}. Never raises for the "nothing nearby" case.
    """
    transactions, _ = get_ura_data()
    if not transactions:
        return {"error": "Could not load URA transaction data. Please try again later."}

    origin = find_origin(transactions, name)
    if origin is None:
        return {"error": f'Could not locate "{name}" in the URA data.'}

    district = project_district(origin)
    if district is None:
        return {"error": "No district information for this property."}

    db = get_mongo_db()
    token = get_onemap_token()
    origin_key = (origin.get("project") or "").strip().upper()

    origin_coords = _resolve_coords(
        db, token, origin_key, origin.get("project", ""), origin.get("street", "")
    )
    if origin_coords is None:
        return {"error": "Could not pinpoint this property on the map."}

    candidates = candidate_projects(transactions, district)
    results = []
    geocoded = 0
    for cand in candidates:
        key = cand["project"].strip().upper()
        if key == origin_key:
            continue  # exclude the origin itself

        coords = _get_cached_coords(db, key)
        if coords is None:
            if geocoded >= MAX_CANDIDATES_TO_GEOCODE:
                continue  # cold-cache cap reached; skip the long tail
            coords = _geocode(cand["project"], cand["street"], token)
            geocoded += 1
            if coords is not None:
                _cache_coords(db, key, cand["project"], cand["street"], *coords)
        if coords is None:
            continue

        dist = haversine_m(origin_coords[0], origin_coords[1], coords[0], coords[1])
        if dist <= radius_m:
            results.append({
                "project": cand["project"],
                "street": cand["street"],
                "distance_m": round(dist),
            })

    results.sort(key=lambda r: r["distance_m"])
    return {
        "origin": origin.get("project", name),
        "district": district,
        "radius_m": radius_m,
        "results": results[:limit],
    }
