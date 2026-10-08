from stock_researcher.data import universe as u

NAMES = ["NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY NEXT 50", "NIFTY MIDCAP 150", "NIFTY FINANCIAL SERVICES"]


def test_normalize_and_compact():
    assert u.normalize("  nifty   50 index ") == "NIFTY 50"
    assert u.compact("Nifty M&M-50") == "NIFTYM&M50"


def test_match_index():
    assert u.match_index("nifty50", NAMES) == "NIFTY 50"
    assert u.match_index("Bank Nifty", NAMES) == "NIFTY BANK"
    assert u.match_index("it", NAMES) == "NIFTY IT"
    assert u.match_index("finnifty", NAMES) == "NIFTY FINANCIAL SERVICES"
    assert u.match_index("does not exist", NAMES) is None


def test_etf_map_and_non_equity():
    assert u.ETF_TO_INDEX["NIFTYBEES"] == "NIFTY 50"
    assert u.ETF_TO_INDEX["BANKBEES"] == "NIFTY BANK"
    assert u.is_non_equity("Gold")
    assert u.is_non_equity("Liquid")
    assert not u.is_non_equity("NIFTY 50 Index")


def test_index_from_assets():
    assert u._index_from_assets("NIFTY 50 Index", NAMES) == "NIFTY 50"
    assert u._index_from_assets("Nifty Midcap 150 TRI", NAMES) == "NIFTY MIDCAP 150"
