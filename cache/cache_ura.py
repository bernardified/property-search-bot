import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from utils import get_mongo_db, is_ura_transactions_stale
from cache.unit_counts import harvest_pipeline_counts

load_dotenv()

logger = logging.getLogger(__name__)

URA_API_KEY = os.getenv("URA_API_KEY")
URA_TOKEN_URL = "https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1"
URA_TRANSACTIONS_BASE_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Transaction&batch="
URA_PIPELINE_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Pipeline"

# URA publishes new transaction data every Tuesday and Friday at 09:00 SGT.
# Staleness is determined by release calendar, not a fixed age threshold.
# See utils.is_ura_transactions_stale() for logic.

# MongoDB via utils.get_mongo_db()


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

def _meta_timestamp() -> float | None:
    """Last-refresh timestamp from the meta doc, or None if unavailable."""
    db = get_mongo_db()
    if db is None:
        return None
    try:
        doc = db['ura_cache'].find_one({"_id": "meta"}, {"timestamp": 1})
        return doc.get("timestamp", 0) if doc else None
    except Exception as e:
        logger.error(f"[URA Cache] Freshness check failed: {e}")
        return None


def _is_cache_fresh() -> bool:
    """
    Return True if the cache is up-to-date — i.e. no URA transaction release
    (Tue/Fri 09:00 SGT, shifted for public holidays) has occurred since last refresh.
    """
    ts = _meta_timestamp()
    return ts is not None and not is_ura_transactions_stale(ts)


# In-process memo of the loaded cache, keyed by the meta timestamp. Loading
# ~100 Mongo chunks per call dominated search latency (~15–20s from Railway);
# a memo hit costs one small meta read instead. Any refresh — here, the cron
# job, or /refresh from another process — writes a new timestamp, which
# invalidates the memo on the next call. Memoized data is shared across
# callers: treat it as READ-ONLY (consumers already do).
_memo_lock = threading.Lock()
_memo_ts: float | None = None
_memo_data: tuple[list, list] | None = None


def _load_cache() -> tuple[list, list]:
    """Load transactions from chunked documents + pipeline from single doc."""
    db = get_mongo_db()
    if db is None:
        return [], []
    try:
        # Load transactions from chunks
        transactions = []
        chunk = 0
        while True:
            doc = db['ura_cache'].find_one({"_id": f"data_chunk_{chunk}"})
            if not doc:
                break
            transactions.extend(doc.get("transactions", []))
            chunk += 1

        # Load pipeline
        pipeline_doc = db['ura_cache'].find_one({"_id": "pipeline"})
        pipeline = pipeline_doc.get("pipeline", []) if pipeline_doc else []

        return transactions, pipeline
    except Exception as e:
        logger.error(f"[URA Cache] Load failed: {e}")
        return [], []


def _save_cache(transactions: list, pipeline: list):
    db = get_mongo_db()
    if db is None:
        return
    try:
        # Kept at 100 to prevent catastrophic 16MB BSON limit crashes
        CHUNK_SIZE = 100 
        current_time = time.time()

        # Wipe old chunks before inserting new ones to prevent orphaned data
        db['ura_cache'].delete_many({"_id": {"$regex": "^data_chunk_"}})

        chunks = [transactions[i:i+CHUNK_SIZE] for i in range(0, len(transactions), CHUNK_SIZE)]
        for i, chunk in enumerate(chunks):
            db['ura_cache'].replace_one(
                {"_id": f"data_chunk_{i}"},
                {
                    "_id": f"data_chunk_{i}", 
                    "transactions": chunk,
                    "updated_at": current_time 
                },
                upsert=True
            )
        logger.info(f"[URA Cache] Saved {len(transactions)} projects in {len(chunks)} chunks")

        db['ura_cache'].replace_one(
            {"_id": "pipeline"},
            {
                "_id": "pipeline",
                "pipeline": pipeline,
                "updated_at": current_time
            },
            upsert=True
        )

        # Pipeline snapshots are transient — a project's totalUnits vanishes
        # from the feed once it TOPs. Persist every count into the permanent
        # unit_counts store so the liquidity feature keeps a denominator for
        # completed developments.
        harvest_pipeline_counts(pipeline)

        db['ura_cache'].replace_one(
            {"_id": "meta"},
            {
                "_id": "meta",
                "timestamp": current_time,
                "project_count": len(transactions),
                "chunk_count": len(chunks)
            },
            upsert=True
        )
        logger.info(f"[URA Cache] Metadata saved — cache complete")
    except Exception as e:
        logger.error(f"[URA Cache] Save failed: {e}")


# ── Public interface ──────────────────────────────────────────────────────────

def get_ura_data() -> tuple[list, list]:
    """
    Return (transactions, pipeline) from MongoDB cache.
    Refreshes automatically if cache is stale or missing.
    Memoized in-process per meta timestamp — see _memo_* above.
    """
    global _memo_ts, _memo_data

    ts = _meta_timestamp()
    if ts is not None and not is_ura_transactions_stale(ts):
        with _memo_lock:
            if _memo_ts == ts and _memo_data is not None:
                logger.info("[URA Cache] Using in-process memo")
                return _memo_data
        logger.info("[URA Cache] Using cached data")
        data = _load_cache()
        if data[0]:
            with _memo_lock:
                _memo_ts, _memo_data = ts, data
        return data

    logger.info("[URA Cache] Cache stale or missing — refreshing from URA API...")
    token = _get_token()
    if not token:
        logger.warning("[URA Cache] No token — falling back to stale cache")
        return _load_cache()

    transactions = _fetch_all_transactions(token)
    pipeline = _fetch_pipeline(token)

    if transactions:
        _save_cache(transactions, pipeline)
        data = (transactions, pipeline)
        with _memo_lock:
            _memo_ts, _memo_data = _meta_timestamp(), data
        return data

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
    db = get_mongo_db()
    if db is None:
        return {"status": "no_db"}
    try:
        doc = db['ura_cache'].find_one({"_id": "meta"})
        if not doc:
            return {"status": "missing"}
        last_refresh_ts = doc.get("timestamp", 0)
        age_hours = (time.time() - last_refresh_ts) / 3600
        stale = is_ura_transactions_stale(last_refresh_ts)
        return {
            "status": "stale" if stale else "fresh",
            "age_hours": round(age_hours, 1),
            "projects": doc.get("project_count", "?"),
            "size_mb": "N/A (MongoDB)",
        }
    except Exception:
        return {"status": "error"}
