import os
import re
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from urllib.parse import quote
from cache.onemap_mrt import find_nearest_mrts as onemap_find_nearest_mrts
from mrt_data import get_line_for_exit, LINE_FORMAT
from utils import haversine_m, get_onemap_token

SGT = ZoneInfo("Asia/Singapore")


def _next_tuesday_9am_sgt() -> int:
    """
    Return a Unix timestamp for the next upcoming Tuesday at 09:00 SGT.
    Used as departure_time for transit API calls so results reflect typical
    weekday-morning conditions rather than the actual time of the request.
    Tuesday is chosen as mid-week with stable, representative service patterns.
    """
    now = datetime.now(SGT)
    days_until_tuesday = (1 - now.weekday()) % 7
    if days_until_tuesday == 0 and now.hour >= 9:
        days_until_tuesday = 7
    target = now + timedelta(days=days_until_tuesday)
    target_9am = target.replace(hour=9, minute=0, second=0, microsecond=0)
    return int(target_9am.timestamp())

load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DISTANCE_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"


# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode_address(address: str) -> tuple[float, float] | None:
    """Convert address to lat/lng using Google Geocoding."""
    params = {"address": f"{address}, Singapore", "key": GOOGLE_MAPS_API_KEY}
    try:
        r = requests.get(GEOCODE_URL, params=params, timeout=10)
        data = r.json()
        if data["status"] == "OK":
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
        print(f"[Maps] Geocode error: {data['status']}")
        return None
    except Exception as e:
        print(f"[Maps] Geocode failed: {e}")
        return None




# ── Postal-code lookup ────────────────────────────────────────────────────────

POSTAL_CODE_RE = re.compile(r"^\d{6}$")


