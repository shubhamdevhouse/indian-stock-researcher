import numpy as np
import pandas as pd

from stock_researcher.analysis import indicators as ind


def test_sma_known_values():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ind.sma(s, 3)
    assert out.isna().sum() == 2
    assert list(out.dropna()) == [2.0, 3.0, 4.0]


def test_rsi_bounds_and_extremes(uptrend):
    r = ind.rsi(uptrend["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    rising = pd.Series(np.arange(1, 60, dtype=float))
    assert ind.rsi(rising).iloc[-1] > 99
    falling = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert ind.rsi(falling).iloc[-1] < 1


def test_atr_constant_range():
    n = 40
    df = pd.DataFrame({"high": np.full(n, 105.0), "low": np.full(n, 95.0), "close": np.full(n, 100.0)})
    assert abs(ind.atr(df).iloc[-1] - 10.0) < 1e-9


def test_macd_positive_in_uptrend(uptrend):
    m = ind.macd(uptrend["close"])
    assert m["macd"].iloc[-1] > 0


def test_compute_adds_columns(uptrend):
    out = ind.compute(uptrend)
    for col in ("sma20", "sma50", "sma200", "rsi14", "macd", "adx", "bb_upper", "atr14", "obv",
                "vol_avg20", "high_252", "deliv_avg20", "value_avg20"):
        assert col in out
    assert len(out) == len(uptrend)


def test_period_return():
    s = pd.Series([100.0, 105.0, 110.0])
    assert abs(ind.period_return(s, 2) - 10.0) < 1e-9
    assert ind.period_return(s, 10) is None
