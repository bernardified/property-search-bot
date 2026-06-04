import os
import re
import time
import logging
import requests
from math import radians, sin, cos, sqrt, atan2
from dotenv import load_dotenv
from pymongo import MongoClient
from mrt_data import MRT_LINES
from pymongo.server_api import ServerApi

load_dotenv()

logger = logging.getLogger(__name__)

ONEMAP_EMAIL = os.getenv("ONEMAP_EMAIL")
ONEMAP_PASSWORD = os.getenv("ONEMAP_PASSWORD")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")

ONEMAP_AUTH_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"
ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"
DISTANCE_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

CACHE_MAX_AGE_DAYS = 30

# ── MongoDB setup ─────────────────────────────────────────────────────────────
_db = None

def _get_db():
    global _db
    if _db is not None:
        return _db
    if not MONGO_URI:
        return None
    try:
        client = MongoClient(
            MONGO_URI,
            server_api=ServerApi('1'),
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
        )
        _db = client['property_bot']
        return _db
    except Exception as e:
        logger.error(f"[MRT Cache] MongoDB connection failed: {e}")
        return None


# ── OneMap token ──────────────────────────────────────────────────────────────
_token = None
_token_expiry = 0

def get_token() -> str | None:
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 300:
        return _token
    if not ONEMAP_EMAIL or not ONEMAP_PASSWORD:
        return None
    try:
        r = requests.post(
            ONEMAP_AUTH_URL,
            json={"email": ONEMAP_EMAIL, "password": ONEMAP_PASSWORD},
            timeout=10
        )
        data = r.json()
        if "access_token" in data:
            _token = data["access_token"]
            _token_expiry = int(data.get("expiry_timestamp", time.time() + 28800))
            return _token
        return None
    except Exception as e:
        logger.error(f"[OneMap] Auth error: {e}")
        return None


