import os
import time
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi

load_dotenv()

logger = logging.getLogger(__name__)

URA_API_KEY = os.getenv("URA_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
URA_TOKEN_URL = "https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1"
URA_TRANSACTIONS_BASE_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Transaction&batch="
URA_PIPELINE_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Pipeline"

CACHE_MAX_AGE_HOURS = 48

# ── MongoDB setup ─────────────────────────────────────────────────────────────
_db = None

def _get_db():
    global _db
    if _db is not None:
        return _db
    if not MONGO_URI:
        logger.warning("[URA Cache] No MONGO_URI set")
        return None
    try:
        client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
        _db = client['property_bot']
        return _db
    except Exception as e:
        logger.error(f"[URA Cache] MongoDB connection failed: {e}")
        return None


# ── URA API helpers ───────────────────────────────────────────────────────────

def _get_token() -> str | None:
    headers = {"AccessKey": URA_API_KEY, "User-Agent": "PropertyBot/1.0"}
    try:
        r = requests.get(URA_TOKEN_URL, headers=headers, timeout=10)
        data = r.json()
        if data.get("Status") == "Success":
            return data["Result"]
        logger.error(f"[URA Cache] Token error: {data}")
        return None
    except Exception as e:
        logger.error(f"[URA Cache] Token failed: {e}")
        return None


def _fetch_all_transactions(token: str) -> list:
    headers = {
        "AccessKey": URA_API_KEY,
        "Token": token,
        "User-Agent": "PropertyBot/1.0",
    }
    all_results = []
    for batch in range(1, 5):
        url = f"{URA_TRANSACTIONS_BASE_URL}{batch}"
        try:
            r = requests.get(url, headers=headers, timeout=30)
            data = r.json()
            if data.get("Status") == "Success":
                results = data.get("Result", [])
                all_results.extend(results)
                logger.info(f"[URA Cache] Batch {batch}: {len(results)} projects")
            else:
                logger.error(f"[URA Cache] Batch {batch} error: {data}")
        except Exception as e:
            logger.error(f"[URA Cache] Batch {batch} failed: {e}")
    return all_results


def _fetch_pipeline(token: str) -> list:
    headers = {
        "AccessKey": URA_API_KEY,
        "Token": token,
        "User-Agent": "PropertyBot/1.0",
    }
    try:
        r = requests.get(URA_PIPELINE_URL, headers=headers, timeout=15)
        data = r.json()
        if data.get("Status") == "Success":
            return data.get("Result", [])
        return []
    except Exception as e:
        logger.error(f"[URA Cache] Pipeline failed: {e}")
        return []


# ── Cache read/write ──────────────────────────────────────────────────────────

def _is_cache_fresh() -> bool:
    db = _get_db()
    if db is None:
        return False
    try:
        doc = db['ura_cache'].find_one({"_id": "meta"})
        if not doc:
            return False
        age_hours = (time.time() - doc.get("timestamp", 0)) / 3600
        return age_hours < CACHE_MAX_AGE_HOURS
    except Exception as e:
        logger.error(f"[URA Cache] Freshness check failed: {e}")
        return False


def _load_cache() -> tuple[list, list]:
    db = _get_db()
    if db is None:
        return [], []
    try:
        doc = db['ura_cache'].find_one({"_id": "data"})
        if not doc:
            return [], []
        return doc.get("transactions", []), doc.get("pipeline", [])
    except Exception as e:
        logger.error(f"[URA Cache] Load failed: {e}")
        return [], []


def _save_cache(transactions: list, pipeline: list):
    db = _get_db()
    if db is None:
        return
    try:
        # Store metadata separately to avoid huge document reads for freshness checks
        db['ura_cache'].replace_one(
            {"_id": "meta"},
            {"_id": "meta", "timestamp": time.time(), "project_count": len(transactions), "updated_at": datetime.now(timezone.utc)},
            upsert=True
        )
        db['ura_cache'].replace_one(
            {"_id": "data"},
            {"_id": "data", "transactions": transactions, "pipeline": pipeline},
            upsert=True
        )
        logger.info(f"[URA Cache] Saved {len(transactions)} projects to MongoDB")
    except Exception as e:
        logger.error(f"[URA Cache] Save failed: {e}")


# ── Public interface ──────────────────────────────────────────────────────────

def get_ura_data() -> tuple[list, list]:
    """
    Return (transactions, pipeline) from MongoDB cache.
    Refreshes automatically if cache is stale or missing.
    """
    if _is_cache_fresh():
        logger.info("[URA Cache] Using cached data")
        return _load_cache()

    logger.info("[URA Cache] Cache stale or missing — refreshing from URA API...")
    token = _get_token()
    if not token:
        logger.warning("[URA Cache] No token — falling back to stale cache")
        return _load_cache()

    transactions = _fetch_all_transactions(token)
    pipeline = _fetch_pipeline(token)

    if transactions:
        _save_cache(transactions, pipeline)

    return transactions, pipeline


def force_refresh() -> bool:
    """Force a cache refresh regardless of age. Returns True on success."""
    logger.info("[URA Cache] Force refreshing...")
    token = _get_token()
    if not token:
        return False
    transactions = _fetch_all_transactions(token)
    pipeline = _fetch_pipeline(token)
    if transactions:
        _save_cache(transactions, pipeline)
        return True
    return False


def cache_status() -> dict:
    """Return info about the current cache state."""
    db = _get_db()
    if db is None:
        return {"status": "no_db"}
    try:
        doc = db['ura_cache'].find_one({"_id": "meta"})
        if not doc:
            return {"status": "missing"}
        age_hours = (time.time() - doc.get("timestamp", 0)) / 3600
        return {
            "status": "fresh" if age_hours < CACHE_MAX_AGE_HOURS else "stale",
            "age_hours": round(age_hours, 1),
            "projects": doc.get("project_count", "?"),
            "size_mb": "N/A (MongoDB)",
        }
    except Exception:
        return {"status": "error"}
