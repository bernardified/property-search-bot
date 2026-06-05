"""
District-based property discovery.
Lets users browse the top most-transacted developments in each Singapore district.

URA transaction records already carry a `district` field (values "01".."28"),
so no geocoding is needed — we read it straight from the cached data.
"""
import logging
from datetime import datetime
from dateutil.relativedelta import relativedelta
from cache.cache_ura import get_ura_data
from utils import parse_float, sqm_to_sqft, parse_mmyy_date

logger = logging.getLogger(__name__)

# Singapore has 28 postal districts (D1–D28).
NUM_DISTRICTS = 28

# Common estate/town names for each district, so users don't need to know
# the numbering. The first segment (before " / ") is used as the compact
# button label; the full string is used in the results header.
DISTRICT_NAMES: dict[int, str] = {
    1:  "Raffles Place / Marina / Cecil",
    2:  "Tanjong Pagar / Anson / Shenton Way",
    3:  "Tiong Bahru / Queenstown / Redhill",
    4:  "Harbourfront / Sentosa / Telok Blangah",
    5:  "Clementi / Pasir Panjang / West Coast",
    6:  "City Hall / Clarke Quay / High Street",
    7:  "Bugis / Beach Road / Golden Mile",
    8:  "Farrer Park / Little India / Lavender",
    9:  "Orchard / River Valley / Cairnhill",
    10: "Bukit Timah / Holland / Tanglin",
    11: "Novena / Newton / Thomson",
    12: "Toa Payoh / Balestier / Boon Keng",
    13: "Macpherson / Potong Pasir / Braddell",
    14: "Geylang / Paya Lebar / Eunos",
    15: "Katong / Marine Parade / Joo Chiat",
    16: "Bedok / Upper East Coast / Eastwood",
    17: "Changi / Loyang / Flora",
    18: "Tampines / Pasir Ris / Simei",
    19: "Hougang / Serangoon / Punggol",
    20: "Ang Mo Kio / Bishan / Thomson",
    21: "Upper Bukit Timah / Clementi Park / Ulu Pandan",
    22: "Jurong / Boon Lay / Tuas",
    23: "Bukit Batok / Bukit Panjang / Choa Chu Kang",
    24: "Lim Chu Kang / Tengah / Kranji",
    25: "Woodlands / Admiralty / Marsiling",
    26: "Upper Thomson / Mandai / Springleaf",
    27: "Yishun / Sembawang / Canberra",
    28: "Seletar / Yio Chu Kang / Fernvale",
}


def district_full_name(district: int) -> str:
    """Full estate name for headers, e.g. 19 -> 'Hougang / Serangoon / Punggol'."""
    return DISTRICT_NAMES.get(district, "")


def district_button_label(district: int) -> str:
    """Compact label for buttons — first 2 towns so two fit per row.

    e.g. 19 -> 'Hougang / Serangoon'. The full set of towns is shown in the
    results header once a district is selected.
    """
    return " / ".join(DISTRICT_NAMES.get(district, "").split(" / ")[:2])


def _normalize_district(value) -> int | None:
    """Convert URA's district string ('01'..'28') to an int, or None if invalid."""
    try:
        d = int(str(value).strip())
        if 1 <= d <= NUM_DISTRICTS:
            return d
    except (ValueError, TypeError):
        pass
    return None


def get_top_developments_by_district(district: int, limit: int = 10) -> list:
    """
    Top N most-transacted developments in a district over the last 6 months.

    Returns a list sorted by transaction count desc:
    [{"project": str, "transaction_count": int, "avg_psf": int | None}, ...]
    """
    all_results, _ = get_ura_data()
    if not all_results:
        return []

    cutoff_date = datetime.now() - relativedelta(months=6)
    projects: dict[str, dict] = {}

    for project_data in all_results:
        project_name = project_data.get("project", "")
        if not project_name:
            continue

        for txn in project_data.get("transaction", []):
            # Filter by district (read straight from the URA record)
            if _normalize_district(txn.get("district")) != district:
                continue

            # Skip landed properties
            prop_type = txn.get("propertyType", "")
            if any(t in prop_type.lower() for t in ["detached", "terrace", "bungalow"]):
                continue

            # Only resale ("3") and sub-sale ("2") — exclude new sales ("1"),
            # which reflect developer launch activity rather than the secondary market
            if str(txn.get("typeOfSale", "")).strip() not in ("2", "3"):
                continue

            # Only the last 6 months
            contract_date = parse_mmyy_date(txn.get("contractDate", ""))
            if not contract_date or contract_date < cutoff_date:
                continue

            area_sqm = parse_float(txn.get("area", 0))
            price = parse_float(txn.get("price", 0))
            if not area_sqm or area_sqm <= 0 or not price:
                continue

            area_sqft = sqm_to_sqft(area_sqm)
            psf = round(price / area_sqft) if area_sqft > 0 else None

            bucket = projects.setdefault(project_name, {
                "project": project_name,
                "transaction_count": 0,
                "psf_values": [],
            })
            bucket["transaction_count"] += 1
            if psf:
                bucket["psf_values"].append(psf)

    results = []
    for data in projects.values():
        if data["transaction_count"] == 0:
            continue
        psf_list = data["psf_values"]
        results.append({
            "project": data["project"],
            "transaction_count": data["transaction_count"],
            "avg_psf": round(sum(psf_list) / len(psf_list)) if psf_list else None,
        })

    results.sort(key=lambda x: x["transaction_count"], reverse=True)
    return results[:limit]


def _html_escape(text: str) -> str:
    """Escape the three characters that are special in Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_district_results(district: int, developments: list, bot_username: str | None = None) -> str:
    """Format the top developments for a district into a Telegram (HTML) message.

    If ``bot_username`` is provided, each development name becomes a tappable
    deep link (``t.me/<bot>?start=d<district>r<rank>``) that pulls up that
    property's transactions. Without it, names render as plain bold text.
    """
    name = district_full_name(district)
    title = _html_escape(f"District {district}" + (f" — {name}" if name else ""))

    if not developments:
        return (
            f"📍 <b>{title}</b>\n\n"
            "No transactions found in the last 6 months.\n"
            "<i>This district may have limited private residential activity.</i>"
        )

    lines = [
        f"📍 <b>{title}</b>",
        "<i>Top developments · Last 6 months · tap a name for details</i>",
        "─────────────────────────────────────────",
    ]
    for rank, dev in enumerate(developments, 1):
        count = dev["transaction_count"]
        psf_str = f"S${dev['avg_psf']:,} psf" if dev["avg_psf"] else "N/A"
        name_html = _html_escape(dev["project"])
        if bot_username:
            link = f"https://t.me/{bot_username}?start=d{district}r{rank}"
            name_html = f'<a href="{link}">{name_html}</a>'
        lines.append(
            f"  {rank}. <b>{name_html}</b>\n"
            f"     📊 {count} txn{'s' if count != 1 else ''} · {psf_str}"
        )
    return "\n".join(lines)
