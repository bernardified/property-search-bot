"""
Short-lived cache of `maps.get_nearby_info` results, one entry per amenity
category per origin.

Why it exists: the amenity bundle is the only thing in the app that spends
money per request. Google bills Distance Matrix per ELEMENT (origin x
destination), and one full bundle is ~45-60 elements plus three Places
searches — roughly US$0.35 — so every repeat view of the same property was
paying for the same answer again: the webapp re-opening a property, a drill-in
from the nearby ring and back, the bot re-fetching for each amenity button.

Keyed per CATEGORY, not per bundle, so the bot (which asks for one category
per button tap) and the webapp (which asks for all six) share entries.

The origin is part of the key in the form the caller gave it:
  * exact coordinate  -> "xy|<lat>,<lng>" (5 dp, ~1 m)
  * address text      -> "addr|<ADDRESS>"
An address hit also stores the coordinate Google geocoded it to, so a fully
cached name search skips the geocode as well. The two are never merged: the
directions links in the rows are routed from whichever origin was used.

Two layers: an in-process memo (works with no Mongo, and saves the round trip)
in front of the Mongo `amenity_cache` collection (shared by the bot and webapp
services, and survives a redeploy — which is exactly when a burst of
re-testing happens). Mongo drops expired docs itself via a TTL index.

TTL is short (TTL_S) on purpose: Google's terms restrict how long Maps
content may be stored, and the saving we want is from repeats within a
session or a few days, not from holding answers for weeks.

Rules:
  * An EMPTY list is never cached — a failed Distance Matrix call comes back
    as an empty category, and holding that would hide the amenity for days.
  * Cache trouble never breaks a lookup: every Mongo error is a miss.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from utils import get_mongo_db

logger = logging.getLogger(__name__)

COLLECTION = "amenity_cache"
TTL_S = 3 * 24 * 3600          # 3 days
MEMO_MAX = 2000                # entries; ~a few KB each

_memo: dict = {}               # key -> (expires_at_epoch, entry)
_memo_lock = threading.Lock()
_index_ready = False


def origin_key(address: str, lat: float | None, lng: float | None) -> str:
    """The origin half of the cache key — see the module docstring."""
    if lat is not None and lng is not None:
        return f"xy|{lat:.5f},{lng:.5f}"
    return f"addr|{(address or '').strip().upper()}"


def _key(category: str, origin: str) -> str:
    return f"{category}|{origin}"


def _collection():
    global _index_ready
    db = get_mongo_db()
    if db is None:
        return None
    coll = db[COLLECTION]
    if not _index_ready:
        try:
            coll.create_index("expires_at", expireAfterSeconds=0)
            _index_ready = True
        except Exception as e:
            logger.warning(f"[AmenityCache] TTL index failed: {e}")
    return coll


def get(category: str, origin: str, now: float | None = None) -> dict | None:
    """{"rows", "lat", "lng"} for a live entry, else None."""
    now = time.time() if now is None else now
    key = _key(category, origin)

    with _memo_lock:
        hit = _memo.get(key)
        if hit:
            if hit[0] > now:
                return hit[1]
            del _memo[key]

    try:
        coll = _collection()
        if coll is None:
            return None
        doc = coll.find_one({"_id": key})
    except Exception as e:
        logger.warning(f"[AmenityCache] read failed: {e}")
        return None
    if not doc:
        return None
    expires = doc["expires_at"]
    if expires.tzinfo is None:          # pymongo hands back naive UTC
        expires = expires.replace(tzinfo=timezone.utc)
    expires_epoch = expires.timestamp()
    if expires_epoch <= now:            # TTL monitor runs ~once a minute
        return None
    entry = {"rows": doc["rows"], "lat": doc.get("lat"), "lng": doc.get("lng")}
    _remember(key, expires_epoch, entry)
    return entry


def put(category: str, origin: str, rows: list, lat: float, lng: float,
        now: float | None = None) -> None:
    """Store a category's rows. Empty rows are ignored (see module rules)."""
    if not rows:
        return
    now = time.time() if now is None else now
    key = _key(category, origin)
    expires_epoch = now + TTL_S
    _remember(key, expires_epoch, {"rows": rows, "lat": lat, "lng": lng})
    try:
        coll = _collection()
        if coll is None:
            return
        coll.replace_one(
            {"_id": key},
            {"_id": key, "rows": rows, "lat": lat, "lng": lng,
             "expires_at": datetime.fromtimestamp(expires_epoch, timezone.utc)},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"[AmenityCache] write failed: {e}")


def _remember(key: str, expires_epoch: float, entry: dict) -> None:
    with _memo_lock:
        if len(_memo) >= MEMO_MAX:
            # Drop the soonest-expiring tenth rather than one at a time.
            for k, _ in sorted(_memo.items(), key=lambda kv: kv[1][0])[: MEMO_MAX // 10]:
                del _memo[k]
        _memo[key] = (expires_epoch, entry)


def clear_memo() -> None:
    with _memo_lock:
        _memo.clear()
