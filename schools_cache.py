import os
import re
import time
import logging
import requests
from math import radians, sin, cos, sqrt, atan2
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi

load_dotenv()

logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")
ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"
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
        logger.error(f"[Schools Cache] MongoDB connection failed: {e}")
        return None


# ── Haversine ─────────────────────────────────────────────────────────────────
def haversine_m(lat1, lng1, lat2, lng2) -> float:
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi/2)**2 + cos(phi1)*cos(phi2)*sin(dlambda/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


# ── Cache read/write ──────────────────────────────────────────────────────────
def _is_cache_fresh() -> bool:
    db = _get_db()
    if db is None:
        return False
    try:
        doc = db['schools_cache'].find_one({"_id": "meta"})
        if not doc:
            return False
        age_days = (time.time() - doc.get("timestamp", 0)) / 86400
        return age_days < CACHE_MAX_AGE_DAYS
    except Exception:
        return False


def _load_cache() -> list:
    db = _get_db()
    if db is None:
        return []
    try:
        doc = db['schools_cache'].find_one({"_id": "data"})
        if not doc:
            return []
        return doc.get("schools", [])
    except Exception as e:
        logger.error(f"[Schools Cache] Load failed: {e}")
        return []


def _save_cache(schools: list):
    db = _get_db()
    if db is None:
        return
    try:
        db['schools_cache'].replace_one(
            {"_id": "meta"},
            {"_id": "meta", "timestamp": time.time(), "school_count": len(schools)},
            upsert=True
        )
        db['schools_cache'].replace_one(
            {"_id": "data"},
            {"_id": "data", "schools": schools},
            upsert=True
        )
        logger.info(f"[Schools Cache] Saved {len(schools)} schools to MongoDB")
    except Exception as e:
        logger.error(f"[Schools Cache] Save failed: {e}")


# ── OneMap school fetching ─────────────────────────────────────────────────────
def _fetch_all_schools(token: str) -> list:
    """
    Fetch all primary schools from OneMap by paginating through results.
    Deduplicates by school name.
    """
    all_schools = []
    seen = set()

    for page in range(1, 20):  # max 20 pages (~200 results — more than enough)
        try:
            r = requests.get(
                ONEMAP_SEARCH_URL,
                params={
                    "searchVal": "primary school",
                    "returnGeom": "Y",
                    "getAddrDetails": "Y",
                    "pageNum": page
                },
                headers={"Authorization": token},
                timeout=10
            )
            data = r.json()
            results = data.get("results", [])
            total_pages = int(data.get("totalNumPages", 1))

            for item in results:
                name = item.get("BUILDING", "") or item.get("SEARCHVAL", "")
                if not name:
                    continue

                name_upper = name.upper()

                # Must be a primary school
                if "PRIMARY" not in name_upper and "PRI SCH" not in name_upper:
                    continue

                # Deduplicate — normalise name by removing block/unit info
                base = re.sub(r'\s+(BLOCK|BLK)\s+\w+', '', name_upper).strip()
                base = re.sub(r'\s+#\S+', '', base).strip()
                if base in seen:
                    continue
                seen.add(base)

                try:
                    lat = float(item["LATITUDE"])
                    lng = float(item["LONGITUDE"])
                except (KeyError, ValueError):
                    continue

                # Clean up name for display
                display_name = name.title()
                display_name = re.sub(r'\bPri\b', 'Primary', display_name)

                all_schools.append({
                    "name": display_name,
                    "lat": lat,
                    "lng": lng,
                })

            logger.info(f"[Schools Cache] Page {page}/{total_pages}: {len(results)} results")

            if page >= total_pages:
                break

            time.sleep(0.2)  # be polite to OneMap

        except Exception as e:
            logger.error(f"[Schools Cache] Page {page} failed: {e}")
            break

    return all_schools


# ── Main public function ──────────────────────────────────────────────────────
def get_schools_cache() -> list:
    """Return full list of primary schools from cache, refreshing if stale."""
    if _is_cache_fresh():
        logger.info("[Schools Cache] Using cached data from MongoDB")
        return _load_cache()

    logger.info("[Schools Cache] Cache stale or missing — fetching from OneMap...")
    from onemap_mrt import get_token
    token = get_token()
    if not token:
        logger.warning("[Schools Cache] No OneMap token — using stale cache")
        return _load_cache()

    schools = _fetch_all_schools(token)
    if schools:
        _save_cache(schools)
        logger.info(f"[Schools Cache] Done — {len(schools)} schools cached")
    else:
        logger.warning("[Schools Cache] No schools fetched — using stale cache")
        return _load_cache()

    return schools


def find_nearest_primary_schools(origin_lat: float, origin_lng: float,
                                  radius_m: float = 1000, top_n: int = 3) -> list:
    """
    Find the nearest primary schools within radius_m of origin.
    Returns up to top_n schools sorted by straight-line distance.
    """
    schools = get_schools_cache()
    if not schools:
        return []

    nearby = []
    for school in schools:
        dist = haversine_m(origin_lat, origin_lng, school["lat"], school["lng"])
        if dist <= radius_m:
            nearby.append({**school, "dist": dist})

    nearby.sort(key=lambda x: x["dist"])
    return nearby[:top_n]
