"""
Liquidity / absorption helper — how fast units in a development sell.

Pure calculation + formatting (no Telegram / IO) in the style of mortgage.py,
with one thin IO orchestrator (`liquidity_for_project`) at the bottom.

The metric auto-switches on construction status:

  - **Under construction** (project in the URA pipeline feed) → *take-up
    rate*: new-sale units sold in the window ÷ total units. The official
    absorption measure for a launch.
  - **Completed** → *turnover rate*: resale + sub-sale units in the window ÷
    total units, annualised. The standard secondary-market liquidity measure —
    listing counts would double-count multi-agent listings, so registered
    transactions over the real unit stock is the cleaner signal.

The denominator (total units) is resolved in tiers: live pipeline feed →
permanent unit_counts store (harvested pipeline history / scraped seed) →
derived from cached new-sale records. When no denominator exists at all, the
summary falls back to median time-between-sales so the feature never errors.

Per-band unit counts are not published anywhere, so the band mix is estimated
from the project's all-time transaction mix (over a project's life every unit
trades at least once, so the cumulative mix converges on the real mix) and is
always labelled as an estimate.
"""
from datetime import datetime

from dateutil.relativedelta import relativedelta

from utils import SIZE_BANDS, get_band, parse_float, parse_mmyy_date, sqm_to_sqft

WINDOW_MONTHS = 6                 # numerator window for the headline rate
FALLBACK_WINDOW_MONTHS = 24       # window for the days-between-sales fallback
NEW_SALE_TYPES = {"1"}
SECONDARY_SALE_TYPES = {"2", "3"}
# A total derived from new-sale records is only trusted when the launch sits
# comfortably inside the cache window — sales before the cache started would
# silently undercount the project.
DERIVED_MARGIN_MONTHS = 6
DAYS_PER_MONTH = 30.44

# Verdict thresholds. Turnover uses the annualised %: islandwide private
# resale turnover runs ~2–4%/yr, so ≥5% reads as liquid and <2% as tightly
# held. Take-up uses the raw 6-month % of the project sold.
TURNOVER_FAST_PCT = 5.0
TURNOVER_SLOW_PCT = 2.0
TAKEUP_FAST_PCT = 15.0
TAKEUP_SLOW_PCT = 5.0

# Same landed exclusion as ura.search_property — bands only make sense for strata.
_LANDED_TYPES = ("detached", "terrace", "bungalow")


def _iter_banded_sales(txns: list, sale_types: set | None = None,
                       months: int | None = None, now: datetime | None = None):
    """Yield (band_label, contract_date, units) for each qualifying transaction.

    Single place for the filters every liquidity calculation shares: sale-type
    scope, landed exclusion, size-band bucketing, and the rolling window.
    """
    cutoff = None
    if months is not None:
        cutoff = (now or datetime.now()) - relativedelta(months=months)
    for txn in txns:
        if sale_types is not None and str(txn.get("typeOfSale", "")).strip() not in sale_types:
            continue
        if any(t in str(txn.get("propertyType", "")).lower() for t in _LANDED_TYPES):
            continue
        area_sqm = parse_float(txn.get("area", 0))
        if not area_sqm or area_sqm <= 0:
            continue
        band = get_band(sqm_to_sqft(area_sqm))
        if not band:
            continue
        dt = parse_mmyy_date(txn.get("contractDate", ""))
        if dt is None or (cutoff and dt < cutoff):
            continue
        # A single new-sale record can bundle several units; resale is 1.
        units = parse_float(txn.get("noOfUnits"))
        units = int(units) if units and units >= 1 else 1
        yield band, dt, units


def count_sales_in_window(txns: list, sale_types: set, months: int,
                          now: datetime | None = None) -> dict:
    """Units sold per band within the last `months` months: {band_label: units}."""
    counts = {}
    for band, _dt, units in _iter_banded_sales(txns, sale_types, months, now):
        counts[band] = counts.get(band, 0) + units
    return counts


def all_time_band_counts(txns: list, sale_types: set | None = None) -> dict:
    """All-time units transacted per band (no window) — drives the mix estimate."""
    counts = {}
    for band, _dt, units in _iter_banded_sales(txns, sale_types):
        counts[band] = counts.get(band, 0) + units
    return counts


