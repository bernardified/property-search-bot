import os
import requests
from dotenv import load_dotenv
from datetime import datetime
from cache.cache_ura import get_ura_data
from utils import SIZE_BANDS, get_band, sqm_to_sqft, parse_float, parse_mmyy_date, format_mmyy_date

load_dotenv()

URA_API_KEY = os.getenv("URA_API_KEY")

AMBIGUITY_THRESHOLD = 0.15   # if 2+ projects score within this of best, surface all for user to choose
MIN_AMBIGUITY_SCORE = 0.85   # only run ambiguity check when best match is already strong; below this, weak matches produce too many false positives

# Words stripped from the user's query before word-match scoring.
# These are generic property-type suffixes and articles that appear in hundreds of
# projects and carry no discriminative signal — keeping them would inflate the score
# of every project sharing the suffix (e.g. "highpark residences" scoring 0.70 for
# all 289 "*RESIDENCES" projects).
# NOT included: location words (PARK, GARDENS, HILL, VIEW, COURT, ESTATE) because
# they ARE the key identifier in names like "CHUAN PARK" or "HORIZON GARDENS".
SEARCH_STOPWORDS = frozenset({
    "RESIDENCES", "RESIDENCE",
    "APARTMENTS", "APARTMENT",
    "RESIDENTIAL",
    "CONDOMINIUM", "CONDOMINIUMS",
    "SUITES",
    "MANSIONS", "MANSION",
    "VILLAS", "VILLA",
    "LODGE",
    "THE",
})


# Shared utilities imported from utils.py


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





def _collect_matched_transactions(development_name: str) -> dict:
    """
    Fuzzy-match a development name against the URA cache and collect every
    transaction across all matched blocks.

    Shared by search_property() and price_trend() so the matching/ambiguity
    logic lives in exactly one place.

    Returns one of:
      {"error": str}                      — no usable match
      {"ambiguous": True, "candidates": …} — several close matches, ask user
      {"matched_transactions": [...],      — success
       "matched_project_name": str, "street": str,
       "fuzzy_match": str|None, "alternatives": [...]}
    """
    all_results, _pipeline_data = get_ura_data()
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

        # All search words found in project name (stopwords excluded)
        search_words = [w for w in sn.split() if len(w) > 2 and w not in SEARCH_STOPWORDS]
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

    # Ambiguity check: multiple strong projects score closely and no exact match.
    # MIN_AMBIGUITY_SCORE guard prevents firing on weak/partial matches (e.g.
    # "highpark residences" scoring 0.78 against hundreds of "RESIDENCES" projects).
    if MIN_AMBIGUITY_SCORE <= best_score < 1.0:
        close_candidates = [p for s, p in scored if s >= best_score - AMBIGUITY_THRESHOLD]
        if len(close_candidates) >= 2:
            return {
                "ambiguous": True,
                "candidates": [
                    {"project": p.get("project", ""), "street": p.get("street", "")}
                    for p in close_candidates[:5]
                ],
            }

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
    search_words = [w for w in search_name.split() if len(w) > 2 and w not in SEARCH_STOPWORDS]
    all_words_found = all(w in matched_project_name for w in search_words)
    fuzzy_name = matched_project_name if (best_score < 0.9 and not all_words_found) else None

    # Collect top 3 alternative matches (excluding the chosen one) for "Did you mean?" flow
    alternatives = [
        p.get("project", "")
        for s, p in scored
        if p.get("project", "").upper() != matched_project_name
    ][:3]

    return {
        "matched_transactions": matched_transactions,
        "matched_project_name": matched_transactions[0]["project"],
        "street": matched_transactions[0]["street"],
        "fuzzy_match": fuzzy_name,
        "alternatives": alternatives,
    }


