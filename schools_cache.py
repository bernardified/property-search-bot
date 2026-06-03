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
    
def _get_schools_config(db) -> dict:
    """Fetch legacy school names and junk filters from MongoDB, or seed if missing."""
    default_config = {
        "_id": "schools_config",
        "legacy_names": [
            "ROSYTH SCHOOL", "CATHOLIC HIGH SCHOOL", "CHIJ ST. NICHOLAS GIRLS' SCHOOL",
            "MAHA BODHI SCHOOL", "RED SWASTIKA SCHOOL", "TAO NAN SCHOOL", 
            "MEE TOH SCHOOL", "KONG HWA SCHOOL", "MARIS STELLA HIGH SCHOOL", 
            "METHODIST GIRLS' SCHOOL", "SINGAPORE CHINESE GIRLS' SCHOOL", 
            "ST. JOSEPH'S INSTITUTION JUNIOR", "ST. STEPHEN'S SCHOOL", 
            "MARYMOUNT CONVENT SCHOOL", "CANOSSA CATHOLIC PRIMARY SCHOOL", 
            "DE LA SALLE SCHOOL", "MONTFORT JUNIOR SCHOOL", "CHONGFU SCHOOL",
            "AI TONG SCHOOL", "POI CHING SCHOOL", "HONG WEN SCHOOL", 
            "PEI CHUN PUBLIC SCHOOL", "ANGLO-CHINESE SCHOOL (JUNIOR)"
        ],
        "junk_words": ["@", "CARE", "NASCANS", "COMMIT", "FORMER", "YMCA", "AFTER SCHOOL", "ACE", "MORNING STAR"]
    }

    if db is None:
        return default_config

    try:
        config_coll = db['app_config']
        config = config_coll.find_one({"_id": "schools_config"})
        
        if not config:
            config_coll.insert_one(default_config)
            logger.info("[Schools Cache] Seeded default schools config to MongoDB.")
            return default_config
            
        return config
    except Exception as e:
        logger.error(f"[Schools Cache] Failed to load config from DB: {e}")
        return default_config


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
    """Fetch primary schools via keyword and append legacy/non-standard schools."""
    schools = []
    seen = set()

    # 1. Bulk Search: Catch all standard "... Primary School" names
    for page in range(1, 10):
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
            if not data.get("results"):
                break
            
            for item in data["results"]:
                name = item.get("SEARCHVAL", "")
                if name in seen:
                    continue
                seen.add(name)
                schools.append({
                    "name": name.title(),
                    "lat": float(item["LATITUDE"]),
                    "lng": float(item["LONGITUDE"])
                })
        except Exception as e:
            logger.error(f"[Schools Cache] Bulk fetch failed on page {page}: {e}")
            break

    # 2. The Legacy Exceptions: Fetched dynamically from MongoDB app_config
    db = _get_db()
    config = _get_schools_config(db)
    
    exceptions = config.get("legacy_names", [])
    junk_words = config.get("junk_words", [])

    for school_name in exceptions:
        try:
            r = requests.get(
                ONEMAP_SEARCH_URL,
                params={
                    "searchVal": school_name,
                    "returnGeom": "Y",
                    "getAddrDetails": "Y",
                    "pageNum": 1
                },
                headers={"Authorization": token},
                timeout=10
            )
            data = r.json()
            results = data.get("results", [])
            
            if results:
                valid_results = []
                
                # Filter out the junk using the dynamic MongoDB list
                for item in results:
                    name_upper = item.get("SEARCHVAL", "").upper()
                    if any(junk in name_upper for junk in junk_words):
                        continue
                    valid_results.append(item)

                if valid_results:
                    best_match = min(valid_results, key=lambda x: len(x.get("SEARCHVAL", "")))
                    final_name = best_match.get("SEARCHVAL", "").title()
                    
                    if not any(school_name.lower() in s['name'].lower() for s in schools):
                        schools.append({
                            "name": final_name,
                            "lat": float(best_match["LATITUDE"]),
                            "lng": float(best_match["LONGITUDE"])
                        })
                        print(f"✅ Successfully injected legacy school: {final_name}")
        except Exception as e:
            logger.error(f"[Schools Cache] Failed to fetch exception {school_name}: {e}")

    return schools


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


def find_nearest_primary_schools(origin_lat: float, origin_lng: float, top_n: int = 5) -> list:
    """
    Find nearest primary schools. 
    Uses a 1200m radius to account for center-to-boundary polygon offsets.
    """
    schools = get_schools_cache()
    if not schools:
        return []

    # Buffer radius to catch schools where the boundary is <1km but center is >1km
    MAX_RADIUS_M = 1200 
    
    nearby = []
    for school in schools:
        dist = haversine_m(origin_lat, origin_lng, school["lat"], school["lng"])
        if dist <= MAX_RADIUS_M:
            nearby.append({**school, "dist": dist})
            
    # Sort by straight-line distance
    nearby.sort(key=lambda x: x["dist"])
    return nearby[:top_n]