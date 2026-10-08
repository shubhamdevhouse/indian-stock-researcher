from datetime import date
from pathlib import Path

import pytest

from stock_researcher.analysis import fundamentals as F
from stock_researcher.analysis import signals
from stock_researcher.data.filings import parse_xbrl, profile_of

FIX = Path(__file__).parent / "fixtures"
CR = 1e7
PERIODS = ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
TODAY = date(2026, 8, 1)


def _xbrl(name):
    return parse_xbrl((FIX / f"{name}.xml").read_text(encoding="utf-8"))


def _row(period, q, taxonomy="corporate", bs=None, ytd=None, text=None, type_sub="Original"):
    return {"period_end": period, "taxonomy": taxonomy, "nature": "consolidated", "type_sub": type_sub,
            "broadcast_date": "01-Aug-2026 10:00:00",
            "data": {"period_end": period, "q": q, "bs": bs or {}, "ytd": ytd or {}, "text": text or {},
                     "ytd_months": 6 if ytd else None}}


def _corp_q(rev, pat, fin=10, oi=5, da=20, pbt=None, eps=None):
    pbt = pat * 1.33 if pbt is None else pbt
    return {"RevenueFromOperations": rev * CR, "ProfitOrLossAttributableToOwnersOfParent": pat * CR,
            "ProfitBeforeTax": pbt * CR, "FinanceCosts": fin * CR, "OtherIncome": oi * CR,
            "DepreciationDepletionAndAmortisationExpense": da * CR,
            "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": eps or pat / 10}


def _corp_series(revs, pats, **last):
    rows = [_row(p, _corp_q(r, n)) for p, r, n in zip(PERIODS, revs, pats)]
    if last:
        rows[-1] = _row(PERIODS[-1], _corp_q(revs[-1], pats[-1], **{k: v for k, v in last.items()
                                                                     if k in ("fin", "oi", "da", "pbt", "eps")}),
                        bs=last.get("bs"), text=last.get("text"), type_sub=last.get("type_sub", "Original"))
    return rows


def _names(a):
    return {r["name"] for r in a["red_flags"]}


# ---------- parser ----------

def test_profile_from_url():
    assert profile_of("https://x/INTEGRATED_FILING_INDAS_1412101_11042025074137_WEB.xml") == "corporate"
    assert profile_of("https://x/INTEGRATED_FILING_BANKING_85786_1_WEB.xml") == "bank"
    assert profile_of("https://x/INTEGRATED_FILING_NBFC_INDAS_88426_1_WEB.xml") == "nbfc"
    assert profile_of("https://x/INTEGRATED_FILING_LI_1_2_WEB.xml") == "life_insurance"
    assert profile_of("https://x/INTEGRATED_FILING_GI_1_2_WEB.xml") == "general_insurance"
    assert profile_of(None) is None


def test_parse_corporate_quarter():
    p = _xbrl("tcs_indas_consolidated")
    assert p["period_end"] == "2026-06-30"
    assert p["q"]["RevenueFromOperations"] == pytest.approx(72275 * CR)
    assert p["q"]["ProfitOrLossAttributableToOwnersOfParent"] == pytest.approx(13349 * CR)
    assert p["ytd"] == {} and p["ytd_months"] is None  # Q1: no year-to-date context
    assert "unmodified" in p["text"]["DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification"].lower()


def test_parse_q2_has_balance_sheet_and_h1_cash_flow():
    p = _xbrl("tcs_indas_consolidated_q2")
    assert p["ytd_months"] == 6
    assert p["bs"]["Equity"] > 0
    assert p["ytd"]["CashFlowsFromUsedInOperatingActivities"] > 0


def test_parse_bank_and_nbfc():
    b = _xbrl("hdfcbank_banking_standalone")
    assert b["q"]["PercentageOfGrossNpa"] == pytest.approx(0.0117)
    m = F._extract("bank", b)
    assert m["gnpa"] == pytest.approx(1.17) and m["cet1"] == pytest.approx(19.57)
    assert m["top"] > 0  # NII = interest earned - interest expended
    n = F._extract("nbfc", _xbrl("bajfinance_nbfc_consolidated"))
    assert n["credit_cost"] == pytest.approx(n["impairment"] / n["top"])


def test_fixture_end_to_end():
    rows = [_row("2026-06-30", _xbrl("tcs_indas_consolidated")["q"])]
    a = F.analyze(rows, today=TODAY)
    assert a["available"] and a["latest_quarter"] == "Q1 FY27"
    assert 0 <= a["points"] <= F.MAX_POINTS


