"""Technical indicators in plain pandas. Input: DataFrame indexed by date (ascending)
with open/high/low/close/volume (+ optional value, delivery_pct)."""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0), n)
    loss = wilder(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                     axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = wilder(true_range(df), n)
    plus_di = 100 * wilder(plus_dm, n) / tr
    minus_di = 100 * wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame({"adx": wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower,
                         "bb_width": (upper - lower) / mid, "bb_pctb": (close - lower) / (upper - lower)})


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3) -> pd.DataFrame:
    lo = df["low"].rolling(n, min_periods=n).min()
    hi = df["high"].rolling(n, min_periods=n).max()
    k = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    return pd.DataFrame({"stoch_k": k, "stoch_d": k.rolling(d).mean()})


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with all indicator columns appended."""
    out = df.copy()
    c = out["close"]
    for n in (20, 50, 200):
        out[f"sma{n}"] = sma(c, n)
    out["ema20"] = ema(c, 20)
    out["rsi14"] = rsi(c)
    out = out.join(macd(c)).join(adx(out)).join(bollinger(c)).join(stochastic(out))
    out["atr14"] = atr(out)
    out["obv"] = obv(out)
    out["vol_avg20"] = sma(out["volume"], 20)
    out["vol_avg50"] = sma(out["volume"], 50)
    out["high_20"] = out["high"].rolling(20).max()
    out["high_252"] = out["high"].rolling(252, min_periods=120).max()
    out["low_252"] = out["low"].rolling(252, min_periods=120).min()
    if "delivery_pct" in out:
        out["deliv_avg20"] = out["delivery_pct"].rolling(20, min_periods=10).mean()
        out["deliv_avg60"] = out["delivery_pct"].rolling(60, min_periods=30).mean()
    if "value" in out:
        out["value_avg20"] = sma(out["value"], 20)
    return out


def period_return(close: pd.Series, sessions: int) -> float | None:
    if len(close) <= sessions:
        return None
    return float(close.iloc[-1] / close.iloc[-1 - sessions] - 1) * 100


def swing_points(df: pd.DataFrame, window: int = 5, lookback: int = 120) -> tuple[list[float], list[float]]:
    """Local swing highs and lows over the recent lookback (fractal pivots)."""
    recent = df.tail(lookback)
    hi, lo = recent["high"], recent["low"]
    span = 2 * window + 1
    is_high = hi == hi.rolling(span, center=True).max()
    is_low = lo == lo.rolling(span, center=True).min()
    return hi[is_high].tolist(), lo[is_low].tolist()
