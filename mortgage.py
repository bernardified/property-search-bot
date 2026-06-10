"""
Mortgage & affordability helper for Singapore private residential property.

Pure calculation + formatting helpers (no Telegram / IO) so they're easy to
unit-test. Models the MAS rules that actually move the numbers for a first
private-home loan:

  - **LTV cap 75%** — minimum 25% down payment for a borrower with no other
    outstanding housing loan. (We don't split the 25% into its 5% cash / 20%
    cash-or-CPF components — that's a cash-flow detail, not affordability.)
  - **Loan tenure cap 30 years** for private residential property.
  - **TDSR 55%** — total monthly debt obligations must stay within 55% of gross
    monthly income. MAS requires this to be computed against a **medium-term
    interest-rate floor of 4%** (the stress rate), not the actual loan rate, so
    the "income you'd need" uses the higher of the actual rate and 4%.
  - **Variable-income haircut 30%** — bonus / commission / other non-fixed income
    is recognised at only 70% for TDSR. Fixed base salary counts at 100%.

Stamp duty (BSD / ABSD) is intentionally out of scope.
"""

DEFAULT_LTV = 0.75               # max loan-to-value for a first housing loan
MIN_DOWN_PAYMENT_PCT = 0.25      # 1 - DEFAULT_LTV
MAX_TENURE_YEARS = 30            # regulatory cap for private residential
DEFAULT_TENURE_YEARS = 30
DEFAULT_RATE_PCT = 2.6           # indicative SG home-loan rate; user can override
TDSR_LIMIT = 0.55               # total debt servicing ratio ceiling
TDSR_STRESS_RATE_PCT = 4.0       # MAS medium-term rate floor for TDSR
VARIABLE_INCOME_HAIRCUT = 0.30   # variable income (bonus/commission) counts at 70%


def monthly_installment(loan: float, annual_rate_pct: float, tenure_years: float) -> float:
    """Standard amortising monthly repayment.

    M = P · r(1+r)^n / ((1+r)^n − 1), where r is the monthly rate and n the
    number of months. Falls back to straight-line (P/n) when the rate is 0.
    """
    if loan <= 0 or tenure_years <= 0:
        return 0.0
    n = tenure_years * 12
    r = (annual_rate_pct / 100) / 12
    if r == 0:
        return loan / n
    factor = (1 + r) ** n
    return loan * r * factor / (factor - 1)


def mortgage_summary(
    price: float,
    down_payment: float,
    annual_rate_pct: float = DEFAULT_RATE_PCT,
    tenure_years: float = DEFAULT_TENURE_YEARS,
    monthly_income: float | None = None,
    variable_income: float = 0.0,
) -> dict:
    """Compute the loan + affordability picture for a purchase.

    `down_payment` is an absolute dollar amount; the loan is the remainder.
    `monthly_income` is total gross monthly income; `variable_income` is the
    portion of it that is non-fixed (bonus/commission), which the TDSR check
    recognises at only 70% (the MAS 30% haircut). Returns every figure the
    formatter needs, plus flags for the two limits a buyer is most likely to
    trip: LTV (down payment under 25%) and — when an income is given — TDSR.
    """
    price = float(price)
    down_payment = float(down_payment)
    loan = max(price - down_payment, 0.0)

    down_payment_pct = (down_payment / price * 100) if price > 0 else 0.0
    ltv_pct = (loan / price * 100) if price > 0 else 0.0
    # Down payment below 25% means the loan exceeds the 75% LTV cap.
    ltv_exceeded = ltv_pct > DEFAULT_LTV * 100 + 1e-9

    installment = monthly_installment(loan, annual_rate_pct, tenure_years)

    # TDSR is assessed at the 4% medium-term floor (or the actual rate if higher).
    stress_rate = max(annual_rate_pct, TDSR_STRESS_RATE_PCT)
    stress_installment = monthly_installment(loan, stress_rate, tenure_years)
    # Income needed so the stressed installment alone fits inside TDSR (assumes
    # no other monthly debt obligations).
    required_income = stress_installment / TDSR_LIMIT if stress_installment > 0 else 0.0

    n_months = tenure_years * 12
    total_repayment = installment * n_months
    total_interest = max(total_repayment - loan, 0.0)

    summary = {
        "price": price,
        "down_payment": down_payment,
        "down_payment_pct": down_payment_pct,
        "loan": loan,
        "ltv_pct": ltv_pct,
        "ltv_exceeded": ltv_exceeded,
        "rate_pct": annual_rate_pct,
        "tenure_years": tenure_years,
        "monthly_installment": installment,
        "total_interest": total_interest,
        "total_repayment": total_repayment,
        "stress_rate_pct": stress_rate,
        "stress_installment": stress_installment,
        "required_income": required_income,
        "monthly_income": None,
        "variable_income": 0.0,
        "eligible_income": None,
        "tdsr_ratio": None,
        "tdsr_pass": None,
    }

    if monthly_income and monthly_income > 0:
        # Apply the 30% haircut to the variable portion; the rest counts in full.
        variable = max(min(variable_income or 0.0, monthly_income), 0.0)
        eligible = monthly_income - VARIABLE_INCOME_HAIRCUT * variable
        ratio = stress_installment / eligible if eligible > 0 else float("inf")
        summary["monthly_income"] = float(monthly_income)
        summary["variable_income"] = float(variable)
        summary["eligible_income"] = eligible
        summary["tdsr_ratio"] = ratio
        summary["tdsr_pass"] = ratio <= TDSR_LIMIT + 1e-9

    return summary


