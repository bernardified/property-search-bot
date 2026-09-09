"""
One-time / occasional warmer + repairer for the Mongo `project_coords`
collection used by the 📌 nearby-properties search.

NEVER imported by bot.py or any runtime module — run it yourself:

    source venv/bin/activate
    python scripts/build_project_coords.py             # warm all districts
    python scripts/build_project_coords.py 19          # warm one district only
    python scripts/build_project_coords.py --repair    # re-derive bad entries
    python scripts/build_project_coords.py --repair --dry-run   # report only

Coordinates come from URA's own surveyed SVY21 `x`/`y` (~88% of projects,
exact and free) and only fall back to a OneMap name-geocode for the rest.
Runtime does the same, so warming is purely an optimisation for that
remaining slice — the nearby search is correct with or without it.

Behaviour:
  - Warm — input: distinct non-landed project names in the URA transaction
    cache, minus anything already present in `project_coords` (a hit OR a
    recorded miss), so reruns only attempt new names. Projects with URA x/y are
    written straight from it; the others are geocoded. Output: one doc per
    project, written immediately, so Ctrl-C loses nothing. A failed geocode is
    stored as a miss marker (lat=None) that the warmer won't retry, while the
    bot still retries misses live.
  - Repair — rewrites entries that disagree with URA's surveyed position by
    more than REPAIR_TOLERANCE_M. Needed because the original warmer geocoded
    by project name and took OneMap's top hit: generic names ("THE SUMMIT",
    "THE VISION") landed on identically-named buildings up to 22 km away.
    Entries with no URA position are reported as unverifiable and left alone.
  - Politeness: ~0.3 s between OneMap calls (never between URA conversions).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cache.cache_ura import _load_cache  # read-only; never triggers an API refresh
from utils import get_mongo_db, get_onemap_token, haversine_m
from nearby import (
    COORDS_COLLECTION,
    SOURCE_ONEMAP,
    SOURCE_URA,
    _geocode,
    _is_landed,
    _normalize_district,
    ura_coords,
)

REQUEST_DELAY_S = 0.3
# A project sits within a few hundred metres of its own address; anything
# further from URA's surveyed position is a different building, not imprecision.
REPAIR_TOLERANCE_M = 1000


def _strata_projects(transactions, district=None):
    """Distinct non-landed projects → {KEY: {project, street, coords}}.

    `coords` is URA's surveyed (lat, lng) when the record carries x/y, else None.
    """
    out = {}
    for pd in transactions:
        name = (pd.get("project") or "").strip()
        if not name:
            continue
        key = name.upper()
        txns = pd.get("transaction", [])
        if txns and all(_is_landed(t.get("propertyType", "")) for t in txns):
            continue
        if district is not None and not any(
            _normalize_district(t.get("district")) == district for t in txns
        ):
            continue
        row = out.setdefault(
            key, {"project": name, "street": (pd.get("street") or "").strip(),
                  "coords": None}
        )
        if row["coords"] is None:
            row["coords"] = ura_coords(pd)
    return out


def _pending(projects, collection):
    """Projects not yet in project_coords (a hit or a recorded miss both count)."""
    seen = {str(d.get("_id", "")).upper() for d in collection.find({}, {"_id": 1})}
    return {k: v for k, v in projects.items() if k not in seen}


def _doc(key, info, coords, source):
    # coords None -> miss marker (lat/lng None); the bot still retries misses live.
    lat, lng = coords if coords is not None else (None, None)
    return {"_id": key, "project": info["project"], "street": info["street"],
            "lat": lat, "lng": lng, "source": source}


def warm(collection, projects):
    pending = _pending(projects, collection)
    print(f"{len(pending)} projects need coordinates.")
    if not pending:
        return

    token = get_onemap_token()
    from_ura = geocoded = misses = 0
    for i, (key, info) in enumerate(sorted(pending.items()), 1):
        coords, source = info["coords"], SOURCE_URA
        if coords is not None:
            from_ura += 1
        else:
            coords, source = _geocode(info["project"], info["street"], token), SOURCE_ONEMAP
            if coords is not None:
                geocoded += 1
            else:
                misses += 1
        label = "not found" if coords is None else f"{coords[0]:.5f},{coords[1]:.5f} ({source})"
        print(f"[{i}/{len(pending)}] {info['project']}: {label}")
        collection.replace_one({"_id": key}, _doc(key, info, coords, source), upsert=True)
        if source == SOURCE_ONEMAP:
            time.sleep(REQUEST_DELAY_S)  # only OneMap needs the courtesy pause

    print(f"\nDone: {from_ura} from URA, {geocoded} geocoded, {misses} misses. "
          f"Collection: {COORDS_COLLECTION}")


def repair(collection, projects, dry_run=False):
    """Re-derive stored coords from URA x/y wherever they disagree."""
    # One read for the whole collection (a few thousand small docs) — a
    # find_one per project turns the pass into thousands of round trips.
    cached = {str(d["_id"]).upper(): d for d in collection.find({})}

    fixed = snapped = unverifiable = absent = 0
    for key, info in sorted(projects.items()):
        ura = info["coords"]
        doc = cached.get(key)
        if doc is None:
            absent += 1
            continue
        if ura is None:
            unverifiable += 1
            continue

        lat, lng = doc.get("lat"), doc.get("lng")
        if lat is None or lng is None:
            reason = "miss marker"
        else:
            off = haversine_m(lat, lng, ura[0], ura[1])
            if off <= REPAIR_TOLERANCE_M:
                # Close enough to be the right building — snap it to the exact
                # surveyed position anyway so the whole collection is uniformly
                # URA-derived. Idempotent: already-URA docs are left untouched.
                if doc.get("source") != SOURCE_URA:
                    snapped += 1
                    if not dry_run:
                        collection.update_one(
                            {"_id": key},
                            {"$set": {"lat": ura[0], "lng": ura[1], "source": SOURCE_URA}},
                        )
                continue
            reason = f"{off / 1000:.1f} km off"

        fixed += 1
        print(f"{'[dry-run] ' if dry_run else ''}{info['project']}: {reason} — "
              f"{lat},{lng} → {ura[0]:.5f},{ura[1]:.5f}")
        if not dry_run:
            collection.replace_one({"_id": key}, _doc(key, info, ura, SOURCE_URA),
                                   upsert=True)

    tense = "Would repair" if dry_run else "Repaired"
    print(f"\n{tense} (>{REPAIR_TOLERANCE_M} m off or a miss marker): {fixed}. "
          f"Snapped to the exact URA position: {snapped}. "
          f"No URA position (left alone): {unverifiable}. Not cached yet: {absent}.")


def main():
    args = sys.argv[1:]
    do_repair = "--repair" in args
    dry_run = "--dry-run" in args
    district = None
    for arg in args:
        if arg.startswith("--"):
            continue
        try:
            district = int(arg)
        except ValueError:
            print(f"Ignoring non-numeric district arg: {arg!r}")

    db = get_mongo_db()
    if db is None:
        print("No Mongo connection (set MONGO_URI in .env) — nothing to do.")
        return

    transactions, _ = _load_cache()
    if not transactions:
        print("URA cache is empty — run the bot (or /refresh) once first.")
        return

    collection = db[COORDS_COLLECTION]
    projects = _strata_projects(transactions, district)
    scope = f"District {district}" if district is not None else "all districts"
    print(f"{len(projects)} strata projects in scope ({scope}).")

    if do_repair:
        repair(collection, projects, dry_run=dry_run)
    else:
        warm(collection, projects)


if __name__ == "__main__":
    main()
