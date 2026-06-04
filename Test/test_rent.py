import os, requests
from dotenv import load_dotenv

# Explicit path to your .env
load_dotenv("/Users/bernardkoh/Desktop/property-bot/.env")

URA_API_KEY = os.getenv("URA_API_KEY")
print(f"Key loaded: {URA_API_KEY[:6] if URA_API_KEY else 'MISSING'}")

# Get token
r = requests.get(
    "https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1",
    headers={"AccessKey": URA_API_KEY, "User-Agent": "PropertyBot/1.0"}
)
print(f"Token status: {r.status_code}")
print(f"Token response: {r.text[:200]}")

token = r.json()["Result"]
headers = {"AccessKey": URA_API_KEY, "Token": token, "User-Agent": "PropertyBot/1.0"}

# Test Rental Median
r2 = requests.get(
    "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Rental_Median",
    headers=headers, timeout=15
)
print(f"\nRental Median status: {r2.status_code}")
print(f"Rental Median sample: {r2.text[:800]}")

# Get current quarter - format is YYqQ e.g. 26q2 for 2026 Q2
r3 = requests.get(
    "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1?service=PMI_Resi_Rental&refPeriod=26q2",
    headers=headers, timeout=30
)
print(f"Rental Contracts status: {r3.status_code}")
print(f"Rental Contracts sample: {r3.text[:800]}")