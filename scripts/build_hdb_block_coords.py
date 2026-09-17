"""
One-time / occasional warmer for the Mongo `hdb_block_coords` collection that
the HDB explore layer (webapp Phase 2) needs.

NEVER imported by bot.py, api.py or any runtime module — run it yourself:

    source venv/bin/activate
    python scripts/build_hdb_block_coords.py                    # warm everything pending
    python scripts/build_hdb_block_coords.py --limit 50         # trial run
    python scripts/build_hdb_block_coords.py --street BISHAN    # one street (substring)
    python scripts/build_hdb_block_coords.py --retry-misses     # re-attempt recorded misses
    python scripts/build_hdb_block_coords.py --prune            # drop docs the index no longer has
    python scripts/build_hdb_block_coords.py --dry-run          # resolve, write nothing

HDB resale records carry no coordinates and HDB Property Information has none
either, so the ~9.6k blocks in the resale window have to be geocoded once via
OneMap and kept permanently (same survive-forever idea as `unit_counts` and
`project_coords`).

Why this is safe when CLAUDE.md warns that geocoding is not:
    the warning is about geocoding by *name* — "THE SUMMIT" has namesakes and
    landed 22 km from the real project. A block plus a street is an ADDRESS,
    and OneMap resolves addresses exactly. It is also self-validating: a hit is
    only accepted when OneMap's own BLK_NO equals the block asked for and its
    ROAD_NAME canonicalises to the same tokens as the street asked for
    (utils.canon_street_tokens, which expands ST/AVE/NTH/CRES/LOR...). A hit
    that fails either test is discarded rather than stored, so a wrong
    coordinate cannot enter the collection — the failure mode is a miss.

Behaviour:
  - Input is the cache's own (block, street) index — read-only, straight off
    the `index_rows` document; this never triggers a cache refresh.
  - Pairs already in the collection (a hit OR a recorded miss) are skipped, so
    reruns only attempt what is left and Ctrl-C loses nothing: every pair is
    written the moment it resolves.
  - A failed geocode is stored as a miss marker (lat=None) so the warm pass
    does not retry it every run; --retry-misses re-attempts exactly those.
  - Keys are `<block>|<canonicalised street>`, so a change to
    canon_street_tokens re-keys that street and strands whatever was stored
    under the old spelling. That is not hypothetical: fixing C'WEALTH, ST. and
    PK re-keyed 11 streets. Recovery is a plain rerun (the new keys look
    unwarmed, so they are re-attempted) followed by --prune for the orphans.
  - Politeness: ~0.3 s between OneMap calls. A full cold run is ~9.6k lookups,
    so budget roughly an hour.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cache.cache_hdb import _load_cache, index_records  # read-only; no refresh
from maps import search_onemap
from utils import canon_street_tokens, get_mongo_db, get_onemap_token, hdb_block_key

REQUEST_DELAY_S = 0.3
COORDS_COLLECTION = "hdb_block_coords"
SOURCE_ONEMAP = "onemap"


# ── Pure helpers ────────────────────────────────────────────────────────────────

def match_result(result: dict, blk: str, street_toks: list):
    """(lat, lng) if this OneMap hit really is that block, else None.

    The whole safety argument of this script lives here: OneMap is asked for an
    address and its answer carries the address back, so the answer can be
    checked against the question instead of trusted.
    """
    if str(result.get("BLK_NO", "")).strip().upper() != blk:
        return None
    if canon_street_tokens(result.get("ROAD_NAME", "")) != street_toks:
        return None
    try:
        return float(result["LATITUDE"]), float(result["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        return None


def index_pairs(rows: list, street_filter: str | None = None) -> dict:
    """Distinct (block, street) pairs from the cache index → {key: {...}}."""
    out: dict = {}
    for r in rows:
        block = str(r.get("block", "")).strip().upper()
        street = str(r.get("street_name", "")).strip().upper()
        if not block or not street:
            continue
        if street_filter and street_filter.upper() not in street:
            continue
        out.setdefault(hdb_block_key(block, street), {"block": block, "street": street})
    return out


def _doc(key, info, coords, postal=None):
    # coords None -> miss marker; --retry-misses is the only pass that revisits it.
    lat, lng = coords if coords is not None else (None, None)
    return {"_id": key, "block": info["block"], "street": info["street"],
            "lat": lat, "lng": lng, "postal": postal, "source": SOURCE_ONEMAP}


# ── IO ──────────────────────────────────────────────────────────────────────────

def load_index(db) -> list:
    """The (block, street) index, read directly — never a refresh.

    cache_hdb.get_index_records() would fall back to get_hdb_resale_data(),
    which refreshes a stale cache from data.gov.sg. A warmer must not decide to
    do that: it reads whatever window is there, or says so and stops.
    """
    try:
        doc = db["hdb_cache"].find_one({"_id": "index_rows"})
    except Exception as e:
        print(f"Index read failed: {e}")
        return []
    if doc and doc.get("records"):
        return doc["records"]
    print("No index document — deriving from the street documents.")
    return index_records(_load_cache())


def geocode_block(block: str, street: str, token: str):
    """(lat, lng, postal) for one HDB block, or (None, None, None).

    Two queries: the street as the data spells it, then the expanded spelling.
    OneMap indexes full words, so "BISHAN ST 22" can miss where "BISHAN STREET
    22" hits; both are validated the same way, so the second is a second
    chance, not a looser one.
    """
    street_toks = canon_street_tokens(street)
    queries = [f"{block} {street}"]
    expanded = f"{block} {' '.join(street_toks)}"
    if expanded != queries[0]:
        queries.append(expanded)

    for query in queries:
        results = search_onemap(query, token)
        time.sleep(REQUEST_DELAY_S)  # after every call, hit or not — 9.6k of these
        for result in results:
            coords = match_result(result, str(block).strip().upper(), street_toks)
            if coords is not None:
                # OneMap writes "NIL" where it has no postal code; that is an
                # absence, and storing the string would read back as a value.
                postal = str(result.get("POSTAL", "")).strip().upper()
                return coords[0], coords[1], (postal if postal.isdigit() else None)
    return None, None, None


def pending_pairs(collection, pairs: dict, retry_misses: bool) -> dict:
    """Pairs still to attempt. A recorded miss counts as done unless retrying."""
    try:
        query = {"lat": None} if retry_misses else {}
        seen = {str(d["_id"]) for d in collection.find(query, {"_id": 1})}
    except Exception as e:
        print(f"Collection read failed: {e}")
        return {}
    if retry_misses:
        return {k: v for k, v in pairs.items() if k in seen}
    return {k: v for k, v in pairs.items() if k not in seen}


def prune(collection, pairs: dict, dry_run=False):
    """Delete documents whose key the current index no longer produces.

    Two things orphan a document: a block leaving the rolling window, and a
    change to canon_street_tokens — the key is derived from it, so correcting
    an abbreviation (C'WEALTH -> COMMONWEALTH) re-keys that street and strands
    whatever was stored under the old spelling. Refuses to run against a
    filtered scope, where "not in the index" would mean "not in the filter"
    and would delete most of the collection.
    """
    try:
        stale = [str(d["_id"]) for d in collection.find({}, {"_id": 1})
                 if str(d["_id"]) not in pairs]
    except Exception as e:
        print(f"Collection read failed: {e}")
        return
    print(f"{len(stale)} orphaned documents.")
    for key in sorted(stale)[:20]:
        print(f"  {'[dry-run] ' if dry_run else ''}drop {key}")
    if len(stale) > 20:
        print(f"  ... and {len(stale) - 20} more")
    if stale and not dry_run:
        collection.delete_many({"_id": {"$in": stale}})
        print(f"Deleted {len(stale)}.")


def warm(collection, pairs: dict, retry_misses=False, limit=None, dry_run=False):
    pending = pending_pairs(collection, pairs, retry_misses)
    ordered = sorted(pending.items())
    if limit is not None:
        ordered = ordered[:limit]
    verb = "miss markers to re-attempt" if retry_misses else "blocks need coordinates"
    print(f"{len(pending)} {verb}" + (f" — attempting {len(ordered)}." if limit else "."))
    if not ordered:
        return

    token = get_onemap_token()
    if not token:
        print("No OneMap token (set ONEMAP_EMAIL / ONEMAP_PASSWORD in .env).")
        return

    hits = misses = 0
    for i, (key, info) in enumerate(ordered, 1):
        lat, lng, postal = geocode_block(info["block"], info["street"], token)
        coords = (lat, lng) if lat is not None else None
        if coords is None:
            misses += 1
            label = "not found"
        else:
            hits += 1
            label = f"{lat:.5f},{lng:.5f}" + (f" ({postal})" if postal else "")
        print(f"[{i}/{len(ordered)}] {'[dry-run] ' if dry_run else ''}"
              f"BLK {info['block']} {info['street']}: {label}")
        if not dry_run:
            collection.replace_one({"_id": key}, _doc(key, info, coords, postal),
                                   upsert=True)
        # The token outlives a long run — utils refreshes it 5 min before expiry.
        if i % 500 == 0:
            token = get_onemap_token() or token

    rate = hits / len(ordered) * 100
    print(f"\n{'Would write' if dry_run else 'Done'}: {hits} located, {misses} misses "
          f"({rate:.1f}% hit rate). Collection: {COORDS_COLLECTION}")


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    retry_misses = "--retry-misses" in args
    do_prune = "--prune" in args
    street_filter = limit = None
    for i, arg in enumerate(args):
        if arg == "--street" and i + 1 < len(args):
            street_filter = args[i + 1]
        elif arg == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print(f"Ignoring non-numeric --limit: {args[i + 1]!r}")

    db = get_mongo_db()
    if db is None:
        print("No Mongo connection (set MONGO_URI in .env) — nothing to do.")
        return

    rows = load_index(db)
    if not rows:
        print("HDB cache is empty — run the bot (or /refresh) once first.")
        return

    pairs = index_pairs(rows, street_filter)
    scope = f"street ~ {street_filter!r}" if street_filter else "the whole window"
    print(f"{len(pairs)} distinct (block, street) pairs in scope ({scope}).")
    if not pairs:
        return

    if do_prune:
        if street_filter:
            print("--prune needs the whole index; drop --street and rerun.")
            return
        prune(db[COORDS_COLLECTION], pairs, dry_run=dry_run)
        return

    warm(db[COORDS_COLLECTION], pairs, retry_misses=retry_misses, limit=limit,
         dry_run=dry_run)


if __name__ == "__main__":
    main()
