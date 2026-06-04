import difflib
import logging
from datetime import datetime
from cache.cache_rental import get_rental_data
from utils import SIZE_BANDS, get_band, parse_sqft_range, parse_mmyy_date, format_mmyy_date

logger = logging.getLogger(__name__)

# Shared utilities imported from utils.py


def find_rental_project(development_name: str, rental_data: list) -> list:
    """Find matching project(s) in rental data using same fuzzy logic as ura.py."""
    search = development_name.upper().strip()
    matched = []

    for project in rental_data:
        pname = project.get("project", "").upper().strip()
        if not pname:
            continue

        # Exact match
        if search == pname:
            return [project]
        # Substring match
        if search in pname or pname in search:
            matched.append((0.95, project))
            continue
        # Fuzzy
        ratio = difflib.SequenceMatcher(None, search, pname).ratio()
        if ratio >= 0.75:
            matched.append((ratio, project))

    if not matched:
        return []
    matched.sort(key=lambda x: x[0], reverse=True)
    best_score = matched[0][0]
    return [p for s, p in matched if s >= best_score * 0.95]


def get_rental_by_band(development_name: str, sale_prices: dict) -> dict:
    """
    Find rental data for a development, grouped by size band.
    sale_prices: dict of band_label -> latest sale price (for yield calculation)
    Returns dict of band_label -> {latest_rent, avg_rent, count, yield_pct}
    """
    rental_data = get_rental_data()
    if not rental_data:
        return {"error": "Rental data unavailable. Please try again later."}

    projects = find_rental_project(development_name, rental_data)
    if not projects:
        return {"error": f'No rental data found for "{development_name}".'}

    # Collect all rental records across matched projects
    all_rentals = []
    for project in projects:
        for record in project.get("rental", []):
            area_str = record.get("areaSqft", "")
            midpoint = parse_sqft_range(area_str)
            if midpoint is None:
                continue
            band = get_band(midpoint)
            if not band:
                continue
            rent = record.get("rent")
            if not rent:
                continue
            lease_date = record.get("leaseDate", "")
            lease_dt = parse_mmyy_date(lease_date)
            all_rentals.append({
                "band": band,
                "rent": float(rent),
                "lease_date": lease_date,
                "lease_dt": lease_dt,
                "area_midpoint": midpoint,
            })

    if not all_rentals:
        return {"error": f'No rental records found for "{development_name}".'}

    # Filter to last 12 months
    now = datetime.now()
    cutoff = datetime(now.year - 1, now.month, 1)
    recent = [r for r in all_rentals if r["lease_dt"] and r["lease_dt"] >= cutoff]

    # If no recent data, use all available
    if not recent:
        recent = all_rentals

    # Group by band
    band_data = {}
    for band_info in SIZE_BANDS:
        label = band_info["label"]
        band_rentals = [r for r in recent if r["band"] == label]
        if not band_rentals:
            continue

        # Latest rental
        latest = max(band_rentals, key=lambda x: x["lease_dt"] or datetime.min)
        avg_rent = round(sum(r["rent"] for r in band_rentals) / len(band_rentals))
        latest_rent = round(latest["rent"])

        # PSF = rent / area midpoint
        latest_psf = round(latest["rent"] / latest["area_midpoint"], 2) if latest["area_midpoint"] else None
        avg_psf = round(sum(r["rent"] / r["area_midpoint"] for r in band_rentals if r["area_midpoint"]) / len(band_rentals), 2) if band_rentals else None

        # Gross yield = (monthly rent × 12 / sale price) × 100
        sale_price = sale_prices.get(label, {}).get("price") if sale_prices else None
        yield_pct = None
        if sale_price and sale_price > 0:
            yield_pct = round((latest_rent * 12 / sale_price) * 100, 2)

        band_data[label] = {
            "latest_rent": latest_rent,
            "latest_psf": latest_psf,
            "latest_date": format_mmyy_date(latest["lease_date"]),
            "avg_rent": avg_rent,
            "avg_psf": avg_psf,
            "count": len(band_rentals),
            "yield_pct": yield_pct,
        }

    if not band_data:
        return {"error": f'No rental data in the last 12 months for "{development_name}".'}

    return {"development": development_name, "bands": band_data}


def format_rental(result: dict) -> str:
    """Format rental result into Telegram message."""
    if "error" in result:
        return f"❌ {result['error']}"

    lines = [
        "🏠 *Rental Prices & Yield*",
        "─────────────────────",
        "_Based on last 12 months of rental contracts_",
        "",
    ]

    for band in SIZE_BANDS:
        label = band["label"]
        label_escaped = label.replace("<", "＜").replace(">", "＞")
        data = result["bands"].get(label)

        if data:
            latest_psf_str = f" (S${data['latest_psf']:.2f} psf/mo)" if data.get("latest_psf") else ""
            avg_psf_str = f" (S${data['avg_psf']:.2f} psf/mo)" if data.get("avg_psf") else ""
            yield_str = f"\n  📈 *Gross yield: {data['yield_pct']}%*" if data.get("yield_pct") else ""
            lines.append(
                f"_{label_escaped}_\n"
                f"  🏠 Latest: S${data['latest_rent']:,}/mo{latest_psf_str} ({data['latest_date']})\n"
                f"  📊 Avg: S${data['avg_rent']:,}/mo{avg_psf_str} ({data['count']} contracts){yield_str}"
            )
        else:
            lines.append(f"_{label_escaped}_\n  No rental data found")
        lines.append("")

    lines.append("_Yield = annual rent ÷ latest transacted price_")
    return "\n".join(lines)
