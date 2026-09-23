"""PropertyGuru listing links.

PropertyGuru has no public API and blocks scrapers, so we never fetch or count
listings (cf. the ToS note in CLAUDE.md). Instead we construct search-result
deep links pre-filtered by project name (freetext) and bedroom count; the user
taps through and PropertyGuru's own app/site runs the search.

Pure functions only — no IO. Easy to unit-test.
"""

from urllib.parse import quote_plus

BASE = "https://www.propertyguru.com.sg"

# (label, beds[] values to emit). PropertyGuru's bedroom filter takes a
# repeatable `beds[]` param; the 4+ bucket fans out to cover larger units.
BED_BUCKETS = [
    ("Studio", [0]),
    ("1 BR", [1]),
    ("2 BR", [2]),
    ("3 BR", [3]),
    ("4+ BR", [4, 5, 6]),
]


def _url(path: str, project_name: str, beds: list[int]) -> str:
    beds_q = "".join(f"&beds[]={b}" for b in beds)
    return f"{BASE}/{path}?freetext={quote_plus(project_name)}{beds_q}"


def listing_links(project_name: str) -> list[tuple[str, str, str]]:
    """Return [(bucket_label, sale_url, rent_url), ...], one row per bedroom bucket.

    Freetext on the project name is the only selector available without an API,
    so links are best-effort: a bucket with no matching units simply lands on an
    empty PropertyGuru result page.
    """
    return [
        (
            label,
            _url("property-for-sale", project_name, beds),
            _url("property-for-rent", project_name, beds),
        )
        for label, beds in BED_BUCKETS
    ]


# ── HDB ──────────────────────────────────────────────────────────────────────
#
# PropertyGuru's generic search drops a flat-type filter passed in the query
# string (propertyTypeCode is normalised back to every HDB type), but it has a
# landing page per flat type that does filter and still takes freetext — so
# HDB links are built on those slugs rather than on a bedroom count. EXECUTIVE
# is two pages there (apartments `EA`, maisonettes `EM`) where resale data has
# one type; there is no multi-generation page at all, which the "All flat
# types" row covers. Freetext is address-near, not exact: a block search also
# lists its neighbours, which for someone comparing flats is no bad thing.

HDB_SLUGS = {
    "1 ROOM": [("1 Room", "hdb-1-room-flat")],
    "2 ROOM": [("2 Room", "hdb-2-room-flat")],
    "3 ROOM": [("3 Room", "hdb-3-room-flat")],
    "4 ROOM": [("4 Room", "hdb-4-room-flat")],
    "5 ROOM": [("5 Room", "hdb-5-room-flat")],
    "EXECUTIVE": [("Executive apartment", "hdb-executive-apartment"),
                  ("Executive maisonette", "hdb-executive-maisonette")],
}


def hdb_listing_links(address: str, flat_types: list[str]) -> list[tuple[str, str, str]]:
    """[(label, sale_url, rent_url), ...] for the flat types a block or street
    actually has (in the order given), then one all-types row for the address."""
    q = quote_plus(address)
    rows = [
        (label, f"{BASE}/{slug}-for-sale?freetext={q}", f"{BASE}/{slug}-for-rent?freetext={q}")
        for ft in flat_types for label, slug in HDB_SLUGS.get(ft, [])
    ]
    rows.append(("All flat types", f"{BASE}/hdb-for-sale?freetext={q}",
                 f"{BASE}/hdb-for-rent?freetext={q}"))
    return rows
