import os
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from utils import get_mongo_db, is_ura_transactions_stale, parse_mmyy_date
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

# The freshness key is read on the way into every cached read below, so the
# lean search path asked for it three times per request — three round trips to
# Atlas to check a number that changes twice a week. A few seconds of memo
# collapses them into one and costs at most that long to notice a refresh,
# which is nothing against a Tue/Fri release. Same trick as hawker_cache's
# MEMO_TTL_S; deliberately short, since this is what invalidates everything.
META_TTL_S = 5
_meta_memo: dict = {"ts": None, "at": 0.0}


def _meta_timestamp() -> float | None:
    """Last-refresh timestamp from the meta doc, or None if unavailable."""
    now = time.time()
    if _meta_memo["ts"] is not None and now - _meta_memo["at"] < META_TTL_S:
        return _meta_memo["ts"]
    db = get_mongo_db()
    if db is None:
        return None
    try:
        doc = db['ura_cache'].find_one({"_id": "meta"}, {"timestamp": 1})
        ts = doc.get("timestamp", 0) if doc else None
    except Exception as e:
        logger.error(f"[URA Cache] Freshness check failed: {e}")
        return None
    if ts is not None:
        _meta_memo.update(ts=ts, at=now)
    return ts


def meta_timestamp() -> float | None:
    """Public view of `_meta_timestamp` — the freshness key derived caches key
    themselves on (see cache/explore_cache.py). One small projected read."""
    return _meta_timestamp()


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
        # One cursor, not one find_one per chunk: 39 sequential round trips
        # cost ~2.4s more than a single batched read of the same ~31MB.
        # Chunks come back unordered, so sort by the index in the _id --
        # consumers do not depend on project order, but a stable order keeps
        # derived payloads identical between rebuilds.
        docs = sorted(
            db['ura_cache'].find({"_id": {"$regex": r"^data_chunk_\d+$"}},
                                 batch_size=200),
            key=lambda d: int(d["_id"].rsplit("_", 1)[1]),
        )
        transactions = [t for d in docs for t in d.get("transactions", [])]

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

        # The oldest contract date in the whole feed: the start of URA's
        # rolling window, and the trust anchor liquidity needs to decide
        # whether a launch sits far enough inside it to derive unit counts
        # from. Free here (this process is holding every transaction) and it
        # saves every later reader a scan of all of them.
        oldest = _oldest_contract_date(transactions)
        db['ura_cache'].replace_one(
            {"_id": "meta"},
            {
                "_id": "meta",
                "timestamp": current_time,
                "project_count": len(transactions),
                "chunk_count": len(chunks),
                "oldest_contract_date": oldest.isoformat() if oldest else None,
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


# ── Lean reads: the index, a project, the pipeline ──────────────────────────
#
# A name search wants two things out of this cache: every project's NAME, to
# match the query against, and then ONE project's transactions. Getting that
# from get_ura_data() means all 39 chunks — ~31MB over the wire and ~240MB of
# Python objects — which a 512MB container cannot do at all: on Railway the
# webapp's /api/property stopped answering entirely (>300s) while every
# endpoint that reads something small stayed instant.
#
# So the name list is read with a PROJECTION, leaving the transactions in
# Mongo (~250KB instead of 31MB), and every row carries the chunk and offset
# it sits at so fetching the winner is one document. A search costs ~1MB.
#
# Positions, not names: a name is not unique here (a multi-block development
# repeats it, and so does a "(DEMOLISHED)" pair), so re-matching by name on
# the second read would be guesswork about which row won the first.
#
# Every reader below returns None/[] on a stale or missing cache, which sends
# the caller back to get_ura_data(). That matters: get_ura_data() is what
# *refreshes* a stale cache, and these reads deliberately cannot.

_index_lock = threading.Lock()
_index_ts: float | None = None
_index_rows: list | None = None

_pipeline_lock = threading.Lock()
_pipeline_ts: float | None = None
_pipeline_data: list | None = None

_oldest_lock = threading.Lock()
_oldest_ts: float | None = None
_oldest_date = None


def _read_project_index() -> list:
    """The projected name list. Chunk number comes from the document `_id` and
    the offset from the array position, so this assumes nothing about how many
    projects a chunk holds."""
    db = get_mongo_db()
    if db is None:
        return []
    try:
        docs = db['ura_cache'].find(
            {"_id": {"$regex": r"^data_chunk_\d+$"}},
            {"transactions.project": 1, "transactions.street": 1,
             "transactions.x": 1, "transactions.y": 1},
            batch_size=200,
        )
        rows = []
        for doc in sorted(docs, key=lambda d: int(d["_id"].rsplit("_", 1)[1])):
            chunk = int(doc["_id"].rsplit("_", 1)[1])
            for offset, proj in enumerate(doc.get("transactions", [])):
                rows.append({
                    "project": proj.get("project", ""),
                    "street": proj.get("street", ""),
                    "x": proj.get("x"),
                    "y": proj.get("y"),
                    "chunk": chunk,
                    "offset": offset,
                })
        return rows
    except Exception as e:
        logger.error(f"[URA Cache] Index read failed: {e}")
        return []


def get_project_index() -> list | None:
    """`[{project, street, x, y, chunk, offset}]` for every cached project, or
    None when the cache is stale, missing or empty — in which case the caller
    must fall back to get_ura_data().

    Memoized on the meta timestamp, like get_ura_data's own memo, so a refresh
    from any process invalidates it. Treat the rows as READ-ONLY.
    """
    global _index_ts, _index_rows
    ts = _meta_timestamp()
    if ts is None or is_ura_transactions_stale(ts):
        return None
    with _index_lock:
        if _index_ts == ts and _index_rows is not None:
            return _index_rows
    rows = _read_project_index()
    if not rows:
        return None
    with _index_lock:
        _index_ts, _index_rows = ts, rows
    return rows


def get_projects_at(refs: list) -> list:
    """Full project dicts (transactions included) for index rows, one read per
    distinct chunk. Order follows `refs`; a row whose chunk has since been
    rewritten is skipped rather than guessed at."""
    db = get_mongo_db()
    if db is None or not refs:
        return []
    try:
        ids = {f"data_chunk_{r['chunk']}" for r in refs}
        chunks = {}
        for doc in db['ura_cache'].find({"_id": {"$in": sorted(ids)}}):
            chunks[int(doc["_id"].rsplit("_", 1)[1])] = doc.get("transactions", [])
        out = []
        for r in refs:
            arr = chunks.get(r["chunk"], [])
            if r["offset"] < len(arr):
                out.append(arr[r["offset"]])
        return out
    except Exception as e:
        logger.error(f"[URA Cache] Chunk read failed: {e}")
        return []


def _oldest_contract_date(projects: list):
    """Oldest MMYY contract date across project dicts, or None."""
    oldest = None
    for proj in projects or []:
        for txn in proj.get("transaction", []):
            dt = parse_mmyy_date(txn.get("contractDate", ""))
            if dt and (oldest is None or dt < oldest):
                oldest = dt
    return oldest


def oldest_contract_date():
    """Start of URA's rolling transaction window, as a datetime, or None.

    Read from the meta document, which `_save_cache` fills in. A cache written
    before that field existed falls back to a projection over just the contract
    dates — ~1s and a fraction of the memory, where scanning the loaded cache
    for it costs the full ~31MB/240MB load. Memoized on the meta timestamp.
    """
    global _oldest_ts, _oldest_date
    ts = _meta_timestamp()
    with _oldest_lock:
        if _oldest_ts == ts and _oldest_date is not None:
            return _oldest_date
    db = get_mongo_db()
    if db is None:
        return None
    try:
        meta = db['ura_cache'].find_one({"_id": "meta"}, {"oldest_contract_date": 1})
        stored = (meta or {}).get("oldest_contract_date")
        if stored:
            date = datetime.fromisoformat(stored)
        else:
            docs = db['ura_cache'].find(
                {"_id": {"$regex": r"^data_chunk_\d+$"}},
                {"transactions.transaction.contractDate": 1},
                batch_size=200,
            )
            date = _oldest_contract_date([p for d in docs
                                          for p in d.get("transactions", [])])
    except Exception as e:
        logger.error(f"[URA Cache] Window anchor read failed: {e}")
        return None
    if date is not None:
        with _oldest_lock:
            _oldest_ts, _oldest_date = ts, date
    return date


def get_pipeline() -> list:
    """Just the pipeline document — the only other thing a property search
    needs from this cache, and 39 chunks lighter than get_ura_data() for it.

    Callers reach here after the search itself, which has already refreshed a
    stale cache through get_ura_data(), so this never has to.
    """
    global _pipeline_ts, _pipeline_data
    ts = _meta_timestamp()
    with _pipeline_lock:
        if _pipeline_ts == ts and _pipeline_data is not None:
            return _pipeline_data
    db = get_mongo_db()
    if db is None:
        return []
    try:
        doc = db['ura_cache'].find_one({"_id": "pipeline"})
    except Exception as e:
        logger.error(f"[URA Cache] Pipeline read failed: {e}")
        return []
    data = (doc or {}).get("pipeline", []) or []
    if data:
        with _pipeline_lock:
            _pipeline_ts, _pipeline_data = ts, data
    return data


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
