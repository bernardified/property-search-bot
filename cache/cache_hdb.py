"""
HDB resale flat-price cache (data.gov.sg CKAN).

Mirrors cache_ura.py but for HDB resale transactions. Data comes from the
public data.gov.sg "Resale Flat Prices" collection (no auth, paginated,
updated ~monthly). Only the Jan-2017-onwards child dataset is used: it is the
only era whose records carry a parseable `remaining_lease` string
("61 years 04 months"), which the HDB lease-decay signal depends on.

A rolling window of the most recent HDB_ROLLING_MONTHS is cached (block-level
depth vs. storage trade-off). The window is fetched month-by-month via exact
`month` filters — data.gov.sg's range/SQL endpoint is disabled, and exact-match
filtering naturally bounds each request and the overall cache.
"""
import os
import json
import time
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from utils import get_mongo_db, is_hdb_resale_stale, SGT, canon_street_tokens

load_dotenv()

logger = logging.getLogger(__name__)

# data.gov.sg "Resale Flat Prices" collection. Resource IDs are resolved live
# from the collection each refresh (data.gov.sg occasionally re-issues them),
# falling back to the known Jan-2017-onwards dataset if resolution fails.
HDB_COLLECTION_ID = "189"
HDB_RESALE_RESOURCE_FALLBACK = "d_8b84c4ee58e3cfc0ece0d773c8ca6abc"
COLLECTION_META_URL = "https://api-production.data.gov.sg/v2/public/api/collections/{cid}/metadata"
DATASTORE_SEARCH_URL = "https://data.gov.sg/api/action/datastore_search"

HDB_ROLLING_MONTHS = 36   # rolling window cached (3 years ≈ 75k rows)
PAGE_SIZE = 10000         # data.gov.sg honours large page sizes; ~2.5k rows/month
CHUNK_SIZE = 500          # Mongo doc chunking, well under the 16MB BSON limit

# "HDB Property Information" — the authoritative list of every HDB block (blk_no
# + street + residential flag), including newer BTOs that have not yet reached
# MOP and so carry no resale transactions. Used to tell an HDB address apart
# from a private one when routing a postal code: OneMap alone can't, because BTO
# blocks return a building name (the project) exactly like a condo does.
HDB_PROPERTY_INFO_RESOURCE = "d_17f5382f26140b1fdae0ba2ef6239d2f"
_hdb_block_cache: dict = {}   # (blk_no, road) → bool, memoised per process


# ── data.gov.sg helpers ─────────────────────────────────────────────────────

def _resolve_resource_id() -> str:
    """
    Resolve the current Jan-2017-onwards resale resource ID from the collection.

    Two child datasets carry `remaining_lease`; the 2017+ one is far larger and
    current, so among children whose schema includes `remaining_lease` we pick
    the one with the most rows. Falls back to the hardcoded ID on any failure.
    """
    try:
        r = requests.get(COLLECTION_META_URL.format(cid=HDB_COLLECTION_ID), timeout=15)
        children = r.json()["data"]["collectionMetadata"]["childDatasets"]
    except Exception as e:
        logger.warning(f"[HDB Cache] Collection resolve failed ({e}) — using fallback ID")
        return HDB_RESALE_RESOURCE_FALLBACK

    best_id, best_total = None, -1
    for rid in children:
        try:
            r = requests.get(
                DATASTORE_SEARCH_URL,
                params={"resource_id": rid, "limit": 1},
                timeout=15,
            )
            result = r.json().get("result")
            if not result:
                continue
            field_ids = {f["id"] for f in result.get("fields", [])}
            if "remaining_lease" in field_ids and result.get("total", 0) > best_total:
                best_id, best_total = rid, result["total"]
        except Exception:
            continue

    if best_id:
        logger.info(f"[HDB Cache] Resolved resale resource {best_id} ({best_total} rows)")
        return best_id
    logger.warning("[HDB Cache] No matching child dataset — using fallback ID")
    return HDB_RESALE_RESOURCE_FALLBACK


def _recent_months(n: int, now: datetime | None = None) -> list[str]:
    """Return the most recent n months as 'YYYY-MM', newest first."""
    now = now or datetime.now(SGT)
    months, y, m = [], now.year, now.month
    for _ in range(n):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return months


