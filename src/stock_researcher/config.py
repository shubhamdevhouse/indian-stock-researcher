"""Runtime configuration, loaded from environment / .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DB_PATH = PROJECT_ROOT / os.getenv("DB_PATH", "data/market.db")  # relative paths are anchored at the project root
REPORTS_DIR = PROJECT_ROOT / os.getenv("REPORTS_DIR", "reports")  # saved reports: reports/<YYYY-MM-DD>/...
FILINGS_DIR = PROJECT_ROOT / os.getenv("FILINGS_DIR", "data/filings")  # raw results XBRL: <SYMBOL>/<period>_*.xml

# Claude models (Bedrock IDs when CLAUDE_CODE_USE_BEDROCK=1)
MAIN_MODEL = os.getenv("MAIN_MODEL", "us.anthropic.claude-opus-5-5")
SUBAGENT_MODEL = os.getenv("SUBAGENT_MODEL", "inherit")
MAX_TURNS = int(os.getenv("MAX_TURNS", "150"))

# Data window: ~550 calendar days ≈ 375 sessions, enough for SMA200 + 52-week levels
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "550"))

# Cache freshness
CONSTITUENTS_TTL = 7 * 24 * 3600
INDEX_LIST_TTL = 24 * 3600
MARKET_STATUS_TTL = 10 * 60
QUOTE_TTL = 15 * 60
ANNOUNCEMENTS_TTL = 24 * 3600
INDEX_PE_TTL = 24 * 3600
FILINGS_LIST_TTL = 12 * 3600  # min gap between filing-list checks while a quarterly result is due
FILINGS_QUARTERS = 8  # quarterly results kept per company (NSE integrated filings start at the Mar-2025 quarter)
# If a symbol was fetched this recently and still looks stale, assume NSE hasn't published yet
REFETCH_COOLDOWN = 2 * 3600

# NSE politeness
NSE_MIN_INTERVAL = float(os.getenv("NSE_MIN_INTERVAL", "0.35"))  # seconds between requests
NSE_RETRIES = 3
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "3"))
FETCH_BATCH_SIZE = int(os.getenv("FETCH_BATCH_SIZE", "25"))  # stocks downloaded per batch
FETCH_BATCH_PAUSE = float(os.getenv("FETCH_BATCH_PAUSE", "3"))  # seconds of rest between batches

DEFAULT_BENCHMARK = "NIFTY 50"
DEFAULT_TOP_N = 10
PAGE_SIZE = 10  # stocks per deep-dive page (one technical + one context subagent each)
SCREEN_PAGE_MAX = 25  # max ranked rows per screen_universe call

# Rough estimates shown in the count picker (calibrated from real runs; tune via env)
EST_BASE_COST = float(os.getenv("EST_BASE_COST", "0.40"))  # USD per analysis run, independent of N
EST_PER_STOCK_COST = float(os.getenv("EST_PER_STOCK_COST", "0.07"))  # USD per fully-proved stock
EST_BASE_SECONDS = float(os.getenv("EST_BASE_SECONDS", "120"))
EST_SECONDS_PER_PAGE = float(os.getenv("EST_SECONDS_PER_PAGE", "150"))  # pages run in parallel but report grows
EST_FETCH_SECONDS_PER_STOCK = float(os.getenv("EST_FETCH_SECONDS_PER_STOCK", "2.7"))
EST_FILINGS_SECONDS_PER_STOCK = float(os.getenv("EST_FILINGS_SECONDS_PER_STOCK", "3.5"))  # first download only
