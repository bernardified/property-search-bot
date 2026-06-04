import os
from dotenv import load_dotenv
load_dotenv()

from cache_ura import force_refresh
force_refresh()

import json
with open("ura_cache.json") as f:
    data = json.load(f)

transactions = data.get("transactions", [])
matches = [p.get("project", "") for p in transactions if "HORIZON" in p.get("project", "").upper()]
for m in sorted(set(matches)):
    print(m)