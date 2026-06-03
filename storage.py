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
    """Fetch the top N most searched properties for the /list command."""
    if searches is None:
        return []

    # The Aggregation Pipeline
    pipeline = [
        {
            "$group": {
                "_id": "$resolved_name",            # Group all identical property searches together
                "count": {"$sum": 1},               # Add 1 to the count for every match
                "last_timestamp": {"$max": "$timestamp"} # Keep the most recent timestamp
            }
        },
        {"$sort": {"count": -1}},                   # 🛑 Sort by count: -1 means DESCENDING order
        {"$limit": limit}                           # 🛑 Stop grabbing data once we hit 10 results
    ]

    try:
        results = list(searches.aggregate(pipeline))
        
        # Format the output for bot.py
        formatted_results = []
        for r in results:
            formatted_results.append({
                "name": r["_id"],
                "count": r["count"],
                "last_searched": r["last_timestamp"].strftime("%Y-%m-%d") if r.get("last_timestamp") else "Unknown"
            })
            
        return formatted_results
    except Exception as e:
        logger.error(f"Failed to aggregate recent searches: {e}")
        return []