from stock_researcher.analysis import signals


def test_uptrend_beats_downtrend(uptrend, downtrend):
    up = signals.analyze("UP", uptrend)
    down = signals.analyze("DOWN", downtrend)
    assert up["score"]["total"] > down["score"]["total"]
    assert up["verdict"] in ("BUY", "ACCUMULATE")
    assert down["verdict"] == "AVOID"


def test_score_within_bounds(uptrend):
    a = signals.analyze("UP", uptrend)
    assert 0 <= a["score"]["total"] <= 100
    for bucket, cap in signals.WEIGHTS.items():
        assert 0 <= a["score"][bucket] <= cap


def test_trade_plan_ordering(uptrend):
    p = signals.analyze("UP", uptrend)["trade_plan"]
    assert p["stop_loss"] < p["entry"] < p["target_1"] < p["target_2"]
    assert p["risk_per_share"] > 0


def test_signals_have_evidence(uptrend):
    a = signals.analyze("UP", uptrend)
    assert a["signals"]
    assert all(s["evidence"] for s in a["signals"])


def test_relative_strength_with_benchmark(uptrend, downtrend):
    a = signals.analyze("UP", uptrend, bench=downtrend, bench_name="BENCH")
    assert a["benchmark"]["name"] == "BENCH"
    assert a["benchmark"]["excess_pct"]["3m"] > 0


def test_insufficient_history(uptrend):
    assert "error" in signals.analyze("X", uptrend.head(30))
