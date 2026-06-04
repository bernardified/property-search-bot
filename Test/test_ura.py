import json

with open("ura_cache.json") as f:
    data = json.load(f)

transactions = data.get("transactions", [])
for p in transactions:
    if "ORIE" in p.get("project", "").upper():
        print(p.get("project"), "|", p.get("street"))