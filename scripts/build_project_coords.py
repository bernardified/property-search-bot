"""
One-time / occasional warmer for the Mongo `project_coords` collection used by
the 📌 nearby-properties search.

NEVER imported by bot.py or any runtime module — run it yourself to pre-populate
coordinates so the first nearby search in a dense district doesn't have to
geocode hundreds of projects inline:

    source venv/bin/activate
    python scripts/build_project_coords.py            # all districts
    python scripts/build_project_coords.py 19          # one district only

It geocodes each strata project via OneMap (the same `_geocode` the bot uses)
and upserts {lat, lng} into `project_coords`. Runtime reads this cache first and
only geocodes live on a miss, so warming is purely an optimisation — the nearby
search is correct with or without it.

Behaviour:
  - Input: distinct non-landed project names in the URA transaction cache,
    minus anything already present in `project_coords` (a hit OR a recorded
    miss) — so reruns only attempt new names.
  - Output: one `project_coords` doc per project, written immediately; Ctrl-C
    loses nothing. A failed geocode is stored as a miss marker (lat=None) so the
    warmer won't retry it, while the bot still retries misses live and upgrades
    the marker to real coords if it ever succeeds.
  - Politeness: ~0.3 s between OneMap calls.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cache.cache_ura import _load_cache  # read-only; never triggers an API refresh
from utils import get_mongo_db, get_onemap_token
from nearby import (
    COORDS_COLLECTION,
    _geocode,
    _is_landed,
    candidate_projects,
    _normalize_district,
)

REQUEST_DELAY_S = 0.3


def _pending(transactions, collection, district=None):
    """Distinct non-landed project (name, street) not yet in project_coords."""
    seen = {str(d.get("_id", "")).upper() for d in collection.find({}, {"_id": 1})}
    out = {}
    for pd in transactions:
        name = (pd.get("project") or "").strip()
        if not name:
            continue
        key = name.upper()
        if key in seen or key in out:
            continue
        txns = pd.get("transaction", [])
        if txns and all(_is_landed(t.get("propertyType", "")) for t in txns):
            continue
        if district is not None and not any(
            _normalize_district(t.get("district")) == district for t in txns
        ):
            continue
        out[key] = {"project": name, "street": (pd.get("street") or "").strip()}
    return out


def main():
    district = None
    if len(sys.argv) > 1:
        try:
            district = int(sys.argv[1])
        except ValueError:
            print(f"Ignoring non-numeric district arg: {sys.argv[1]!r}")

    db = get_mongo_db()
    if db is None:
        print("No Mongo connection (set MONGO_URI in .env) — nothing to warm.")
        return

    transactions, _ = _load_cache()
    if not transactions:
        print("URA cache is empty — run the bot (or /refresh) once first.")
        return

    collection = db[COORDS_COLLECTION]
    pending = _pending(transactions, collection, district)
    scope = f"District {district}" if district is not None else "all districts"
    print(f"{len(pending)} projects need coordinates ({scope}).")
    if not pending:
        return

    token = get_onemap_token()
    found = misses = 0
    for i, (key, info) in enumerate(sorted(pending.items()), 1):
        coords = _geocode(info["project"], info["street"], token)
        doc = {"_id": key, "project": info["project"], "street": info["street"]}
        if coords is not None:
            doc["lat"], doc["lng"] = coords
            found += 1
            print(f"[{i}/{len(pending)}] {info['project']}: {coords[0]:.5f},{coords[1]:.5f}")
        else:
            doc["lat"] = doc["lng"] = None  # miss marker — bot still retries live
            misses += 1
            print(f"[{i}/{len(pending)}] {info['project']}: not found")
        collection.replace_one({"_id": key}, doc, upsert=True)
        time.sleep(REQUEST_DELAY_S)

    print(f"\nDone: {found} geocoded, {misses} misses this run. Collection: {COORDS_COLLECTION}")


if __name__ == "__main__":
    main()
