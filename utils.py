"""
Shared utilities used across the entire bot.
Single source of truth for size bands, MongoDB connections,
haversine distance, OneMap token, date helpers, and cache staleness logic.
"""
import os
import re
import time
import logging
from datetime import datetime, date, timedelta
from math import radians, sin, cos, sqrt, atan2
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")

logger = logging.getLogger(__name__)

# ── Size bands ────────────────────────────────────────────────────────────────
# Edit here only — changes propagate to ura.py, rental.py automatically.
SIZE_BANDS = [
    {"label": "<= 600 sqft",      "min": 0,    "max": 600},
    {"label": "601 – 700 sqft",   "min": 601,  "max": 700},
    {"label": "701 – 800 sqft",   "min": 701,  "max": 800},
    {"label": "801 – 900 sqft",   "min": 801,  "max": 900},
    {"label": "901 – 1000 sqft",  "min": 901,  "max": 1000},
    {"label": "1001 – 1100 sqft", "min": 1001, "max": 1100},
    {"label": "1101 – 1200 sqft", "min": 1101, "max": 1200},
    {"label": "> 1200 sqft",      "min": 1201, "max": float("inf")},
]


def get_band(sqft: float) -> str | None:
    """Return the size band label for a given sqft value."""
    for band in SIZE_BANDS:
        if band["min"] <= sqft <= band["max"]:
            return band["label"]
    return None


# ── HDB flat types ──────────────────────────────────────────────────────────
# Ordered for display, parallel to SIZE_BANDS — HDB resale results are grouped
# by flat type the way private transactions are grouped by size band. Values
# match data.gov.sg's `flat_type` field exactly. Sourced from the live dataset
# (Jun 2026): the 7 types below are the complete current set.
FLAT_TYPES = [
    "1 ROOM",
    "2 ROOM",
    "3 ROOM",
    "4 ROOM",
    "5 ROOM",
    "EXECUTIVE",
    "MULTI-GENERATION",
]

# HDB street-name abbreviations → full words. Shared so both the resale search
# layer (hdb.py) and the postal-routing block check (cache_hdb.py) canonicalise
# streets the same way — OneMap spells out "AVENUE" while HDB data uses "AVE".
STREET_ABBREV = {
    "AVE": "AVENUE", "AVENUE": "AVENUE",
    "ST": "STREET", "STREET": "STREET",
    "RD": "ROAD", "ROAD": "ROAD",
    "DR": "DRIVE", "DRIVE": "DRIVE",
    "CRES": "CRESCENT", "CRESCENT": "CRESCENT",
    "CL": "CLOSE", "CLOSE": "CLOSE",
    "CTRL": "CENTRAL", "CENTRAL": "CENTRAL",
    "BT": "BUKIT", "BUKIT": "BUKIT",
    "JLN": "JALAN", "JALAN": "JALAN",
    "LOR": "LORONG", "LORONG": "LORONG",
    "NTH": "NORTH", "NORTH": "NORTH",
    "STH": "SOUTH", "SOUTH": "SOUTH",
    "UPP": "UPPER", "UPPER": "UPPER",
    "GDNS": "GARDENS", "GARDENS": "GARDENS",
    "TER": "TERRACE", "TERRACE": "TERRACE",
    "PL": "PLACE", "PLACE": "PLACE",
}


def canon_street_tokens(text) -> list:
    """Uppercase a street/road string, strip punctuation, and expand HDB
    abbreviations to full words. Returns the significant tokens so two spellings
    of the same road compare equal (e.g. "ANG MO KIO AVE 6" ↔ "...AVENUE 6")."""
    import re as _re
    toks = _re.sub(r"[^A-Z0-9 ]", " ", str(text).upper()).split()
    return [STREET_ABBREV.get(t, t) for t in toks]


def parse_remaining_lease(text) -> float | None:
    """Parse an HDB remaining-lease string to a number of years (float).

    Handles the two forms data.gov.sg emits — "61 years 04 months" → 61.33
    and "70 years" → 70.0 — plus the bare integer ("70") used by older eras.
    Returns None if unparseable.
    """
    if text is None:
        return None
    s = str(text).strip().lower()
    if not s:
        return None
    yrs = re.search(r"(\d+)\s*year", s)
    mos = re.search(r"(\d+)\s*month", s)
    if yrs or mos:
        y = int(yrs.group(1)) if yrs else 0
        m = int(mos.group(1)) if mos else 0
        return round(y + m / 12, 2)
    try:
        return float(s)  # bare number = years
    except ValueError:
        return None


