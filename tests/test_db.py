from datetime import date

from stock_researcher import db


def _row(d, close):
    return {"date": d, "open": close, "high": close, "low": close, "close": close, "volume": 100}


def test_upsert_and_last_date(tmp_db):
    assert db.last_price_date("ABC") is None
    db.upsert_prices("ABC", [_row(date(2026, 1, 1), 10), _row(date(2026, 1, 2), 11)])
    assert db.last_price_date("ABC") == date(2026, 1, 2)
    # incremental append + overwrite of an existing day
    db.upsert_prices("ABC", [_row(date(2026, 1, 2), 12), _row(date(2026, 1, 5), 13)])
    df = db.load_prices("ABC")
    assert len(df) == 3
    assert df["close"].tolist() == [10, 12, 13]
    assert df.index.is_monotonic_increasing


def test_snapshots_ttl(tmp_db):
    db.put_snapshot("k", "x", {"a": 1})
    assert db.get_snapshot("k", "x") == {"a": 1}
    assert db.get_snapshot("k", "x", max_age=-1) is None
    assert db.get_snapshot("k", "missing") is None


def test_constituents_and_etf_map(tmp_db):
    db.save_constituents("NIFTY TEST", [{"symbol": "AAA"}, {"symbol": "BBB"}])
    rows = db.load_constituents("NIFTY TEST")
    assert {r["symbol"] for r in rows} == {"AAA", "BBB"}
    db.set_etf_index("TESTBEES", "NIFTY TEST", "test")
    assert db.get_etf_index("TESTBEES") == "NIFTY TEST"