def estimate_band_units(band_counts: dict, total_units: int) -> dict:
    """Apportion `total_units` across bands by their all-time transaction share.

    Largest-remainder rounding so the per-band estimates sum exactly to the
    project total. Returns {} when there is nothing to apportion.
    """
    total_txns = sum(band_counts.values())
    if not total_txns or not total_units:
        return {}
    quotas = {b: c / total_txns * total_units for b, c in band_counts.items()}
    units = {b: int(q) for b, q in quotas.items()}
    leftover = total_units - sum(units.values())
    for b in sorted(quotas, key=lambda b: quotas[b] - units[b], reverse=True)[:leftover]:
        units[b] += 1
    return units


def derive_units_from_new_sales(txns: list, cache_oldest: datetime | None) -> int | None:
    """Tier-3 denominator: total units ≈ sum of new-sale units in the cache.

    Only trusted when the earliest new sale is at least DERIVED_MARGIN_MONTHS
    after the oldest record in the whole cache — otherwise the launch may
    predate the cache and the sum would undercount.
    """
    if cache_oldest is None:
        return None
    earliest = None
    total = 0
    for _band, dt, units in _iter_banded_sales(txns, NEW_SALE_TYPES):
        total += units
        if earliest is None or dt < earliest:
            earliest = dt
    if not total or earliest < cache_oldest + relativedelta(months=DERIVED_MARGIN_MONTHS):
        return None
    return total


def _median_gap_from_dates(dates: list) -> float | None:
    """Median gap in days between consecutive sale dates (month-granular)."""
    if len(dates) < 2:
        return None
    dates = sorted(dates)
    gaps = sorted((b.year - a.year) * 12 + (b.month - a.month)
                  for a, b in zip(dates, dates[1:]))
    mid = len(gaps) // 2
    median_months = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    if median_months <= 0:
        # Several sales landed in the same month — MMYY dates can't resolve the
        # true gap, so spread the observed span evenly across the sales instead.
        span = (dates[-1].year - dates[0].year) * 12 + (dates[-1].month - dates[0].month)
        median_months = max(span / (len(dates) - 1), 0.25)
    return median_months * DAYS_PER_MONTH


def median_gap_days(txns: list, sale_types: set,
                    months: int = FALLBACK_WINDOW_MONTHS,
                    now: datetime | None = None) -> dict:
    """Median days between sales, overall and per band, within the window.

    Bands (or the overall figure) with fewer than 2 sales report None.
    """
    all_dates = []
    band_dates = {}
    for band, dt, units in _iter_banded_sales(txns, sale_types, months, now):
        # Bundled units count as repeated sales at the same date.
        for _ in range(units):
            all_dates.append(dt)
            band_dates.setdefault(band, []).append(dt)
    return {
        "overall": _median_gap_from_dates(all_dates),
        "bands": {band: _median_gap_from_dates(dates) for band, dates in band_dates.items()},
        "window_months": months,
    }


def liquidity_verdict(mode: str, annualised_pct: float | None = None,
                      six_month_pct: float | None = None) -> tuple:
    """(emoji, label) verdict for a rate. Turnover judges the annualised %,
    take-up the raw 6-month %."""
    if mode == "take_up":
        pct, fast, slow = six_month_pct, TAKEUP_FAST_PCT, TAKEUP_SLOW_PCT
        labels = ("Selling fast", "Selling at a typical pace", "Selling slowly")
    else:
        pct, fast, slow = annualised_pct, TURNOVER_FAST_PCT, TURNOVER_SLOW_PCT
        labels = ("Trades often — liquid", "Trades at a typical pace", "Trades rarely — tightly held")
    if pct is None:
        return ("⚪", "Not enough data")
    if pct >= fast:
        return ("🟢", labels[0])
    if pct < slow:
        return ("🔴", labels[2])
    return ("🟡", labels[1])


