import os
import re
import time
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi

load_dotenv()

logger = logging.getLogger(__name__)

URA_API_KEY = os.getenv("URA_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
URA_TOKEN_URL = "https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1"
URA_RENTAL_BASE_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Rental&refPeriod="

# Refresh monthly — rental data updates on 15th of each month
CACHE_MAX_AGE_HOURS = 24 * 30

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
        logger.error(f"[Rental Cache] MongoDB connection failed: {e}")
        return None


# ── Quarter helpers ───────────────────────────────────────────────────────────

def get_last_4_quarters() -> list[str]:
    """Return last 4 quarters in URA format e.g. ['26q2', '26q1', '25q4', '25q3']"""
    now = datetime.now()
    year = now.year
    quarter = (now.month - 1) // 3 + 1
    quarters = []
    for _ in range(4):
        quarters.append(f"{str(year)[2:]}q{quarter}")
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return quarters


# ── URA API helpers ───────────────────────────────────────────────────────────

def _get_token() -> str | None:
    headers = {"AccessKey": URA_API_KEY, "User-Agent": "PropertyBot/1.0"}
    try:
        r = requests.get(URA_TOKEN_URL, headers=headers, timeout=10)
        data = r.json()
        if data.get("Status") == "Success":
            return data["Result"]
        logger.error(f"[Rental Cache] Token error: {data}")
        return None
    except Exception as e:
        logger.error(f"[Rental Cache] Token failed: {e}")
        return None


def _fetch_rental_quarter(token: str, period: str) -> list:
    """Fetch rental contracts for a single quarter."""
    headers = {
        "AccessKey": URA_API_KEY,
        "Token": token,
        "User-Agent": "PropertyBot/1.0",
    }
    try:
        r = requests.get(
            f"{URA_RENTAL_BASE_URL}{period}",
            headers=headers,
            timeout=30
        )
        data = r.json()
        if data.get("Status") == "Success":
            results = data.get("Result", [])
            logger.info(f"[Rental Cache] {period}: {len(results)} projects")
            return results
        logger.error(f"[Rental Cache] {period} error: {data}")
        return []
    except Exception as e:
        logger.error(f"[Rental Cache] {period} failed: {e}")
        return []


def _fetch_all_rentals(token: str) -> list:
    """Fetch rental data for last 4 quarters and merge."""
    quarters = get_last_4_quarters()
    all_projects = {}  # project name -> merged rental list

    for quarter in quarters:
        results = _fetch_rental_quarter(token, quarter)
        for project in results:
            name = project.get("project", "").upper().strip()
            if not name:
                continue
            if name not in all_projects:
                all_projects[name] = {
                    "project": project.get("project", ""),
                    "street": project.get("street", ""),
                    "rental": [],
                }
            all_projects[name]["rental"].extend(project.get("rental", []))
        time.sleep(0.5)  # be polite between quarter fetches

    return list(all_projects.values())


# ── Cache read/write ──────────────────────────────────────────────────────────

def _is_cache_fresh() -> bool:
    db = _get_db()
    if db is None:
        return False
    try:
        doc = db['rental_cache'].find_one({"_id": "meta"})
        if not doc:
            return False
        age_hours = (time.time() - doc.get("timestamp", 0)) / 3600
        return age_hours < CACHE_MAX_AGE_HOURS
    except Exception:
        return False


def _load_cache() -> list:
    db = _get_db()
    if db is None:
        return []
    try:
        # Load from chunks (rental data can also be large)
        projects = []
        chunk = 0
        while True:
            doc = db['rental_cache'].find_one({"_id": f"chunk_{chunk}"})
            if not doc:
                break
            projects.extend(doc.get("projects", []))
            chunk += 1
        return projects
    except Exception as e:
        logger.error(f"[Rental Cache] Load failed: {e}")
        return []


def _save_cache(projects: list):
    db = _get_db()
    if db is None:
        return
    try:
        CHUNK_SIZE = 300
        # Clear old chunks
        db['rental_cache'].delete_many({"_id": {"$regex": "^chunk_"}})

        chunks = [projects[i:i+CHUNK_SIZE] for i in range(0, len(projects), CHUNK_SIZE)]
        for i, chunk in enumerate(chunks):
            db['rental_cache'].replace_one(
                {"_id": f"chunk_{i}"},
                {"_id": f"chunk_{i}", "projects": chunk},
                upsert=True
            )

        db['rental_cache'].replace_one(
            {"_id": "meta"},
            {
                "_id": "meta",
                "timestamp": time.time(),
                "project_count": len(projects),
                "chunk_count": len(chunks),
                "quarters": get_last_4_quarters(),
            },
            upsert=True
        )
        logger.info(f"[Rental Cache] Saved {len(projects)} projects in {len(chunks)} chunks")
    except Exception as e:
        logger.error(f"[Rental Cache] Save failed: {e}")


# ── Public interface ──────────────────────────────────────────────────────────

def get_rental_data() -> list:
    """Return rental data from cache, refreshing if stale."""
    if _is_cache_fresh():
        logger.info("[Rental Cache] Using cached data")
        return _load_cache()

    logger.info("[Rental Cache] Refreshing rental data...")
    token = _get_token()
    if not token:
        logger.warning("[Rental Cache] No token — using stale cache")
        return _load_cache()

    projects = _fetch_all_rentals(token)
    if projects:
        _save_cache(projects)

    return projects


def force_refresh_rental() -> bool:
    """Force refresh regardless of cache age."""
    token = _get_token()
    if not token:
        return False
    projects = _fetch_all_rentals(token)
    if projects:
        _save_cache(projects)
        return True
    return False


def rental_cache_status() -> dict:
    db = _get_db()
    if db is None:
        return {"status": "no_db"}
    try:
        doc = db['rental_cache'].find_one({"_id": "meta"})
        if not doc:
            return {"status": "missing"}
        age_hours = (time.time() - doc.get("timestamp", 0)) / 3600
        return {
            "status": "fresh" if age_hours < CACHE_MAX_AGE_HOURS else "stale",
            "age_hours": round(age_hours, 1),
            "projects": doc.get("project_count", "?"),
            "quarters": doc.get("quarters", []),
        }
    except Exception:
        return {"status": "error"}
