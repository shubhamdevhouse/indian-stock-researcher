"""Earnings-quality score (0-15) and red-flag gates from quarterly results filings.

Input is the list of stored quarters from data/filings.py (oldest first). As in signals.py, every scored item
carries an `evidence` string with the exact values, so the agents can quote it. NSE integrated filings only
start at the Mar-2025 quarter, so growth is year-on-year for the same quarter, not a multi-year trend.
"""
from datetime import date, timedelta

from .signals import _f, pct

MAX_POINTS = 15
NEUTRAL_POINTS = MAX_POINTS / 2  # used when a company has no parseable filing
WEIGHTS = {"growth": 6, "profitability": 4, "quality": 5}
STALE_DAYS = 135  # latest quarter older than this = results overdue (SEBI: 45 days, 60 for Q4)
CR = 1e7


def quarter_label(d: date) -> str:
    """Indian financial-year label: 2026-06-30 -> 'Q1 FY27'."""
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(d.month)
    fy = d.year + 1 if d.month >= 4 else d.year
    return f"Q{q} FY{fy % 100:02d}" if q else d.isoformat()


def cr(x) -> str:
    return "n/a" if x is None else f"₹{x / CR:,.0f} Cr"


def _chg(cur, base):
    """% change; None when the base is missing or not positive (growth off a loss is meaningless)."""
    if cur is None or base is None or base <= 0:
        return None
    return (cur / base - 1) * 100