# ---------- metric math ----------

def test_quarter_label():
    assert F.quarter_label(date(2026, 6, 30)) == "Q1 FY27"
    assert F.quarter_label(date(2026, 3, 31)) == "Q4 FY26"


def test_yoy_pairing_and_growth_points():
    a = F.analyze(_corp_series([100, 105, 110, 115, 125], [10, 11, 12, 13, 15]), today=TODAY)
    assert a["metrics"]["topline_yoy_pct"] == 25.0  # 2026-06 vs 2025-06
    assert a["metrics"]["pat_yoy_pct"] == 50.0
    assert a["breakdown"]["growth"] == 6


def test_ebitda_margin_bps():
    rows = _corp_series([100] * 5, [10] * 5)
    rows[-1] = _row(PERIODS[-1], _corp_q(100, 10, pbt=15.3))  # +2 Cr EBITDA on 100 revenue = +200 bps
    a = F.analyze(rows, today=TODAY)
    assert a["metrics"]["margin_change_bps"] == pytest.approx(200, abs=1)


def test_ttm_roe_and_debt_equity():
    rows = _corp_series([100] * 5, [10] * 5, bs={"Equity": 200 * CR, "BorrowingsCurrent": 100 * CR})
    a = F.analyze(rows, today=TODAY)
    assert a["metrics"]["ttm_pat_cr"] == 40
    assert a["metrics"]["roe_pct"] == 20.0
    assert a["metrics"]["debt_equity"] == 0.5


def test_bank_roa_annualised():
    q = {"InterestEarned": 100 * CR, "InterestExpended": 60 * CR, "ReturnOnAssets": 0.003, "CET1Ratio": 0.15,
         "PercentageOfGrossNpa": 0.012, "PercentageOfNpa": 0.004, "ProfitLossForThePeriod": 10 * CR}
    a = F.analyze([_row(p, q, "bank") for p in PERIODS], today=TODAY)
    assert a["metrics"]["roa_annualised_pct"] == 1.2
    assert a["metrics"]["cet1_pct"] == 15.0


def test_ratio_units_harmonised_across_quarters():
    """A filer that switched from 0.0011 (mis-scaled) to 0.1205 must not show a 100× jump."""
    rows = [_row(p, {"GrossPremiumIncome": 100 * CR, "ProfitLossForThePeriod": 5 * CR,
                     "ExpensesOfManagementRatio": v}, "life_insurance")
            for p, v in zip(PERIODS, [0.0011, 0.0011, 0.0012, 0.0009, 0.1205])]
    a = F.analyze(rows, today=TODAY)
    ev = next(s["evidence"] for s in a["signals"] if s["name"].startswith("expense"))
    assert "12.0% vs 11.0%" in ev


def test_split_does_not_distort_eps():
    rows = [_row(p, _corp_q(100, 10, eps=e)) for p, e in zip(PERIODS, [2, 2, 1, 1, 1])]  # 1:1 bonus
    a = F.analyze(rows, close=100, today=TODAY)
    assert a["metrics"]["eps_yoy_pct"] is None and "share count" in a["metrics"]["eps_note"]
    assert a["metrics"]["ttm_eps"] == 4.0  # TTM profit on today's share count
    assert "peg" not in a["metrics"]


# ---------- gates ----------

def test_loss_making_gate():
    assert "loss_making" in _names(F.analyze(_corp_series([100] * 5, [10, 10, 10, 10, -5]), today=TODAY))


def test_earnings_collapse_gate():
    a = F.analyze(_corp_series([100, 100, 100, 100, 90], [10, 10, 10, 10, 6]), today=TODAY)
    assert "earnings_collapse" in _names(a)
    # profit down but revenue up: no gate
    b = F.analyze(_corp_series([100, 100, 100, 100, 110], [10, 10, 10, 10, 6]), today=TODAY)
    assert "earnings_collapse" not in _names(b)


def test_audit_qualification_gate():
    q = "Declaration of qualified opinion"
    a = F.analyze(_corp_series([100] * 5, [10] * 5, text={
        "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification": q}), today=TODAY)
    assert "audit_qualification" in _names(a)
    ok = F.analyze(_corp_series([100] * 5, [10] * 5, text={
        "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification": "Not applicable"}), today=TODAY)
    assert "audit_qualification" not in _names(ok)


