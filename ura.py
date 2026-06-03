import os
import requests
from dotenv import load_dotenv
from datetime import datetime
from cache_ura import get_ura_data

load_dotenv()

URA_API_KEY = os.getenv("URA_API_KEY")

# Size bands in sqft (URA data is in sqm, we convert)
SIZE_BANDS = [
    {"label": "< 600 sqft",       "min": 0,    "max": 600},
    {"label": "600 – 700 sqft",   "min": 600,  "max": 700},
    {"label": "700 – 800 sqft",   "min": 700,  "max": 800},
    {"label": "800 – 900 sqft",   "min": 800,  "max": 900},
    {"label": "900 – 1000 sqft",  "min": 900,  "max": 1000},
    {"label": "> 1000 sqft",      "min": 1000, "max": float("inf")},
]


# API calls are handled by cache_ura.py — data is served from local cache


def get_project_info(project_name: str, pipeline_data: list) -> dict:
    """
    Look up a project in the pipeline data.
    Returns totalUnits and expectedTOPYear if found.
    Falls back to None if not found (completed project).
    """
    search = project_name.upper().strip()
    for item in pipeline_data:
        name = item.get("project", "").upper().strip()
        if search in name or name in search:
            return {
                "total_units": item.get("totalUnits"),
                "expected_top": item.get("expectedTOPYear"),
            }
    return {"total_units": None, "expected_top": None}


def sqm_to_sqft(sqm: float) -> float:
    return sqm * 10.7639


def parse_float(value) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_contract_date(date_str: str) -> datetime | None:
    """
    Parse URA contractDate format.
    URA uses MMYY e.g. "0921" = September 2021.
    """
    try:
        date_str = date_str.strip()
        if len(date_str) == 4:
            mm = int(date_str[:2])
            yy = int(date_str[2:])
            year = 2000 + yy
            return datetime(year, mm, 1)
        return None
    except (ValueError, TypeError):
        return None


def format_contract_date(date_str: str) -> str:
    """Convert MMYY to human-readable e.g. '0921' -> 'Sep 2021'"""
    dt = parse_contract_date(date_str)
    if dt:
        return dt.strftime("%b %Y")
    return date_str


def get_band(sqft: float) -> str | None:
    for band in SIZE_BANDS:
        if band["min"] <= sqft < band["max"]:
            return band["label"]
    return None


