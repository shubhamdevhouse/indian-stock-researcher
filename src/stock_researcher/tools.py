"""In-process MCP server exposing market data + analysis tools to the agents."""
import asyncio
import json
import logging

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import config, db, interaction, service
from .data import universe

log = logging.getLogger(__name__)

SERVER_KEY = "market"


def _ok(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, default=str, ensure_ascii=False)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": f"ERROR: {msg}"}], "is_error": True}


async def _run(fn, *args, **kwargs) -> dict:
    try:
        return _ok(await asyncio.to_thread(fn, *args, **kwargs))
    except universe.UniverseError as e:
        return _err(str(e))
    except Exception as e:
        log.exception("tool failed")
        return _err(f"{type(e).__name__}: {e}")


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


UNIVERSE = {"type": "string", "description": "NSE index name (e.g. 'NIFTY 50', 'NIFTY BANK') or Indian equity ETF symbol (e.g. 'NIFTYBEES')"}
SYMBOL = {"type": "string", "description": "NSE equity symbol, e.g. 'RELIANCE'"}


@tool("resolve_universe", "Resolve an index or ETF name to its NSE index and list of constituent stocks (cached 7 days).",
      _schema({"name": UNIVERSE}, ["name"]))
async def resolve_universe(args):
    def run():
        u = service.resolve(args["name"])
        u.pop("constituents")
        return u
    return await _run(run)


@tool("list_indices", "List NSE index names, optionally filtered by a substring. Use when a name is ambiguous.",
      _schema({"filter": {"type": "string", "description": "Optional substring, e.g. 'MIDCAP'"}}, []))
async def list_indices(args):
    return await _run(universe.list_indices, args.get("filter"))


@tool("refresh_data", "Incrementally refresh daily price history and quarterly results filings for every stock in an "
      "index/ETF plus the index itself. Only missing sessions / new results are fetched; returns "
      "cached/fetched/failed counts.",
      _schema({"universe": UNIVERSE, "force": {"type": "boolean", "description": "Refetch full history (rarely needed)"}},
              ["universe"]))
async def refresh_data(args):
    return await _run(service.refresh, args["universe"], bool(args.get("force", False)))


@tool("plan_analysis", "ALWAYS call first for a new index/ETF analysis request. Resolves the universe, shows the user "
      "its stock count (the maximum they can pick) with cost/time estimates, and asks them how many top-ranked stocks "
      "should get full proof. Returns chosen_n and the deep-dive pages, or cancelled=true.",
      _schema({"universe": UNIVERSE,
               "horizon": {"type": "string", "description": "User's holding horizon if stated, e.g. 'a few days'"}},
              ["universe"]))
async def plan_analysis(args):
    def run():
        info = service.plan_analysis_info(args["universe"])
        info["horizon"] = args.get("horizon")
        n = interaction.ask_count(info)
        out = {k: info[k] for k in ("index", "kind", "etf", "total", "uncached")}
        if n <= 0:
            out = {**out, "cancelled": True, "chosen_n": 0}
        else:
            out = {**out, "cancelled": False, "chosen_n": n, "pages": service.pages_for(n),
                   "page_size": config.PAGE_SIZE,
                   "estimate": service.estimate(n, info["uncached"], info.get("new_filings", 0))}
        interaction.last_plan = {**out, "universe": args["universe"], "horizon": args.get("horizon")}
        return out
    return await _run(run)


@tool("screen_universe", "Score EVERY constituent of an index/ETF (0-100) and return one page of the ranking "
      f"(max {config.SCREEN_PAGE_MAX} rows per call; use offset/next_offset to page), plus index trend and breadth. "
      "The ranking is computed once and cached, so paging is cheap.",
      _schema({"universe": UNIVERSE,
               "offset": {"type": "integer", "description": "0-based rank offset (default 0)"},
               "limit": {"type": "integer", "description": f"Rows to return, max {config.SCREEN_PAGE_MAX} (default {config.SCREEN_PAGE_MAX})"},
               "top_n": {"type": "integer", "description": "Deprecated alias for limit"}},
              ["universe"]))
