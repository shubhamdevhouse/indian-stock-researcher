"""Rule-based technical signals, composite score and trade plan.

Every signal carries a human-readable `evidence` string with the exact values used,
so the agents can cite them as proof rather than inventing numbers.
"""
import math

import pandas as pd

from . import indicators as ind

MIN_SESSIONS = 60
WEIGHTS = {"trend": 30, "momentum": 25, "volume": 15, "relative_strength": 20, "breakout": 10}
TECHNICAL_WEIGHT = 0.85  # final score = 0.85 × technical (0-100) + fundamentals (0-15)
VERDICT_ORDER = ["AVOID", "WATCH", "ACCUMULATE", "BUY"]
LOW_LIQUIDITY_CR = 5.0  # avg daily traded value below ₹5 crore


def _f(x, nd: int = 2):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


def inr(x) -> str:
    return "n/a" if x is None else f"₹{x:,.2f}"


def pct(x) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


class _Scorer:
    def __init__(self):
        self.signals: list[dict] = []
        self.points = {k: 0.0 for k in WEIGHTS}

    def add(self, bucket: str, pts: float, name: str, direction: str, evidence: str):
        self.points[bucket] += pts
        self.signals.append({"bucket": bucket, "name": name, "direction": direction,
                             "points": pts, "evidence": evidence})


def _bars_since_cross(a: pd.Series, b: pd.Series, lookback: int = 10, up: bool = True) -> int | None:
    diff = (a - b).tail(lookback + 1)
    for i in range(len(diff) - 1, 0, -1):
        prev, cur = diff.iloc[i - 1], diff.iloc[i]
        if (up and prev <= 0 < cur) or (not up and prev >= 0 > cur):
            return len(diff) - 1 - i
    return None


