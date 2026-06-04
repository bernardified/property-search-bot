import os, requests
from dotenv import load_dotenv
load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Test near The Garden Residences (Serangoon)
lat, lng = 1.36618, 103.87367

for keyword, place_type in [
    ("supermarket", "supermarket"),
    ("NTUC FairPrice", "supermarket"),
    ("grocery", "grocery_or_supermarket"),
]:
    r = requests.get(
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
        params={
            "location": f"{lat},{lng}",
            "radius": 1000,
            "keyword": keyword,
            "type": place_type,
            "key": GOOGLE_MAPS_API_KEY,
        }
    )
    data = r.json()
    print(f"\nKeyword='{keyword}', Type='{place_type}':")
    print(f"Status: {data['status']}")
    for p in data.get("results", [])[:5]:
        print(f"  - {p['name']}")

import os, requests
from dotenv import load_dotenv
load_dotenv()

import sys
sys.path.insert(0, '/Users/bernardkoh/Desktop/property-bot')

from maps import geocode_address, find_nearest_supermarkets

# Test with a known address
address = "LORONG 1 TOA PAYOH"  # The Orie's street
coords = geocode_address(address)
print(f"Coords for '{address}': {coords}")

if coords:
    lat, lng = coords
    results = find_nearest_supermarkets(lat, lng)
    print(f"Supermarkets found: {len(results)}")
    for r in results:
        print(f"  - {r['name']}")