def search_property(development_name: str) -> dict:
    """
    Search for the latest transaction per size band for a given development.
    Uses local cache — instant response after first load.
    """
    all_results, pipeline_data = get_ura_data()
    if not all_results:
        return {"error": "Could not load URA transaction data. Please try again later."}

    search_name = development_name.upper().strip()
    matched_transactions = []

    # Score each project against the search term
    # Scoring tiers:
    # 1.0  — exact match (search == project name)
    # 0.95 — search is substring of project (e.g. "The Sail" in "THE SAIL @ MARINA BAY")
    # 0.90 — project is substring of search (e.g. "HORIZON GARDENS" in "FAR HORIZON GARDENS")
    #         penalised vs above because user typed MORE words than the project name
    # 0.85 — all search words found in project name
    # 0.6+ — fuzzy character similarity
    import difflib

    def match_score(project_name: str) -> float:
        pn = project_name.upper().strip()
        sn = search_name

        # Exact match
        if sn == pn:
            return 1.0

        # Search term is contained within project name (e.g. "THE SAIL" in "THE SAIL @ MARINA BAY")
        if sn in pn:
            return 0.95

        # Project name is contained within search term
        # Lower score — user typed extra words that aren't in the project name
        # e.g. "FAR HORIZON GARDENS" contains "HORIZON GARDENS" — wrong match
        if pn in sn:
            # Reward closer length match — longer project name = better match
            length_ratio = len(pn) / len(sn)
            return 0.7 + (0.2 * length_ratio)  # 0.7–0.9 range

        # All search words found in project name
        search_words = [w for w in sn.split() if len(w) > 2]
        if search_words:
            found = sum(1 for w in search_words if w in pn)
            if found == len(search_words):
                return 0.85
            if found > 0:
                return 0.6 + (0.2 * found / len(search_words))

        # Fuzzy character similarity
        return difflib.SequenceMatcher(None, sn, pn).ratio()

    # Collect all projects with score above threshold
    scored = []
    for project in all_results:
        project_name = project.get("project", "").upper().strip()
        if not project_name:
            continue
        score = match_score(project_name)
        if score >= 0.6:
            scored.append((score, project))

    if not scored:
        return {"error": f'No transactions found for "{development_name}".\nTip: Try the official URA project name, e.g. "The Sail" or "Parc Esta".'}

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score = scored[0][0]

    if best_score >= 0.9:
        # Take all projects at the top score (handles multi-block developments)
        top_projects = [p for s, p in scored if s >= 0.9]
    else:
        # Take single best match
        top_projects = [scored[0][1]]

    for project in top_projects:
        for txn in project.get("transaction", []):
            matched_transactions.append({
                "project": project.get("project", ""),
                "street": project.get("street", ""),
                "txn": txn,
            })

    if not matched_transactions:
        return {"error": f'No transactions found for "{development_name}".\nTip: Try the official URA project name, e.g. "The Sail" or "Parc Esta".'}

    # If it was a fuzzy match, note the actual name found
    # Only flag as fuzzy match if the user's search words are NOT all found in the result
    # e.g. "srgn" matched "SERANGOON" — no warning needed, result is clearly correct
    # e.g. "residnces" matched "RESIDENCES" — warn because it was a typo correction
    matched_project_name = top_projects[0].get("project", "").upper()
    search_words = [w for w in search_name.split() if len(w) > 2]
    all_words_found = all(w in matched_project_name for w in search_words)
    fuzzy_name = matched_project_name if (best_score < 0.9 and not all_words_found) else None

    # Collect top 3 alternative matches (excluding the chosen one) for "Did you mean?" flow
    alternatives = [
        p.get("project", "")
        for s, p in scored
        if p.get("project", "").upper() != matched_project_name
    ][:3]

    # Find the latest transaction per size band
    band_latest = {}
    band_psf_list = {}  # band -> list of PSF values in last 12 months

    for item in matched_transactions:
        txn = item["txn"]

        # Skip landed / non-strata (no floor range for strata, but area should be reasonable)
        property_type = txn.get("propertyType", "")
        if any(t in property_type.lower() for t in ["detached", "terrace", "bungalow"]):
            continue

        area_sqm = parse_float(txn.get("area", 0))
        if area_sqm is None or area_sqm <= 0:
            continue

        area_sqft = sqm_to_sqft(area_sqm)
        band = get_band(area_sqft)
        if not band:
            continue

        price = parse_float(txn.get("price", 0))
        if not price:
            continue

        psf = round(price / area_sqft) if area_sqft > 0 else None
        contract_date_raw = txn.get("contractDate", "")
        contract_date_parsed = parse_contract_date(contract_date_raw)

        entry = {
            "project": item["project"],
            "street": item["street"],
            "contract_date_raw": contract_date_raw,
            "contract_date_parsed": contract_date_parsed,
            "contract_date_display": format_contract_date(contract_date_raw),
            "price": price,
            "psf": psf,
            "area_sqft": round(area_sqft),
            "floor_range": txn.get("floorRange", "-"),
            "type_of_sale": _sale_type_label(txn.get("typeOfSale", "")),
            "property_type": property_type,
            "tenure": txn.get("tenure", ""),
        }

        if band not in band_latest:
            band_latest[band] = entry
        else:
            existing = band_latest[band]["contract_date_parsed"]
            new = contract_date_parsed
            if new and (not existing or new > existing):
                band_latest[band] = entry

        # Collect PSF for 12-month average
        if psf and contract_date_parsed:
            cutoff = datetime.now().replace(day=1)
            from dateutil.relativedelta import relativedelta
            cutoff = cutoff - relativedelta(months=12)
            if contract_date_parsed >= cutoff:
                if band not in band_psf_list:
                    band_psf_list[band] = []
                band_psf_list[band].append(psf)

    if not band_latest:
        return {"error": f'Found "{development_name}" but could not parse any valid transactions.\nThe project may only have landed housing records.'}

    # Get total units: first try pipeline (uncompleted), then sum noOfUnits from transactions
    matched_project_name = matched_transactions[0]["project"]
    pipeline_info = get_project_info(matched_project_name, pipeline_data)

    total_units = pipeline_info.get("total_units")
    expected_top = pipeline_info.get("expected_top")

    # Fallback: get noOfUnits from transaction records if not in pipeline
    if not total_units:
        unit_counts = set()
        for item in matched_transactions:
            val = item["txn"].get("noOfUnits")
            if val:
                try:
                    unit_counts.add(int(val))
                except (ValueError, TypeError):
                    pass
        # noOfUnits in URA txn data is per-transaction not total project units
        # so we can't reliably sum them — leave as None if not in pipeline
        total_units = None

    # Compute average PSF per band
    band_avg_psf = {}
    for band, psf_list in band_psf_list.items():
        if psf_list:
            band_avg_psf[band] = {
                "avg_psf": round(sum(psf_list) / len(psf_list)),
                "count": len(psf_list),
            }

    return {
        "development": matched_project_name,
        "street": matched_transactions[0]["street"],
        "bands": band_latest,
        "band_avg_psf": band_avg_psf,
        "fuzzy_match": fuzzy_name,
        "alternatives": alternatives,
        "total_units": total_units,
        "expected_top": expected_top,
    }


