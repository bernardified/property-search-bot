import os
import logging
from datetime import datetime, timezone
from pymongo import MongoClient
from pymongo.server_api import ServerApi

logger = logging.getLogger(__name__)

# 1. Pull the connection string from Railway environment variables
MONGO_URI = os.getenv("MONGO_URI")

# 2. Initialize the connection globally
if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
        db = client['property_bot'] # This creates/uses a database named 'property_bot'
        searches = db['searches']   # This creates/uses a collection named 'searches'
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        searches = None
else:
    logger.warning("MONGO_URI not found in environment variables.")
    searches = None


def record_search(user_id, username, query, resolved_name):
    """Save a new search event to MongoDB."""
    if searches is None:
        return

    document = {
        "user_id": user_id,
        "username": username,
        "query": query,
        "resolved_name": resolved_name,
        "timestamp": datetime.now(timezone.utc)
    }
    
    try:
        searches.insert_one(document)
    except Exception as e:
        logger.error(f"Failed to insert search record: {e}")


def get_recent_searches(limit=10):
    """Fetch the most searched properties for the /list command."""
    if searches is None:
        return []

    # Aggregation pipeline to group by property name and count them
    pipeline = [
        {
            "$group": {
                "_id": "$resolved_name",            # Group by the resolved property name
                "count": {"$sum": 1},               # Count how many times it was searched
                "last_timestamp": {"$max": "$timestamp"} # Get the most recent search time
            }
        },
        {"$sort": {"count": -1}},                   # Sort by highest count first
        {"$limit": limit}                           # Limit to top N results
    ]

    try:
        results = list(searches.aggregate(pipeline))
        
        # Format the output to exactly match what your bot.py expects
        formatted_results = []
        for r in results:
            formatted_results.append({
                "name": r["_id"],
                "count": r["count"],
                # Format the raw datetime object into a clean string (e.g., '2026-06-03')
                "last_searched": r["last_timestamp"].strftime("%Y-%m-%d") if r.get("last_timestamp") else "Unknown"
            })
            
        return formatted_results
    except Exception as e:
        logger.error(f"Failed to aggregate recent searches: {e}")
        return []