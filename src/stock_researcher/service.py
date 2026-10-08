"""High-level operations shared by the MCP tools and the CLI."""
import statistics
from datetime import date, timedelta

from . import config, db
from .analysis import fundamentals, signals
from .data import filings, prices, universe
from .data.nse_client import nse


ANALYSIS_VERSION = "v5"  # bump when scoring/adjustment logic changes to invalidate cached analyses


def resolve(name: str, force: bool = False) -> dict:
    return universe.resolve_universe(name, force)


def refresh(name: str, force: bool = False, progress=None) -> dict:
    u = universe.resolve_universe(name)
    summary = prices.refresh_many(u["symbols"], force, progress)
    results = filings.refresh_many(u["symbols"], force, progress)
    bench = prices.refresh_index(u["index"], force)
    if u["index"] != config.DEFAULT_BENCHMARK:
        prices.refresh_index(config.DEFAULT_BENCHMARK, force)
    return {"universe": u["index"], "kind": u["kind"], "etf": u["etf"], "constituents": u["count"],
            "prices": summary, "filings": results, "index_prices": bench}


def _benchmark(index_name: str):
    df = prices.get_index_prices(index_name)
    if len(df) > 70:
        return df, index_name
    df = prices.get_index_prices(config.DEFAULT_BENCHMARK)
    return (df, config.DEFAULT_BENCHMARK) if len(df) > 70 else (None, index_name)


def analyze_symbol(symbol: str, benchmark: str = config.DEFAULT_BENCHMARK, refresh_first: bool = True) -> dict:
    symbol = symbol.strip().upper()
    if refresh_first:
        r = prices.refresh_symbol(symbol)
        if r["status"] == "failed" and r.get("last_date") is None:
            return {"symbol": symbol, "error": r["error"]}
        prices.refresh_index(benchmark)
        filings.refresh_symbol(symbol)
    df = prices.get_prices(symbol)
    if df.empty:
        return {"symbol": symbol, "error": "no price data"}
    as_of = df.index[-1].date().isoformat()
    quarters = filings.load(symbol)
    stamp = f"{quarters[-1]['period_end']}#{quarters[-1]['seq_id']}" if quarters else "none"
    key = f"{ANALYSIS_VERSION}:{symbol}:{benchmark}:{as_of}:{stamp}"
    cached = db.get_snapshot("analysis", key)
    if cached:
        return cached
    bench, bench_name = _benchmark(benchmark)
    result = signals.analyze(symbol, df, bench, bench_name)
    if "error" not in result:
        result = signals.combine(result, fundamentals.analyze(quarters, close=result["close"]))
    if df.attrs.get("adjustments"):
        result["price_adjustments"] = df.attrs["adjustments"]
    if "error" not in result:
        db.put_snapshot("analysis", key, result)
    return result


SCORING_NOTE = ("score 0-100 = 0.85 × technical (trend 30 + momentum 25 + volume 15 + relative strength 20 + "
                "breakout 10) + fundamentals 0-15 from the latest quarterly results (growth 6, profitability 4, "
                "quality 5; 7.5 if no filing); BUY ≥70 (and above SMA200, RSI≤75), ACCUMULATE ≥55, WATCH ≥40, "
                "else AVOID; any fundamental red flag caps the verdict at WATCH")


