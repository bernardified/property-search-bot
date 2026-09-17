"""Persisted explore-map payload — the webapp's entire landing render.

`/api/developments` is *derived* data: ~31 MB of BSON pulled out of the URA
transaction cache and decoded, to produce 550 KB of dots (80 KB deflated).
Measured cold, that load is ~9 s — and since the explore map *is* the landing
state, that 9 s was the landing page.

So the derived payload is stored as one small document, keyed by the meta
timestamps of the two caches it comes from. A hit is a single small read and
the landing page never touches the transaction cache at all; the key changes
only when a cache actually refreshes, which is exactly when the dots change.

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
from utils import get_mongo_db, is_ura_transactions_stale, is_rental_stale

logger = logging.getLogger(__name__)

COLLECTION = "explore_cache"
DOC_ID = "developments"


def source_key() -> tuple[float, float] | None:
    """`(ura_ts, rental_ts)` — what a stored payload is keyed on — or None.

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
    return (ura, rental)


def load() -> tuple[dict | None, tuple | None]:
    """The stored payload and the key it was built for — `(None, None)` on a
    miss. The caller compares that key against `source_key()`: equal means
    current, different means a cache has refreshed underneath it."""
    db = get_mongo_db()
    if db is None:
        return None, None
    try:
        doc = db[COLLECTION].find_one({"_id": DOC_ID})
        if not doc or "blob" not in doc:
            return None, None
        payload = json.loads(zlib.decompress(bytes(doc["blob"])))
        return payload, (doc.get("ura_ts"), doc.get("rental_ts"))
    except Exception as e:
        logger.error(f"[Explore Cache] Load failed: {e}")
        return None, None


def save(payload: dict, key: tuple[float, float]) -> None:
    """Store `payload` under `key`. Deflated because 550 KB of JSON is 80 KB
    of blob and the read is on the landing page's critical path; a failure
    here only costs the next process a rebuild, so it never propagates."""
    db = get_mongo_db()
    if db is None:
        return
    try:
        blob = zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
        db[COLLECTION].replace_one(
            {"_id": DOC_ID},
            {
                "_id": DOC_ID,
                "ura_ts": key[0],
                "rental_ts": key[1],
                "count": payload.get("count", 0),
                "built_at": time.time(),
                "blob": Binary(blob),
            },
            upsert=True,
        )
        logger.info(f"[Explore Cache] Saved {payload.get('count', 0)} dots "
                    f"({len(blob) // 1024} KB)")
    except Exception as e:
        logger.error(f"[Explore Cache] Save failed: {e}")