def liquidity_summary(txns: list, total_units: int | None, units_source: str | None,
                      under_construction: bool, cache_oldest: datetime | None = None,
                      now: datetime | None = None, seed_units: int | None = None) -> dict:
    """Build the full liquidity picture for one development.

    `total_units`/`units_source` carry the tier-1/2 resolution (live pipeline
    or harvested history); tier 3 (derived) and tier 4 (`seed_units`) are
    resolved here so the precedence lives in one place. With no denominator at
    all, `fallback` carries the days-between-sales picture instead.
    """
    mode = "take_up" if under_construction else "turnover"
    sale_types = NEW_SALE_TYPES if mode == "take_up" else SECONDARY_SALE_TYPES

    if not total_units:
        derived = derive_units_from_new_sales(txns, cache_oldest)
        if derived:
            total_units, units_source = derived, "derived"
        elif seed_units:
            total_units, units_source = int(seed_units), "seed"
        else:
            total_units, units_source = None, None

    counts_6m = count_sales_in_window(txns, sale_types, WINDOW_MONTHS, now)
    # Mix estimation uses ALL transactions (every sale type, all time) — the
    # broadest sample of which sizes actually exist in the project.
    all_time = all_time_band_counts(txns)
    overall_count = sum(counts_6m.values())

    summary = {
        "mode": mode,
        "window_months": WINDOW_MONTHS,
        "total_units": total_units,
        "units_source": units_source,
        "units_estimated": units_source == "derived",
        "overall": None,
        "bands": {},
        "band_mix_estimated": False,
        "fallback": None,
    }

    if total_units:
        rate_6m = overall_count / total_units * 100
        annualised = rate_6m * (12 / WINDOW_MONTHS) if mode == "turnover" else None
        emoji, verdict = liquidity_verdict(mode, annualised, rate_6m)
        summary["overall"] = {
            "count_6m": overall_count,
            "rate_6m_pct": rate_6m,
            "annualised_pct": annualised,
            "verdict_emoji": emoji,
            "verdict": verdict,
        }
        est_units = estimate_band_units(all_time, total_units)
        summary["band_mix_estimated"] = bool(est_units)
    else:
        est_units = {}
        summary["fallback"] = median_gap_days(txns, sale_types, FALLBACK_WINDOW_MONTHS, now)

    for band in (b["label"] for b in SIZE_BANDS):
        if band not in all_time and band not in counts_6m:
            continue
        count = counts_6m.get(band, 0)
        units = est_units.get(band)
        rate_6m_pct = count / units * 100 if units else None
        summary["bands"][band] = {
            "count_6m": count,
            "est_units": units,
            "rate_6m_pct": rate_6m_pct,
            "annualised_pct": (rate_6m_pct * (12 / WINDOW_MONTHS)
                               if rate_6m_pct is not None and mode == "turnover" else None),
            "all_time_count": all_time.get(band, 0),
        }
    return summary


def _format_gap(days: float) -> str:
    """Render a gap in days as a friendly '~N weeks/months' string."""
    if days < 10:
        return "~1 week"
    weeks = days / 7
    if weeks < 9:
        return f"~{max(round(weeks), 2)} weeks"
    months = days / DAYS_PER_MONTH
    if months < 23:
        return f"~{round(months)} months"
    return f"~{days / 365.25:.1f} years"


