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
