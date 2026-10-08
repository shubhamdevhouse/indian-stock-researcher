from datetime import date
from pathlib import Path

import pytest

from stock_researcher import config, db
from stock_researcher.data import filings

FIX = Path(__file__).parent / "fixtures"
TCS_XML = (FIX / "tcs_indas_consolidated.xml").read_text(encoding="utf-8")


def _listing(qe="30-JUN-2026", nature="Consolidated", seq="173420", sub="Original", bcast="09-Jul-2026 18:36:12"):
    return {"symbol": "TCS", "qe_Date": qe, "consolidated": nature, "type_Sub": sub, "broadcast_Date": bcast,
            "seq_Id": seq, "type": filings.FINANCIALS, "audited": "Un-Audited",
            "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_{seq}_1_WEB.xml"}


class FakeNSE:
    def __init__(self, listing):
        self.listing, self.list_calls, self.downloads = listing, 0, 0

    def integrated_filings(self, symbol):
        self.list_calls += 1
        return self.listing

    def filing_xbrl(self, url):
        self.downloads += 1
        return TCS_XML


@pytest.fixture
def env(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FILINGS_DIR", tmp_path / "filings")
    fake = FakeNSE([_listing(), _listing(nature="Standalone", seq="173421")])
    monkeypatch.setattr(filings, "nse", lambda: fake)
    return fake


def test_db_round_trip(tmp_db):
    db.upsert_filing({"symbol": "X", "period_end": "2026-06-30", "nature": "consolidated", "taxonomy": "corporate",
                      "seq_id": "1", "type_sub": "Original", "broadcast_date": "b", "data": {"q": {"a": 1.0}}})
    rows = db.load_filings("X")
    assert rows[0]["data"] == {"q": {"a": 1.0}} and rows[0]["seq_id"] == "1"
    assert db.last_filing_period("X") == date(2026, 6, 30)
    stamp = db.filings_stamp(["X"])
    assert stamp.startswith("1-")
    db.delete_filings("X")
    assert db.load_filings("X") == [] and db.filings_stamp(["X"]).startswith("0-")


def test_select_prefers_consolidated_and_latest_revision():
    rows = [_listing(), _listing(nature="Standalone", seq="2"),
            _listing(seq="9", sub="Revision", bcast="20-Jul-2026 10:00:00"),
            _listing(qe="31-MAR-2026", seq="5", bcast="20-Apr-2026 10:00:00")]
    chosen = filings.select_filings(rows)
    assert [f["seq_Id"] for f in chosen] == ["9", "5"]


def test_select_prefers_standalone_for_banks():
    rows = [{**_listing(), "xbrl": "x/INTEGRATED_FILING_BANKING_1_1_WEB.xml"},
            {**_listing(nature="Standalone", seq="2"), "xbrl": "x/INTEGRATED_FILING_BANKING_2_1_WEB.xml"}]
    assert filings.select_filings(rows)[0]["consolidated"] == "Standalone"


def test_refresh_archives_and_skips(env):
    out = filings.refresh_symbol("TCS", today=date(2026, 8, 1))
    assert out["status"] == "fetched" and out["added"] == 1 and env.downloads == 1
    d = config.FILINGS_DIR / "TCS"
    assert (d / "2026-06-30_consolidated_original_173420.xml").read_text(encoding="utf-8") == TCS_XML
    assert (d / "2026-06-30_consolidated_original_173420.json").exists()
    assert len(filings.load("TCS")) == 1
    # next quarter has not ended yet: no network call at all
    again = filings.refresh_symbol("TCS", today=date(2026, 9, 15))
    assert again["status"] == "cached" and env.list_calls == 1


def test_refresh_after_quarter_end_respects_ttl_and_seq(env):
    filings.refresh_symbol("TCS", today=date(2026, 8, 1))
    assert filings.refresh_symbol("TCS", today=date(2026, 10, 7))["status"] == "cached"  # within list TTL
    out = filings.refresh_symbol("TCS", force=True, today=date(2026, 10, 7))
    assert out["added"] == 0 and env.downloads == 1  # same seq_id: nothing re-downloaded


def test_second_refresh_reads_archive_not_network(env):
    filings.refresh_symbol("TCS", today=date(2026, 8, 1))
    db.delete_filings()
    filings.refresh_symbol("TCS", force=True, today=date(2026, 8, 1))
    assert env.downloads == 1 and len(filings.load("TCS")) == 1


def test_rebuild_from_disk(env):
    filings.refresh_symbol("TCS", today=date(2026, 8, 1))
    db.delete_filings()
    env.integrated_filings = env.filing_xbrl = None  # any network use would now crash
    out = filings.rebuild_from_disk()
    assert out == {"restored": 1, "failed": {}}
    assert db.load_filings("TCS")[0]["taxonomy"] == "corporate"