def format_liquidity_summary(s: dict, development: str) -> str:
    """Render a `liquidity_summary` dict as a Markdown Telegram message."""
    take_up = s["mode"] == "take_up"
    lines = [
        f"📊 *Liquidity — {development.title()}*",
        "─────────────────────",
        ("New-sale take-up" if take_up else "Resale + sub-sale activity")
        + f", last {s['window_months']} months",
    ]

    src_labels = {
        "pipeline": "URA pipeline",
        "pipeline_history": "URA pipeline archive",
        "derived": "summed from new-sale records — approximate",
        "seed": "public project records",
    }

    if s["total_units"]:
        approx = "~" if s["units_estimated"] else ""
        lines.append(
            f"🏢 Total units: {approx}{s['total_units']:,} _({src_labels.get(s['units_source'], 'unknown')})_"
        )
        o = s["overall"]
        lines.append("")
        if take_up:
            lines.append(
                f"*Overall:* {o['count_6m']} units sold → {o['rate_6m_pct']:.1f}% "
                f"of the project taken up in {s['window_months']} mo"
            )
        else:
            lines.append(
                f"*Overall:* {o['count_6m']} units changed hands → {o['rate_6m_pct']:.1f}% "
                f"of the project in {s['window_months']} mo (≈{o['annualised_pct']:.1f}%/yr)"
            )
        lines.append(f"{o['verdict_emoji']} {o['verdict']}")

        band_lines = []
        for band in (b["label"] for b in SIZE_BANDS):
            info = s["bands"].get(band)
            if not info or not info["est_units"]:
                continue
            if take_up:
                emoji, _ = liquidity_verdict("take_up", None, info["rate_6m_pct"])
                band_lines.append(
                    f"• {band}: {info['count_6m']} of ~{info['est_units']:,} → "
                    f"{info['rate_6m_pct']:.1f}% in {s['window_months']} mo {emoji}"
                )
            else:
                emoji, _ = liquidity_verdict("turnover", info["annualised_pct"])
                band_lines.append(
                    f"• {band}: {info['count_6m']} of ~{info['est_units']:,} → "
                    f"≈{info['annualised_pct']:.1f}%/yr {emoji}"
                )
        if band_lines:
            lines += ["", "*By size band* _(unit mix estimated from transaction history)_"]
            lines += band_lines

        metric_note = (
            "Take-up = share of the project sold by the developer."
            if take_up else
            "Turnover = share of units changing hands."
        )
        lines += [
            "",
            f"_{metric_note} Band unit counts are estimates; the project total is "
            + ("approximate._" if s["units_estimated"] else "exact._"),
        ]
    else:
        fb = s["fallback"] or {}
        window = fb.get("window_months", FALLBACK_WINDOW_MONTHS)
        lines.append("🏢 Total units: unknown — showing sales pace instead")
        lines.append("")
        if fb.get("overall"):
            lines.append(
                f"*Overall:* a unit sells every {_format_gap(fb['overall'])} "
                f"(last {window} months)"
            )
        else:
            lines.append(
                f"*Overall:* fewer than 2 sales in the last {window} months — "
                "very rarely traded"
            )
        band_lines = []
        for band in (b["label"] for b in SIZE_BANDS):
            info = s["bands"].get(band)
            if not info or not info["all_time_count"]:
                continue
            gap = (fb.get("bands") or {}).get(band)
            if gap:
                band_lines.append(f"• {band}: one sale every {_format_gap(gap)}")
            else:
                band_lines.append(f"• {band}: fewer than 2 sales — too few to gauge")
        if band_lines:
            lines += ["", "*By size band*"] + band_lines

    lines += [
        "",
        "_Based on URA-registered transactions (sales, not asking listings)._",
    ]
    return "\n".join(lines)


# ── IO orchestrator ───────────────────────────────────────────────────────────

def liquidity_for_project(project_name: str) -> dict:
    """Resolve a development name and assemble its liquidity summary.

    Returns {"summary": dict, "development": str} or {"error": str}. Imports
    stay function-local so the pure math above is testable without the cache
    stack, and `get_ura_data` resolves through the `ura` module (one patch
    target covers both the matcher and this loader).
    """
    from ura import _collect_matched_transactions, get_project_info, get_ura_data
    from cache.unit_counts import get_unit_count

    matched = _collect_matched_transactions(project_name)
    if "error" in matched:
        return {"error": matched["error"]}
    if "ambiguous" in matched:
        # Buttons always carry an already-resolved exact name, so this is a
        # belt-and-braces path rather than a real flow.
        return {"error": f'Multiple developments match "{project_name}". Please search again and pick one.'}

    development = matched["matched_project_name"]
    txns = [item["txn"] for item in matched["matched_transactions"]]

    all_results, pipeline = get_ura_data()
    pipeline_info = get_project_info(development, pipeline)
    under_construction = pipeline_info.get("expected_top") is not None

    total_units = None
    units_source = None
    raw_total = parse_float(pipeline_info.get("total_units"))
    if raw_total and raw_total > 0:
        total_units, units_source = int(raw_total), "pipeline"

    seed_units = None
    if not total_units:
        stored = get_unit_count(development)
        if stored:
            if stored["source"] == "pipeline":
                total_units, units_source = stored["total_units"], "pipeline_history"
            else:
                # Seed counts rank below derivation — pass through for tier 4.
                seed_units = stored["total_units"]

    # Oldest contract date in the whole cache — the trust anchor for deriving
    # totals from new-sale records (see derive_units_from_new_sales).
    cache_oldest = None
    for project in all_results:
        for txn in project.get("transaction", []):
            dt = parse_mmyy_date(txn.get("contractDate", ""))
            if dt and (cache_oldest is None or dt < cache_oldest):
                cache_oldest = dt

    summary = liquidity_summary(
        txns, total_units, units_source, under_construction,
        cache_oldest=cache_oldest, seed_units=seed_units,
    )
    return {"summary": summary, "development": development}