def rank_universe(name: str, progress=None, refresh_data: bool = True) -> dict:
    """Score every constituent, sorted best-first. Cached per index + data date, so paging is free.
    refresh_data=False uses only what is already in the local cache (no NSE price calls)."""
    refreshed = refresh(name, progress=progress) if refresh_data else {"prices": None}
    u = universe.resolve_universe(name)
    as_of = prices.expected_last_session().isoformat()
    key = f"{ANALYSIS_VERSION}:{u['index']}:{as_of}:{len(u['symbols'])}:{db.filings_stamp(u['symbols'])}"
    cached = db.get_snapshot("screen", key)
    if cached:
        cached["data_refresh"] = refreshed["prices"]
        return cached

    results = [analyze_symbol(s, u["index"], refresh_first=False) for s in u["symbols"]]
    ok = [a for a in results if "error" not in a]
    errors = {a["symbol"]: a["error"] for a in results if "error" in a}
    ok.sort(key=lambda a: a["score"]["total"], reverse=True)

    n = len(ok) or 1
    breadth = {
        "stocks_analysed": len(ok),
        "pct_above_sma200": round(100 * sum(1 for a in ok if a["indicators"]["sma200"] and a["close"] > a["indicators"]["sma200"]) / n, 1),
        "pct_above_sma50": round(100 * sum(1 for a in ok if a["indicators"]["sma50"] and a["close"] > a["indicators"]["sma50"]) / n, 1),
        "advancers_1d": sum(1 for a in ok if (a["change_1d_pct"] or 0) > 0),
        "decliners_1d": sum(1 for a in ok if (a["change_1d_pct"] or 0) < 0),
        "median_rsi": round(statistics.median([a["indicators"]["rsi14"] for a in ok if a["indicators"]["rsi14"]]), 1) if ok else None,
        "verdict_counts": {v: sum(1 for a in ok if a["verdict"] == v) for v in ("BUY", "ACCUMULATE", "WATCH", "AVOID")},
        "fundamental_red_flags": sum(1 for a in ok if (a.get("fundamentals") or {}).get("red_flags")),
        "no_filings": sum(1 for a in ok if not (a.get("fundamentals") or {}).get("available")),
    }
    bench, bench_name = _benchmark(u["index"])
    index_trend = None
    if bench is not None:
        ia = signals.analyze(bench_name, bench.assign(volume=0.0), None, bench_name)
        if "error" not in ia:
            iv = ia["indicators"]
            index_trend = {"index": bench_name, "close": ia["close"], "as_of": ia["as_of"],
                           "sma50": iv["sma50"], "sma200": iv["sma200"], "rsi14": iv["rsi14"],
                           "adx": iv["adx"], "returns_pct": ia["returns_pct"]}
    ranked = {
        "universe": u["index"], "kind": u["kind"], "etf": u["etf"],
        "as_of": ok[0]["as_of"] if ok else None,
        "benchmark": bench_name, "index_trend": index_trend, "breadth": breadth, "errors": errors,
        "rows": [{"rank": i, **signals.compact_row(a)} for i, a in enumerate(ok, 1)],
    }
    if refresh_data:  # unrefreshed data may be older than the key's session, so don't cache it under that key
        db.put_snapshot("screen", key, ranked)
    ranked["data_refresh"] = refreshed["prices"]
    return ranked


def screen(name: str, offset: int = 0, limit: int = config.SCREEN_PAGE_MAX, progress=None) -> dict:
    """One page of the ranked universe (rank offset+1 .. offset+limit)."""
    r = rank_universe(name, progress)
    rows = r["rows"]
    offset = max(0, offset)
    limit = max(1, min(limit, config.SCREEN_PAGE_MAX))
    page = rows[offset:offset + limit]
    end = offset + len(page)
    out = {k: r[k] for k in ("universe", "kind", "etf", "as_of", "data_refresh", "benchmark", "index_trend", "breadth")}
    out.update({
        "total": len(rows), "offset": offset, "returned": len(page),
        "has_more": end < len(rows), "next_offset": end if end < len(rows) else None,
        "rows": page, "scoring": SCORING_NOTE,
    })
    if offset == 0:
        out["bottom"] = rows[-5:] if len(rows) > limit else []
        out["errors"] = r["errors"]
    return out


