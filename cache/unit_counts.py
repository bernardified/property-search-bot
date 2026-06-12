"""
Permanent project → total-units store backing the liquidity feature.

URA's pipeline feed (PMI_Resi_Pipeline) lists a project's totalUnits only
while it is uncompleted — once it TOPs it drops out of the feed and the
count is lost. Every cache refresh therefore harvests the pipeline snapshot
into a permanent Mongo collection (`unit_counts`) so completed developments
keep their unit count, and an optional scraped seed file
(cache/unit_counts_seed.json, produced offline by scripts/scrape_unit_counts.py)
fills in older condos that completed before harvesting began.

Lookups are EXACT uppercase-name matches (the doc _id). Unlike
ura.get_project_info's bidirectional substring matching — fine for a small
transient feed — a permanent table must never let "THE M" claim
"THE MEYERISE"'s count.
"""
import json
import logging
import os
import time

from utils import get_mongo_db

logger = logging.getLogger(__name__)

COLLECTION = "unit_counts"
SEED_FILE = os.path.join(os.path.dirname(__file__), "unit_counts_seed.json")


def _key(project_name: str) -> str:
    return str(project_name).upper().strip()


def harvest_pipeline_counts(pipeline: list) -> int:
    """Upsert every pipeline project's totalUnits into the permanent store.

    Pipeline data is authoritative — it overwrites seed-sourced docs.
    Returns the number of docs upserted (0 when Mongo is unavailable).
    """
    db = get_mongo_db()
    if db is None or not pipeline:
        return 0
    saved = 0
    try:
        for item in pipeline:
            name = _key(item.get("project", ""))
            try:
                total = int(item.get("totalUnits"))
            except (ValueError, TypeError):
                continue
            if not name or total <= 0:
                continue
            db[COLLECTION].replace_one(
                {"_id": name},
                {
                    "_id": name,
                    "project": name,
                    "total_units": total,
                    "source": "pipeline",
                    "expected_top": item.get("expectedTOPYear"),
                    "updated_at": time.time(),
                },
                upsert=True,
            )
            saved += 1
        logger.info(f"[Unit Counts] Harvested {saved} projects from pipeline")
    except Exception as e:
        logger.error(f"[Unit Counts] Harvest failed: {e}")
    return saved


def get_unit_count(project_name: str) -> dict | None:
    """Exact-name lookup. Returns {"total_units": int, "source": str} or None."""
    db = get_mongo_db()
    if db is None:
        return None
    try:
        doc = db[COLLECTION].find_one({"_id": _key(project_name)})
        if doc and doc.get("total_units"):
            return {
                "total_units": int(doc["total_units"]),
                "source": doc.get("source", "pipeline"),
            }
    except Exception as e:
        logger.error(f"[Unit Counts] Lookup failed: {e}")
    return None


def merge_seed_file(path: str = SEED_FILE) -> int:
    """Merge scraped seed entries into Mongo. Never overwrites a
    pipeline-sourced doc — only fills gaps or refreshes older seed docs.
    Silently no-ops when the file is absent. Returns docs written.
    """
    db = get_mongo_db()
    if db is None or not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            seed = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[Unit Counts] Seed file unreadable: {e}")
        return 0
    written = 0
    try:
        for name, entry in seed.items():
            # "_misses" and any other bookkeeping keys are not projects.
            if name.startswith("_") or not isinstance(entry, dict):
                continue
            total = entry.get("total_units")
            if not isinstance(total, int) or total <= 0:
                continue
            key = _key(name)
            existing = db[COLLECTION].find_one({"_id": key})
            if existing and existing.get("source") != "seed":
                continue
            db[COLLECTION].replace_one(
                {"_id": key},
                {
                    "_id": key,
                    "project": key,
                    "total_units": total,
                    "source": "seed",
                    "expected_top": None,
                    "updated_at": time.time(),
                },
                upsert=True,
            )
            written += 1
        logger.info(f"[Unit Counts] Merged {written} seed entries")
    except Exception as e:
        logger.error(f"[Unit Counts] Seed merge failed: {e}")
    return written


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    # Optionally pass a seed-file path (default: cache/unit_counts_seed.json).
    path = sys.argv[1] if len(sys.argv) > 1 else SEED_FILE
    print(f"{merge_seed_file(path)} entries merged from {path}")