# ── Haversine ─────────────────────────────────────────────────────────────────
def haversine_m(lat1, lng1, lat2, lng2) -> float:
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi/2)**2 + cos(phi1)*cos(phi2)*sin(dlambda/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


# ── MongoDB cache read/write ───────────────────────────────────────────────────

def _is_cache_fresh() -> bool:
    db = _get_db()
    if db is None:
        return False
    try:
        doc = db['mrt_cache'].find_one({"_id": "meta"})
        if not doc:
            return False
        age_days = (time.time() - doc.get("timestamp", 0)) / 86400
        return age_days < CACHE_MAX_AGE_DAYS
    except Exception:
        return False


def _load_cache() -> dict:
    db = _get_db()
    if db is None:
        return {}
    try:
        doc = db['mrt_cache'].find_one({"_id": "data"})
        if not doc:
            return {}
        return doc.get("stations", {})
    except Exception as e:
        logger.error(f"[MRT Cache] Load failed: {e}")
        return {}


def _save_cache(stations: dict):
    db = _get_db()
    if db is None:
        return
    try:
        db['mrt_cache'].replace_one(
            {"_id": "meta"},
            {"_id": "meta", "timestamp": time.time(), "station_count": len(stations)},
            upsert=True
        )
        db['mrt_cache'].replace_one(
            {"_id": "data"},
            {"_id": "data", "stations": stations},
            upsert=True
        )
        logger.info(f"[MRT Cache] Saved {len(stations)} stations to MongoDB")
    except Exception as e:
        logger.error(f"[MRT Cache] Save failed: {e}")


# ── OneMap station fetching ───────────────────────────────────────────────────

def _fetch_station_data(station_name: str, token: str) -> dict | None:
    query = f"{station_name} MRT Station"
    try:
        r = requests.get(
            ONEMAP_SEARCH_URL,
            params={
                "searchVal": query,
                "returnGeom": "Y",
                "getAddrDetails": "N",
                "pageNum": 1
            },
            headers={"Authorization": token},
            timeout=10
        )
        results = r.json().get("results", [])
    except Exception as e:
        logger.error(f"[OneMap] Fetch failed for {station_name}: {e}")
        return None

    station_coords = None
    exits = []

    for item in results:
        name = item.get("SEARCHVAL", "").upper()
        try:
            lat = float(item["LATITUDE"])
            lng = float(item["LONGITUDE"])
        except (KeyError, ValueError):
            continue

        exit_match = re.search(r'EXIT\s+([A-Z0-9]+)', name)
        if exit_match:
            exits.append({
                "letter": exit_match.group(1),
                "lat": lat,
                "lng": lng,
            })
        elif "MRT STATION" in name and "EXIT" not in name:
            if station_coords is None:
                station_coords = {"lat": lat, "lng": lng}

    if not station_coords and not exits:
        return None
    if not station_coords and exits:
        station_coords = {"lat": exits[0]["lat"], "lng": exits[0]["lng"]}

    return {
        "name": station_name,
        "lat": station_coords["lat"],
        "lng": station_coords["lng"],
        "exits": exits,
    }

def build_mrt_cache() -> dict:
    """Build or load the full MRT station coordinate cache from MongoDB."""
    if _is_cache_fresh():
        logger.info("[MRT Cache] Using cached data from MongoDB")
        return _load_cache()

    logger.info("[MRT Cache] Building MRT cache from OneMap...")
    token = get_token()
    if not token:
        logger.warning("[MRT Cache] No token — using stale cache")
        return _load_cache()

    ALL_MRT_STATIONS = set()
    for stations in MRT_LINES.values():
        ALL_MRT_STATIONS.update(stations)

    stations = {}
    for station_name in ALL_MRT_STATIONS:
        data = _fetch_station_data(station_name, token)
        if data:
            stations[station_name.upper()] = data
            logger.info(f"[MRT Cache] Cached {station_name}")
        time.sleep(0.15)

    if stations:
        _save_cache(stations)
    logger.info(f"[MRT Cache] Done — {len(stations)} stations cached")
    return stations


# ── Walking distance for exit precision ──────────────────────────────────────

def get_best_exit_by_walking(origin_lat, origin_lng, exits) -> dict | None:
    if not exits:
        return None
    if len(exits) == 1:
        return exits[0]

    dest_str = "|".join([f"{ex['lat']},{ex['lng']}" for ex in exits])
    params = {
        "origins": f"{origin_lat},{origin_lng}",
        "destinations": dest_str,
        "mode": "walking",
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        r = requests.get(DISTANCE_URL, params=params, timeout=10)
        data = r.json()
        if data["status"] != "OK":
            return exits[0]
        elements = data["rows"][0]["elements"]
        best_duration = float("inf")
        best_exits = []
        for i, el in enumerate(elements):
            if el["status"] == "OK":
                duration = el["duration"]["value"]
                if duration < best_duration:
                    best_duration = duration
                    best_exits = [exits[i]]
                elif duration == best_duration:
                    best_exits.append(exits[i])
        if not best_exits:
            return exits[0]
        if len(best_exits) == 1:
            return best_exits[0]
        combined_letter = "/".join(ex["letter"] for ex in best_exits)
        return {"letter": combined_letter, "lat": best_exits[0]["lat"], "lng": best_exits[0]["lng"]}
    except Exception as e:
        logger.error(f"[MRT Exit] Distance call failed: {e}")
        return exits[0]


# ── Main public function ──────────────────────────────────────────────────────

def find_nearest_mrts(origin_lat: float, origin_lng: float, top_n: int = 3, radius_m: float = 2500) -> list:
    stations = build_mrt_cache()
    if not stations:
        return []

    candidates = []
    for key, station in stations.items():
        exits = station.get("exits", [])
        if exits:
            nearest_straight = min(
                haversine_m(origin_lat, origin_lng, ex["lat"], ex["lng"])
                for ex in exits
            )
            if nearest_straight <= radius_m:
                candidates.append((nearest_straight, station))
        else:
            station_dist = haversine_m(origin_lat, origin_lng, station["lat"], station["lng"])
            if station_dist <= radius_m:
                candidates.append((station_dist, station))

    candidates.sort(key=lambda x: x[0])
    top_candidates = candidates[:max(top_n * 2, 6)]

    results = []
    for straight_dist, station in top_candidates:
        exits = station.get("exits", [])
        if exits:
            best_exit = get_best_exit_by_walking(origin_lat, origin_lng, exits)
            if best_exit:
                exit_label = f" (Exit {best_exit['letter']})"
                dest_lat = best_exit["lat"]
                dest_lng = best_exit["lng"]
            else:
                exit_label = ""
                dest_lat = station["lat"]
                dest_lng = station["lng"]
        else:
            exit_label = ""
            dest_lat = station["lat"]
            dest_lng = station["lng"]

        results.append({
            "name": station["name"],
            "exit_label": exit_label,
            "dest_lat": dest_lat,
            "dest_lng": dest_lng,
            "straight_dist": straight_dist,
        })

    results.sort(key=lambda x: x["straight_dist"])
    return results[:top_n]
