"""MANUAL one-time/occasional builder for `cache/malls.json` — the shopping-mall
register the explore map's nearest-mall filter measures against.

Never imported by runtime code. Run it when a mall opens or closes:

    python scripts/build_mall_coords.py            # rebuild cache/malls.json
    python scripts/build_mall_coords.py --dry-run  # print, write nothing

WHY A CHECKED-IN LIST AND NOT AN API
Singapore publishes no shopping-mall register. OneMap has 165 themes and none
of them is malls; data.gov.sg has none either. Google Places can find them, but
its terms forbid storing Places content beyond 30 days, and a per-dot Places
lookup is impossible anyway — the filter needs a distance for every one of
~2.4k developments and ~9.6k HDB blocks, which is a haversine loop over a small
register, exactly like the MRT one.

So the register is a curated list of mall NAMES (below), resolved to
coordinates through OneMap — SLA data, already attributed in the webapp
footer — and checked into the repo. Names are the part that needs a human;
coordinates are not.

Only a result whose own building name matches the query is accepted, because
OneMap's elastic search always answers something: a miss returns the nearest
textual neighbour, not nothing. Rejects and misses are printed for review
rather than silently dropped.
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from maps import search_onemap                                    # noqa: E402
from utils import get_onemap_token                                # noqa: E402

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "cache", "malls.json")

# Singapore's bounding box — a sanity check on every coordinate, since a
# geocode that lands outside it is wrong whatever the name says.
SG_BOUNDS = (1.15, 1.48, 103.6, 104.1)

# Shopping malls and shopping centres with a meaningful retail draw, by region.
# Strip malls, single-supermarket HDB podiums and pure office lobbies are left
# out: "nearest mall" should mean somewhere you would spend an afternoon.
MALL_NAMES = [
    # ── Orchard / Tanglin ──
    "ION ORCHARD", "NGEE ANN CITY", "THE PARAGON", "WISMA ATRIA", "MANDARIN GALLERY",
    "313@SOMERSET", "ORCHARD CENTRAL", "ORCHARDGATEWAY", "PLAZA SINGAPURA",
    "THE CENTREPOINT", "LUCKY PLAZA", "FAR EAST PLAZA", "FORUM THE SHOPPING MALL",
    "PALAIS RENAISSANCE", "SHAW CENTRE", "SCOTTS SQUARE", "TANGLIN MALL",
    "THE CATHAY", "CONCORDE HOTEL AND SHOPPING MALL",
    "ORCHARD PLAZA", "ORCHARD SHOPPING CENTRE", "MIDPOINT ORCHARD",
    "DELFI ORCHARD", "HOLLAND ROAD SHOPPING CENTRE", "RAFFLES HOLLAND V",
    "JELITA MALL", "SERENE CENTRE", "CORONATION SHOPPING PLAZA",

    # ── City / Marina / Bugis / Chinatown ──
    "RAFFLES CITY SHOPPING CENTRE", "SUNTEC CITY", "MARINA SQUARE",
    "MILLENIA WALK", "THE SHOPPES AT MARINA BAY SANDS", "MARINA BAY LINK MALL",
    "CITYLINK MALL", "FUNAN", "CAPITOL BUILDING", "PENINSULA PLAZA",
    "PENINSULA SHOPPING COMPLEX", "BUGIS JUNCTION", "BUGIS+", "SIM LIM SQUARE",
    "SIM LIM TOWER", "BURLINGTON SQUARE", "PARKLANE SHOPPING MALL",
    "FU LU SHOU COMPLEX", "ALBERT COMPLEX", "CHINATOWN POINT",
    "PEOPLE'S PARK COMPLEX", "PEOPLE'S PARK CENTRE", "HONG LIM COMPLEX",
    "THE CENTRAL", "CLARKE QUAY", "UE SQUARE", "CANNINGHILL SQUARE",
    "GUOCO TOWER", "100 AM", "DOWNTOWN GALLERY", "FAR EAST SQUARE",
    "GREAT WORLD", "VALLEY POINT", "TIONG BAHRU PLAZA", "ZHONGSHAN MALL",
    "TEKKA PLACE", "THE POIZ CENTRE", "CITY SQUARE MALL", "MUSTAFA CENTRE",
    "JALAN BESAR PLAZA", "DUO GALLERIA", "SHAW PLAZA",
    "VELOCITY @ NOVENA SQUARE", "UNITED SQUARE", "SQUARE 2", "GOLDHILL PLAZA",
    "HDB HUB", "WOODLEIGH MALL",

    # ── East ──
    "PARKWAY PARADE", "112 KATONG", "KATONG V", "KATONG SHOPPING CENTRE",
    "ROXY SQUARE", "KINEX", "CITY PLAZA", "PAYA LEBAR QUARTER",
    "PAYA LEBAR SQUARE", "SINGAPORE POST CENTRE", "TANJONG KATONG COMPLEX",
    "JOO CHIAT COMPLEX", "LEISURE PARK KALLANG", "KALLANG WAVE MALL",
    "APERIA", "BEDOK MALL", "DJITSUN MALL BEDOK", "EASTPOINT MALL",
    "CHANGI CITY POINT", "JEWEL CHANGI AIRPORT", "OUR TAMPINES HUB",
    "TAMPINES MALL", "CENTURY SQUARE", "TAMPINES ONE", "WHITE SANDS",
    "DOWNTOWN EAST", "LOYANG POINT", "ELIAS MALL", "PASIR RIS WEST PLAZA",
    "MACPHERSON MALL", "SINGAPORE SHOPPING CENTRE",

    # ── North-East ──
    "NEX", "MYVILLAGE AT SERANGOON GARDEN", "HEARTLAND MALL",
    "HOUGANG MALL", "HOUGANG 1", "HOUGANG GREEN SHOPPING MALL",
    "THE MIDTOWN", "COMPASS ONE", "RIVERVALE MALL", "RIVERVALE PLAZA",
    "SENGKANG GRAND MALL", "BUANGKOK SQUARE", "THE SELETAR MALL",
    "WATERWAY POINT", "PUNGGOL PLAZA", "OASIS TERRACES", "ONE PUNGGOL",
    "NORTHSHORE PLAZA", "GREENWICH V", "AMK HUB", "BROADWAY PLAZA",
    "JUBILEE SQUARE", "JUNCTION 8", "THOMSON PLAZA",
    "UPPER SERANGOON SHOPPING CENTRE",

    # ── North ──
    "CAUSEWAY POINT", "VISTA POINT", "WOODLANDS MART", "MARSILING MALL",
    "NORTHPOINT CITY", "JUNCTION NINE", "WISTERIA MALL", "SUN PLAZA",
    "SEMBAWANG SHOPPING CENTRE", "CANBERRA PLAZA", "KAMPUNG ADMIRALTY",

    # ── West / Bukit Timah ──
    "JEM", "WESTGATE", "IMM BUILDING", "JURONG POINT",
    "TAMAN JURONG SHOPPING CENTRE", "PIONEER MALL", "GEK POH SHOPPING CENTRE",
    "THE CLEMENTI MALL", "321 CLEMENTI", "WEST COAST PLAZA", "WEST MALL",
    "LE QUEST", "GRANTRAL MALL", "BUKIT PANJANG PLAZA", "HILLION MALL",
    "GREENRIDGE SHOPPING CENTRE", "FAJAR SHOPPING CENTRE",
    "LIMBANG SHOPPING CENTRE", "YEWTEE POINT", "YEW TEE SQUARE",
    "LOT ONE SHOPPERS' MALL", "SUNSHINE PLACE", "HILLV2", "THE RAIL MALL",
    "BUKIT TIMAH PLAZA", "BEAUTY WORLD CENTRE", "BEAUTY WORLD PLAZA",
    "BUKIT TIMAH SHOPPING CENTRE", "THE GRANDSTAND", "KAP",
    "THE STAR VISTA", "ROCHESTER MALL", "CLEMENTI ARCADE",

    # ── South ──
    "VIVOCITY", "HARBOURFRONT CENTRE", "ALEXANDRA RETAIL CENTRE",
    "ALEXANDRA CENTRAL", "ANCHORPOINT", "QUEENSWAY SHOPPING CENTRE",
    "DAWSON PLACE",
]


def norm(s: str) -> str:
    """Upper-case alphanumerics only — the form the name match is done in, so
    'LOT ONE SHOPPERS' MALL' and 'LOT ONE SHOPPERS MALL' are the same name."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def accept(query: str, result: dict) -> bool:
    """Is this OneMap hit really the mall we asked for?

    OneMap always answers: ask for a mall it does not know and it returns the
    nearest textual neighbour, so a result is only trusted when its own
    building name contains the query name (or the query contains it — 'JEM'
    comes back as 'JEM', 'IMM BUILDING' as 'IMM'). Without this the register
    fills up with plausible-looking wrong buildings.
    """
    q = norm(query)
    for field in ("BUILDING", "SEARCHVAL"):
        name = norm(result.get(field, ""))
        if name and (q in name or name in q):
            return True
    return False


