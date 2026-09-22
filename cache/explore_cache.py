"""Persisted explore-map payloads — the webapp's map layers, pre-derived.

`/api/developments` is *derived* data: ~31 MB of BSON pulled out of the URA
transaction cache and decoded, to produce 550 KB of dots (80 KB deflated).
Measured cold, that load is ~9 s — and since the explore map *is* the landing
state, that 9 s was the landing page.

So each derived payload is stored as one small document, keyed by the meta
timestamps of the caches it comes from. A hit is a single small read and the
landing page never touches the transaction cache at all; the key changes only
when a cache actually refreshes, which is exactly when the dots change.

Two layers live here, one document each, because they are derived from
different caches and refresh on different schedules:

  developments  private dots, keyed (schema, ura_ts, rental_ts)  — Tue/Fri + 15th
  hdb_blocks    HDB block dots, keyed (schema, hdb_ts)            — ~monthly

Keying them separately is the point: a URA refresh must not invalidate a
payload built from HDB resale data, and vice versa.

This store is a pure optimisation and never a source of truth: a miss, or a
key that no longer matches, just means the caller rebuilds from the caches and
saves the result back, so a wiped collection self-heals on the next request.
"""

import json
import logging
import time
import zlib

from bson.binary import Binary

from cache.cache_ura import meta_timestamp as ura_meta_timestamp
from cache.cache_rental import meta_timestamp as rental_meta_timestamp
from cache.cache_hdb import meta_timestamp as hdb_meta_timestamp, is_cache_fresh as hdb_is_fresh
from utils import get_mongo_db, is_ura_transactions_stale, is_rental_stale

logger = logging.getLogger(__name__)

COLLECTION = "explore_cache"
DOC_ID = "developments"
HDB_DOC_ID = "hdb_blocks"

# The shape of the rows inside a stored payload. It leads every key because a
# blob is only current if it was built by code that agrees with this one: the
# cache timestamps say nothing about a deploy that ADDS a field to the dots
# (mall_m, lease_years), and without this a new build would keep serving the
# old rows until URA next refreshed — a filter with no data behind it. Bump it
# whenever build_developments or build_hdb_blocks changes what a row carries.
LAYER_SCHEMA = 2


def source_key() -> tuple | None:
    """`(LAYER_SCHEMA, ura_ts, rental_ts)` — what a stored payload is keyed
    on — or None.

    None means "do not use the store": either cache is missing, or one is
    stale and therefore due a refresh, and refreshing is what the full
    `get_ura_data()` path does. Two tiny projected reads (~10 ms), against
    the ~9 s the store exists to avoid.
    """
    ura, rental = ura_meta_timestamp(), rental_meta_timestamp()
    if ura is None or rental is None:
        return None
    if is_ura_transactions_stale(ura) or is_rental_stale(rental):
        return None
    return (LAYER_SCHEMA, ura, rental)


def hdb_source_key() -> tuple | None:
    """`(LAYER_SCHEMA, hdb_ts)` for the HDB block layer, or None to force the
    slow path.

    Same contract as `source_key()`, against the one cache that layer comes
    from. `is_cache_fresh()` rather than a staleness check on the timestamp
    alone, because a *partial* window carries a perfectly recent timestamp and
    must not key a payload — the cache module already refuses to treat one as
    fresh, and dots derived from it would inherit the same half-window.
    """
    ts = hdb_meta_timestamp()
    if ts is None or not hdb_is_fresh():
        return None
    return (LAYER_SCHEMA, ts)


def load(doc_id: str = DOC_ID) -> tuple[dict | None, tuple | None]:
    """The stored payload and the key it was built for — `(None, None)` on a
    miss. The caller compares that key against the matching `*_source_key()`:
    equal means current, different means a cache has refreshed underneath it.

    A document written before keys were stored as a list reads back with key
    None, which is a mismatch, not a miss: the payload is still served while
    the caller rebuilds behind it.
    """
    db = get_mongo_db()
    if db is None:
        return None, None
    try:
        doc = db[COLLECTION].find_one({"_id": doc_id})
        if not doc or "blob" not in doc:
            return None, None
        payload = json.loads(zlib.decompress(bytes(doc["blob"])))
        key = doc.get("key")
        return payload, (tuple(key) if key else None)
    except Exception as e:
        logger.error(f"[Explore Cache] Load failed for {doc_id}: {e}")
        return None, None


def save(payload: dict, key: tuple, doc_id: str = DOC_ID) -> None:
    """Store `payload` under `key`. Deflated because 550 KB of JSON is 80 KB
    of blob and the read is on the landing page's critical path; a failure
    here only costs the next process a rebuild, so it never propagates.

    The key is stored as a plain list so one shape serves both layers — the
    schema version plus two cache timestamps on the private side, and the
    schema version plus one on the HDB side.
    """
    db = get_mongo_db()
    if db is None:
        return
    try:
        blob = zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
        db[COLLECTION].replace_one(
            {"_id": doc_id},
            {
                "_id": doc_id,
                "key": list(key),
                "count": payload.get("count", 0),
                "built_at": time.time(),
                "blob": Binary(blob),
            },
            upsert=True,
        )
        logger.info(f"[Explore Cache] Saved {payload.get('count', 0)} {doc_id} "
                    f"({len(blob) // 1024} KB)")
    except Exception as e:
        logger.error(f"[Explore Cache] Save failed for {doc_id}: {e}")