# ── Unit conversions ──────────────────────────────────────────────────────────

def sqm_to_sqft(sqm: float) -> float:
    return sqm * 10.7639


def parse_float(value) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_sqft_range(area_sqft_str: str) -> float | None:
    """
    Parse URA rental area range string to midpoint in sqft.
    e.g. "600-700" -> 650.0
    """
    try:
        parts = str(area_sqft_str).strip().split("-")
        if len(parts) == 2:
            return (float(parts[0]) + float(parts[1])) / 2
        return float(parts[0])
    except (ValueError, AttributeError):
        return None


# ── Date helpers ──────────────────────────────────────────────────────────────

def parse_mmyy_date(date_str: str) -> datetime | None:
    """Parse URA MMYY date format. e.g. "0921" -> datetime(2021, 9, 1)"""
    try:
        date_str = str(date_str).strip()
        if len(date_str) == 4:
            mm = int(date_str[:2])
            yy = int(date_str[2:])
            return datetime(2000 + yy, mm, 1)
        return None
    except (ValueError, TypeError):
        return None


def format_mmyy_date(date_str: str) -> str:
    """Convert MMYY to human-readable e.g. '0921' -> 'Sep 2021'"""
    dt = parse_mmyy_date(date_str)
    return dt.strftime("%b %Y") if dt else date_str


