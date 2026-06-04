import os, requests
from dotenv import load_dotenv
load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

for address in [
    "THE ORIE LORONG 1 TOA PAYOH, Singapore",
    "LORONG 1 TOA PAYOH, Singapore",
    "The Orie, Singapore",
]:
    r = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": address, "key": GOOGLE_MAPS_API_KEY}
    )
    data = r.json()
    if data["status"] == "OK":
        loc = data["results"][0]["geometry"]["location"]
        print(f"{address}\n  -> {loc}\n  -> {data['results'][0]['formatted_address']}\n")
    else:
        print(f"{address} -> {data['status']}\n")