def analyze(symbol: str, prices: pd.DataFrame, bench: pd.DataFrame | None = None,
            bench_name: str = "NIFTY 50") -> dict:
    prices = prices.dropna(subset=["close"])
    if len(prices) < MIN_SESSIONS:
        return {"symbol": symbol, "error": f"insufficient history ({len(prices)} sessions)"}

    df = ind.compute(prices)
    last, prev = df.iloc[-1], df.iloc[-2]
    c = _f(last["close"])
    v = {k: _f(last.get(k)) for k in (
        "sma20", "sma50", "sma200", "ema20", "rsi14", "macd", "macd_signal", "macd_hist", "adx",
        "plus_di", "minus_di", "bb_upper", "bb_lower", "bb_width", "bb_pctb", "stoch_k", "stoch_d",
        "atr14", "high_20", "high_252", "low_252", "deliv_avg20", "deliv_avg60")}
    v["delivery_pct"] = _f(last.get("delivery_pct"))
    v["vol_ratio_5_50"] = _f(df["volume"].tail(5).mean() / last["vol_avg50"]) if last["vol_avg50"] else None
    v["vol_ratio_1_20"] = _f(last["volume"] / last["vol_avg20"]) if last["vol_avg20"] else None
    v["avg_value_cr_20d"] = _f(last.get("value_avg20", float("nan")) / 1e7)
    v["sma50_10d_ago"] = _f(df["sma50"].iloc[-11]) if len(df) > 11 else None
    v["rsi14_5d_ago"] = _f(df["rsi14"].iloc[-6])
    v["change_1d_pct"] = _f((last["close"] / prev["close"] - 1) * 100)

    rets = {k: _f(ind.period_return(df["close"], n), 1) for k, n in
            (("1w", 5), ("1m", 21), ("3m", 63), ("6m", 126), ("1y", 250))}
    bench_rets, rs = {}, {}
    if bench is not None and len(bench) > 70:
        b = bench["close"].reindex(df.index).ffill().dropna()
        for k, n in (("1m", 21), ("3m", 63), ("6m", 126)):
            bench_rets[k] = _f(ind.period_return(b, n), 1)
            if rets.get(k) is not None and bench_rets[k] is not None:
                rs[k] = _f(rets[k] - bench_rets[k], 1)

    s = _Scorer()

    # ---- Trend (30) ----
    if v["sma200"] is not None:
        if c > v["sma200"]:
            s.add("trend", 8, "above_sma200", "bull", f"Close {inr(c)} above SMA200 {inr(v['sma200'])} (long-term uptrend)")
        else:
            s.add("trend", 0, "below_sma200", "bear", f"Close {inr(c)} below SMA200 {inr(v['sma200'])} (long-term downtrend)")
        if v["sma50"] is not None:
            if v["sma50"] > v["sma200"]:
                s.add("trend", 8, "golden_alignment", "bull", f"SMA50 {inr(v['sma50'])} > SMA200 {inr(v['sma200'])}")
            else:
                s.add("trend", 0, "death_alignment", "bear", f"SMA50 {inr(v['sma50'])} < SMA200 {inr(v['sma200'])}")
            cross = _bars_since_cross(df["sma50"], df["sma200"], 20, up=True)
            if cross is not None:
                s.add("trend", 0, "golden_cross", "bull", f"SMA50 crossed above SMA200 {cross} sessions ago")
    if v["sma50"] is not None:
        if c > v["sma50"]:
            s.add("trend", 6, "above_sma50", "bull", f"Close {inr(c)} above SMA50 {inr(v['sma50'])}")
        else:
            s.add("trend", 0, "below_sma50", "bear", f"Close {inr(c)} below SMA50 {inr(v['sma50'])}")
        if v["sma50_10d_ago"] and v["sma50"] > v["sma50_10d_ago"]:
            s.add("trend", 4, "sma50_rising", "bull", f"SMA50 rising ({inr(v['sma50_10d_ago'])} → {inr(v['sma50'])} over 10 sessions)")
    if v["ema20"] is not None and c > v["ema20"]:
        s.add("trend", 4, "above_ema20", "bull", f"Close above EMA20 {inr(v['ema20'])} (short-term trend up)")

    # ---- Momentum (25) ----
    r = v["rsi14"]
    if r is not None:
        rising = v["rsi14_5d_ago"] is not None and r > v["rsi14_5d_ago"]
        trend_txt = f" ({'rising' if rising else 'falling'} from {v['rsi14_5d_ago']} 5 sessions ago)" if v["rsi14_5d_ago"] else ""
        if 50 <= r <= 70:
            s.add("momentum", 10, "rsi_bullish_zone", "bull", f"RSI(14) {r} in bullish 50-70 zone{trend_txt}")
        elif 70 < r <= 75:
            s.add("momentum", 6, "rsi_strong", "bull", f"RSI(14) {r} strong, nearing overbought{trend_txt}")
        elif r > 75:
            s.add("momentum", 2, "rsi_overbought", "bear", f"RSI(14) {r} overbought (>75) — risk of pullback")
        elif 40 <= r < 50:
            s.add("momentum", 4, "rsi_neutral", "neutral", f"RSI(14) {r} neutral{trend_txt}")
        else:
            s.add("momentum", 0, "rsi_weak", "bear", f"RSI(14) {r} weak (<40){trend_txt}")
    if v["macd"] is not None and v["macd_signal"] is not None:
        if v["macd"] > v["macd_signal"]:
            s.add("momentum", 6, "macd_above_signal", "bull",
                  f"MACD {v['macd']} above signal {v['macd_signal']} (hist {v['macd_hist']})")
        else:
            s.add("momentum", 0, "macd_below_signal", "bear",
                  f"MACD {v['macd']} below signal {v['macd_signal']} (hist {v['macd_hist']})")
        bull_x = _bars_since_cross(df["macd"], df["macd_signal"], 5, up=True)
        bear_x = _bars_since_cross(df["macd"], df["macd_signal"], 5, up=False)
        if bull_x is not None:
            s.add("momentum", 4, "macd_bull_cross", "bull", f"MACD bullish crossover {bull_x} sessions ago")
        if bear_x is not None:
            s.add("momentum", 0, "macd_bear_cross", "bear", f"MACD bearish crossover {bear_x} sessions ago")
        h3 = _f(df["macd_hist"].iloc[-4])
        if h3 is not None and v["macd_hist"] > h3:
            s.add("momentum", 3, "macd_hist_rising", "bull", f"MACD histogram rising ({h3} → {v['macd_hist']} over 3 sessions)")
        if v["macd"] > 0:
            s.add("momentum", 2, "macd_positive", "bull", "MACD above zero line")

    # ---- Volume (15) ----
    vr = v["vol_ratio_5_50"]
    if vr is not None:
        up_week = rets.get("1w") is not None and rets["1w"] > 0
        if vr >= 1.2 and up_week:
            s.add("volume", 5, "volume_expansion_up", "bull", f"5-day avg volume {vr}× the 50-day avg on a {pct(rets['1w'])} week (accumulation)")
        elif vr >= 1.2:
            s.add("volume", 0, "volume_expansion_down", "bear", f"5-day avg volume {vr}× the 50-day avg on a {pct(rets.get('1w'))} week (distribution)")
    obv_slope = df["obv"].iloc[-1] - df["obv"].iloc[-21] if len(df) > 21 else None
    if obv_slope is not None:
        if obv_slope > 0:
            s.add("volume", 5, "obv_rising", "bull", "On-Balance Volume higher than 20 sessions ago (buying pressure)")
        else:
            s.add("volume", 0, "obv_falling", "bear", "On-Balance Volume lower than 20 sessions ago (selling pressure)")
    if v["deliv_avg20"] is not None and v["deliv_avg60"] is not None:
        if v["deliv_avg20"] > v["deliv_avg60"]:
            s.add("volume", 5, "delivery_rising", "bull",
                  f"Delivery % 20-day avg {v['deliv_avg20']}% > 60-day avg {v['deliv_avg60']}% (genuine buying, not intraday churn)")
        else:
            s.add("volume", 0, "delivery_falling", "neutral",
                  f"Delivery % 20-day avg {v['deliv_avg20']}% ≤ 60-day avg {v['deliv_avg60']}%")

    # ---- Relative strength (20) ----
    basis = rs.get("3m") if rs else rets.get("3m")
    label = f"vs {bench_name}" if rs else "(absolute; no benchmark data)"
    if basis is not None:
        pts = 12 if basis > 10 else 9 if basis > 5 else 6 if basis > 0 else 3 if basis > -5 else 0
        detail = f"3m return {pct(rets.get('3m'))}" + (f" vs {bench_name} {pct(bench_rets.get('3m'))}" if rs else "")
        s.add("relative_strength", pts, "rs_3m", "bull" if basis > 0 else "bear",
              f"{detail} → {'out' if basis > 0 else 'under'}performance {pct(basis)} {label}")
    b6 = rs.get("6m") if rs else rets.get("6m")
    if b6 is not None and b6 > 0:
        s.add("relative_strength", 4, "rs_6m", "bull", f"6m {'excess ' if rs else ''}return {pct(b6)} {label}")
    if rets.get("1m") is not None and rets["1m"] > 0:
        s.add("relative_strength", 4, "positive_1m", "bull", f"1m return {pct(rets['1m'])}")

    # ---- Breakout / volatility (10) ----
    if v["high_252"]:
        dist = (c / v["high_252"] - 1) * 100
        if dist >= -5:
            s.add("breakout", 5, "near_52w_high", "bull", f"Within {abs(dist):.1f}% of 52-week high {inr(v['high_252'])}")
        elif dist >= -10:
            s.add("breakout", 3, "approaching_52w_high", "bull", f"{abs(dist):.1f}% below 52-week high {inr(v['high_252'])}")
        elif dist <= -30:
            s.add("breakout", 0, "far_from_high", "bear", f"{abs(dist):.1f}% below 52-week high {inr(v['high_252'])}")
    prior_high20 = _f(df["high"].iloc[-21:-1].max())
    if prior_high20 and c > prior_high20:
        s.add("breakout", 3, "breakout_20d", "bull", f"Close {inr(c)} broke above prior 20-session high {inr(prior_high20)}")
    if v["adx"] is not None and v["plus_di"] is not None:
        if v["adx"] > 25 and v["plus_di"] > v["minus_di"]:
            s.add("breakout", 2, "strong_uptrend_adx", "bull", f"ADX {v['adx']} (>25) with +DI {v['plus_di']} > -DI {v['minus_di']} (strong trend)")
        elif v["adx"] > 25:
            s.add("breakout", 0, "strong_downtrend_adx", "bear", f"ADX {v['adx']} with -DI {v['minus_di']} > +DI {v['plus_di']} (strong downtrend)")
        elif v["adx"] < 18:
            s.add("breakout", 0, "no_trend_adx", "neutral", f"ADX {v['adx']} (<18): range-bound, trend signals less reliable")
    if v["bb_width"] is not None:
        width_pct = df["bb_width"].tail(120).rank(pct=True).iloc[-1]
        if width_pct <= 0.15:
            s.add("breakout", 0, "bollinger_squeeze", "neutral",
                  f"Bollinger band width in bottom {width_pct * 100:.0f}% of last 120 sessions (volatility squeeze; breakout pending)")

    # ---- Liquidity flag ----
    flags = []
    if v["avg_value_cr_20d"] is not None and v["avg_value_cr_20d"] < LOW_LIQUIDITY_CR:
        flags.append(f"Low liquidity: avg daily traded value ₹{v['avg_value_cr_20d']} cr")
    if r is not None and r > 75:
        flags.append(f"Overextended: RSI {r}")
    if v["sma50"] and (c / v["sma50"] - 1) > 0.15:
        flags.append(f"Stretched {((c / v['sma50'] - 1) * 100):.1f}% above SMA50 — wait for pullback")

    score = {k: round(min(s.points[k], WEIGHTS[k]), 1) for k in WEIGHTS}
    total = round(sum(score.values()), 1)
    verdict = _verdict(total, c, v)

    supports, resistances = _levels(df, c, v)
    plan = _trade_plan(df, c, v, prior_high20, resistances)

    return {
        "symbol": symbol,
        "as_of": df.index[-1].date().isoformat(),
        "close": c,
        "change_1d_pct": v["change_1d_pct"],
        "score": {"total": total, **score, "max": WEIGHTS},
        "verdict": verdict,
        "flags": flags,
        "returns_pct": rets,
        "benchmark": {"name": bench_name, "returns_pct": bench_rets, "excess_pct": rs} if rs else None,
        "indicators": v,
        "levels": {"supports": supports, "resistances": resistances,
                   "high_52w": v["high_252"], "low_52w": v["low_252"]},
        "trade_plan": plan,
        "signals": s.signals,
    }


