"""
HDB resale search & formatting — the public-housing parallel to the URA stack
(ura.py + rental.py + district_search.py), kept as a separate module set rather
than bolted onto ura.py.

Data is the data.gov.sg resale flat-price feed served from cache_hdb. Each raw
record carries: month (YYYY-MM), town, flat_type, block, street_name,
storey_range, floor_area_sqm, flat_model, lease_commence_date, remaining_lease
("61 years 04 months"), resale_price.

Three discovery paths, mirroring the private side:
  • Browse by town   → town_overview()   (cf. district_search.get_top_*)
  • Street lookup     → street_summary()  (per-flat-type aggregate + block list)
  • Block lookup      → block_detail()    (cf. ura.search_property)
resolve_query() classifies free text into a block vs. street lookup and surfaces
disambiguation candidates, mirroring ura.py's ambiguity contract.

Results are grouped by FLAT_TYPE (parallel to how the private side groups by
SIZE_BAND). HDB-specific signals surfaced: remaining lease (lease decay) and PSF.

Pure functions accept `records=` (and `now=`) so tests never hit the network;
the cache is loaded lazily only when `records` is omitted.
"""
import re
import difflib
import logging
from statistics import median
from datetime import datetime
from dateutil.relativedelta import relativedelta
from cache.cache_hdb import get_hdb_resale_data
from utils import FLAT_TYPES, STREET_ABBREV, sqm_to_sqft, parse_float, parse_remaining_lease

logger = logging.getLogger(__name__)

# Default analysis window for aggregates. HDB blocks transact infrequently, so
# this is wider than the private side's 12-month windows; the cache itself holds
# a 36-month rolling window, so this is the effective ceiling.
DEFAULT_WINDOW_MONTHS = 12

# Street-name abbreviations (shared via utils.STREET_ABBREV) are expanded on
# BOTH the query and the data so a user typing "avenue" matches data that stores
# "AVE" (and vice-versa).

# A leading HDB block token, e.g. "406", "216A", "1B".
_BLOCK_RE = re.compile(r"^\d+[A-Z]?$")


# ── Normalisation ───────────────────────────────────────────────────────────

def _load() -> list:
    return get_hdb_resale_data()


def _norm(rec: dict) -> dict | None:
    """Normalise one raw resale record into typed fields. None if unusable."""
    price = parse_float(rec.get("resale_price"))
    area_sqm = parse_float(rec.get("floor_area_sqm"))
    if not price or not area_sqm or area_sqm <= 0:
        return None
    area_sqft = sqm_to_sqft(area_sqm)
    month = str(rec.get("month", "")).strip()
    try:
        y, m = month.split("-")
        month_dt = datetime(int(y), int(m), 1)
    except (ValueError, AttributeError):
        month_dt = None
    return {
        "town": str(rec.get("town", "")).strip().upper(),
        "flat_type": str(rec.get("flat_type", "")).strip().upper(),
        "block": str(rec.get("block", "")).strip().upper(),
        "street": str(rec.get("street_name", "")).strip().upper(),
        "storey_range": str(rec.get("storey_range", "")).strip(),
        "flat_model": str(rec.get("flat_model", "")).strip(),
        "area_sqft": area_sqft,
        "price": price,
        "psf": round(price / area_sqft) if area_sqft else None,
        "month": month,
        "month_dt": month_dt,
        "lease_years": parse_remaining_lease(rec.get("remaining_lease")),
    }


def _normalise_all(records: list | None) -> list:
    """Load (if needed) and normalise records, dropping unusable rows."""
    raw = records if records is not None else _load()
    out = []
    for r in raw:
        n = _norm(r)
        if n:
            out.append(n)
    return out


def _recent(rows: list, months: int, now: datetime | None = None) -> list:
    """Filter normalised rows to the last `months`. Falls back to all rows
    when the window is empty (sparse blocks), so a result is never blank."""
    if months <= 0:
        return rows
    now = now or datetime.now()
    cutoff = now.replace(day=1) - relativedelta(months=months)
    windowed = [r for r in rows if r["month_dt"] and r["month_dt"] >= cutoff]
    return windowed or rows


# ── Street / town canonicalisation ──────────────────────────────────────────

def _canon_tokens(text: str) -> list[str]:
    """Uppercase, expand street abbreviations, return significant tokens."""
    toks = re.sub(r"[^A-Z0-9 ]", " ", str(text).upper()).split()
    return [STREET_ABBREV.get(t, t) for t in toks]


def expand_street(street: str) -> str:
    """Expand a street's abbreviations to full words ("ANG MO KIO AVE 10" →
    "ANG MO KIO AVENUE 10"). Used to give OneMap a fuller string to geocode."""
    return " ".join(_canon_tokens(street))


# ── Towns (browse) — mirrors district_search ────────────────────────────────

def hdb_towns(records: list | None = None) -> list[str]:
    """Distinct towns present in the data, sorted. Sourced from the data (not
    hardcoded), so a new town (e.g. Tengah) appears automatically once its
    first resale registers."""
    rows = _normalise_all(records)
    return sorted({r["town"] for r in rows if r["town"]})


