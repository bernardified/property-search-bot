"""
Scheduled job to refresh URA cache.
Runs via Railway cron — see railway.toml for schedule.
"""
import logging
from cache_ura import force_refresh, cache_status

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    logger.info("=== URA Cache Refresh Job Started ===")

    before = cache_status()
    logger.info(f"Cache before: {before}")

    success = force_refresh()

    after = cache_status()
    if success:
        logger.info(f"✅ Refresh successful — {after.get('projects', '?')} projects loaded")
    else:
        logger.error("❌ Refresh failed — URA API may be unavailable")

    logger.info("=== URA Cache Refresh Job Done ===")


if __name__ == "__main__":
    main()