def _fetch_month(resource_id: str, month: str) -> list:
    """Fetch all resale records for a single 'YYYY-MM', paginating if needed."""
    records, offset = [], 0
    while True:
        try:
            r = requests.get(
                DATASTORE_SEARCH_URL,
                params={
                    "resource_id": resource_id,
                    "filters": json.dumps({"month": month}),
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
                timeout=30,
            )
            result = r.json().get("result")
        except Exception as e:
            logger.error(f"[HDB Cache] Fetch {month} offset {offset} failed: {e}")
            break
        if not result:
            break
        recs = result.get("records", [])
        records.extend(recs)
        if len(recs) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return records


def _fetch_resale(resource_id: str, months: list[str]) -> list:
    """Fetch the full rolling window across the given months."""
    all_records = []
    for month in months:
        recs = _fetch_month(resource_id, month)
        all_records.extend(recs)
        if recs:
            logger.info(f"[HDB Cache] {month}: {len(recs)} resale txns")
    return all_records


# ── Cache read/write ────────────────────────────────────────────────────────

def _is_cache_fresh() -> bool:
    db = get_mongo_db()
    if db is None:
        return False
    try:
        doc = db['hdb_cache'].find_one({"_id": "meta"})
        if not doc:
            return False
        return not is_hdb_resale_stale(doc.get("timestamp", 0))
    except Exception as e:
        logger.error(f"[HDB Cache] Freshness check failed: {e}")
        return False


def _load_cache() -> list:
    db = get_mongo_db()
    if db is None:
        return []
    try:
        records, chunk = [], 0
        while True:
            doc = db['hdb_cache'].find_one({"_id": f"data_chunk_{chunk}"})
            if not doc:
                break
            records.extend(doc.get("records", []))
            chunk += 1
        return records
    except Exception as e:
        logger.error(f"[HDB Cache] Load failed: {e}")
        return []


def _save_cache(records: list, resource_id: str, months: list[str]):
    db = get_mongo_db()
    if db is None:
        return
    try:
        current_time = time.time()
        # Wipe old chunks before inserting to prevent orphaned data.
        db['hdb_cache'].delete_many({"_id": {"$regex": "^data_chunk_"}})

        chunks = [records[i:i + CHUNK_SIZE] for i in range(0, len(records), CHUNK_SIZE)]
        for i, chunk in enumerate(chunks):
            db['hdb_cache'].replace_one(
                {"_id": f"data_chunk_{i}"},
                {"_id": f"data_chunk_{i}", "records": chunk, "updated_at": current_time},
                upsert=True,
            )
        logger.info(f"[HDB Cache] Saved {len(records)} resale txns in {len(chunks)} chunks")

        db['hdb_cache'].replace_one(
            {"_id": "meta"},
            {
                "_id": "meta",
                "timestamp": current_time,
                "record_count": len(records),
                "chunk_count": len(chunks),
                "resource_id": resource_id,
                "window_months": len(months),
                "latest_month": months[0] if months else None,
            },
            upsert=True,
        )
        logger.info("[HDB Cache] Metadata saved — cache complete")
    except Exception as e:
        logger.error(f"[HDB Cache] Save failed: {e}")


# ── Public interface ────────────────────────────────────────────────────────

def is_hdb_residential_block(blk_no: str, road: str) -> bool:
    """Return True if (blk_no, road) is a residential HDB block.

    Authoritative check against the HDB Property Information dataset, which lists
    every HDB block — including newer BTOs that have no resale history yet. This
    is the discriminator the postal-code router uses to route HDB vs private,
    since OneMap returns a building name for BTO blocks just like for condos.

    The dataset filters exactly on `blk_no` but stores streets abbreviated
    ("ANG MO KIO AVE 6") while OneMap spells them out ("...AVENUE 6"), so we
    filter on block number only and compare streets via canonicalised tokens.
    Results are memoised per process; any network failure returns False (the
    caller then falls back to private routing)."""
    blk = str(blk_no).strip().upper()
    road_toks = canon_street_tokens(road)
    if not blk or not road_toks:
        return False
    key = (blk, " ".join(road_toks))
    if key in _hdb_block_cache:
        return _hdb_block_cache[key]

    try:
        r = requests.get(
            DATASTORE_SEARCH_URL,
            params={
                "resource_id": HDB_PROPERTY_INFO_RESOURCE,
                "filters": json.dumps({"blk_no": blk}),
                "limit": 100,
            },
            timeout=15,
        )
        records = r.json().get("result", {}).get("records", [])
        is_hdb = any(
            str(rec.get("residential", "")).strip().upper() == "Y"
            and canon_street_tokens(rec.get("street", "")) == road_toks
            for rec in records
        )
    except Exception as e:
        logger.warning(f"[HDB Cache] Block check failed for {key}: {e}")
        is_hdb = False

    _hdb_block_cache[key] = is_hdb
    return is_hdb


def get_hdb_resale_data() -> list:
    """
    Return the rolling window of HDB resale records from MongoDB cache.
    Refreshes automatically if the cache is stale or missing.
    """
    if _is_cache_fresh():
        logger.info("[HDB Cache] Using cached data")
        return _load_cache()

    logger.info("[HDB Cache] Cache stale or missing — refreshing from data.gov.sg...")
    resource_id = _resolve_resource_id()
    months = _recent_months(HDB_ROLLING_MONTHS)
    records = _fetch_resale(resource_id, months)

    if records:
        _save_cache(records, resource_id, months)
        return records
    logger.warning("[HDB Cache] Fetch empty — falling back to stale cache")
    return _load_cache()


def force_refresh_hdb() -> bool:
    """Force a cache refresh regardless of age. Returns True on success."""
    logger.info("[HDB Cache] Force refreshing...")
    resource_id = _resolve_resource_id()
    months = _recent_months(HDB_ROLLING_MONTHS)
    records = _fetch_resale(resource_id, months)
    if records:
        _save_cache(records, resource_id, months)
        return True
    return False


def hdb_cache_status() -> dict:
    """Return info about the current cache state."""
    db = get_mongo_db()
    if db is None:
        return {"status": "no_db"}
    try:
        doc = db['hdb_cache'].find_one({"_id": "meta"})
        if not doc:
            return {"status": "missing"}
        last_refresh_ts = doc.get("timestamp", 0)
        age_hours = (time.time() - last_refresh_ts) / 3600
        stale = is_hdb_resale_stale(last_refresh_ts)
        return {
            "status": "stale" if stale else "fresh",
            "age_hours": round(age_hours, 1),
            "records": doc.get("record_count", "?"),
            "latest_month": doc.get("latest_month", "?"),
            "resource_id": doc.get("resource_id", "?"),
        }
    except Exception:
        return {"status": "error"}
