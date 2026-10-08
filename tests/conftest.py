import numpy as np
import pandas as pd
import pytest


def make_prices(n=300, drift=0.0015, vol=0.012, seed=7, start=100.0, value=5e8):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + vol * rng.standard_normal(n)))
    idx = pd.bdate_range("2025-01-01", periods=n)
    open_ = close * (1 + 0.003 * rng.standard_normal(n))
    high = np.maximum(open_, close) * (1 + np.abs(0.006 * rng.standard_normal(n)))
    low = np.minimum(open_, close) * (1 - np.abs(0.006 * rng.standard_normal(n)))
    volume = rng.integers(800_000, 1_200_000, n).astype(float)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close,
        "prev_close": np.r_[close[0], close[:-1]], "volume": volume,
        "value": np.full(n, value), "delivery_pct": rng.uniform(40, 60, n),
    }, index=idx)


@pytest.fixture
def uptrend():
    return make_prices(drift=0.003, vol=0.008)


@pytest.fixture
def downtrend():
    return make_prices(drift=-0.003, vol=0.008)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    from stock_researcher import config
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    return path