def _ratio(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _pctpt(x):
    """Ratios are filed as fractions (0.0117 = 1.17%) by most filers and as percents by some."""
    if x is None:
        return None
    return x * 100 if abs(x) <= 1 else x


RATIO_KEYS = ("gnpa", "nnpa", "cet1", "roa", "combined", "expense_ratio", "solvency")


def _harmonise(series, keys=RATIO_KEYS):
    """Some filers switch between fraction and percent across quarters (SBILIFE: solvency 0.0196 → 1.96).
    Rescale any value ~100× off the latest quarter's value to the latest quarter's units."""
    for key in keys:
        ref = series[-1][1].get(key)
        if not ref:
            continue
        for _, m, _ in series[:-1]:
            v = m.get(key)
            if v:
                r = abs(ref / v)
                if 30 < r < 300:
                    m[key] = v * 100
                elif 1 / 300 < r < 1 / 30:
                    m[key] = v / 100


def _fix_attributable(series):
    """Owners' share of profit is mis-tagged by some filers (0, or a stray sub-total). Use the company's usual
    owners' share of total profit for any quarter that is missing or more than 15 points off it."""
    shares = sorted(m["pat"] / m["pat_total"] for _, m, _ in series
                    if m.get("pat") and m.get("pat_total") and m["pat_total"] > 0 and m["pat"] > 0)
    usual = shares[len(shares) // 2] if shares else 1.0
    for _, m, _ in series:
        total = m.get("pat_total")
        if not total:
            continue
        if not m.get("pat") or (total > 0 and abs(m["pat"] / total - usual) > 0.15):
            m["pat"] = total * usual


def _lin(x, lo, hi, pts):
    """0 at lo, pts at hi, linear between (lo > hi for 'lower is better')."""
    t = (x - lo) / (hi - lo)
    return round(pts * min(1.0, max(0.0, t)), 2)


class _Bucket:
    def __init__(self):
        self.signals: list[dict] = []
        self.points = {k: 0.0 for k in WEIGHTS}

    def add(self, bucket, name, pts, max_pts, evidence, value=None):
        """pts=None means the metric could not be computed: award half the points as neutral."""
        if pts is None:
            pts, direction, evidence = max_pts / 2, "neutral", f"{evidence}: n/a (neutral {max_pts / 2:g} pts)"
        else:
            direction = "bull" if pts >= 0.6 * max_pts else "bear" if pts <= 0.3 * max_pts else "neutral"
        self.points[bucket] += pts
        self.signals.append({"bucket": f"fundamental_{bucket}", "name": name, "direction": direction,
                             "points": round(pts, 2), "max": max_pts, "evidence": evidence, "value": _f(value)})


# ---------- per-quarter extraction ----------

def _g(facts: dict, *tags):
    for t in tags:
        v = facts.get(t)
        if v is not None:
            return v
    return None


def _extract(profile: str, d: dict) -> dict:
    q, bs, ytd = d.get("q", {}), d.get("bs", {}), d.get("ytd", {})
    m = {"pat": _g(q, "ProfitOrLossAttributableToOwnersOfParent",
                   "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
                   "ProfitLossForPeriod", "ProfitLossForThePeriod",
                   "ProfitLossAfterTaxAndExtraordinaryItems", "ProfitLossAfterTax"),
         "eps": _g(q, "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                   "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                   "DilutedEarningsPerShareAfterExtraordinaryItems", "BasicEarningsPerShareAfterExtraordinaryItems",
                   "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"),
         "pat_total": _g(q, "ProfitLossForPeriod", "ProfitLossForThePeriod"),
         "equity": _g(bs, "EquityAttributableToOwnersOfParent", "Equity")}
    if profile == "bank":
        ii, ie = _g(q, "InterestEarned"), _g(q, "InterestExpended")
        m.update(top=None if ii is None or ie is None else ii - ie, top_name="NII",
                 ppop=_g(q, "OperatingProfitBeforeProvisionAndContingencies"),
                 prov=_g(q, "ProvisionsOtherThanTaxAndContingencies"),
                 gnpa=_pctpt(_g(q, "PercentageOfGrossNpa") or None), nnpa=_pctpt(_g(q, "PercentageOfNpa") or None),
                 cet1=_pctpt(_g(q, "CET1Ratio") or None), roa=_pctpt(_g(q, "ReturnOnAssets") or None))
    elif profile == "nbfc":
        ii, fin = _g(q, "InterestEarned"), _g(q, "FinanceCosts")
        nii = None if ii is None or fin is None else ii - fin
        imp = _g(q, "ImpairmentOnFinancialInstruments")
        opex = sum(_g(q, t) or 0 for t in ("EmployeeBenefitExpense", "DepreciationDepletionAndAmortisationExpense",
                                          "OtherExpenses", "FeesAndCommissionExpense"))
        net_income = None if _g(q, "Income") is None or fin is None else _g(q, "Income") - fin
        m.update(top=nii, top_name="NII", credit_cost=_ratio(imp, nii), impairment=imp,
                 cost_income=_ratio(opex, net_income) if opex else None)
    elif profile in ("life_insurance", "general_insurance"):
        m.update(top=_g(q, "GrossPremiumIncome", "GrossPremiumsWritten"), top_name="Gross premium",
                 combined=_pctpt(_g(q, "CombinedRatio") or None),
                 expense_ratio=_pctpt(_g(q, "ExpensesOfManagementRatio") or None),
                 solvency=_g(q, "SolvencyRatio") or None)
    else:
        rev, fin = _g(q, "RevenueFromOperations"), _g(q, "FinanceCosts") or 0.0
        pbt = _g(q, "ProfitBeforeExceptionalItemsAndTax", "ProfitBeforeTax")
        exc = _g(q, "ExceptionalItemsBeforeTax") or 0.0
        if _g(q, "ProfitBeforeExceptionalItemsAndTax") is None and pbt is not None:
            pbt -= exc  # PBT includes exceptional items; strip them
        da, oi = _g(q, "DepreciationDepletionAndAmortisationExpense") or 0.0, _g(q, "OtherIncome") or 0.0
        ebitda = None if pbt is None else pbt + fin + da - oi
        borrow = [bs.get("BorrowingsCurrent"), bs.get("BorrowingsNoncurrent")]
        m.update(top=rev, top_name="Revenue", ebitda=ebitda, margin=_ratio(ebitda, rev), pbt=pbt, exceptional=exc,
                 other_income=oi, finance=fin,
                 icr=None if pbt is None or fin <= 0 else (pbt + fin) / fin,
                 borrowings=None if all(b is None for b in borrow) else sum(b or 0 for b in borrow),
                 cfo_ytd=ytd.get("CashFlowsFromUsedInOperatingActivities"),
                 pat_ytd=_g(ytd, "ProfitLossForPeriod", "ProfitOrLossAttributableToOwnersOfParent"),
                 ytd_months=d.get("ytd_months"))
    return m


def _ttm(series, key):
    """Sum of the last four quarters (None unless all four are present)."""
    end = series[-1][0]
    last4 = [m.get(key) for d, m, _ in series if (end - d).days < 360]
    return sum(last4) if len(last4) == 4 and None not in last4 else None


def _latest_bs(series):
    """(date, metrics) of the newest quarter that carries a balance sheet (Q2/Q4 filings)."""
    return next(((d, m) for d, m, _ in reversed(series) if m.get("equity")), None)


def _roe(series):
    ttm, bs = _ttm(series, "pat"), _latest_bs(series)
    return (ttm / bs[1]["equity"] * 100, ttm, bs) if ttm is not None and bs else (None, ttm, bs)


def _audit_flag(text: dict) -> str | None:
    s = (text.get("DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification") or "").lower()
    if s and "unmodified" not in s and "not applicable" not in s and ("qualif" in s or "modified" in s):
        return text["DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification"]
    return None


# ---------- analysis ----------

def analyze(quarters: list[dict], close: float | None = None, today: date | None = None) -> dict:
    if not quarters:
        return {"available": False, "points": NEUTRAL_POINTS, "max": MAX_POINTS,
                "note": "fundamentals: unavailable (no integrated results filing found)", "red_flags": []}
    today = today or date.today()
    profile = quarters[-1].get("taxonomy") or "corporate"
    series = [(date.fromisoformat(r["period_end"]), _extract(profile, r["data"]), r) for r in quarters]
    _harmonise(series)
    _fix_attributable(series)
    end, cur, latest_row = series[-1]

    def back(days):
        target = end - timedelta(days=days)
        return next((m for d, m, _ in series if abs((d - target).days) <= 10), None)

    yoy, prev = back(365), back(91)
    label, yoy_label = quarter_label(end), quarter_label(end - timedelta(days=365))
    s = _Bucket()
    top_name = cur.get("top_name", "Revenue")

    # ---- growth (6) ----
    top_yoy = _chg(cur.get("top"), yoy and yoy.get("top"))
    s.add("growth", "topline_yoy", None if top_yoy is None else _lin(top_yoy, -5, 20, 3), 3,
          f"{top_name} {cr(cur.get('top'))} vs {cr(yoy and yoy.get('top'))} → {pct(top_yoy)} YoY ({label} vs {yoy_label})"
          if top_yoy is not None else f"{top_name} YoY growth", top_yoy)
    pat, pat_base = cur.get("pat"), yoy and yoy.get("pat")
    pat_yoy = _chg(pat, pat_base)
    if pat_yoy is not None:
        s.add("growth", "pat_yoy", _lin(pat_yoy, -10, 25, 3), 3,
              f"Net profit {cr(pat)} vs {cr(pat_base)} → {pct(pat_yoy)} YoY ({label} vs {yoy_label})", pat_yoy)
    elif pat is not None and pat_base is not None:  # base was a loss
        s.add("growth", "pat_yoy", 3 if pat > 0 else 0, 3,
              f"Net profit {cr(pat)} vs {cr(pat_base)} a year ago ({'turned profitable' if pat > 0 else 'still loss-making'})")
    else:
        s.add("growth", "pat_yoy", None, 3, "Net profit YoY growth")

    # ---- profitability trend (4) and quality (5) ----
    metrics: dict = {"topline_yoy_pct": _f(top_yoy, 1), "pat_yoy_pct": _f(pat_yoy, 1)}
    pat_qoq = _chg(pat, prev and prev.get("pat"))
    if profile == "bank":
        _bank(s, cur, yoy, prev, metrics, label)
    elif profile == "nbfc":
        _nbfc(s, cur, yoy, prev, metrics, series, label)
    elif profile in ("life_insurance", "general_insurance"):
        _insurer(s, profile, cur, yoy, pat_qoq, metrics, label)
    else:
        _corporate(s, cur, yoy, pat_qoq, metrics, series, label)

    # ---- TTM and valuation context (not scored) ----
    ttm_pat, ttm_eps = _ttm(series, "pat"), _ttm(series, "eps")
    eps, eps_yoy = cur.get("eps"), _chg(cur.get("eps"), yoy and yoy.get("eps"))
    if ttm_pat and pat and pat > 0 and eps and eps > 0:
        ttm_eps = ttm_pat * eps / pat  # on today's share count, so a split/bonus inside the window can't distort it
    if eps_yoy is not None and pat_yoy is not None and pat > 0 and pat_base > 0 and abs(eps_yoy - pat_yoy) > 25:
        metrics["eps_note"] = (f"EPS {pct(eps_yoy)} YoY vs net profit {pct(pat_yoy)} YoY: share count changed "
                               "(split/bonus/issue); EPS growth and PEG not used")
        eps_yoy = None
    metrics.update(ttm_pat_cr=_f(ttm_pat and ttm_pat / CR, 0), ttm_eps=_f(ttm_eps), eps_yoy_pct=_f(eps_yoy, 1),
                   pat_qoq_pct=_f(pat_qoq, 1))
    if close and ttm_eps and ttm_eps > 0:
        pe = close / ttm_eps
        metrics["pe_ttm"] = _f(pe, 1)
        if eps_yoy and eps_yoy > 0:
            metrics["peg"] = _f(pe / eps_yoy)
    if metrics.get("roe_pct") is None:
        metrics["roe_pct"] = _f(_roe(series)[0], 1)

    # ---- gates ----
    red, soft = [], []
    if pat is not None and pat < 0:
        red.append({"name": "loss_making", "evidence": f"Net loss {cr(pat)} in {label}"})
    if pat_yoy is not None and pat_yoy < -30 and top_yoy is not None and top_yoy < 0:
        red.append({"name": "earnings_collapse",
                    "evidence": f"Net profit {pct(pat_yoy)} YoY with {top_name} {pct(top_yoy)} YoY ({label})"})
    if audit := _audit_flag(latest_row["data"].get("text", {})):
        red.append({"name": "audit_qualification", "evidence": f"Auditor: {audit} ({label})"})
    age = (today - end).days
    if age > STALE_DAYS:
        red.append({"name": "results_overdue",
                    "evidence": f"Latest filed results are for {label} ({age} days old); newer results are overdue"})
    red += metrics.pop("_red", [])
    if (latest_row.get("type_sub") or "").lower().startswith("revis"):
        soft.append(f"{label} results were revised after the original filing")

    score = {k: round(min(s.points[k], WEIGHTS[k]), 1) for k in WEIGHTS}
    return {
        "available": True, "profile": profile, "nature": latest_row.get("nature"),
        "latest_quarter": label, "period_end": end.isoformat(), "results_date": latest_row.get("broadcast_date"),
        "points": round(sum(score.values()), 1), "max": MAX_POINTS, "breakdown": score,
        "metrics": metrics, "signals": s.signals, "red_flags": red, "soft_flags": soft,
        "quarters": [_row(profile, d, m) for d, m, _ in series[-6:]],
        "source": "NSE integrated filing (XBRL)",
    }


def _row(profile, d, m) -> dict:
    out = {"quarter": quarter_label(d), "period_end": d.isoformat(),
           f"{m.get('top_name', 'Revenue').lower().replace(' ', '_')}_cr": _f(m.get("top") and m["top"] / CR, 0),
           "pat_cr": _f(m.get("pat") and m["pat"] / CR, 0), "eps": _f(m.get("eps"))}
    if profile == "corporate":
        out["ebitda_margin_pct"] = _f(m.get("margin") and m["margin"] * 100, 1)
    elif profile == "bank":
        out.update(gnpa_pct=_f(m.get("gnpa")), nnpa_pct=_f(m.get("nnpa")))
    elif profile == "nbfc":
        out["credit_cost_pct_of_nii"] = _f(m.get("credit_cost") and m["credit_cost"] * 100, 1)
    return out


def _cover(icr):
    return f"{icr:.1f}×" if icr is not None else "unknown"


def _corporate(s, cur, yoy, pat_qoq, metrics, series, label):
    mg, mg_base = cur.get("margin"), yoy and yoy.get("margin")
    bps = None if mg is None or mg_base is None else (mg - mg_base) * 1e4
    s.add("profitability", "ebitda_margin_yoy", None if bps is None else _lin(bps, -200, 200, 3), 3,
          f"EBITDA margin {mg * 100:.1f}% vs {mg_base * 100:.1f}% a year ago ({bps:+.0f} bps)"
          if bps is not None else "EBITDA margin YoY", bps)
    s.add("profitability", "pat_qoq", None if pat_qoq is None else (1 if pat_qoq > 0 else 0), 1,
          f"Net profit {pct(pat_qoq)} QoQ" if pat_qoq is not None else "Net profit QoQ", pat_qoq)

    pbt = cur.get("pbt")
    oi_share = _ratio(cur.get("other_income"), pbt) if pbt and pbt > 0 else None
    s.add("quality", "other_income_share", None if oi_share is None else _lin(oi_share * 100, 35, 15, 1), 1,
          f"Other income {cr(cur.get('other_income'))} = {oi_share * 100:.0f}% of pre-exceptional PBT {cr(pbt)}"
          if oi_share is not None else "Other income share of PBT", oi_share and oi_share * 100)
    exc_share = _ratio(abs(cur.get("exceptional") or 0), abs(pbt)) if pbt else None
    s.add("quality", "exceptional_items", None if exc_share is None else (1 if exc_share < 0.10 else 0), 1,
          f"Exceptional items {cr(cur.get('exceptional'))} = {exc_share * 100:.0f}% of PBT"
          if exc_share is not None else "Exceptional items share of PBT", exc_share and exc_share * 100)
    icr = cur.get("icr")
    if icr is None and cur.get("finance") == 0 and pbt is not None:
        s.add("quality", "interest_coverage", 1, 1, "No finance cost (debt-free)")
    else:
        s.add("quality", "interest_coverage", None if icr is None else _lin(icr, 1.5, 5, 1), 1,
              f"Interest coverage {icr:.1f}× (PBT + finance cost {cr(cur.get('finance'))} / finance cost)"
              if icr is not None else "Interest coverage", icr)
    cash = next(((d, m) for d, m, _ in reversed(series) if m.get("cfo_ytd") is not None and m.get("pat_ytd")), None)
    conv = None
    if cash and cash[1]["pat_ytd"] > 0:
        conv = cash[1]["cfo_ytd"] / cash[1]["pat_ytd"]
        span = f"{cash[1].get('ytd_months') or '?'}M to {quarter_label(cash[0])}"
    s.add("quality", "cash_conversion", None if conv is None else _lin(conv, 0.5, 0.9, 1), 1,
          f"Operating cash flow {cr(cash[1]['cfo_ytd'])} = {conv:.2f}× net profit {cr(cash[1]['pat_ytd'])} ({span})"
          if conv is not None else "Operating cash flow / net profit", conv)

    roe, ttm, bs = _roe(series)
    s.add("quality", "roe", None if roe is None else _lin(roe, 8, 18, 1), 1,
          f"ROE {roe:.1f}% (TTM net profit {cr(ttm)} / equity {cr(bs[1]['equity'])} at {quarter_label(bs[0])})"
          if roe is not None else "ROE (TTM)", roe)
    metrics.update(ebitda_margin_pct=_f(mg and mg * 100, 1), margin_change_bps=_f(bps, 0),
                   interest_coverage=_f(icr, 1), cash_conversion=_f(conv), roe_pct=_f(roe, 1))

    red = []
    if icr is not None and icr < 1.5:
        red.append({"name": "weak_debt_service", "evidence": f"Interest coverage {icr:.2f}× (< 1.5) in {label}"})
    if bs and bs[1].get("borrowings") is not None:
        de = bs[1]["borrowings"] / bs[1]["equity"]
        metrics["debt_equity"] = _f(de)
        if de > 2 and (icr is None or icr < 3):  # a lending arm can lift D/E while interest is well covered
            red.append({"name": "high_leverage", "evidence": f"Borrowings {cr(bs[1]['borrowings'])} = {de:.2f}× "
                                                             f"equity at {quarter_label(bs[0])} (> 2) with interest "
                                                             f"coverage {_cover(icr)}"})
    metrics["_red"] = red


def _bank(s, cur, yoy, prev, metrics, label):
    ppop_yoy = _chg(cur.get("ppop"), yoy and yoy.get("ppop"))
    s.add("profitability", "ppop_yoy", None if ppop_yoy is None else _lin(ppop_yoy, -5, 20, 2), 2,
          f"Operating profit before provisions {cr(cur.get('ppop'))} → {pct(ppop_yoy)} YoY"
          if ppop_yoy is not None else "Pre-provision operating profit YoY", ppop_yoy)
    cc, cc_base = _ratio(cur.get("prov"), cur.get("ppop")), yoy and _ratio(yoy.get("prov"), yoy.get("ppop"))
    delta = None if cc is None or cc_base is None else (cc - cc_base) * 100
    s.add("profitability", "provisions_to_ppop", None if delta is None else _lin(delta, 10, -10, 2), 2,
          f"Provisions {cr(cur.get('prov'))} = {cc * 100:.0f}% of pre-provision profit vs {cc_base * 100:.0f}% a year ago"
          if delta is not None else "Provisions / pre-provision profit trend", delta)

    g, g_prev = cur.get("gnpa"), prev and prev.get("gnpa")
    g_chg = None if g is None or g_prev is None else g - g_prev
    s.add("quality", "gnpa_trend", None if g_chg is None else _lin(g_chg, 0.25, -0.10, 2), 2,
          f"Gross NPA {g:.2f}% vs {g_prev:.2f}% last quarter ({g_chg * 100:+.0f} bps)"
          if g_chg is not None else "Gross NPA trend", g_chg)
    cet1 = cur.get("cet1")
    s.add("quality", "cet1", None if cet1 is None else _lin(cet1, 10, 13, 1), 1,
          f"CET1 ratio {cet1:.2f}%" if cet1 is not None else "CET1 ratio", cet1)
    roa = cur.get("roa") * 4 if cur.get("roa") is not None else None  # filed for the quarter, not annualised
    s.add("quality", "roa", None if roa is None else _lin(roa, 0.6, 1.4, 2), 2,
          f"ROA {roa:.2f}% annualised ({cur['roa']:.2f}% for the quarter)" if roa is not None else "ROA", roa)
    metrics.update(gnpa_pct=_f(g), nnpa_pct=_f(cur.get("nnpa")), cet1_pct=_f(cet1), roa_annualised_pct=_f(roa),
                   ppop_yoy_pct=_f(ppop_yoy, 1))

    red = []
    n, n_prev = cur.get("nnpa"), prev and prev.get("nnpa")
    if g_chg is not None and g_chg > 0.25 and n is not None and n_prev is not None and n > n_prev:
        red.append({"name": "asset_quality_slip", "evidence": f"Gross NPA {g_prev:.2f}% → {g:.2f}% and net NPA "
                                                              f"{n_prev:.2f}% → {n:.2f}% QoQ ({label})"})
    if cet1 is not None and cet1 < 10:
        red.append({"name": "thin_capital", "evidence": f"CET1 ratio {cet1:.2f}% (< 10%) in {label}"})
    metrics["_red"] = red


def _nbfc(s, cur, yoy, prev, metrics, series, label):
    cc, cc_base = cur.get("credit_cost"), yoy and yoy.get("credit_cost")
    delta = None if cc is None or cc_base is None else (cc - cc_base) * 100
    s.add("profitability", "credit_cost_yoy", None if delta is None else _lin(delta, 10, -10, 2), 2,
          f"Impairment {cr(cur.get('impairment'))} = {cc * 100:.0f}% of NII vs {cc_base * 100:.0f}% a year ago"
          if delta is not None else "Credit cost (impairment / NII) YoY", delta)
    ci, ci_base = cur.get("cost_income"), yoy and yoy.get("cost_income")
    ci_d = None if ci is None or ci_base is None else (ci - ci_base) * 100
    s.add("profitability", "cost_to_income_yoy", None if ci_d is None else _lin(ci_d, 5, -5, 2), 2,
          f"Cost-to-income {ci * 100:.0f}% vs {ci_base * 100:.0f}% a year ago"
          if ci_d is not None else "Cost-to-income YoY", ci_d)
    cc_prev = prev and prev.get("credit_cost")
    q_d = None if cc is None or cc_prev is None else (cc - cc_prev) * 100
    s.add("quality", "credit_cost_qoq", None if q_d is None else _lin(q_d, 5, -5, 2), 2,
          f"Credit cost {cc * 100:.0f}% of NII vs {cc_prev * 100:.0f}% last quarter"
          if q_d is not None else "Credit cost QoQ", q_d)
    roe, ttm, bs = _roe(series)
    s.add("quality", "roe", None if roe is None else _lin(roe, 10, 18, 3), 3,
          f"ROE {roe:.1f}% (TTM net profit {cr(ttm)} / equity {cr(bs[1]['equity'])})" if roe is not None else "ROE (TTM)",
          roe)
    metrics.update(credit_cost_pct_of_nii=_f(cc and cc * 100, 1), cost_to_income_pct=_f(ci and ci * 100, 1),
                   roe_pct=_f(roe, 1))
    red = []
    if cc is not None and cc_base and cc_base > 0 and cc > 0.05 and cc > 1.5 * cc_base:
        red.append({"name": "credit_cost_spike", "evidence": f"Impairment {cc * 100:.0f}% of NII vs "
                                                             f"{cc_base * 100:.0f}% a year ago ({label})"})
    metrics["_red"] = red


def _insurer(s, profile, cur, yoy, pat_qoq, metrics, label):
    key, name = ("combined", "Combined ratio") if profile == "general_insurance" else ("expense_ratio",
                                                                                      "Expense-of-management ratio")
    r, r_base = cur.get(key), yoy and yoy.get(key)
    delta = None if r is None or r_base is None else r - r_base
    s.add("profitability", f"{key}_yoy", None if delta is None else _lin(delta, 3, -3, 2), 2,
          f"{name} {r:.1f}% vs {r_base:.1f}% a year ago" if delta is not None else f"{name} YoY", delta)
    s.add("profitability", "pat_qoq", None if pat_qoq is None else (2 if pat_qoq > 0 else 0), 2,
          f"Net profit {pct(pat_qoq)} QoQ" if pat_qoq is not None else "Net profit QoQ", pat_qoq)
    s.add("quality", "insurer_quality", None, 5, "Balance-sheet quality for insurers (not in the quarterly filing)")
    metrics.update({f"{key}_pct": _f(r, 1), "solvency_ratio_as_filed": _f(cur.get("solvency")), "_red": []})
