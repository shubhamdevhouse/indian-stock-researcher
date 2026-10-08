from stock_researcher import cli, config, interaction, service
from stock_researcher.data import prices


def _ranked(n):
    return {"universe": "NIFTY TEST", "kind": "index", "etf": None, "as_of": "2026-10-06",
            "index_trend": None, "breadth": {}, "errors": {}, "data_refresh": {},
            "rows": [{"rank": i, "symbol": f"S{i}"} for i in range(1, n + 1)]}


def test_screen_pages(monkeypatch):
    monkeypatch.setattr(service, "rank_universe", lambda name, progress=None: _ranked(60))
    p1 = service.screen("x", 0, 25)
    assert [r["rank"] for r in p1["rows"]] == list(range(1, 26))
    assert p1["has_more"] and p1["next_offset"] == 25 and p1["total"] == 60 and "bottom" in p1
    p3 = service.screen("x", 50, 25)
    assert [r["rank"] for r in p3["rows"]] == list(range(51, 61))
    assert not p3["has_more"] and p3["next_offset"] is None and "bottom" not in p3
    assert service.screen("x", 0, 500)["returned"] == config.SCREEN_PAGE_MAX


def test_pages_for():
    assert service.pages_for(25) == [[1, 10], [11, 20], [21, 25]]
    assert service.pages_for(10) == [[1, 10]]


def test_refresh_many_batches(monkeypatch):
    sleeps = []
    monkeypatch.setattr(config, "FETCH_BATCH_SIZE", 4)
    monkeypatch.setattr(prices, "expected_last_session", lambda: __import__("datetime").date(2026, 10, 6))
    monkeypatch.setattr(prices, "stale_symbols", lambda syms, exp=None: [s for s in syms if s != "FRESH"])
    monkeypatch.setattr(prices.db, "last_price_date", lambda s: None)
    monkeypatch.setattr(prices, "refresh_symbol", lambda s, f, e: {"symbol": s, "status": "fetched"})
    monkeypatch.setattr(prices.time, "sleep", lambda t: sleeps.append(t))
    seen = []
    out = prices.refresh_many([f"S{i}" for i in range(10)] + ["FRESH"], progress=seen.append)
    assert out["batches"] == 3 and out["fetched"] == 10 and out["cached"] == 1
    assert len(sleeps) == 2  # pause between batches only
    assert {r["batch"] for r in seen} == {1, 2, 3}


def test_estimate_and_ask_count_capped():
    e10, e50 = service.estimate(10, 0), service.estimate(50, 0)
    assert e50["est_cost_usd"] > e10["est_cost_usd"] and e50["pages"] == 5
    interaction.set_prompter(lambda info: 999)
    try:
        assert interaction.ask_count({"total": 50}) == 50
    finally:
        interaction.set_prompter(None)
    assert interaction.ask_count({"total": 7}) == 7  # default prompter caps at universe size


def test_prompt_count_validates_range():
    info = {"index": "NIFTY 200", "etf": None, "total": 200, "default_n": 10, "uncached": 0,
            "fetch_minutes": 0, "options": [service.estimate(10, 0)]}
    answers = iter(["0x", "500", "25"])
    assert cli.prompt_count(info, lambda _: next(answers)) == 25
    assert cli.prompt_count(info, lambda _: "") == 10
    assert cli.prompt_count(info, lambda _: "q") == 0