def town_index(town: str, records: list | None = None) -> int | None:
    """1-based index of a town in the sorted list (for the t<NN> deep link)."""
    towns = hdb_towns(records)
    try:
        return towns.index(town.strip().upper()) + 1
    except ValueError:
        return None


def town_by_index(idx: int, records: list | None = None) -> str | None:
    """Town name for a 1-based index, or None if out of range."""
    towns = hdb_towns(records)
    return towns[idx - 1] if 1 <= idx <= len(towns) else None


# ── Aggregation ─────────────────────────────────────────────────────────────

def _aggregate_by_flat_type(rows: list) -> dict:
    """Group normalised rows by flat type → median price, count, avg PSF,
    typical (median) remaining lease, and the latest transaction."""
    out: dict[str, dict] = {}
    for ft in FLAT_TYPES:
        ft_rows = [r for r in rows if r["flat_type"] == ft]
        if not ft_rows:
            continue
        prices = [r["price"] for r in ft_rows]
        psfs = [r["psf"] for r in ft_rows if r["psf"]]
        leases = [r["lease_years"] for r in ft_rows if r["lease_years"] is not None]
        latest = max(ft_rows, key=lambda r: r["month_dt"] or datetime.min)
        out[ft] = {
            "count": len(ft_rows),
            "median_price": round(median(prices)),
            "avg_psf": round(sum(psfs) / len(psfs)) if psfs else None,
            "typical_lease": round(median(leases)) if leases else None,
            "latest": latest,
        }
    return out


# ── Town overview (browse target) ───────────────────────────────────────────

def town_overview(town: str, records: list | None = None,
                  months: int = DEFAULT_WINDOW_MONTHS, now: datetime | None = None) -> dict:
    """Per-flat-type resale snapshot for a whole town (the Browse-by-town
    target). Mirrors district_search's town-level discovery."""
    rows = [r for r in _normalise_all(records) if r["town"] == town.strip().upper()]
    if not rows:
        return {"error": f'No HDB resale data for "{town.title()}".'}
    windowed = _recent(rows, months, now)
    return {
        "town": town.strip().upper(),
        "flat_types": _aggregate_by_flat_type(windowed),
        "total_txns": len(windowed),
        "window_months": months,
    }


# ── Query classification ────────────────────────────────────────────────────

def resolve_query(query: str, records: list | None = None) -> dict:
    """Classify a free-text block/street query and resolve it against the data.

    Returns one of (mirroring ura.py's contract):
      {"error": str}
      {"ambiguous": True, "candidates": [street, ...]}   — several streets match
      {"kind": "block",  "block": str, "street": str}
      {"kind": "street", "street": str}
    """
    rows = _normalise_all(records)
    if not rows:
        return {"error": "HDB resale data unavailable. Please try again later."}

    toks = _canon_tokens(query)
    if not toks:
        return {"error": "Please enter a block and/or street, e.g. '406 Ang Mo Kio Ave 10'."}

    block = toks[0] if _BLOCK_RE.match(toks[0]) else None
    street_toks = toks[1:] if block else toks
    if not street_toks:
        return {"error": "Please include a street name, e.g. '406 Ang Mo Kio Ave 10'."}

    street_set = set(street_toks)
    # Map canonical street tokens → the original (data) street string.
    streets: dict[str, str] = {}
    for r in rows:
        streets.setdefault(r["street"], " ".join(_canon_tokens(r["street"])))

    # Streets whose token set is a superset of the query's tokens.
    matches = [orig for orig, canon in streets.items() if street_set <= set(canon.split())]

    if not matches:
        # Fuzzy fallback on the full canonical street string.
        q = " ".join(street_toks)
        scored = sorted(
            ((difflib.SequenceMatcher(None, q, canon).ratio(), orig)
             for orig, canon in streets.items()),
            reverse=True,
        )
        if scored and scored[0][0] >= 0.6:
            best = scored[0][0]
            matches = [orig for s, orig in scored if s >= best - 0.05]
        else:
            return {"error": f'No HDB blocks found for "{query}".'}

    if block:
        # Narrow to streets that actually have this block.
        with_block = [s for s in matches if any(
            r["block"] == block and r["street"] == s for r in rows)]
        if len(with_block) == 1:
            return {"kind": "block", "block": block, "street": with_block[0]}
        if not with_block:
            return {"error": f'No block {block} found on a matching street for "{query}".'}
        return {"ambiguous": True, "block": block, "candidates": with_block}

    if len(matches) == 1:
        return {"kind": "street", "street": matches[0]}
    return {"ambiguous": True, "block": None, "candidates": sorted(matches)[:8]}


# ── Street summary ──────────────────────────────────────────────────────────

