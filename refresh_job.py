"""
Scheduled job to refresh all caches.
Runs via Railway cron — see railway.toml for schedule.
"""
import logging
import os
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from cache.cache_ura import force_refresh, cache_status
from cache.cache_rental import force_refresh_rental, rental_cache_status
from cache.onemap_mrt import build_mrt_cache
from cache.schools_cache import get_schools_cache

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def clear_collection(db, name: str):
    """Force a cache rebuild by clearing the collection."""
    try:
        db[name].delete_many({})
        logger.info(f"Cleared {name}")
    except Exception as e:
        logger.error(f"Failed to clear {name}: {e}")


def main():
    logger.info("=== Cache Refresh Job Started ===")

    # Connect to MongoDB to clear stale caches
    mongo_uri = os.getenv("MONGO_URI")
    db = None
    if mongo_uri:
        try:
            client = MongoClient(
                mongo_uri,
                server_api=ServerApi("1"),
                serverSelectionTimeoutMS=10000,
            )
            db = client["property_bot"]
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}")

    # 1. URA transactions
    logger.info("Refreshing URA transactions...")
    ura_ok = force_refresh()
    status = cache_status()
    if ura_ok:
        logger.info(f"✅ URA — {status.get('projects', '?')} projects")
    else:
        logger.error("❌ URA refresh failed")

    # 2. MRT stations
    logger.info("Refreshing MRT stations...")
    if db is not None:
        clear_collection(db, "mrt_cache")
    stations = build_mrt_cache()
    logger.info(f"✅ MRT — {len(stations)} stations")

    # 3. Primary schools
    logger.info("Refreshing primary schools...")
    if db is not None:
        clear_collection(db, "schools_cache")
    schools = get_schools_cache()
    logger.info(f"✅ Schools — {len(schools)} schools")

    # 4. Rental data
    logger.info("Refreshing rental data...")
    rental_ok = force_refresh_rental()
    r_status = rental_cache_status()
    if rental_ok:
        logger.info(f"✅ Rental — {r_status.get('projects', '?')} projects")
    else:
        logger.error("❌ Rental refresh failed")

    logger.info("=== Cache Refresh Job Done ===")


if __name__ == "__main__":
    main()