def _verdict(total: float, c: float, v: dict) -> str:
    above200 = v["sma200"] is None or c > v["sma200"]
    rsi = v["rsi14"] or 50
    if total >= 70 and above200 and rsi <= 75:
        return "BUY"
    if total >= 55:
        return "ACCUMULATE"
    if total >= 40:
        return "WATCH"
    return "AVOID"


def combine(a: dict, fund: dict | None) -> dict:
    """Blend the technical analysis with the filing-based fundamentals score, then apply the red-flag gate:
    any fundamental red flag caps the verdict at WATCH."""
    if "error" in a:
        return a
    fund = fund or {"available": False, "points": 7.5, "max": 15, "red_flags": []}
    tech = a["score"]["total"]
    total = round(TECHNICAL_WEIGHT * tech + fund["points"], 1)
    verdict = _verdict(total, a["close"], a["indicators"])
    flags = list(a["flags"])
    capped_from = None
    if fund.get("red_flags") and VERDICT_ORDER.index(verdict) > VERDICT_ORDER.index("WATCH"):
        capped_from, verdict = verdict, "WATCH"
    for rf in fund.get("red_flags", []):
        flags.append(f"Fundamental red flag ({rf['name']}): {rf['evidence']}")
    score = {**a["score"], "total": total, "technical": tech, "fundamental": fund["points"],
             "max": {**WEIGHTS, "fundamental": fund.get("max", 15)}}
    out = {**a, "score": score, "verdict": verdict, "flags": flags, "fundamentals": fund}
    if capped_from:
        out["verdict_capped"] = f"{capped_from} → WATCH by fundamental red flag(s)"
    return out


