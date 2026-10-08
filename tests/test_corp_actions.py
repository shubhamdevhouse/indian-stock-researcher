import pandas as pd

from stock_researcher.data import corp_actions as ca


def test_theoretical_factor():
    assert ca.theoretical_factor("Bonus 1:2") == 1.5
    assert ca.theoretical_factor("Bonus 2:1") == 3.0
    assert ca.theoretical_factor("Face Value Split (Sub-Division) - From Rs 5/- Per Share To Re 1/- Per Share") == 5.0
    assert ca.theoretical_factor("Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") == 10.0
    assert ca.theoretical_factor("Dividend - Rs 4 Per Share") is None
    assert ca.theoretical_factor("Demerger") is None


def _frame(closes, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "prev_close": c.shift(1),
                         "volume": 100.0}, index=idx)


def test_split_adjusted_only_when_gap_visible():
    df = _frame([500, 510, 520, 104, 106])  # 5:1 split on 4th session
    ex = df.index[3].strftime("%d-%b-%Y")
    actions = [{"exDate": ex, "subject": "Face Value Split (Sub-Division) - From Rs 5/- Per Share To Re 1/- Per Share"}]
    events = ca.adjustment_events(df, actions)
    assert len(events) == 1 and events[0]["factor"] == 5.0
    out = ca.apply(df, events)
    assert out["close"].round(2).tolist() == [100, 102, 104, 104, 106]
    assert out["volume"].tolist() == [500, 500, 500, 100, 100]
    # already-adjusted data: no gap -> no adjustment
    assert ca.adjustment_events(out, actions) == []


def test_same_day_actions_multiply_and_demerger_uses_gap():
    df = _frame([1000, 1000, 100, 100, 60])
    actions = [{"exDate": df.index[2].strftime("%d-%b-%Y"), "subject": "Bonus 4:1"},
               {"exDate": df.index[2].strftime("%d-%b-%Y"), "subject": "Face Value Split - From Rs 2/- To Re 1/-"},
               {"exDate": df.index[4].strftime("%d-%b-%Y"), "subject": "Demerger"}]
    events = ca.adjustment_events(df, actions)
    assert [e["factor"] for e in events] == [10.0, round(100 / 60, 4)]