def street_summary(street: str, records: list | None = None,
                   months: int = DEFAULT_WINDOW_MONTHS, now: datetime | None = None) -> dict:
    """Per-flat-type aggregate across all blocks on a street, plus the list of
    distinct blocks (for follow-up buttons). The street-only result that 'does
    both' — aggregate text + block buttons."""
    target = street.strip().upper()
    rows = [r for r in _normalise_all(records) if r["street"] == target]
    if not rows:
        return {"error": f'No HDB resale data for "{street.title()}".'}
    windowed = _recent(rows, months, now)

    blocks: dict[str, int] = {}
    for r in windowed:
        blocks[r["block"]] = blocks.get(r["block"], 0) + 1

    return {
        "street": target,
        "town": windowed[0]["town"],
        "flat_types": _aggregate_by_flat_type(windowed),
        "blocks": sorted(blocks.items(), key=lambda kv: (-kv[1], _block_sort_key(kv[0]))),
        "total_txns": len(windowed),
        "window_months": months,
    }


def _block_sort_key(block: str):
    """Sort blocks numerically then by letter suffix: 5, 5A, 12, 216, 216A."""
    m = re.match(r"(\d+)([A-Z]?)", block)
    return (int(m.group(1)), m.group(2)) if m else (10**9, block)


# ── Block detail ────────────────────────────────────────────────────────────

def block_detail(block: str, street: str, records: list | None = None) -> dict:
    """Per-flat-type detail for a single block (cf. ura.search_property).

    Uses the full cached window (no recency filter): a specific block transacts
    rarely, so showing the most recent sale per flat type matters more than
    restricting to the last 12 months."""
    b, s = block.strip().upper(), street.strip().upper()
    rows = [r for r in _normalise_all(records) if r["block"] == b and r["street"] == s]
    if not rows:
        return {"error": f'No resale transactions for Block {block} {street.title()}.'}
    return {
        "block": b,
        "street": s,
        "town": rows[0]["town"],
        "flat_types": _aggregate_by_flat_type(rows),
        "total_txns": len(rows),
    }


# ── Formatting (Telegram Markdown, parallel to ura.format_transactions) ──────

def _lease_str(years: float | None) -> str:
    if years is None:
        return ""
    whole = int(years)
    months = round((years - whole) * 12)
    return f"{whole}y {months}m" if months else f"{whole}y"


def _window_label(months: int) -> str:
    if months <= 0:
        return "all cached transactions"
    if months % 12 == 0:
        yrs = months // 12
        return f"last {yrs} year{'s' if yrs != 1 else ''}"
    return f"last {months} months"


def _format_flat_type_block(flat_types: dict, show_latest: bool) -> list[str]:
    """Render the per-flat-type aggregate body shared by all three views."""
    lines = []
    for ft in FLAT_TYPES:
        data = flat_types.get(ft)
        if not data:
            continue
        psf_str = f" · S${data['avg_psf']:,} psf" if data.get("avg_psf") else ""
        lease_str = f" · 🔑 {_lease_str(data['typical_lease'])} lease" if data.get("typical_lease") else ""
        line = (
            f"_{ft.title()}_\n"
            f"  💵 Median S${data['median_price']:,}{psf_str}\n"
            f"  📊 {data['count']} txn{'s' if data['count'] != 1 else ''}{lease_str}"
        )
        if show_latest and data.get("latest"):
            lt = data["latest"]
            storey = f" · Storey {lt['storey_range']}" if lt.get("storey_range") else ""
            line += f"\n  📅 Latest: S${int(lt['price']):,} ({lt['month']}){storey}"
        lines.append(line)
        lines.append("")
    return lines


def format_town_overview(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']}"
    town = result["town"].title()
    win = _window_label(result.get("window_months", DEFAULT_WINDOW_MONTHS))
    lines = [
        f"🏠 *{town} — HDB resale*",
        f"_Median price by flat type · {win} · {result['total_txns']} txns_",
        "─────────────────────",
        "",
    ]
    body = _format_flat_type_block(result["flat_types"], show_latest=False)
    if not body:
        return f"🏠 *{town} — HDB resale*\n\nNo resale transactions in the {win}."
    lines += body
    return "\n".join(lines)


def format_street_summary(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']}"
    street = result["street"].title()
    town = result["town"].title()
    win = _window_label(result.get("window_months", DEFAULT_WINDOW_MONTHS))
    n_blocks = len(result.get("blocks", []))
    lines = [
        f"🏠 *{street}*",
        f"📍 {town} · {n_blocks} block{'s' if n_blocks != 1 else ''} transacted",
        f"_Median by flat type · {win} · {result['total_txns']} txns_",
        "─────────────────────",
        "",
    ]
    lines += _format_flat_type_block(result["flat_types"], show_latest=False)
    lines.append("_Tap a block below for its own transactions._")
    return "\n".join(lines)


def format_block_detail(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']}"
    block = result["block"]
    street = result["street"].title()
    town = result["town"].title()
    lines = [
        f"🏠 *Block {block} {street}*",
        f"📍 {town} · {result['total_txns']} resale txns on record",
        "─────────────────────",
        "",
    ]
    lines += _format_flat_type_block(result["flat_types"], show_latest=True)
    return "\n".join(lines)
