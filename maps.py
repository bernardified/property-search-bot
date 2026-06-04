import os
import re
import time
import requests
from dotenv import load_dotenv
from urllib.parse import quote
from math import radians, sin, cos, sqrt, atan2
from onemap_mrt import find_nearest_mrts as onemap_find_nearest_mrts
from mrt_data import get_line_for_exit, LINE_FORMAT

load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
ONEMAP_EMAIL = os.getenv("ONEMAP_EMAIL")
ONEMAP_PASSWORD = os.getenv("ONEMAP_PASSWORD")

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
DISTANCE_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
PLACES_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
ONEMAP_AUTH_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"
ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"

# ── OneMap token cache ────────────────────────────────────────────────────────
_onemap_token = None
_onemap_token_expiry = 0


def get_onemap_token() -> str | None:
    """Return a valid OneMap token, refreshing if expired."""
    global _onemap_token, _onemap_token_expiry

    if _onemap_token and time.time() < _onemap_token_expiry - 300:
        return _onemap_token

    if not ONEMAP_EMAIL or not ONEMAP_PASSWORD:
        print("[OneMap] No credentials in .env")
        return None

    try:
        r = requests.post(
            ONEMAP_AUTH_URL,
            json={"email": ONEMAP_EMAIL, "password": ONEMAP_PASSWORD},
            timeout=10
        )
        data = r.json()
        if "access_token" in data:
            _onemap_token = data["access_token"]
            _onemap_token_expiry = int(data.get("expiry_timestamp", time.time() + 28800))
            print("[OneMap] Token refreshed")
            return _onemap_token
        print(f"[OneMap] Auth failed: {data}")
        return None
    except Exception as e:
        print(f"[OneMap] Auth error: {e}")
        return None


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


# ── Haversine distance ────────────────────────────────────────────────────────

