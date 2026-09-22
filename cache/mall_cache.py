"""The shopping-mall register — coordinates for every mall the explore map's
nearest-mall filter measures against.

Unlike every other cache here this one is a STATIC file, `cache/malls.json`,
checked into the repo and rebuilt by hand with scripts/build_mall_coords.py.
That is not laziness: Singapore publishes no mall register. OneMap has 165
themes and none of them is malls, data.gov.sg has none either, and Google
Places — which maps.py does use, per origin, for the amenity list — forbids
storing its place data beyond 30 days.

A distance filter needs the other shape anyway. `maps.find_nearest_malls`
answers "which malls are near THIS address" one origin at a time; the filter
asks every one of ~2.4k developments and ~9.6k HDB blocks at once, which is a
haversine loop over a small list — exactly what the MRT filter already does
over `mrt_cache.json`'s 123 stations.

Malls open and close a few times a year, so the list is read once per process
and never checked for freshness; rerun the script when one does.

One caveat for a rerun: a handful of malls are indexed by their tenants, so
OneMap can answer the same query with a different shop inside the same
building from one run to the next. That moves a coordinate by a few tens of
metres — Joo Chiat Complex by 95 m across two runs — which is noise against
the 400 m band the filter's finest step uses, but it does mean a rerun shows
small diffs on entries nobody touched.
"""

import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

MALLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "malls.json")

_malls: list | None = None


def load_malls() -> list:
    """The register: [{name, lat, lng, address, postal}], or [] if the file is
    missing or unreadable — a missing register drops `mall_m` and its filter
    rather than failing the layer it rides on."""
    global _malls
    if _malls is None:
        try:
            with open(MALLS_PATH) as f:
                _malls = json.load(f)
        except Exception as e:
            logger.warning(f"[Malls] Could not read {MALLS_PATH}: {e}")
            _malls = []
    return _malls


def mall_coords() -> list:
    """[(lat, lng)] — the form the distance loop wants."""
    return [(m["lat"], m["lng"]) for m in load_malls()
            if m.get("lat") is not None and m.get("lng") is not None]


_fingerprint: str | None = None


def register_fingerprint() -> str:
    """A short content hash of the register, or "" when there is none.

    The explore layers are derived from this file as much as they are from the
    transaction caches — `mall_m` is on every dot — so it belongs in the key
    those payloads are stored under. Without it, adding a mall would change
    nothing on the map until URA happened to refresh (a month, on the HDB
    layer, whose cache moves that slowly).

    Content, not mtime: a checkout or a redeploy rewrites mtime without
    changing a single mall, and that would rebuild both layers for nothing.
    """
    global _fingerprint
    if _fingerprint is None:
        malls = load_malls()
        _fingerprint = (hashlib.sha256(
            json.dumps(malls, sort_keys=True).encode()).hexdigest()[:12]
            if malls else "")
    return _fingerprint
