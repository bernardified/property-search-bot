import requests
from dotenv import load_dotenv
import os
load_dotenv()

token = "your_fresh_onemap_token"

r = requests.get(
    "https://www.onemap.gov.sg/api/common/elastic/search",
    params={
        "searchVal": "Lentor MRT Station",
        "returnGeom": "Y",
        "getAddrDetails": "N",
        "pageNum": 1
    },
    headers={"Authorization": token}
)
for item in r.json().get("results", []):
    print(item.get("SEARCHVAL"), item.get("LATITUDE"), item.get("LONGITUDE"))