# ── Geospatial ────────────────────────────────────────────────────────────────

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line distance in metres between two lat/lng coordinates."""
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


# ── MongoDB ───────────────────────────────────────────────────────────────────

_mongo_client = None
_mongo_db = None

def get_mongo_db():
    """
    Return a shared MongoDB database instance.
    Creates the connection once and reuses it across all modules.
    """
    global _mongo_client, _mongo_db
    if _mongo_db is not None:
        return _mongo_db

    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        logger.warning("[MongoDB] MONGO_URI not set")
        return None
    try:
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi
        _mongo_client = MongoClient(
            mongo_uri,
            server_api=ServerApi('1'),
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
        )
        _mongo_db = _mongo_client['property_bot']
        logger.info("[MongoDB] Connected")
        return _mongo_db
    except Exception as e:
        logger.error(f"[MongoDB] Connection failed: {e}")
        return None


def clear_mongo_collection(collection_name: str):
    """Delete all documents in a collection (used by refresh_job)."""
    db = get_mongo_db()
    if db is None:
        return False
    try:
        db[collection_name].delete_many({})
        logger.info(f"[MongoDB] Cleared {collection_name}")
        return True
    except Exception as e:
        logger.error(f"[MongoDB] Failed to clear {collection_name}: {e}")
        return False


# ── OneMap token ──────────────────────────────────────────────────────────────

_onemap_token = None
_onemap_token_expiry = 0

ONEMAP_AUTH_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"


def get_onemap_token() -> str | None:
    """
    Return a valid OneMap token, refreshing automatically when expired.
    Shared across onemap_mrt.py, schools_cache.py, and maps.py.
    """
    global _onemap_token, _onemap_token_expiry

    if _onemap_token and time.time() < _onemap_token_expiry - 300:
        return _onemap_token

    email = os.getenv("ONEMAP_EMAIL")
    password = os.getenv("ONEMAP_PASSWORD")
    if not email or not password:
        logger.warning("[OneMap] No credentials in environment")
        return None

    try:
        import requests
        r = requests.post(
            ONEMAP_AUTH_URL,
            json={"email": email, "password": password},
            timeout=10
        )
        data = r.json()
        if "access_token" in data:
            _onemap_token = data["access_token"]
            _onemap_token_expiry = int(data.get("expiry_timestamp", time.time() + 28800))
            logger.info("[OneMap] Token refreshed")
            return _onemap_token
        logger.error(f"[OneMap] Auth failed: {data}")
        return None
    except Exception as e:
        logger.error(f"[OneMap] Auth error: {e}")
        return None

# ── Cache staleness helpers ───────────────────────────────────────────────────
#
# These replace simple age-based checks with release-calendar-aware logic.
#
# URA transactions: published every Tuesday and Friday at 09:00 SGT.
# URA rental contracts: published on the 15th of each month at 09:00 SGT.
# Both shift to the next working day if the nominal date is a public holiday.
#
# SG public holidays sourced from MOM gazetted list.
# Update _HOLIDAYS each year — Hari Raya dates are provisional until confirmed.

def sg_public_holidays(year: int) -> set[date]:
    """
    Return the set of Singapore public holiday dates for the given year,
    including in-lieu Mondays when a holiday falls on Sunday.

    Covers 2026. Extend _HOLIDAYS for subsequent years.
    Hari Raya Puasa and Haji dates are provisional; update when MOM confirms.
    """
    _HOLIDAYS: dict[int, list[date]] = {
        2026: [
            date(2026, 1, 1),   # New Year's Day (Thu)
            date(2026, 2, 17),  # Chinese New Year Day 1 (Tue)
            date(2026, 2, 18),  # Chinese New Year Day 2 (Wed)
            date(2026, 3, 21),  # Hari Raya Puasa — provisional (Sat, no weekday impact)
            date(2026, 4, 3),   # Good Friday (Fri)
            date(2026, 5, 1),   # Labour Day (Fri)
            date(2026, 5, 27),  # Hari Raya Haji — provisional (Wed)
            date(2026, 5, 31),  # Vesak Day (Sun)
            date(2026, 6, 1),   # Vesak Day in-lieu (Mon)
            date(2026, 8, 9),   # National Day (Sun)
            date(2026, 8, 10),  # National Day in-lieu (Mon)
            date(2026, 11, 8),  # Deepavali (Sun)
            date(2026, 11, 9),  # Deepavali in-lieu (Mon)
            date(2026, 12, 25), # Christmas Day (Fri)
        ],
    }
    return set(_HOLIDAYS.get(year, []))


def next_working_day(d: date) -> date:
    """
    Return d itself if it is a working day (Mon–Fri, not a SG public holiday).
    Otherwise advance day-by-day until the next working day.
    """
    holidays = sg_public_holidays(d.year)
    while d.weekday() >= 5 or d in holidays:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
        holidays = sg_public_holidays(d.year)  # refresh in case we rolled into a new year
    return d


def _release_occurred(nominal_day: date, release_hour: int,
                       since_sgt: datetime, now_sgt: datetime) -> bool:
    """
    Return True if a release scheduled on nominal_day at release_hour SGT
    has occurred after since_sgt and no later than now_sgt.
    Applies next_working_day shift for holidays/weekends.
    """
    actual = next_working_day(nominal_day)
    release_dt = datetime(actual.year, actual.month, actual.day, release_hour, 0, tzinfo=SGT)
    return since_sgt < release_dt <= now_sgt


def is_ura_transactions_stale(last_refresh_ts: float) -> bool:
    """
    Return True if URA transaction data needs refreshing.

    URA publishes new transaction data every Tuesday and Friday at 09:00 SGT.
    Releases shift to the next working day if the date is a public holiday.
    Returns True if at least one such release has occurred since last_refresh_ts.
    """
    now_sgt = datetime.now(SGT)
    since_sgt = datetime.fromtimestamp(last_refresh_ts, tz=SGT)
    check = since_sgt.date()
    while check <= now_sgt.date():
        if check.weekday() in (1, 4):  # Tuesday=1, Friday=4
            if _release_occurred(check, 9, since_sgt, now_sgt):
                return True
        check += timedelta(days=1)
    return False


def is_hdb_resale_stale(last_refresh_ts: float) -> bool:
    """
    Return True if HDB resale data needs refreshing.

    HDB resale prices (data.gov.sg CKAN) update roughly monthly, early in the
    month. Unlike URA, there is no fixed published release time, so we use a
    simple calendar-month heuristic: the cache is stale once the SGT calendar
    month has advanced past the month it was last refreshed in. This avoids
    over-fetching; intra-month corrections are picked up on the next month's
    first query.
    """
    now_sgt = datetime.now(SGT)
    since_sgt = datetime.fromtimestamp(last_refresh_ts, tz=SGT)
    return (now_sgt.year, now_sgt.month) > (since_sgt.year, since_sgt.month)


def is_rental_stale(last_refresh_ts: float) -> bool:
    """
    Return True if rental contract data needs refreshing.

    URA publishes rental contracts on the 15th of each month at 09:00 SGT.
    Releases shift to the next working day if the 15th is a public holiday or weekend.
    Returns True if at least one such release has occurred since last_refresh_ts.
    """
    now_sgt = datetime.now(SGT)
    since_sgt = datetime.fromtimestamp(last_refresh_ts, tz=SGT)
    for delta_months in range(3):  # check current month + next 2 (handles long cache gaps)
        year = since_sgt.year
        month = since_sgt.month + delta_months
        while month > 12:
            month -= 12
            year += 1
        if _release_occurred(date(year, month, 15), 9, since_sgt, now_sgt):
            return True
    return False