def search_property(development_name: str) -> dict:
    """
    Search for the latest transaction per size band for a given development.
    Uses local cache — instant response after first load.
    """
    matched = _collect_matched_transactions(development_name)
    if "error" in matched or "ambiguous" in matched:
        return matched

    matched_transactions = matched["matched_transactions"]
    fuzzy_name = matched["fuzzy_match"]
    alternatives = matched["alternatives"]
    _all_results, pipeline_data = get_ura_data()

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
        contract_date_parsed = parse_mmyy_date(contract_date_raw)

        entry = {
            "project": item["project"],
            "street": item["street"],
            "contract_date_raw": contract_date_raw,
            "contract_date_parsed": contract_date_parsed,
            "contract_date_display": format_mmyy_date(contract_date_raw),
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

    # Overall 12-month average PSF across all size bands (shown in the header)
    all_psf = [psf for psf_list in band_psf_list.values() for psf in psf_list]
    overall_avg_psf = round(sum(all_psf) / len(all_psf)) if all_psf else None
    overall_psf_count = len(all_psf)

    return {
        "development": matched_project_name,
        "street": matched_transactions[0]["street"],
        "bands": band_latest,
        "band_avg_psf": band_avg_psf,
        "overall_avg_psf": overall_avg_psf,
        "overall_psf_count": overall_psf_count,
        "fuzzy_match": fuzzy_name,
        "alternatives": alternatives,
        "total_units": total_units,
        "expected_top": expected_top,
    }


def _sale_type_label(type_code: str) -> str:
    """Convert URA sale type code to label."""
    return {"1": "New Sale", "2": "Sub Sale", "3": "Resale"}.get(str(type_code), type_code)


# ── Price trend ───────────────────────────────────────────────────────────────
#
# Tracks overall average PSF over time (all size bands combined — PSF is already
# size-normalised). Only resale (typeOfSale 3) and sub-sale (2) are counted;
# new-sale prices reflect the developer's launch pricing, not the resale market,
# so including them would distort the secondary-market trend.

TREND_SALE_TYPES = {"2", "3"}        # sub-sale + resale only
HALF_YEAR_TXN_THRESHOLD = 40         # >= this many txns over >= 2 yrs → half-yearly buckets


def price_trend(development_name: str) -> dict:
    """
    Build an over-time average-PSF trend for a development.

    Buckets adaptively: yearly by default, switching to half-yearly when there
    is enough volume (HALF_YEAR_TXN_THRESHOLD txns spanning >= 2 years) for the
    finer resolution to be meaningful. Empty periods are dropped.

    Returns:
      {"error": str} / {"ambiguous": ...}  — mirrors search_property's contract
      {"development", "street", "fuzzy_match",
       "periods": [{"label", "avg_psf", "count"}, ...],   # ascending in time
       "pct_change": int|None, "span_label": str, "total_txns": int}
    """
    matched = _collect_matched_transactions(development_name)
    if "error" in matched or "ambiguous" in matched:
        return matched

    # (year, half) -> list of PSF values.  half is 1 (Jan–Jun) or 2 (Jul–Dec).
    psf_by_period: dict[tuple[int, int], list[int]] = {}
    total = 0

    for item in matched["matched_transactions"]:
        txn = item["txn"]

        if str(txn.get("typeOfSale", "")) not in TREND_SALE_TYPES:
            continue

        property_type = txn.get("propertyType", "")
        if any(t in property_type.lower() for t in ["detached", "terrace", "bungalow"]):
            continue

        area_sqm = parse_float(txn.get("area", 0))
        if area_sqm is None or area_sqm <= 0:
            continue
        area_sqft = sqm_to_sqft(area_sqm)

        price = parse_float(txn.get("price", 0))
        if not price:
            continue

        dt = parse_mmyy_date(txn.get("contractDate", ""))
        if not dt:
            continue

        psf = round(price / area_sqft)
        half = 1 if dt.month <= 6 else 2
        psf_by_period.setdefault((dt.year, half), []).append(psf)
        total += 1

    if total == 0:
        return {"error": f'No resale or sub-sale transactions found for "{development_name}" to build a price trend.'}

    year_span = max(y for y, _ in psf_by_period) - min(y for y, _ in psf_by_period)
    half_yearly = total >= HALF_YEAR_TXN_THRESHOLD and year_span >= 1

    # Re-key into the chosen granularity. For yearly, collapse both halves into half=0.
    buckets: dict[tuple[int, int], list[int]] = {}
    for (year, half), psfs in psf_by_period.items():
        key = (year, half) if half_yearly else (year, 0)
        buckets.setdefault(key, []).extend(psfs)

    periods = []
    for (year, half) in sorted(buckets):
        psfs = buckets[(year, half)]
        label = f"{year} H{half}" if half_yearly else str(year)
        periods.append({
            "label": label,
            "avg_psf": round(sum(psfs) / len(psfs)),
            "count": len(psfs),
        })

    # Headline % change: first vs last populated period.
    if len(periods) >= 2:
        first, last = periods[0]["avg_psf"], periods[-1]["avg_psf"]
        pct_change = round((last - first) / first * 100) if first else None
    else:
        pct_change = None

    span_label = _trend_span_label(periods)

    return {
        "development": matched["matched_project_name"],
        "street": matched["street"],
        "fuzzy_match": matched["fuzzy_match"],
        "periods": periods,
        "pct_change": pct_change,
        "span_label": span_label,
        "total_txns": total,
    }


def _trend_span_label(periods: list[dict]) -> str:
    """Human label for the time span covered, e.g. '4 yrs' or '8 mths'."""
    if len(periods) < 2:
        return ""

    def start_month(label: str) -> tuple[int, int]:
        parts = label.split()
        year = int(parts[0])
        month = 7 if (len(parts) > 1 and parts[1] == "H2") else 1
        return year, month

    fy, fm = start_month(periods[0]["label"])
    ly, lm = start_month(periods[-1]["label"])
    months = (ly - fy) * 12 + (lm - fm)
    if months >= 12:
        years = round(months / 12)
        return f"{years} yr" if years == 1 else f"{years} yrs"
    return f"{months} mths"


def format_price_trend(result: dict) -> str:
    """Render a price_trend() result as a Telegram (Markdown) message with text bars."""
    if "error" in result:
        return f"❌ {result['error']}"
    if "ambiguous" in result:
        return "❌ Multiple matching developments — please search by the exact name first."

    development = result.get("development", "Unknown")
    periods = result.get("periods", [])
    pct = result.get("pct_change")
    span = result.get("span_label", "")
    total = result.get("total_txns", 0)
    fuzzy = result.get("fuzzy_match")

    lines = [f"📈 *{development} — PSF trend*"]
    if fuzzy:
        lines.append(f"⚠️ _Did you mean: {fuzzy}?_")

    if pct is not None and span:
        arrow = "↑" if pct > 0 else ("↓" if pct < 0 else "→")
        sign = "+" if pct > 0 else ""
        lines.append(f"{arrow} {sign}{pct}% over {span} · {total} txns")
    else:
        lines.append(f"{total} txns · _not enough history for a trend_")

    lines += ["_resale + sub-sale only_", "─────────────────────"]

    max_psf = max((p["avg_psf"] for p in periods), default=0)
    BAR_WIDTH = 8
    for p in periods:
        filled = round(BAR_WIDTH * p["avg_psf"] / max_psf) if max_psf else 0
        filled = max(1, filled)  # always show at least one block
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        lines.append(f"`{p['label']:<7}` S${p['avg_psf']:,} {bar} ({p['count']})")

    return "\n".join(lines)


def format_transactions(result: dict) -> str:
    """Format the transaction result into a readable Telegram message."""
    if "error" in result:
        return f"❌ {result['error']}"

    development = result.get("development", "Unknown")
    street = result.get("street", "")
    bands = result.get("bands", {})
    band_avg_psf = result.get("band_avg_psf", {})
    overall_avg_psf = result.get("overall_avg_psf")
    overall_psf_count = result.get("overall_psf_count", 0)
    fuzzy_match = result.get("fuzzy_match")
    total_units = result.get("total_units")
    expected_top = result.get("expected_top")

    # Pull tenure and sale type from first available band for the header
    first_txn = next(iter(bands.values()), {})
    tenure = first_txn.get("tenure", "")
    sale_type = first_txn.get("type_of_sale", "")
    meta = " · ".join(filter(None, [sale_type, tenure]))

    psf_suffix = f"  ·  📊 S${overall_avg_psf:,} avg psf" if overall_avg_psf else ""
    lines = [
        f"🏢 *{development}*{psf_suffix}",
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