def _trim_analysis(a: dict) -> dict:
    if "levels" in a:
        a = {**a, "levels": {**a["levels"], "supports": a["levels"]["supports"][:3],
                             "resistances": a["levels"]["resistances"][:3]}}
    if (a.get("fundamentals") or {}).get("quarters"):  # the full table stays available via the fundamentals tool
        a = {**a, "fundamentals": {**a["fundamentals"], "quarters": a["fundamentals"]["quarters"][-5:]}}
    return a


def analyze_many(symbols: list[str], benchmark: str = config.DEFAULT_BENCHMARK) -> dict:
    symbols = [s.strip().upper() for s in symbols][:config.PAGE_SIZE]
    return {"benchmark": benchmark, "count": len(symbols),
            "analyses": [_trim_analysis(analyze_symbol(s, benchmark)) for s in symbols]}


def market_context_batch(symbols: list[str], index: str | None = None) -> dict:
    out = market_context(None, index)
    out["stocks"] = [_stock_context(s.strip().upper()) for s in symbols[:config.PAGE_SIZE]]
    return out


def estimate(n: int, uncached: int, new_filings: int = 0) -> dict:
    pages = -(-n // config.PAGE_SIZE)
    seconds = (config.EST_BASE_SECONDS + pages * config.EST_SECONDS_PER_PAGE
               + uncached * config.EST_FETCH_SECONDS_PER_STOCK
               + new_filings * config.EST_FILINGS_SECONDS_PER_STOCK)
    return {"n": n, "pages": pages,
            "est_cost_usd": round(config.EST_BASE_COST + n * config.EST_PER_STOCK_COST, 2),
            "est_minutes": max(1, round(seconds / 60))}


def plan_analysis_info(name: str) -> dict:
    """Universe size, data-download need and cost/time estimates for several N (used by the count picker)."""
    u = universe.resolve_universe(name)
    total = u["count"]
    uncached = len(prices.stale_symbols(u["symbols"]))
    new_filings = filings.first_download_count(u["symbols"])
    choices = sorted({n for n in (5, 10, 25, 50, 100) if n < total} | {total})
    return {"index": u["index"], "kind": u["kind"], "etf": u["etf"], "input": name, "total": total,
            "uncached": uncached, "new_filings": new_filings,
            "fetch_minutes": round((uncached * config.EST_FETCH_SECONDS_PER_STOCK
                                    + new_filings * config.EST_FILINGS_SECONDS_PER_STOCK) / 60, 1),
            "default_n": min(config.DEFAULT_TOP_N, total),
            "options": [estimate(n, uncached, new_filings) for n in choices]}


def pages_for(n: int) -> list[list[int]]:
    return [[i + 1, min(i + config.PAGE_SIZE, n)] for i in range(0, n, config.PAGE_SIZE)]


def fundamentals_detail(symbol: str) -> dict:
    """Quarterly-results fundamentals for one stock: per-quarter table, metrics, scored evidence, red flags."""
    symbol = symbol.strip().upper()
    r = filings.refresh_symbol(symbol)
    df = prices.get_prices(symbol)
    close = float(df["close"].iloc[-1]) if not df.empty else None
    out = {"symbol": symbol, **fundamentals.analyze(filings.load(symbol), close=close)}
    if r["status"] == "failed":
        out["refresh_error"] = r.get("error")
    return out


def price_history(symbol: str, days: int = 60) -> dict:
    symbol = symbol.strip().upper()
    prices.refresh_symbol(symbol)
    full = prices.get_prices(symbol)
    df = full.tail(max(5, min(days, 250)))
    cols = ["open", "high", "low", "close", "volume", "delivery_pct"]
    rows = [{"date": d.date().isoformat(), **{c: (None if r[c] != r[c] else r[c]) for c in cols}}
            for d, r in df[cols].iterrows()]
    return {"symbol": symbol, "prices": "adjusted for splits/bonuses/demergers",
            "adjustments": full.attrs.get("adjustments", []), "rows": rows}


def _cached(kind: str, key: str, ttl: float, fetch):
    data = db.get_snapshot(kind, key, ttl)
    if data is None:
        data = fetch()
        db.put_snapshot(kind, key, data)
    return data


def _stock_context(symbol: str) -> dict:
    out = {"symbol": symbol}
    try:
        q = _cached("quote", symbol, config.QUOTE_TTL, lambda: nse().stock_quote(symbol))
        m, p, t, s = q.get("metaData", {}), q.get("priceInfo", {}), q.get("tradeInfo", {}), q.get("secInfo", {})
        out["quote"] = {
            "company": m.get("companyName"), "last_price": m.get("closePrice") or t.get("lastPrice"),
            "change_pct": m.get("pChange"), "vwap": m.get("averagePrice"),
            "sector": s.get("sector"), "industry": s.get("basicIndustry"),
            "sector_index": (s.get("pdSectorInd") or "").strip(),
            "symbol_pe": s.get("pdSymbolPe"), "sector_pe": s.get("pdSectorPe"),
            "market_cap_cr": round(t["totalMarketCap"] / 1e7) if t.get("totalMarketCap") else None,
            "delivery_pct_today": t.get("deliveryToTradedQuantity"),
            "impact_cost": t.get("impactCost"),
            "year_high": p.get("yearHigh"), "year_high_date": (p.get("yearHightDt") or "")[:11],
            "year_low": p.get("yearLow"), "year_low_date": (p.get("yearLowDt") or "")[:11],
            "annual_volatility_pct": p.get("cmAnnualVolatility"), "price_band": p.get("priceBand"),
            "status": s.get("isSuspended"), "updated": q.get("lastUpdateTime"),
        }
    except Exception as e:
        out["quote_error"] = str(e)
    try:
        today = date.today()
        ann = _cached("announcements", symbol, config.ANNOUNCEMENTS_TTL,
                      lambda: nse().announcements(symbol, today - timedelta(days=45), today))
        out["announcements_45d"] = [
            {"date": a.get("an_dt"), "subject": a.get("desc"), "detail": (a.get("attchmntText") or "")[:240]}
            for a in (ann or [])[:12]]
    except Exception as e:
        out["announcements_error"] = str(e)
    return out


def _index_context(index_name: str) -> dict:
    out = {"index": index_name}
    try:
        allx = _cached("all_indices", "all", config.MARKET_STATUS_TTL, lambda: nse().all_indices())
        row = next((r for r in allx["data"] if r.get("index") == index_name), None)
        if row:
            out["live"] = {k: row.get(k) for k in (
                "last", "percentChange", "yearHigh", "yearLow", "pe", "pb", "dy",
                "advances", "declines", "unchanged", "perChange30d", "perChange365d")}
    except Exception as e:
        out["live_error"] = str(e)
    try:
        today = date.today()
        hist = _cached("index_pe", index_name, config.INDEX_PE_TTL,
                       lambda: nse().index_pe_history(index_name, today - timedelta(days=365), today))
        pes = [h["pe"] for h in hist if h.get("pe")]
        if pes:
            cur = pes[-1]
            out["valuation_1y"] = {
                "pe_now": cur, "pe_min": min(pes), "pe_max": max(pes),
                "pe_median": round(statistics.median(pes), 2),
                "pe_percentile": round(100 * sum(1 for x in pes if x <= cur) / len(pes)),
                "pb_now": hist[-1].get("pb"), "div_yield_now": hist[-1].get("div_yield"),
            }
    except Exception as e:
        out["valuation_error"] = str(e)
    return out


def market_context(symbol: str | None = None, index: str | None = None) -> dict:
    out = {"market_status": prices.market_status()}
    if index:
        try:
            idx_name, _, _ = universe.resolve_index_name(index)
        except universe.UniverseError:
            idx_name = universe.normalize(index)
        out["index"] = _index_context(idx_name)
    if symbol:
        out["stock"] = _stock_context(symbol.strip().upper())
    return out
