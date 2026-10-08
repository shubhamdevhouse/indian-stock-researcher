import csv
from datetime import datetime

from stock_researcher import config, reports, service


def _ranked():
    return {"universe": "NIFTY 50", "kind": "index", "etf": None, "as_of": "2026-10-06",
            "rows": [{"rank": 1, "symbol": "AAA", "score": 81, "verdict": "BUY", "close": 100.0, "top_bull": ["x", "y"],
                      "bear": [], "flags": []},
                     {"rank": 2, "symbol": "BBB", "score": 52, "verdict": "WATCH", "close": 50.0, "top_bull": [],
                      "bear": ["z"], "flags": []}]}


def test_save_report_date_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    now = datetime(2026, 10, 7, 15, 30, 12)
    meta = {"index": "NIFTY 50", "chosen_n": 1, "total": 50, "question": "Analyze NIFTY 50", "cost_usd": 0.5,
            "seconds": 90, "as_of": "2026-10-06", "model": "m"}
    p = reports.save_report(meta, "## Report\nbody", now=now)
    assert p == tmp_path / "2026-10-07" / "153012_NIFTY-50_top1.txt"
    text = p.read_text()
    assert "top 1 of 50" in text and "$0.50" in text and "Data as of" in text and text.rstrip().endswith("body")
    a = reports.save_report({"question": "why?"}, "because", now=datetime(2026, 10, 8, 9, 0, 1))
    assert a == tmp_path / "2026-10-08" / "090001_answer.txt"


def test_export_ranking_and_list(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(service, "rank_universe", lambda name, progress=None, refresh_data=True: _ranked())
    monkeypatch.setattr(service, "analyze_symbol", lambda s, b, refresh_first=True: {
        "trade_plan": {"entry": 100.0, "stop_loss": 95.0, "target_1": 107.5, "target_2": 115.0}})
    out = reports.export_ranking("nifty", now=datetime(2026, 10, 7, 15, 30, 12))
    assert out["count"] == 2 and out["path"].name == "153012_NIFTY-50_ranking.csv"
    rows = list(csv.DictReader(out["path"].open()))
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]
    assert rows[0]["stop_loss"] == "95.0" and rows[0]["top_bull"] == "x; y"
    reports.save_report({"question": "q"}, "t", now=datetime(2026, 10, 8, 9, 0, 1))
    listed = reports.list_reports()
    assert listed[0]["date"] == "2026-10-08" and listed[0]["name"] == "answer"
    assert {r["kind"] for r in listed} == {"txt", "csv"}
