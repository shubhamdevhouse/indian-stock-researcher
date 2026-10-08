"""Saving reports to disk, one folder per day:

    reports/2026-10-07/153012_NIFTY-50_top5.txt      final report (plain text)
    reports/2026-10-07/153012_NIFTY-50_ranking.csv   full ranking of every constituent
    reports/2026-10-07/160455_answer.txt             /save of any other answer
"""
import csv
import re
from datetime import datetime
from pathlib import Path

from . import config, service

RANKING_COLUMNS = ["rank", "symbol", "score", "verdict", "tech_score", "fund_score", "fund_quarter", "red_flags",
                   "close", "chg_1d", "rsi", "ret_3m", "rs_3m", "above_sma200",
                   "entry", "stop_loss", "target_1", "target_2", "top_bull", "bear", "flags"]
_NAME = re.compile(r"^(?P<time>\d{6})_(?P<rest>.+)\.(?P<ext>txt|csv)$")


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper() or "UNKNOWN"


def _day_dir(now: datetime) -> Path:
    d = config.REPORTS_DIR / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _header(meta: dict, now: datetime) -> str:
    lines = [("Saved", now.strftime("%Y-%m-%d %H:%M:%S"))]
    if meta.get("index"):
        u = meta["index"] + (f" (via ETF {meta['etf']})" if meta.get("etf") else "")
        lines.append(("Universe", u))
    if meta.get("chosen_n"):
        lines.append(("Stocks analysed", f"top {meta['chosen_n']} of {meta.get('total', '?')}"))
    for label, key in (("Horizon", "horizon"), ("Data as of", "as_of"), ("Model", "model")):
        if meta.get(key):
            lines.append((label, str(meta[key])))
    if meta.get("seconds") is not None:
        lines.append(("Duration", f"{meta['seconds']:.0f}s"))
    if meta.get("cost_usd") is not None:
        lines.append(("Session cost", f"${meta['cost_usd']:.2f}"))
    if meta.get("question"):
        lines.append(("Question", meta["question"]))
    width = max(len(k) for k, _ in lines)
    body = "\n".join(f"{k.ljust(width)} : {v}" for k, v in lines)
    rule = "=" * 78
    return (f"{rule}\nNSE Stock Research Report\n{rule}\n{body}\n"
            "Educational research only - not SEBI-registered investment advice.\n"
            f"{rule}\n\n")


def save_report(meta: dict, text: str, now: datetime | None = None) -> Path:
    """Write an analysis report (meta has index/chosen_n) or a plain answer as .txt; returns the path."""
    now = now or datetime.now()
    stem = f"{now:%H%M%S}_" + (f"{slug(meta['index'])}_top{meta['chosen_n']}" if meta.get("index") and meta.get("chosen_n")
                               else "answer")
    path = _day_dir(now) / f"{stem}.txt"
    path.write_text(_header(meta, now) + text.strip() + "\n", encoding="utf-8")
    return path


def _cell(v):
    if isinstance(v, list):
        return "; ".join(map(str, v))
    return "" if v is None else v


def export_ranking(name: str, refresh_data: bool = False, now: datetime | None = None) -> dict:
    """Write the full deterministic ranking of a universe to CSV (no LLM). Uses only cached prices by default."""
    now = now or datetime.now()
    r = service.rank_universe(name, refresh_data=refresh_data)
    path = _day_dir(now) / f"{now:%H%M%S}_{slug(r['universe'])}_ranking.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RANKING_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in r["rows"]:
            a = service.analyze_symbol(row["symbol"], r["universe"], refresh_first=False)
            plan = a.get("trade_plan") or {}
            w.writerow({k: _cell(row.get(k, plan.get(k))) for k in RANKING_COLUMNS})
    return {"path": path, "count": len(r["rows"]), "universe": r["universe"], "as_of": r["as_of"]}


def list_reports(limit: int = 30) -> list[dict]:
    """Saved files, newest first, across all date folders."""
    root = config.REPORTS_DIR
    if not root.exists():
        return []
    out = []
    for day in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
        for f in sorted(day.iterdir(), key=lambda f: (f.stat().st_mtime, f.name), reverse=True):
            m = _NAME.match(f.name)
            if not m and f.suffix not in (".txt", ".csv"):
                continue
            t = m["time"] if m else ""
            out.append({"date": day.name, "time": f"{t[:2]}:{t[2:4]}:{t[4:]}" if t else "",
                        "name": m["rest"] if m else f.stem, "kind": f.suffix[1:], "path": f})
            if len(out) >= limit:
                return out
    return out
