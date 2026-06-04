import os, sys, requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from cache.onemap_mrt import find_nearest_mrts

# Chuan Park coordinates
results = find_nearest_mrts(1.3519273, 103.8640435, top_n=3)
for r in results:
    print(r)

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Chuan Park origin
origin = "1.3519273,103.8640435"

# Both exits
exit_a = "1.35143709131033,103.864928562503"
exit_b = "1.35173478946581,103.86291944158"

params = {
    "origins": origin,
    "destinations": f"{exit_a}|{exit_b}",
    "mode": "walking",
    "key": GOOGLE_MAPS_API_KEY,
}
r = requests.get("https://maps.googleapis.com/maps/api/distancematrix/json", params=params)
data = r.json()
elements = data["rows"][0]["elements"]
print(f"Exit A: {elements[0]['duration']['text']} ({elements[0]['distance']['text']})")
print(f"Exit B: {elements[1]['duration']['text']} ({elements[1]['distance']['text']})")