async def screen_universe(args):
    limit = int(args.get("limit") or args.get("top_n") or config.SCREEN_PAGE_MAX)
    return await _run(service.screen, args["universe"], int(args.get("offset") or 0), limit)


@tool("technical_snapshots", f"Full technical analysis (all signals with evidence, score breakdown, verdict, levels, "
      f"trade plan) for up to {config.PAGE_SIZE} stocks in one call. Use for a deep-dive page.",
      _schema({"symbols": {"type": "array", "items": {"type": "string"}, "maxItems": config.PAGE_SIZE},
               "benchmark": {"type": "string", "description": "Index for relative strength: pass the universe index being analysed (screen_universe's `benchmark`); defaults to 'NIFTY 50' only if omitted"}},
              ["symbols"]))
async def technical_snapshots(args):
    return await _run(service.analyze_many, args["symbols"], args.get("benchmark") or config.DEFAULT_BENCHMARK)


@tool("technical_snapshot", "Full analysis for one stock: indicator values, every signal with its evidence, "
      "score breakdown (technical + fundamentals), verdict, support/resistance, a trade plan (entry, stop, targets) "
      "and the quarterly-results fundamentals block with red flags.",
      _schema({"symbol": SYMBOL,
               "benchmark": {"type": "string", "description": "Index for relative strength: pass the universe index being analysed (screen_universe's `benchmark`); defaults to 'NIFTY 50' only if omitted"}},
              ["symbol"]))
async def technical_snapshot(args):
    return await _run(service.analyze_symbol, args["symbol"], args.get("benchmark") or config.DEFAULT_BENCHMARK)


@tool("fundamentals", "Quarterly-results fundamentals for one stock from NSE integrated filings (XBRL): last ~6 "
      "quarters of revenue/NII, net profit, EPS, margins (or NPAs for banks), YoY/QoQ growth, ROE, cash conversion, "
      "TTM P/E, the 0-15 fundamental score with evidence, and red flags that cap the verdict at WATCH.",
      _schema({"symbol": SYMBOL}, ["symbol"]))
async def fundamentals(args):
    return await _run(service.fundamentals_detail, args["symbol"])


@tool("price_history", "Recent daily OHLCV + delivery % rows for a stock (default 60 sessions, max 250).",
      _schema({"symbol": SYMBOL, "days": {"type": "integer"}}, ["symbol"]))
async def price_history(args):
    return await _run(service.price_history, args["symbol"], int(args.get("days") or 60))


@tool("market_context", "NSE context: market status; for an index its live P/E, P/B, advances/declines and P/E vs its "
      "1-year range; for a stock its live quote, sector, stock vs sector P/E, delivery %, 52-week dates and "
      "corporate announcements from the last 45 days.",
      _schema({"symbol": SYMBOL, "index": {"type": "string", "description": "Index name"}}, []))
async def market_context(args):
    return await _run(service.market_context, args.get("symbol"), args.get("index"))


@tool("market_context_batch", f"NSE context for up to {config.PAGE_SIZE} stocks in one call (sector, stock vs sector "
      "P/E, delivery %, 52-week dates, announcements) plus market status and, if given, the index valuation.",
      _schema({"symbols": {"type": "array", "items": {"type": "string"}, "maxItems": config.PAGE_SIZE},
               "index": {"type": "string", "description": "Index name"}}, ["symbols"]))
async def market_context_batch(args):
    return await _run(service.market_context_batch, args["symbols"], args.get("index"))


@tool("cache_status", "Show what is cached in the local SQLite database.", _schema({}, []))
async def cache_status(args):
    return await _run(db.cache_stats)


ALL_TOOLS = [plan_analysis, resolve_universe, list_indices, refresh_data, screen_universe, technical_snapshots,
             technical_snapshot, fundamentals, price_history, market_context, market_context_batch, cache_status]


def tool_name(t) -> str:
    return f"mcp__{SERVER_KEY}__{t.name}"


def build_server():
    return create_sdk_mcp_server(name="market-data", version="1.0.0", tools=ALL_TOOLS)
