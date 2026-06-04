import os
import re
import time
import logging
import requests
from dotenv import load_dotenv
from utils import get_mongo_db, haversine_m, get_onemap_token

load_dotenv()

logger = logging.getLogger(__name__)

ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"
CACHE_MAX_AGE_DAYS = 30


# MongoDB via utils.get_mongo_db()
    
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
            "PEI CHUN PUBLIC SCHOOL", "ANGLO-CHINESE SCHOOL (JUNIOR)",
            "HAIG GIRLS SCHOOL", "CHIJ KATONG PRIMARY", "NGEE ANN PRIMARY SCHOOL",
            "OPERA ESTATE PRIMARY SCHOOL", "FAIRFIELD METHODIST SCHOOL",
            "QUEENSTOWN PRIMARY SCHOOL", "RADIN MAS PRIMARY SCHOOL",
            "ZHANGDE PRIMARY SCHOOL", "CEDAR PRIMARY SCHOOL",
            "EAST VIEW PRIMARY SCHOOL", "ELIAS PARK PRIMARY SCHOOL",
            "FERN GREEN PRIMARY SCHOOL", "FUHUA PRIMARY SCHOOL",
            "LIANHUA PRIMARY SCHOOL", "NAN HUA PRIMARY SCHOOL",
            "NORTHOAKS PRIMARY SCHOOL", "PEIYING PRIMARY SCHOOL",
            "RIVER VALLEY PRIMARY SCHOOL", "RULANG PRIMARY SCHOOL",
            "SWISS COTTAGE PRIMARY SCHOOL", "WESTWOOD PRIMARY SCHOOL",
            "WHITE SANDS PRIMARY SCHOOL", "WOODGROVE PRIMARY SCHOOL",
            "XINGHUA PRIMARY SCHOOL", "YISHUN PRIMARY SCHOOL"
        ],
        "junk_words": [
            "@", "CARE", "NASCANS", "COMMIT", "FORMER", "YMCA",
            "AFTER SCHOOL", "AFTERSCHOOL", "ACE", "MORNING STAR",
            "PTE", "LTD", "KINDERGARTEN", "CHILDCARE", "STUDENT CARE",
            "ENRICHMENT", "TUITION",
        ]
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


# haversine_m via utils.haversine_m


# ── Cache read/write ──────────────────────────────────────────────────────────
def _is_cache_fresh() -> bool:
    db = get_mongo_db()
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
    db = get_mongo_db()
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
    db = get_mongo_db()
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
def _normalise_school_name(name: str) -> str:
    """Normalise school name for deduplication — strip block/unit info."""
    name = re.sub(r'\s+(BLOCK|BLK)\s+\w+', '', name.upper()).strip()
    name = re.sub(r'\s+#\S+', '', name).strip()
    return name


def _is_valid_primary_school(name: str, junk_words: list) -> bool:
    """
    Return True only if this is clearly a primary school entry.

    Junk check runs FIRST — a name containing both 'PRIMARY SCHOOL' and a
    junk word (e.g. 'Amp-Mercu Student Care @ Tampines Primary School')
    must be rejected before the 'PRIMARY SCHOOL' check can accept it.
    """
    name_upper = name.upper()
    if any(j in name_upper for j in junk_words):
        return False
    if "PRIMARY SCHOOL" in name_upper or "PRI SCH" in name_upper:
        return True
    return False


def _fetch_all_schools(token: str) -> list:
    """Fetch primary schools via keyword and append legacy/non-standard schools."""
    schools = []
    seen = set()  # normalised names for deduplication

    # Fetch config once — junk_words used by BOTH bulk and legacy paths
    db = get_mongo_db()
    config = _get_schools_config(db)
    junk_words = config.get("junk_words", [])
    exceptions = config.get("legacy_names", [])

    # 1. Bulk Search: paginate fully through all pages
    page = 1
    while True:
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

            if not results:
                break

            for item in results:
                name = item.get("SEARCHVAL", "")
                if not name:
                    continue
                if not _is_valid_primary_school(name, junk_words):
                    continue
                norm = _normalise_school_name(name)
                if norm in seen:
                    continue
                seen.add(norm)
                try:
                    schools.append({
                        "name": name.title(),
                        "lat": float(item["LATITUDE"]),
                        "lng": float(item["LONGITUDE"])
                    })
                except (KeyError, ValueError):
                    continue

            logger.info(f"[Schools Cache] Bulk page {page}/{total_pages}: {len(results)} results, {len(schools)} schools so far")

            if page >= total_pages:
                break
            page += 1
            time.sleep(0.2)

        except Exception as e:
            logger.error(f"[Schools Cache] Bulk fetch failed on page {page}: {e}")
            break

    # 2. Legacy exceptions: schools without "PRIMARY SCHOOL" in their OneMap name
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
                valid_results = [
                    item for item in results
                    if not any(j in item.get("SEARCHVAL", "").upper() for j in junk_words)
                ]

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
    token = get_onemap_token()
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
    Find nearest primary schools within 2km.
    Uses 2000m radius matching what property portals advertise.
    """
    schools = get_schools_cache()
    if not schools:
        return []

    MAX_RADIUS_M = 2000 
    
    nearby = []
    for school in schools:
        dist = haversine_m(origin_lat, origin_lng, school["lat"], school["lng"])
        if dist <= MAX_RADIUS_M:
            nearby.append({**school, "dist": dist})
            
    nearby.sort(key=lambda x: x["dist"])
    return nearby[:top_n]