def _levels(df: pd.DataFrame, c: float, v: dict) -> tuple[list[float], list[float]]:
    highs, lows = ind.swing_points(df)
    sup = sorted({round(x, 2) for x in lows if x < c}, reverse=True)[:3]
    res = sorted({round(x, 2) for x in highs if x > c})[:3]
    if v["high_252"] and v["high_252"] > c and (not res or v["high_252"] > res[-1]):
        res.append(v["high_252"])
    return sup, res


def _trade_plan(df: pd.DataFrame, c: float, v: dict, prior_high20: float | None, resistances: list) -> dict:
    a = v["atr14"]
    if not a:
        return {}
    swing_low = float(df["low"].tail(10).min())
    stop = max(c - 2 * a, swing_low - 0.25 * a)
    if stop > c - a:  # too tight: give at least 1 ATR of room
        stop = c - a
    risk = c - stop
    t1, t2 = c + 1.5 * risk, c + 3 * risk
    next_res = next((x for x in resistances if x > c * 1.005), None)
    plan = {
        "entry": _f(c),
        "entry_note": "Buy near current close" if not prior_high20 or c >= prior_high20
        else f"Aggressive: near {inr(c)}; conservative: on close above 20-session high {inr(prior_high20)}",
        "stop_loss": _f(stop),
        "stop_basis": f"max(close − 2×ATR {inr(2 * a)}, 10-session swing low {inr(swing_low)} − 0.25×ATR)",
        "risk_per_share": _f(risk),
        "risk_pct": _f(risk / c * 100, 1),
        "target_1": _f(t1),
        "target_2": _f(t2),
        "reward_risk_t2": 3.0,
        "next_resistance": next_res,
    }
    if next_res and next_res < t1:
        plan["warning"] = f"Resistance at {inr(next_res)} sits below target 1 {inr(t1)}; expect supply there"
    return plan


def compact_row(a: dict) -> dict:
    """One-line summary used by the screener table."""
    if "error" in a:
        return {"symbol": a["symbol"], "error": a["error"]}
    bulls = [x["name"] for x in sorted(a["signals"], key=lambda x: -x["points"]) if x["direction"] == "bull"]
    bears = [x["name"] for x in a["signals"] if x["direction"] == "bear"]
    i = a["indicators"]
    return {
        "symbol": a["symbol"], "score": a["score"]["total"], "verdict": a["verdict"],
        "close": a["close"], "chg_1d": a["change_1d_pct"], "rsi": i["rsi14"],
        "ret_3m": a["returns_pct"].get("3m"),
        "rs_3m": (a["benchmark"] or {}).get("excess_pct", {}).get("3m"),
        "above_sma200": bool(i["sma200"] and a["close"] > i["sma200"]),
        "top_bull": bulls[:4], "bear": bears[:3], "flags": a["flags"],
        "tech_score": a["score"].get("technical", a["score"]["total"]),
        "fund_score": a["score"].get("fundamental"),
        "fund_quarter": (a.get("fundamentals") or {}).get("latest_quarter"),
        "red_flags": [rf["name"] for rf in (a.get("fundamentals") or {}).get("red_flags", [])],
    }
