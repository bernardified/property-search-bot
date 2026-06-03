import json
import os
from datetime import datetime

STORAGE_FILE = "search_history.json"


def _load() -> dict:
    """Load storage file, return empty structure if not found."""
    if not os.path.exists(STORAGE_FILE):
        return {"searches": []}
    try:
        with open(STORAGE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"searches": []}


def _save(data: dict):
    """Save data to storage file."""
    try:
        with open(STORAGE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except IOError as e:
        print(f"[Storage] Failed to save: {e}")


def record_search(user_id: int, username: str, query: str, resolved_name: str):
    """
    Record a search to persistent storage.
    - query: what the user typed
    - resolved_name: what URA matched it to
    """
    data = _load()

    entry = {
        "user_id": user_id,
        "username": username or "unknown",
        "query": query,
        "resolved_name": resolved_name,
        "timestamp": datetime.now().isoformat(),
    }

    data["searches"].append(entry)

    # Keep only last 500 searches to prevent file bloat
    if len(data["searches"]) > 500:
        data["searches"] = data["searches"][-500:]

    _save(data)


def get_recent_searches(limit: int = 10) -> list:
    """
    Return the most searched developments globally, ranked by frequency.
    Returns list of dicts with resolved_name, count, last_searched.
    """
    data = _load()
    searches = data.get("searches", [])

    if not searches:
        return []

    # Aggregate by resolved_name
    counts = {}
    last_seen = {}

    for entry in searches:
        name = entry.get("resolved_name", "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        ts = entry.get("timestamp", "")
        if name not in last_seen or ts > last_seen[name]:
            last_seen[name] = ts

    # Sort by count descending, then by last searched
    ranked = sorted(
        counts.keys(),
        key=lambda n: (counts[n], last_seen.get(n, "")),
        reverse=True
    )

    results = []
    for name in ranked[:limit]:
        ts_str = last_seen.get(name, "")
        try:
            dt = datetime.fromisoformat(ts_str)
            last_searched = _relative_time(dt)
        except ValueError:
            last_searched = "unknown"

        results.append({
            "name": name,
            "count": counts[name],
            "last_searched": last_searched,
        })

    return results


def _relative_time(dt: datetime) -> str:
    """Convert a datetime to a human-readable relative string."""
    now = datetime.now()
    diff = now - dt
    seconds = int(diff.total_seconds())

    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        mins = seconds // 60
        return f"{mins} min ago"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    elif seconds < 86400 * 7:
        days = seconds // 86400
        return f"{days}d ago"
    else:
        return dt.strftime("%d %b %Y")
