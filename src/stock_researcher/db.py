"""SQLite cache for prices, constituents and JSON snapshots."""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, prev_close REAL,
    vwap REAL, volume INTEGER, value REAL, trades INTEGER,
    delivery_qty INTEGER, delivery_pct REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS index_prices (
    index_name TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    PRIMARY KEY (index_name, date)
);
CREATE TABLE IF NOT EXISTS constituents (
    index_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    data TEXT,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (index_name, symbol)
);
CREATE TABLE IF NOT EXISTS etf_map (
    etf_symbol TEXT PRIMARY KEY,
    index_name TEXT NOT NULL,
    source TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS filings (
    symbol TEXT NOT NULL,
    period_end TEXT NOT NULL,
    nature TEXT NOT NULL,
    taxonomy TEXT,
    seq_id TEXT,
    type_sub TEXT,
    broadcast_date TEXT,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (symbol, period_end, nature)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
"""

PRICE_COLS = ["open", "high", "low", "close", "prev_close", "vwap", "volume",
              "value", "trades", "delivery_qty", "delivery_pct"]
INDEX_COLS = ["open", "high", "low", "close"]

_initialized: set[str] = set()


@contextmanager
def connect(db_path: Path | None = None):
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        if str(path) not in _initialized:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _initialized.add(str(path))
        yield conn
        conn.commit()
    finally:
        conn.close()


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


# ---------- prices ----------

def upsert_prices(symbol: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ["symbol", "date"] + PRICE_COLS
    sql = f"INSERT OR REPLACE INTO prices ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with connect() as c:
        c.executemany(sql, [[symbol, _iso(r["date"])] + [r.get(k) for k in PRICE_COLS] for r in rows])
    return len(rows)


def load_prices(symbol: str) -> pd.DataFrame:
    with connect() as c:
        df = pd.read_sql_query(
            "SELECT * FROM prices WHERE symbol = ? ORDER BY date", c, params=(symbol,))
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.set_index("date")


def last_price_date(symbol: str) -> date | None:
    with connect() as c:
        row = c.execute("SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def upsert_index_prices(index_name: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ["index_name", "date"] + INDEX_COLS
    sql = f"INSERT OR REPLACE INTO index_prices ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with connect() as c:
        c.executemany(sql, [[index_name, _iso(r["date"])] + [r.get(k) for k in INDEX_COLS] for r in rows])
    return len(rows)


def load_index_prices(index_name: str) -> pd.DataFrame:
    with connect() as c:
        df = pd.read_sql_query(
            "SELECT * FROM index_prices WHERE index_name = ? ORDER BY date", c, params=(index_name,))
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    return df.set_index("date")


def last_index_date(index_name: str) -> date | None:
    with connect() as c:
        row = c.execute("SELECT MAX(date) FROM index_prices WHERE index_name = ?", (index_name,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


# ---------- constituents / ETF map ----------

def save_constituents(index_name: str, rows: list[dict]) -> None:
    now = time.time()
    with connect() as c:
        c.execute("DELETE FROM constituents WHERE index_name = ?", (index_name,))
        c.executemany(
            "INSERT INTO constituents (index_name, symbol, data, fetched_at) VALUES (?,?,?,?)",
            [(index_name, r["symbol"], json.dumps(r), now) for r in rows])


def load_constituents(index_name: str, max_age: float | None = None) -> list[dict] | None:
    with connect() as c:
        rows = c.execute(
            "SELECT data, fetched_at FROM constituents WHERE index_name = ?", (index_name,)).fetchall()
    if not rows:
        return None
    if max_age is not None and time.time() - min(r[1] for r in rows) > max_age:
        return None
    return [json.loads(r[0]) for r in rows]


def get_etf_index(etf_symbol: str) -> str | None:
    with connect() as c:
        row = c.execute("SELECT index_name FROM etf_map WHERE etf_symbol = ?", (etf_symbol,)).fetchone()
    return row[0] if row else None


def set_etf_index(etf_symbol: str, index_name: str, source: str) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO etf_map VALUES (?,?,?)", (etf_symbol, index_name, source))


# ---------- quarterly results filings ----------

FILING_COLS = ["symbol", "period_end", "nature", "taxonomy", "seq_id", "type_sub", "broadcast_date"]


def upsert_filing(row: dict) -> None:
    """row: FILING_COLS + data (parsed facts dict)."""
    with connect() as c:
        c.execute(f"INSERT OR REPLACE INTO filings ({','.join(FILING_COLS)}, data, fetched_at) "
                  f"VALUES ({','.join('?' * (len(FILING_COLS) + 2))})",
                  [row.get(k) for k in FILING_COLS] + [json.dumps(row["data"]), time.time()])


def load_filings(symbol: str) -> list[dict]:
    """All stored quarters for a symbol, oldest first."""
    with connect() as c:
        rows = c.execute(f"SELECT {','.join(FILING_COLS)}, data FROM filings WHERE symbol = ? "
                         "ORDER BY period_end, nature", (symbol,)).fetchall()
    return [{**dict(zip(FILING_COLS, r[:-1])), "data": json.loads(r[-1])} for r in rows]


def last_filing_period(symbol: str) -> date | None:
    with connect() as c:
        row = c.execute("SELECT MAX(period_end) FROM filings WHERE symbol = ?", (symbol,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def filings_stamp(symbols: list[str]) -> str:
    """Changes whenever any of the symbols gets a new or revised filing (used in cache keys)."""
    if not symbols:
        return "0"
    with connect() as c:
        row = c.execute(f"SELECT COUNT(*), MAX(fetched_at) FROM filings WHERE symbol IN "
                        f"({','.join('?' * len(symbols))})", symbols).fetchone()
    return f"{row[0]}-{int(row[1] or 0)}"


def delete_filings(symbol: str | None = None) -> None:
    with connect() as c:
        if symbol:
            c.execute("DELETE FROM filings WHERE symbol = ?", (symbol,))
        else:
            c.execute("DELETE FROM filings")


# ---------- snapshots & fetch log ----------

def get_snapshot(kind: str, key: str, max_age: float | None = None):
    with connect() as c:
        row = c.execute(
            "SELECT data, fetched_at FROM snapshots WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    if not row or (max_age is not None and time.time() - row[1] > max_age):
        return None
    return json.loads(row[0])


def put_snapshot(kind: str, key: str, data) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
                  (kind, key, json.dumps(data, default=str), time.time()))


def log_fetch(kind: str, key: str) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?)", (kind, key, time.time()))


def last_fetch(kind: str, key: str) -> float | None:
    with connect() as c:
        row = c.execute("SELECT fetched_at FROM fetch_log WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    return row[0] if row else None


def cache_stats() -> dict:
    with connect() as c:
        p = c.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM prices").fetchone()
        i = c.execute("SELECT index_name, COUNT(*), MAX(date) FROM index_prices GROUP BY index_name").fetchall()
        k = c.execute("SELECT index_name, COUNT(*) FROM constituents GROUP BY index_name").fetchall()
        s = c.execute("SELECT kind, COUNT(*) FROM snapshots GROUP BY kind").fetchall()
        f = c.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(period_end) FROM filings").fetchone()
    return {
        "db_path": str(config.DB_PATH),
        "prices": {"rows": p[0], "symbols": p[1], "first_date": p[2], "last_date": p[3]},
        "index_prices": {name: {"rows": n, "last_date": d} for name, n, d in i},
        "constituents": dict(k),
        "snapshots": dict(s),
        "filings": {"quarters": f[0], "symbols": f[1], "latest_period": f[2], "archive": str(config.FILINGS_DIR)},
    }