def resolve_postal_code(postal: str) -> dict | None:
    """Resolve a 6-digit Singapore postal code to its building / development.

    Uses OneMap's elastic-search endpoint, which returns the building name,
    road, full address and coordinates for a postal code. Postal codes are a
    unique key in OneMap, so the matching record (if any) is authoritative.

    Returns:
        {"building", "road", "address", "postal", "lat", "lng"}  — building may
        be "" when OneMap has no building name on record (landed homes, some
        HDB blocks), in which case it can't be matched to a private development.
        None — when the postal code yields no OneMap result at all.
    """
    postal = postal.strip()
    if not POSTAL_CODE_RE.match(postal):
        return None

    results = search_onemap(postal, get_onemap_token())
    if not results:
        return None

    # OneMap can return nearby hits alongside the exact one — prefer the record
    # whose POSTAL matches exactly, falling back to the top result.
    match = next(
        (r for r in results if str(r.get("POSTAL", "")).strip() == postal),
        results[0],
    )

    building = str(match.get("BUILDING", "")).strip()
    if building.upper() in ("NIL", "NA", ""):
        building = ""

    try:
        lat = float(match["LATITUDE"])
        lng = float(match["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        lat = lng = None

    return {
        "building": building,
        "block": str(match.get("BLK_NO", "")).strip(),
        "road": str(match.get("ROAD_NAME", "")).strip(),
        "address": str(match.get("ADDRESS", "")).strip(),
        "postal": postal,
        "lat": lat,
        "lng": lng,
    }


def geocode_building(query: str) -> dict | None:
    """Geocode a free-text address (e.g. an HDB block + street) via OneMap's
    elastic search — the same authoritative source as resolve_postal_code, but
    keyed on a building/address string instead of a postal code.

    Returns {"building", "road", "address", "lat", "lng"} for the top hit, or
    None when there is no result or it carries no coordinate.
    """
    results = search_onemap(query, get_onemap_token())
    if not results:
        return None
    m = results[0]
    try:
        lat = float(m["LATITUDE"])
        lng = float(m["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        return None
    building = str(m.get("BUILDING", "")).strip()
    if building.upper() in ("NIL", "NA", ""):
        building = ""
    return {
        "building": building,
        "road": str(m.get("ROAD_NAME", "")).strip(),
        "address": str(m.get("ADDRESS", "")).strip(),
        "lat": lat,
        "lng": lng,
    }


# ── OneMap MRT search ─────────────────────────────────────────────────────────

def search_onemap(query: str, token: str) -> list:
    """Search OneMap and return raw results."""
    try:
        r = requests.get(
            ONEMAP_SEARCH_URL,
            params={
                "searchVal": query,
                "returnGeom": "Y",
                "getAddrDetails": "Y",
                "pageNum": 1
            },
            headers={"Authorization": token},
            timeout=10
        )
        return r.json().get("results", [])
    except Exception as e:
        print(f"[OneMap] Search failed for '{query}': {e}")
        return []


def is_mrt_exit(name: str) -> bool:
    """Return True if this is an MRT exit entry (not the station itself)."""
    return bool(re.search(r'exit\s+[a-z]', name.lower()))


def is_mrt_station(name: str) -> bool:
    """Return True if this is a main MRT station entry (not an exit)."""
    name_upper = name.upper()
    return "MRT STATION" in name_upper and not is_mrt_exit(name)


def clean_station_name(name: str) -> str:
    """
    Clean station name for display.
    'LORONG CHUAN MRT STATION (CC14)' -> 'Lorong Chuan'
    """
    name = re.sub(r'\s*\([A-Z]{2,3}\d+\)\s*', '', name)
    name = re.sub(r'\s*MRT\s*STATION\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s*MRT\s*$', '', name, flags=re.IGNORECASE)
    return name.strip().title()


def get_exit_letter(name: str) -> str:
    """Extract exit letter from 'LORONG CHUAN MRT STATION EXIT A' -> 'A'"""
    match = re.search(r'EXIT\s+([A-Z])', name.upper())
    return match.group(1) if match else ""



# ── Google Places — mall only ─────────────────────────────────────────────────

def find_nearest_mall(lat: float, lng: float) -> dict | None:
    """Use Google Places to find nearest shopping mall.

    Uses rankby=distance (not radius) so genuinely-nearest malls surface.
    A radius+keyword search ranks by Google's "prominence" instead, which
    drops small neighbourhood malls — e.g. Hougang 1 (388m) was being hidden
    behind prominent malls 1.6km+ away. rankby=distance forbids `radius` and
    needs a keyword/type; "shopping mall" keeps the list to real malls
    (type=shopping_mall alone pulls in mis-tagged shops/warehouses).
    """
    params = {
        "location": f"{lat},{lng}",
        "rankby": "distance",
        "keyword": "shopping mall",
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(PLACES_URL, params=params, timeout=10)
        data = r.json()
        if data["status"] == "OK" and data["results"]:
            candidates = []
            for p in data["results"][:8]:
                candidates.append({
                    "name": p["name"],
                    "lat": p["geometry"]["location"]["lat"],
                    "lng": p["geometry"]["location"]["lng"],
                })
            return candidates
        return []
    except Exception as e:
        print(f"[Maps] Mall search failed: {e}")
        return []


# ── Supermarkets via Google Places ───────────────────────────────────────────

MAJOR_SUPERMARKET_CHAINS = [
    "fairprice", "ntuc", "cold storage", "giant", "sheng siong",
    "prime supermarket", "hao mart", "marketplace", "jason's",
    "meidi-ya", "don don donki", "donki",
]

def is_major_supermarket(name: str) -> bool:
    """Filter to major supermarket chains only."""
    name_lower = name.lower()
    return any(chain in name_lower for chain in MAJOR_SUPERMARKET_CHAINS)


def find_nearest_supermarkets(lat: float, lng: float) -> list:
    """Use Google Places to find nearest major supermarkets within 1km."""
    params = {
        "location": f"{lat},{lng}",
        "radius": 1000,
        "keyword": "supermarket",
        "type": "supermarket",
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(PLACES_URL, params=params, timeout=10)
        data = r.json()
        if data["status"] == "OK" and data["results"]:
            candidates = []
            for p in data["results"][:15]:
                name = p["name"]
                if not is_major_supermarket(name):
                    continue
                candidates.append({
                    "name": name,
                    "lat": p["geometry"]["location"]["lat"],
                    "lng": p["geometry"]["location"]["lng"],
                })
            return candidates
        return []
    except Exception as e:
        print(f"[Maps] Supermarket search failed: {e}")
        return []


# ── Walking distances ─────────────────────────────────────────────────────────

def get_walking_distances_bulk(origin_lat, origin_lng, destinations) -> list:
    """Get walking distances from one origin to multiple destinations."""
    if not destinations:
        return []
    dest_str = "|".join([f"{d['lat']},{d['lng']}" for d in destinations])
    params = {
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": dest_str,
        "mode": "walking",
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(DISTANCE_URL, params=params, timeout=10)
        data = r.json()
        if data["status"] == "OK":
            results = []
            for el in data["rows"][0]["elements"]:
                if el["status"] == "OK":
                    results.append({
                        "distance_text": el["distance"]["text"],
                        "distance_m": el["distance"]["value"],
                        "duration_text": el["duration"]["text"],
                    })
                else:
                    results.append(None)
            return results
        return [None] * len(destinations)
    except Exception as e:
        print(f"[Maps] Distance fetch failed: {e}")
        return [None] * len(destinations)


def build_google_maps_link(origin, dest_lat, dest_lng, travel_mode: str = "walking") -> str:
    """Directions link from an origin to a destination.

    `origin` may be a name string (geocoded by Google from the text) or a
    (lat, lng) tuple. Use coords when the caller already knows the exact point
    so the routed directions match the distance/time we computed from the same
    coordinate; use a name when only the address text is known.
    """
    if isinstance(origin, (tuple, list)):
        origin_param = f"{origin[0]},{origin[1]}"
    else:
        origin_param = quote(f"{origin}, Singapore")
    return (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={origin_param}"
        f"&destination={dest_lat},{dest_lng}"
        f"&travelmode={travel_mode}"
    )


def get_transit_distances_bulk(origin_lat, origin_lng, destinations) -> list:
    """
    Get public transit distances from one origin to multiple destinations.
    Uses next Tuesday 09:00 SGT as departure_time so results reflect typical
    weekday-morning service rather than whatever time the user taps the button.
    """
    if not destinations:
        return []
    dest_str = "|".join([f"{d['lat']},{d['lng']}" for d in destinations])
    params = {
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": dest_str,
        "mode": "transit",
        "departure_time": _next_tuesday_9am_sgt(),
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(DISTANCE_URL, params=params, timeout=10)
        data = r.json()
        if data["status"] == "OK":
            results = []
            for el in data["rows"][0]["elements"]:
                if el["status"] == "OK":
                    results.append({
                        "distance_text": el["distance"]["text"],
                        "distance_m": el["distance"]["value"],
                        "duration_text": el["duration"]["text"],
                    })
                else:
                    results.append(None)
            return results
        return [None] * len(destinations)
    except Exception as e:
        print(f"[Maps] Transit distance fetch failed: {e}")
        return [None] * len(destinations)


# ── Transit enrichment ────────────────────────────────────────────────────────

WALK_THRESHOLD_M = 1000  # above this, show transit alternative

def _enrich_with_transit(origin_lat, origin_lng, origin_name, results: list) -> list:
    """
    For any result whose walking distance exceeds WALK_THRESHOLD_M, fetch
    the transit time and switch the maps link to transit mode.

    Mutates results in-place and returns them.
    Each result dict must have: distance_m, dest_lat, dest_lng, maps_link.
    """
    far_indices = [i for i, r in enumerate(results) if r.get("distance_m", 0) > WALK_THRESHOLD_M]
    if not far_indices:
        return results

    far_dests = [{"lat": results[i]["dest_lat"], "lng": results[i]["dest_lng"]} for i in far_indices]
    transit_data = get_transit_distances_bulk(origin_lat, origin_lng, far_dests)

    for list_idx, result_idx in enumerate(far_indices):
        td = transit_data[list_idx] if list_idx < len(transit_data) else None
        if td:
            results[result_idx]["transit_duration"] = td["duration_text"]
            results[result_idx]["transit_distance"] = td["distance_text"]
        # Switch the maps link to transit mode regardless (walking > 1km = take transit)
        results[result_idx]["maps_link"] = build_google_maps_link(
            origin_name,
            results[result_idx]["dest_lat"],
            results[result_idx]["dest_lng"],
            travel_mode="transit",
        )

    return results


# ── Primary schools via cached OneMap data ───────────────────────────────────

def find_nearest_primary_schools(lat: float, lng: float) -> list:
    """Find nearest primary schools using MongoDB-cached OneMap data."""
    from cache.schools_cache import find_nearest_primary_schools as cached_schools
    return cached_schools(lat, lng, top_n=5)


# ── Main function ─────────────────────────────────────────────────────────────

def get_nearby_info(address: str, lat: float | None = None, lng: float | None = None) -> dict:
    """Find nearby amenities (MRT, malls, schools, supermarkets) for a location.

    The origin coordinate drives both candidate selection (nearest N) and the
    walking/transit distances. When `lat`/`lng` are supplied (e.g. the exact
    OneMap coordinate from a postal-code search) they are used directly —
    skipping Google geocoding — and directions links are routed from those
    coords so the displayed times match the tap-through. Otherwise `address`
    is geocoded by name as before.
    """
    if lat is not None and lng is not None:
        origin = (lat, lng)   # route links from the exact coordinate
    else:
        coords = geocode_address(address)
        if not coords:
            return {"error": f'Could not locate "{address}" on Google Maps.'}
        lat, lng = coords
        origin = address      # only the address text is known — geocode by name

    # ── MRT via OneMap cached station data ───────────────────────────────────
    mrt_candidates = onemap_find_nearest_mrts(lat, lng, top_n=3)

    mrt_results = []
    if mrt_candidates:
        dest_list = [{"lat": m["dest_lat"], "lng": m["dest_lng"]} for m in mrt_candidates]
        distances = get_walking_distances_bulk(lat, lng, dest_list)

        for station, dist in zip(mrt_candidates, distances):
            if dist:
                raw_name = f"{station['name']} MRT{station['exit_label']}"
                line_label = get_line_for_exit(raw_name)   # e.g. " [🟡 CCL, 🟣 NEL]"
                mrt_results.append({
                    "name": f"{raw_name}{line_label}",
                    "distance": dist["distance_text"],
                    "duration": dist["duration_text"],
                    "distance_m": dist["distance_m"],
                    "dest_lat": station["dest_lat"],
                    "dest_lng": station["dest_lng"],
                    "maps_link": build_google_maps_link(origin, station["dest_lat"], station["dest_lng"]),
                })

        mrt_results = _enrich_with_transit(lat, lng, origin, mrt_results)

    # ── Mall via Google Places ────────────────────────────────────────────────
    mall_results = []
    mall_candidates = find_nearest_mall(lat, lng)
    if mall_candidates:
        distances = get_walking_distances_bulk(lat, lng, mall_candidates)
        combined = []
        for place, dist in zip(mall_candidates, distances):
            if dist:
                combined.append({**place, **dist})
        combined.sort(key=lambda x: x["distance_m"])
        for item in combined[:3]:
            mall_results.append({
                "name": item["name"],
                "distance": item["distance_text"],
                "duration": item["duration_text"],
                "distance_m": item["distance_m"],
                "dest_lat": item["lat"],
                "dest_lng": item["lng"],
                "maps_link": build_google_maps_link(origin, item["lat"], item["lng"]),
            })
        mall_results = _enrich_with_transit(lat, lng, origin, mall_results)

    # ── Primary schools via OneMap ───────────────────────────────────────────
    school_results = []
    schools = find_nearest_primary_schools(lat, lng)
    if schools:
        dest_list = [{"lat": s["lat"], "lng": s["lng"]} for s in schools]
        distances = get_walking_distances_bulk(lat, lng, dest_list)
        for school, dist in zip(schools, distances):
            if dist:
                school_results.append({
                    "name": school["name"],
                    "distance": dist["distance_text"],
                    "duration": dist["duration_text"],
                    "distance_m": dist["distance_m"],
                    "dest_lat": school["lat"],
                    "dest_lng": school["lng"],
                    "maps_link": build_google_maps_link(origin, school["lat"], school["lng"]),
                    "dist": school["dist"],
                })
        school_results = _enrich_with_transit(lat, lng, origin, school_results)

    # ── Supermarkets via Google Places ──────────────────────────────────────────
    supermarket_results = []
    supermarket_candidates = find_nearest_supermarkets(lat, lng)
    if supermarket_candidates:
        distances = get_walking_distances_bulk(lat, lng, supermarket_candidates)
        combined = []
        for place, dist in zip(supermarket_candidates, distances):
            if dist:
                combined.append({**place, **dist})
        combined.sort(key=lambda x: x["distance_m"])
        for item in combined[:3]:
            supermarket_results.append({
                "name": item["name"],
                "distance": item["distance_text"],
                "duration": item["duration_text"],
                "distance_m": item["distance_m"],
                "dest_lat": item["lat"],
                "dest_lng": item["lng"],
                "maps_link": build_google_maps_link(origin, item["lat"], item["lng"]),
            })
        supermarket_results = _enrich_with_transit(lat, lng, origin, supermarket_results)

    return {"address": address, "lat": lat, "lng": lng, "mrts": mrt_results, "malls": mall_results, "schools": school_results, "supermarkets": supermarket_results}
