import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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

def is_shopping_mall(place: dict) -> bool:
    """True only for places Google itself classifies as a shopping mall.

    The keyword search returns the mall AND everything trading inside it, so
    Paya Lebar Quarter answered with "SKP @ Paya Lebar Quarter Mall", "OWNDAYS
    PLQ Mall", "2nd STREET PLQ Mall" and "Starbucks Reserve @ PLQ" — shops
    whose names contain "Mall", ahead of PLQ Mall itself. Their `types` say
    what they are (`store`, `clothing_store`, `cafe`), and the mall's says
    `shopping_mall`, so the RESPONSE's types are the filter.

    Note this is not the `type=shopping_mall` REQUEST parameter, which is a
    different thing and is still avoided (see find_nearest_mall): that changes
    what Google searches for and drags in mis-tagged warehouses.

    Measured across 11 spread-out origins — CBD, heartland, Sentosa, Lim Chu
    Kang — the filter never left fewer than 8 malls, so it needs no fallback.
    """
    return "shopping_mall" in (place.get("types") or [])


def find_nearest_mall(lat: float, lng: float) -> dict | None:
    """Use Google Places to find nearest shopping mall.

    Uses rankby=distance (not radius) so genuinely-nearest malls surface.
    A radius+keyword search ranks by Google's "prominence" instead, which
    drops small neighbourhood malls — e.g. Hougang 1 (388m) was being hidden
    behind prominent malls 1.6km+ away. rankby=distance forbids `radius` and
    needs a keyword/type; "shopping mall" keeps the search broad, and
    is_shopping_mall then drops the tenants it also returns.

    A handful of Google mis-tags survive (a craft shop inside PLQ carries
    `shopping_mall`). Both discriminators tried against them cost more than
    they saved: a `user_ratings_total` floor drops real malls at about 1:1
    (Marina Bay Link Mall has 12 ratings; a mis-tagged salon has 146), and
    collapsing near-neighbours to the best-rated one picks the loudest tenant
    over the quiet mall it sits in. So the mis-tags stay.
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
            for p in data["results"]:
                if not is_shopping_mall(p):
                    continue
                candidates.append({
                    "name": p["name"],
                    "lat": p["geometry"]["location"]["lat"],
                    "lng": p["geometry"]["location"]["lng"],
                })
                if len(candidates) >= 8:
                    break      # rankby=distance, so these are already the nearest
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


# ── Coffee shops / kopitiams via Google Places ───────────────────────────────
#
# The one food amenity with no register behind it. A kopitiam is a private
# tenancy in an HDB commercial block, so nothing authoritative lists them:
# HDB's own Property Information dataset flags 2,526 blocks as `commercial`,
# but that is every block with a minimart, clinic or hairdresser, and its
# `market_hawker` flag (107 blocks) means a market, not a coffee shop. So this
# one does go through Places — and needs a name filter to be worth anything.
#
# Two kinds of noise come back consistently, both verified against real
# origins: individual STALLS inside a coffee shop ("Hao Yun Lai Fried Hokkien
# Prawn Mee", "Tham's Roasted Delights"), which would list one venue five
# times, and SPECIALITY CAFES ("Starbucks Reserve @ PLQ", "Tiong Hoe Specialty
# Coffee", "144 Brew Kopi"), which are not what anyone means by the coffee shop
# downstairs. The filter is deliberately conservative: it keeps only names that
# say what they are, so it misses the occasional genuine one ("Johnson Eatery")
# rather than promising a Starbucks is your kopitiam.

COFFEESHOP_WORDS = [
    "coffee shop", "coffeeshop", "coffee house", "kopitiam",
    "food house", "foodhouse", "eating house", "kopi house",
    "food centre", "food court",
]

NOT_A_COFFEESHOP = [
    "starbucks", "specialty", "speciality", "roaster", "brew",
    "cafe bar", "% arabica", "coffee bean", "toast box", "ya kun",
    "coffee co", "craft coffee", "cart coffee",
]

# rankby=distance (as with malls) rather than a radius: prominence ranking
# answered an Ang Mo Kio origin with a Hougang coffee shop 4km away. rankby
# forbids `radius`, so the cap is applied here instead — 1km, the supermarket
# radius, because a coffee shop is a downstairs amenity and one 2km away is
# not the question being asked.
COFFEESHOP_MAX_M = 1000


def is_coffeeshop(name: str) -> bool:
    """True only for names that say they are a coffee shop. Exclusions run
    FIRST — "Kimly Coffeeshop" must pass while "144 Brew Kopi" does not."""
    n = (name or "").lower()
    if any(w in n for w in NOT_A_COFFEESHOP):
        return False
    return any(w in n for w in COFFEESHOP_WORDS)


def find_nearest_coffeeshops(lat: float, lng: float) -> list:
    """Nearest HDB-style coffee shops / kopitiams, filtered by name."""
    params = {
        "location": f"{lat},{lng}",
        "rankby": "distance",
        "keyword": "coffeeshop",
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(PLACES_URL, params=params, timeout=10)
        data = r.json()
        if data.get("status") != "OK" or not data.get("results"):
            return []
        out = []
        for p in data["results"]:
            if not is_coffeeshop(p["name"]):
                continue
            loc = p["geometry"]["location"]
            dist = haversine_m(lat, lng, loc["lat"], loc["lng"])
            if dist > COFFEESHOP_MAX_M:
                break            # rankby=distance, so everything after is further
            out.append({"name": p["name"], "lat": loc["lat"], "lng": loc["lng"],
                        "dist": dist})
        return out[:3]
    except Exception as e:
        print(f"[Maps] Coffee shop search failed: {e}")
        return []


def find_nearest_hawkers(lat: float, lng: float) -> list:
    """Nearest government hawker centres, from NEA's own register.

    Not a Places call: `keyword=hawker centre` answers with coffee shops and
    mall food courts mixed in, and no Places type separates them. See
    cache/hawker_cache.py.
    """
    from cache.hawker_cache import find_nearest_hawkers as cached_hawkers
    return cached_hawkers(lat, lng, top_n=3)


# ── Main function ─────────────────────────────────────────────────────────────

def get_nearby_info(address: str, lat: float | None = None, lng: float | None = None) -> dict:
    """Find nearby amenities (MRT, malls, schools, supermarkets, hawker centres
    and coffee shops) for a location.

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

    def _mrts():
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
        return mrt_results

    def _malls():
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
        return mall_results

    def _schools():
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
        return school_results

    def _supermarkets():
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
        return supermarket_results

    def _hawkers():
        # ── Hawker centres via NEA's register (no Places call) ───────────────────
        #
        # The one amenity with no _enrich_with_transit pass. Everything else here
        # can plausibly be reached by bus or train; a hawker centre is somewhere
        # you walk to, and the transit leg is a second Distance Matrix round trip
        # on the slowest endpoint in the app. Walking distance alone answers it.
        hawker_results = []
        hawkers = find_nearest_hawkers(lat, lng)
        if hawkers:
            dest_list = [{"lat": h["lat"], "lng": h["lng"]} for h in hawkers]
            distances = get_walking_distances_bulk(lat, lng, dest_list)
            for hawker, dist in zip(hawkers, distances):
                if dist:
                    hawker_results.append({
                        "name": hawker["name"],
                        "distance": dist["distance_text"],
                        "duration": dist["duration_text"],
                        "distance_m": dist["distance_m"],
                        "dest_lat": hawker["lat"],
                        "dest_lng": hawker["lng"],
                        "maps_link": build_google_maps_link(origin, hawker["lat"], hawker["lng"]),
                        "stalls": hawker["stalls"],
                        "dist": hawker["dist"],
                    })
        return hawker_results

    def _coffeeshops():
        # ── Coffee shops / kopitiams via Google Places ──────────────────────────
        #
        # No transit leg, for the same reason hawker centres have none: this is a
        # walk-downstairs amenity, and the pass is a second Distance Matrix round
        # trip on the slowest endpoint in the app.
        coffeeshop_results = []
        coffeeshops = find_nearest_coffeeshops(lat, lng)
        if coffeeshops:
            dest_list = [{"lat": c["lat"], "lng": c["lng"]} for c in coffeeshops]
            distances = get_walking_distances_bulk(lat, lng, dest_list)
            for shop, dist in zip(coffeeshops, distances):
                if dist:
                    coffeeshop_results.append({
                        "name": shop["name"],
                        "distance": dist["distance_text"],
                        "duration": dist["duration_text"],
                        "distance_m": dist["distance_m"],
                        "dest_lat": shop["lat"],
                        "dest_lng": shop["lng"],
                        "maps_link": build_google_maps_link(origin, shop["lat"], shop["lng"]),
                        "dist": shop["dist"],
                    })
        return coffeeshop_results

    # Six independent lookups, run concurrently.
    #
    # They share nothing but the origin coordinate and are only combined in the
    # return below, so running them in sequence made the endpoint's latency the
    # SUM of six chains rather than the longest one — 17 blocking round trips
    # to Google, ~3.5s warm. These are `requests` calls waiting on a socket, so
    # threads are the right tool and the GIL is not in the way.
    #
    # A category that raises loses itself and nothing else: the amenity list
    # comes back short rather than the whole request failing. (Each find_*
    # already swallows its own network errors; this is the backstop for
    # anything they don't.)
    jobs = {
        "mrts": _mrts, "malls": _malls, "schools": _schools,
        "supermarkets": _supermarkets, "hawkers": _hawkers,
        "coffeeshops": _coffeeshops,
    }
    found = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn): key for key, fn in jobs.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                found[key] = future.result()
            except Exception as e:
                print(f"[Maps] {key} lookup failed: {e}")
                found[key] = []

    return {"address": address, "lat": lat, "lng": lng, **found}
