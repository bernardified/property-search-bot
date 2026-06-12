"""
One-time, MANUAL seed builder for cache/unit_counts_seed.json.

NEVER imported by bot.py or any runtime module — run it yourself, at your own
discretion, when you want unit counts for older completed condos that predate
both the pipeline harvest and the new-sale derivation:

    source venv/bin/activate
    python scripts/scrape_unit_counts.py

It looks up each project's "total units" figure on a property portal's
project-detail page. Portals change markup and tighten bot protection over
time, so the URL template and extraction patterns below are deliberately
plain constants — if the hit rate is poor, fetch one project page in a
browser, find the units figure in the HTML, and adjust UNITS_PATTERNS (or
swap PORTAL_URL_TEMPLATE for another portal) accordingly.

Behaviour:
  - Input: distinct strata project names in the URA transaction cache, minus
    anything already in the live pipeline, the Mongo unit_counts collection,
    or the seed file (hits AND misses) — so reruns only attempt new names.
  - Output: cache/unit_counts_seed.json, rewritten atomically after every
    success; Ctrl-C loses nothing. Failed lookups are recorded under
    "_misses" so they aren't retried (delete entries from that list to retry).
  - Politeness: 4–7 s jittered delay between requests; aborts after 3
    consecutive 403/429 responses.

Afterwards, merge into Mongo with:  python -m cache.unit_counts
(refresh_job.py also merges the seed on every scheduled run).
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cache.cache_ura import _load_cache  # read-only; never triggers an API refresh
from cache.unit_counts import SEED_FILE, get_unit_count

PORTAL_URL_TEMPLATE = "https://www.99.co/singapore/condos-apartments/{slug}"
# Tried in order against the page HTML; first capture group must be the count.
UNITS_PATTERNS = [
    r'"total_units"\s*:\s*"?([\d,]{1,6})',
    r'"totalUnits"\s*:\s*"?([\d,]{1,6})',
    r'([\d,]{1,6})\s*(?:total\s+)?units',
]
REQUEST_DELAY_S = (4.0, 7.0)        # min, max jittered sleep between requests
MAX_CONSECUTIVE_BLOCKS = 3          # 403/429 streak before giving up
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-SG,en;q=0.9",
}
# Plausible strata project sizes; anything outside is a regex false positive.
SANE_UNITS_RANGE = (4, 12000)


def _slug(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _load_seed() -> dict:
    if os.path.exists(SEED_FILE):
        with open(SEED_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"_misses": []}


def _save_seed(seed: dict):
    tmp = SEED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seed, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, SEED_FILE)


def _extract_units(html: str) -> int | None:
    for pattern in UNITS_PATTERNS:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            units = int(m.group(1).replace(",", ""))
            if SANE_UNITS_RANGE[0] <= units <= SANE_UNITS_RANGE[1]:
                return units
    return None


def _pending_projects() -> list[str]:
    transactions, pipeline = _load_cache()
    if not transactions:
        print("URA cache is empty — run the bot (or /refresh) once first.")
        return []
    pipeline_names = {str(p.get("project", "")).upper().strip() for p in pipeline}
    seed = _load_seed()
    seeded = {k.upper() for k in seed if not k.startswith("_")}
    missed = {str(m).upper() for m in seed.get("_misses", [])}

    pending = []
    for project in transactions:
        name = str(project.get("project", "")).upper().strip()
        if not name or name in pipeline_names or name in seeded or name in missed:
            continue
        # Skip landed-only "developments" — no meaningful unit count.
        txns = project.get("transaction", [])
        if txns and all(
            any(t in str(x.get("propertyType", "")).lower()
                for t in ("detached", "terrace", "bungalow"))
            for x in txns
        ):
            continue
        if get_unit_count(name):  # already harvested or seeded into Mongo
            continue
        pending.append(name)
    return sorted(set(pending))


def main():
    pending = _pending_projects()
    print(f"{len(pending)} projects need a unit count.")
    if not pending:
        return

    seed = _load_seed()
    seed.setdefault("_misses", [])
    blocks = 0
    found = 0

    for i, name in enumerate(pending, 1):
        url = PORTAL_URL_TEMPLATE.format(slug=_slug(name))
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            print(f"[{i}/{len(pending)}] {name}: request failed ({e}) — recorded as miss")
            seed["_misses"].append(name)
            _save_seed(seed)
            continue

        if r.status_code in (403, 429):
            blocks += 1
            print(f"[{i}/{len(pending)}] {name}: HTTP {r.status_code} ({blocks}/{MAX_CONSECUTIVE_BLOCKS})")
            if blocks >= MAX_CONSECUTIVE_BLOCKS:
                print("Portal is blocking requests — stop and rerun later (progress is saved).")
                break
            time.sleep(60)
            continue
        blocks = 0

        units = _extract_units(r.text) if r.status_code == 200 else None
        if units:
            seed[name] = {
                "total_units": units,
                "source_url": url,
                "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            found += 1
            print(f"[{i}/{len(pending)}] {name}: {units} units")
        else:
            seed["_misses"].append(name)
            print(f"[{i}/{len(pending)}] {name}: not found (HTTP {r.status_code})")
        _save_seed(seed)
        time.sleep(random.uniform(*REQUEST_DELAY_S))

    print(f"\nDone: {found} found this run, {len(seed['_misses'])} total misses.")
    print(f"Seed file: {SEED_FILE}")
    print("Merge into Mongo with:  python -m cache.unit_counts")


if __name__ == "__main__":
    main()