def format_mortgage_summary(s: dict) -> str:
    """Render a `mortgage_summary` dict as a Markdown Telegram message."""
    lines = [
        "🏦 *Mortgage & Affordability*",
        "─────────────────────",
        f"🏠 Property price: *S${s['price']:,.0f}*",
        f"💵 Down payment: S${s['down_payment']:,.0f} _({s['down_payment_pct']:.0f}%)_",
        f"🏦 Loan amount: S${s['loan']:,.0f} _(LTV {s['ltv_pct']:.0f}%)_",
        f"📈 Rate: {s['rate_pct']:.2f}% p.a.  ·  ⏳ Tenure: {s['tenure_years']:.0f} yrs",
        "",
        f"💳 *Monthly repayment: S${s['monthly_installment']:,.0f}*",
        f"   _Total interest over {s['tenure_years']:.0f} yrs: S${s['total_interest']:,.0f}_",
    ]

    if s["ltv_exceeded"]:
        lines += [
            "",
            "⚠️ _Down payment is below 25%, so the loan exceeds the 75% LTV cap "
            "for a first housing loan. You'd need a larger down payment._",
        ]

    lines += [
        "",
        "📊 *Affordability (TDSR)*",
        "─────────────────────",
        f"Eligible income needed: *S${s['required_income']:,.0f}/mo*",
        f"_TDSR limit is 55% of recognised income, stress-tested at {s['stress_rate_pct']:.1f}% "
        f"(monthly instalment at that rate: S${s['stress_installment']:,.0f})._",
    ]

    if s["monthly_income"] is not None:
        verdict = "✅ Within TDSR" if s["tdsr_pass"] else "❌ Exceeds TDSR"
        lines.append("")
        if s["variable_income"] > 0:
            pct = int(VARIABLE_INCOME_HAIRCUT * 100)
            lines += [
                f"Your income: S${s['monthly_income']:,.0f}/mo "
                f"_(incl. S${s['variable_income']:,.0f} variable)_",
                f"After {pct}% haircut on variable → eligible "
                f"*S${s['eligible_income']:,.0f}/mo*",
                f"TDSR {s['tdsr_ratio'] * 100:.0f}%  {verdict}",
            ]
        else:
            lines.append(
                f"Your income: S${s['monthly_income']:,.0f}/mo  →  "
                f"TDSR {s['tdsr_ratio'] * 100:.0f}%  {verdict}"
            )

    lines += [
        "",
        "_Estimates only — excludes stamp duty (BSD/ABSD), other debts, and "
        "lender-specific rules. Verify with a mortgage banker._",
    ]
    return "\n".join(lines)