def in_singapore(lat: float, lng: float) -> bool:
    lo_lat, hi_lat, lo_lng, hi_lng = SG_BOUNDS
    return lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng


def result_name(r: dict) -> str:
    return str(r.get("BUILDING") or r.get("SEARCHVAL") or "")


def resolve(name: str, token: str) -> dict | None:
    """Best OneMap hit for one mall name, or None if nothing passes.

    Among the hits that pass `accept`, the shortest building name wins, with
    an exact match beating everything. OneMap indexes tenants as buildings in
    their own right, so a bare mall name pulls its shops up alongside it — and
    a tenant of the SAME name somewhere else outranked the mall itself:
    'PARAGON' answered with 'PARAGON DENTAL CARE' in Pasir Ris, 6 km from
    Paragon on Orchard Road. The mall is the shortest name containing the
    query; its tenants are that name plus something.
    """
    cands = []
    for r in search_onemap(name, token)[:8]:
        if not accept(name, r):
            continue
        try:
            lat, lng = float(r["LATITUDE"]), float(r["LONGITUDE"])
        except (KeyError, TypeError, ValueError):
            continue
        if in_singapore(lat, lng):
            cands.append((r, lat, lng))
    if not cands:
        return None

    q = norm(name)
    r, lat, lng = min(cands, key=lambda c: (norm(result_name(c[0])) != q,
                                            len(norm(result_name(c[0])))))
    return {
        "name": name,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "address": str(r.get("ADDRESS", "")).strip(),
        "postal": str(r.get("POSTAL", "")).strip(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args()

    token = get_onemap_token()
    if not token:
        sys.exit("No OneMap token — set ONEMAP_EMAIL / ONEMAP_PASSWORD in .env")

    malls, misses = [], []
    for i, name in enumerate(MALL_NAMES, 1):
        hit = resolve(name, token)
        if hit:
            malls.append(hit)
            print(f"[{i:3}/{len(MALL_NAMES)}] {name:42} {hit['lat']:.5f},{hit['lng']:.5f}"
                  f"  {hit['address'][:48]}")
        else:
            misses.append(name)
            print(f"[{i:3}/{len(MALL_NAMES)}] {name:42} MISS")
        time.sleep(0.15)          # OneMap rate limit; this runs once, not hot

    malls.sort(key=lambda m: m["name"])
    print(f"\nResolved {len(malls)} of {len(MALL_NAMES)}")
    if misses:
        print("Misses (check the spelling against OneMap, or drop the entry):")
        for m in misses:
            print(f"  {m}")

    if args.dry_run:
        return
    with open(OUT_PATH, "w") as f:
        json.dump(malls, f, indent=1)
        f.write("\n")
    print(f"Wrote {os.path.normpath(OUT_PATH)}")


if __name__ == "__main__":
    main()
