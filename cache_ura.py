import os
import json
import time
import requests
from dotenv import load_dotenv

load_dotenv()

URA_API_KEY = os.getenv("URA_API_KEY")
URA_TOKEN_URL = "https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1"
URA_TRANSACTIONS_BASE_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Transaction&batch="
URA_PIPELINE_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Pipeline"

CACHE_FILE = "ura_cache.json"
CACHE_MAX_AGE_HOURS = 48  # URA updates Tue/Fri so 48h is safe


def _get_token() -> str | None:
    headers = {"AccessKey": URA_API_KEY, "User-Agent": "PropertyBot/1.0"}
    try:
        r = requests.get(URA_TOKEN_URL, headers=headers, timeout=10)
        data = r.json()
        if data.get("Status") == "Success":
            return data["Result"]
        print(f"[URA Cache] Token error: {data}")
        return None
    except Exception as e:
        print(f"[URA Cache] Token failed: {e}")
        return None


def _fetch_all_transactions(token: str) -> list:
    headers = {
        "AccessKey": URA_API_KEY,
        "Token": token,
        "User-Agent": "PropertyBot/1.0",
    }
    all_results = []
    for batch in range(1, 5):
        url = f"{URA_TRANSACTIONS_BASE_URL}{batch}"
        try:
            r = requests.get(url, headers=headers, timeout=30)
            data = r.json()
            if data.get("Status") == "Success":
                results = data.get("Result", [])
                all_results.extend(results)
                print(f"[URA Cache] Batch {batch}: {len(results)} projects")
            else:
                print(f"[URA Cache] Batch {batch} error: {data}")
        except Exception as e:
            print(f"[URA Cache] Batch {batch} failed: {e}")
    return all_results


def _fetch_pipeline(token: str) -> list:
    headers = {
        "AccessKey": URA_API_KEY,
        "Token": token,
        "User-Agent": "PropertyBot/1.0",
    }
    try:
        r = requests.get(URA_PIPELINE_URL, headers=headers, timeout=15)
        data = r.json()
        if data.get("Status") == "Success":
            return data.get("Result", [])
        return []
    except Exception as e:
        print(f"[URA Cache] Pipeline failed: {e}")
        return []


def _is_cache_fresh() -> bool:
    if not os.path.exists(CACHE_FILE):
        return False
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        age_hours = (time.time() - data.get("timestamp", 0)) / 3600
        return age_hours < CACHE_MAX_AGE_HOURS
    except Exception:
        return False


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(transactions: list, pipeline: list):
    data = {
        "timestamp": time.time(),
        "transactions": transactions,
        "pipeline": pipeline,
    }
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
        size_mb = os.path.getsize(CACHE_FILE) / 1024 / 1024
        print(f"[URA Cache] Saved — {len(transactions)} projects, {size_mb:.1f}MB")
    except Exception as e:
        print(f"[URA Cache] Save failed: {e}")


def get_ura_data() -> tuple[list, list]:
    """
    Return (transactions, pipeline) from local cache.
    Refreshes automatically if cache is stale or missing.
    """
    if _is_cache_fresh():
        data = _load_cache()
        print("[URA Cache] Using cached data")
        return data.get("transactions", []), data.get("pipeline", [])

    print("[URA Cache] Cache stale or missing — refreshing from URA API...")
    token = _get_token()
    if not token:
        # If we can't refresh, try to use stale cache rather than failing
        if os.path.exists(CACHE_FILE):
            print("[URA Cache] Using stale cache as fallback")
            data = _load_cache()
            return data.get("transactions", []), data.get("pipeline", [])
        return [], []

    transactions = _fetch_all_transactions(token)
    pipeline = _fetch_pipeline(token)

    if transactions:
        _save_cache(transactions, pipeline)

    return transactions, pipeline


def force_refresh() -> bool:
    """Force a cache refresh regardless of age. Returns True on success."""
    print("[URA Cache] Force refreshing...")
    token = _get_token()
    if not token:
        return False
    transactions = _fetch_all_transactions(token)
    pipeline = _fetch_pipeline(token)
    if transactions:
        _save_cache(transactions, pipeline)
        return True
    return False


def cache_status() -> dict:
    """Return info about the current cache state."""
    if not os.path.exists(CACHE_FILE):
        return {"status": "missing"}
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        age_hours = (time.time() - data.get("timestamp", 0)) / 3600
        size_mb = os.path.getsize(CACHE_FILE) / 1024 / 1024
        return {
            "status": "fresh" if age_hours < CACHE_MAX_AGE_HOURS else "stale",
            "age_hours": round(age_hours, 1),
            "projects": len(data.get("transactions", [])),
            "size_mb": round(size_mb, 1),
        }
    except Exception:
        return {"status": "corrupt"}
