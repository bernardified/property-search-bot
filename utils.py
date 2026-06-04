"""
Shared utilities used across the entire bot.
Single source of truth for size bands, MongoDB connections,
haversine distance, OneMap token, and date helpers.
"""
import os
import time
import logging
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2

logger = logging.getLogger(__name__)

# ── Size bands ────────────────────────────────────────────────────────────────
# Edit here only — changes propagate to ura.py, rental.py automatically.
SIZE_BANDS = [
    {"label": "<= 600 sqft",      "min": 0,    "max": 600},
    {"label": "601 – 700 sqft",   "min": 601,  "max": 700},
    {"label": "701 – 800 sqft",   "min": 701,  "max": 800},
    {"label": "801 – 900 sqft",   "min": 801,  "max": 900},
    {"label": "901 – 1000 sqft",  "min": 901,  "max": 1000},
    {"label": "> 1000 sqft",      "min": 1001, "max": float("inf")},
]


def get_band(sqft: float) -> str | None:
    """Return the size band label for a given sqft value."""
    for band in SIZE_BANDS:
        if band["min"] <= sqft <= band["max"]:
            return band["label"]
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