def test_results_overdue_gate():
    assert "results_overdue" in _names(F.analyze(_corp_series([100] * 5, [10] * 5), today=date(2026, 11, 20)))
    assert "results_overdue" not in _names(F.analyze(_corp_series([100] * 5, [10] * 5), today=TODAY))


def test_weak_debt_service_and_high_leverage_gates():
    rows = _corp_series([100] * 5, [10] * 5, fin=30, pbt=10,
                        bs={"Equity": 100 * CR, "BorrowingsNoncurrent": 300 * CR})
    assert {"weak_debt_service", "high_leverage"} <= _names(F.analyze(rows, today=TODAY))


def test_bank_gates():
    def q(g, n, cet1):
        return {"InterestEarned": 100 * CR, "InterestExpended": 60 * CR, "PercentageOfGrossNpa": g,
                "PercentageOfNpa": n, "CET1Ratio": cet1, "ProfitLossForThePeriod": 10 * CR}
    rows = [_row(p, q(0.012, 0.004, 0.15)) for p in PERIODS[:-1]] + [_row(PERIODS[-1], q(0.016, 0.005, 0.09))]
    for r in rows:
        r["taxonomy"] = "bank"
    assert {"asset_quality_slip", "thin_capital"} <= _names(F.analyze(rows, today=TODAY))


def test_nbfc_credit_cost_gate():
    def q(imp):
        return {"InterestEarned": 200 * CR, "FinanceCosts": 100 * CR, "ImpairmentOnFinancialInstruments": imp * CR,
                "Income": 220 * CR, "ProfitOrLossAttributableToOwnersOfParent": 30 * CR}
    rows = [_row(p, q(i), "nbfc") for p, i in zip(PERIODS, [5, 5, 5, 5, 12])]
    assert "credit_cost_spike" in _names(F.analyze(rows, today=TODAY))


def test_revision_is_soft_flag_only():
    a = F.analyze(_corp_series([100] * 5, [10] * 5, type_sub="Revision"), today=TODAY)
    assert a["red_flags"] == [] and a["soft_flags"]


def test_no_filings_is_neutral():
    a = F.analyze([])
    assert a["available"] is False and a["points"] == F.NEUTRAL_POINTS and a["red_flags"] == []


# ---------- combine ----------

def test_combine_scaling_and_cap(uptrend):
    tech = signals.analyze("UP", uptrend)
    good = signals.combine(tech, {"available": True, "points": 15, "max": 15, "red_flags": []})
    assert good["score"]["total"] == round(0.85 * tech["score"]["total"] + 15, 1)
    assert good["score"]["technical"] == tech["score"]["total"]
    bad = signals.combine(tech, {"available": True, "points": 15, "max": 15,
                                 "red_flags": [{"name": "loss_making", "evidence": "Net loss"}]})
    assert signals.VERDICT_ORDER.index(bad["verdict"]) <= signals.VERDICT_ORDER.index("WATCH")
    assert any("loss_making" in f for f in bad["flags"])
    assert good["verdict"] in ("BUY", "ACCUMULATE") and bad["verdict"] == "WATCH" and "verdict_capped" in bad


def test_combine_without_fundamentals_is_neutral(uptrend):
    tech = signals.analyze("UP", uptrend)
    out = signals.combine(tech, None)
    assert out["score"]["fundamental"] == 7.5


def test_mis_tagged_attributable_profit_falls_back_to_usual_share():
    rows = _corp_series([100] * 5, [10] * 5)
    for r, attr in zip(rows, [10, 10, 0.2, 10, 0]):  # 0.2: stray sub-total; 0: not tagged
        r["data"]["q"]["ProfitLossForPeriod"] = 10 * CR
        r["data"]["q"]["ProfitOrLossAttributableToOwnersOfParent"] = attr * CR
    a = F.analyze(rows, today=TODAY)
    assert a["metrics"]["pat_yoy_pct"] == 0.0 and a["metrics"]["ttm_pat_cr"] == 40
    assert "loss_making" not in _names(a)


def test_high_leverage_needs_weak_coverage():
    bs = {"Equity": 100 * CR, "BorrowingsNoncurrent": 300 * CR}
    covered = _corp_series([100] * 5, [10] * 5, fin=2, pbt=20, bs=bs)  # coverage 11x: a lending arm, not distress
    assert "high_leverage" not in _names(F.analyze(covered, today=TODAY))