def haversine_m(lat1, lng1, lat2, lng2) -> float:
    """Straight-line distance in metres between two coordinates."""
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi/2)**2 + cos(phi1)*cos(phi2)*sin(dlambda/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


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


def find_nearest_mrts(origin_lat: float, origin_lng: float, top_n: int = 3) -> list:
    """
    Use OneMap to find the top_n nearest MRT stations with their closest exit.
    Strategy:
    1. Search OneMap for nearby MRT stations by name using our whitelist
    2. For each candidate station, search for its exits
    3. Pick the nearest exit per station
    4. Rank stations by distance to nearest exit
    5. Return top_n
    """
    token = get_onemap_token()
    if not token:
        return []

    # Singapore MRT station names to search — we search in batches of nearby ones
    # by using a broader search and filtering by distance
    # Strategy: search "MRT STATION" broadly and filter by proximity

    # OneMap doesn't support radius search, so we search common patterns
    # and filter by haversine distance
    candidate_stations = {}  # station_base_name -> {lat, lng, name}

    # Search pages of "MRT STATION" results
    for page in range(1, 4):
        try:
            r = requests.get(
                ONEMAP_SEARCH_URL,
                params={
                    "searchVal": "MRT STATION",
                    "returnGeom": "Y",
                    "getAddrDetails": "N",
                    "pageNum": page
                },
                headers={"Authorization": token},
                timeout=10
            )
            results = r.json().get("results", [])
            if not results:
                break
            for item in results:
                name = item.get("SEARCHVAL", "")
                if not is_mrt_station(name):
                    continue
                try:
                    lat = float(item["LATITUDE"])
                    lng = float(item["LONGITUDE"])
                except (KeyError, ValueError):
                    continue
                dist = haversine_m(origin_lat, origin_lng, lat, lng)
                if dist > 2500:  # only consider within 2.5km
                    continue
                base = clean_station_name(name).upper()
                # Keep closest entry per station name
                if base not in candidate_stations or dist < candidate_stations[base]["dist"]:
                    candidate_stations[base] = {
                        "name": clean_station_name(name),
                        "lat": lat,
                        "lng": lng,
                        "dist": dist,
                    }
        except Exception as e:
            print(f"[OneMap] Page {page} search failed: {e}")
            break

    if not candidate_stations:
        return []

    # Sort by straight-line distance, take top candidates to check exits for
    sorted_stations = sorted(candidate_stations.values(), key=lambda x: x["dist"])[:6]

    # For each station, find its nearest exit
    results = []
    for station in sorted_stations:
        exit_query = f"{station['name'].upper()} MRT STATION EXIT"
        exits = search_onemap(exit_query, token)
        exits = [e for e in exits if is_mrt_exit(e.get("SEARCHVAL", ""))]

        nearest_exit = None
        nearest_exit_dist = float("inf")

        for ex in exits:
            try:
                elat = float(ex["LATITUDE"])
                elng = float(ex["LONGITUDE"])
            except (KeyError, ValueError):
                continue
            dist = haversine_m(origin_lat, origin_lng, elat, elng)
            if dist < nearest_exit_dist:
                nearest_exit_dist = dist
                nearest_exit = {
                    "letter": get_exit_letter(ex.get("SEARCHVAL", "")),
                    "lat": elat,
                    "lng": elng,
                    "dist": dist,
                }

        # Use nearest exit coords if found, otherwise station coords
        if nearest_exit:
            dest_lat = nearest_exit["lat"]
            dest_lng = nearest_exit["lng"]
            exit_label = f" (Exit {nearest_exit['letter']})" if nearest_exit["letter"] else ""
        else:
            dest_lat = station["lat"]
            dest_lng = station["lng"]
            exit_label = ""

        results.append({
            "name": station["name"],
            "exit_label": exit_label,
            "dest_lat": dest_lat,
            "dest_lng": dest_lng,
            "straight_dist": nearest_exit["dist"] if nearest_exit else station["dist"],
        })

    # Sort by straight-line distance to nearest exit
    results.sort(key=lambda x: x["straight_dist"])
    return results[:top_n]


# ── Google Places — mall only ─────────────────────────────────────────────────

def find_nearest_mall(lat: float, lng: float) -> dict | None:
    """Use Google Places to find nearest shopping mall."""
    params = {
        "location": f"{lat},{lng}",
        "radius": 2000,
        "keyword": "shopping mall",
        "type": "shopping_mall",
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


def build_google_maps_link(origin_name, dest_lat, dest_lng, travel_mode: str = "walking") -> str:
    encoded = quote(f"{origin_name}, Singapore")
    return (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={encoded}"
        f"&destination={dest_lat},{dest_lng}"
        f"&travelmode={travel_mode}"
    )


def get_transit_distances_bulk(origin_lat, origin_lng, destinations) -> list:
    """Get public transit distances from one origin to multiple destinations."""
    if not destinations:
        return []
    dest_str = "|".join([f"{d['lat']},{d['lng']}" for d in destinations])
    params = {
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": dest_str,
        "mode": "transit",
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
    from schools_cache import find_nearest_primary_schools as cached_schools
    return cached_schools(lat, lng, top_n=5)


# ── Main function ─────────────────────────────────────────────────────────────

def get_nearby_info(address: str) -> dict:
    coords = geocode_address(address)
    if not coords:
        return {"error": f'Could not locate "{address}" on Google Maps.'}
    lat, lng = coords

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
                    "maps_link": build_google_maps_link(address, station["dest_lat"], station["dest_lng"]),
                })

        mrt_results = _enrich_with_transit(lat, lng, address, mrt_results)

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
                "maps_link": build_google_maps_link(address, item["lat"], item["lng"]),
            })
        mall_results = _enrich_with_transit(lat, lng, address, mall_results)

    # ── Primary schools via OneMap ───────────────────────────────────────────
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
                    "maps_link": build_google_maps_link(address, school["lat"], school["lng"]),
                    "dist": school["dist"],
                })
        school_results = _enrich_with_transit(lat, lng, address, school_results)

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
                "maps_link": build_google_maps_link(address, item["lat"], item["lng"]),
            })
        supermarket_results = _enrich_with_transit(lat, lng, address, supermarket_results)

    return {"address": address, "lat": lat, "lng": lng, "mrts": mrt_results, "malls": mall_results, "schools": school_results, "supermarkets": supermarket_results}


# ── Formatting ────────────────────────────────────────────────────────────────

def format_nearby(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']}"

    lines = ["📍 *Nearby Amenities*", "─────────────────────"]

    mrts = result.get("mrts", [])
    if mrts:
        lines.append("🚇 *Nearest MRT Stations*")
        for i, mrt in enumerate(mrts, 1):
            lines.append(
                f"  {i}. {mrt['name']}\n"
                f"     🚶 {mrt['duration']} ({mrt['distance']})\n"
                f"     [Walking directions]({mrt['maps_link']})"
            )
    else:
        lines.append("🚇 *Nearest MRT Stations*\n  None found within 2.5km")

    lines.append("")

    malls = result.get("malls", [])
    if malls:
        lines.append("🛍️ *Nearest Shopping Malls*")
        for i, mall in enumerate(malls, 1):
            lines.append(
                f"  {i}. {mall['name']}\n"
                f"     🚶 {mall['duration']} ({mall['distance']})\n"
                f"     [Walking directions]({mall['maps_link']})"
            )
    else:
        lines += ["🛍️ *Nearest Shopping Malls*", "  None found within 2km"]

    lines.append("")

    schools = result.get("schools", [])
    if schools:
        lines.append("🏫 *Nearest Primary Schools* _(within 1km)_")
        for i, school in enumerate(schools, 1):
            lines.append(
                f"  {i}. {school['name']}\n"
                f"     🚶 {school['duration']} ({school['distance']})\n"
                f"     [Walking directions]({school['maps_link']})"
            )
    else:
        lines += ["🏫 *Nearest Primary Schools*", "  None found within 1km"]

    return "\n".join(lines)