def _sale_type_label(type_code: str) -> str:
    """Convert URA sale type code to label."""
    return {"1": "New Sale", "2": "Sub Sale", "3": "Resale"}.get(str(type_code), type_code)


def format_transactions(result: dict) -> str:
    """Format the transaction result into a readable Telegram message."""
    if "error" in result:
        return f"❌ {result['error']}"

    development = result.get("development", "Unknown")
    street = result.get("street", "")
    bands = result.get("bands", {})
    band_avg_psf = result.get("band_avg_psf", {})
    fuzzy_match = result.get("fuzzy_match")
    total_units = result.get("total_units")
    expected_top = result.get("expected_top")

    # Pull tenure and sale type from first available band for the header
    first_txn = next(iter(bands.values()), {})
    tenure = first_txn.get("tenure", "")
    sale_type = first_txn.get("type_of_sale", "")
    meta = " · ".join(filter(None, [sale_type, tenure]))

    lines = [
        f"🏢 *{development}*",
        f"📍 {street}",
    ]
    if fuzzy_match:
        lines.append(f"⚠️ _Did you mean: {fuzzy_match}?_")
    if meta:
        lines.append(f"🏷 _{meta}_")
    if total_units:
        lines.append(f"🏗 _Total units: {total_units}_")
    if expected_top and expected_top != "na":
        lines.append(f"📆 _Expected TOP: {expected_top}_")
    lines += [
        "",
        "💰 *Latest Transacted Prices*",
        "─────────────────────",
    ]

    for band in SIZE_BANDS:
        label = band["label"]
        # Escape < and > so Telegram markdown does not break
        label_escaped = label.replace("<", "＜").replace(">", "＞")
        if label in bands:
            txn = bands[label]
            price_str = f"S${int(txn['price']):,}"
            psf_str = f"S${txn['psf']:,} psf" if txn.get("psf") else "N/A"
            floor_str = f" · Floor {txn['floor_range']}" if txn.get("floor_range") and txn["floor_range"] != "-" else ""
            avg_info = band_avg_psf.get(label)
            avg_str = f"\n  📊 _12m avg: S${avg_info['avg_psf']:,} psf ({avg_info['count']} txns)_" if avg_info else ""
            lines.append(
                f"_{label_escaped}_\n"
                f"  💵 {price_str} ({psf_str})\n"
                f"  📐 {txn['area_sqft']} sqft{floor_str}\n"
                f"  📅 {txn['contract_date_display']}"
                f"{avg_str}"
            )
        else:
            lines.append(f"_{label_escaped}_\n  No transactions found")
        lines.append("")  # blank line between bands

    return "\n".join(lines)
