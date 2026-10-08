"""Interactive chat loop."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeSDKClient, ResultMessage, TaskNotificationMessage,
                              TaskStartedMessage, TaskUpdatedMessage, TextBlock, ToolUseBlock)
from claude_agent_sdk.types import TERMINAL_TASK_STATUSES
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import config, db, interaction, reports, service
from .orchestrator import build_options

console = Console()
log = logging.getLogger(__name__)

HELP = """[bold]Ask in plain English[/], e.g.
  • Analyze NIFTY 50 and give me stocks I can hold for a few days for profit, with proof
  • Research NIFTYBEES / BANKBEES / NIFTY 200
  • Why is HDFCBANK not in the list?   • Compare TCS vs INFY
  You'll be asked how many stocks to analyse (up to the index size) with a cost estimate.
[bold]Commands[/]:
  /analyze <index|etf>   full swing-pick analysis (asks how many stocks)
  /refresh <index|etf>   update prices + quarterly results, no LLM    /cache   show cache stats
  /export <index|etf>    save full ranking CSV, no LLM       /reports list saved reports
  /save                  save the last answer as .txt        /clear   fresh conversation   /help   /quit
  Every full analysis is saved automatically to reports/<date>/ (report .txt + ranking .csv).
"""

FOLLOW_UP_GRACE = 20  # seconds to wait for a queued orchestrator turn after all agents finished

ANALYZE_TEMPLATE = ("Analyze {name} and find stocks I can hold for a few days to a few weeks (swing trades) for profit. "
                    "Give full technical proof for every pick.")


def _setup_logging():
    log_path = config.PROJECT_ROOT / "data" / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_path, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _check_env():
    if os.getenv("CLAUDE_CODE_USE_BEDROCK") != "1" and not os.getenv("ANTHROPIC_API_KEY"):
        console.print("[yellow]Warning:[/] CLAUDE_CODE_USE_BEDROCK=1 is not set (see .env.example). "
                      "Claude calls will fail without Bedrock or API credentials.")


def _describe_tool(block: ToolUseBlock) -> str:
    if block.name in ("Agent", "Task"):
        who = block.input.get("subagent_type", "agent")
        return f"[magenta]→ delegating to {who}[/]: {block.input.get('description', '')}"
    if block.name == "SubagentHandback":
        return "[magenta]✓ subagent reported back[/]"
    name = block.name.split("__")[-1]
    args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in block.input.items())
    return f"[dim]⚙ {name}({args})[/]"


async def _ask(client: ClaudeSDKClient, prompt: str) -> dict:
    """Stream one question; returns the orchestrator's text plus run metadata (for saving)."""
    interaction.last_plan = None
    await client.query(prompt)
    started = time.monotonic()
    pending: set[str] = set()  # background tasks; their completion re-invokes the orchestrator
    waiting_for = 0
    parts: list[str] = []
    report_from = 0  # text written after the last background agent finished = the final report
    replied = False  # main-agent output seen for THIS question (guards against a stale queued result)
    errored = False
    used_agents = False
    settled = False  # all agents done and a turn finished; wait briefly for a queued follow-up turn
    cost = None
    stream = client.receive_messages()
    while True:
        try:
            msg = await (asyncio.wait_for(anext(stream), FOLLOW_UP_GRACE) if settled else anext(stream))
        except (TimeoutError, StopAsyncIteration):
            final = parts[report_from:] or parts
            return {"question": prompt, "text": "\n\n".join(final), "plan": interaction.last_plan,
                    "seconds": time.monotonic() - started, "cost_usd": cost, "model": config.MAIN_MODEL}
        settled = False
        if isinstance(msg, TaskStartedMessage):
            pending.add(msg.task_id)
            used_agents = True
        elif isinstance(msg, TaskNotificationMessage | TaskUpdatedMessage):
            if isinstance(msg, TaskNotificationMessage) or (msg.patch or {}).get("status") in TERMINAL_TASK_STATUSES:
                pending.discard(msg.task_id)
                report_from = len(parts)
        elif isinstance(msg, AssistantMessage):
            sub = msg.parent_tool_use_id is not None
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    replied = replied or not sub
                    console.print(("   " if sub else "") + _describe_tool(block))
                elif isinstance(block, TextBlock) and not sub and block.text.strip():
                    replied = True
                    console.print(Markdown(block.text))
                    parts.append(block.text.strip())
        elif isinstance(msg, ResultMessage):
            if not replied and not msg.is_error:
                log.info("skipping stale result (uuid=%s turns=%s)", msg.uuid, msg.num_turns)
                continue
            errored = errored or msg.is_error
            cost = msg.total_cost_usd if msg.total_cost_usd is not None else cost
            if not pending:
                total = f" · session total ${cost:.2f}" if cost else ""
                status = "" if not msg.is_error else f" · [red]{msg.subtype}[/]"
                console.print(f"[dim]({time.monotonic() - started:.0f}s{total}{status})[/]\n")
                # Agents finishing close together queue separate orchestrator turns: the first may only say
                # "still waiting for the other one". Keep listening briefly so the real report isn't lost.
                if used_agents and not errored:
                    settled = True
                    continue
                final = parts[report_from:] or parts
                return {"question": prompt, "text": "\n\n".join(final), "plan": interaction.last_plan,
                        "seconds": time.monotonic() - started, "cost_usd": cost, "model": config.MAIN_MODEL}
            if len(pending) != waiting_for:
                done = "agent finished, " if waiting_for else ""
                console.print(f"[dim]… {done}{len(pending)} agent(s) still working[/]")
                waiting_for = len(pending)


def prompt_count(info: dict, input_fn=None) -> int:
    """Show universe size (the max), data-download need and cost estimates; ask how many stocks to analyse."""
    input_fn = input_fn or console.input
    total, default = info["total"], info["default_n"]
    label = info["index"] + (f" (via ETF {info['etf']})" if info.get("etf") else "")
    t = Table(title=f"{label} — {total} stocks (max {total})", title_justify="left", box=None)
    for col in ("stocks", "est. Claude cost", "est. time", "agent pages"):
        t.add_column(col, justify="right")
    for o in info["options"]:
        t.add_row(str(o["n"]), f"~${o['est_cost_usd']:.2f}", f"~{o['est_minutes']} min", str(o["pages"]))
    console.print()
    console.print(t)
    if info["uncached"] or info.get("new_filings"):
        need = [f"{info['uncached']} need a price download"] if info["uncached"] else []
        if info.get("new_filings"):
            need.append(f"{info['new_filings']} need their quarterly results downloaded")
        console.print(f"[dim]{'; '.join(need)} (~{info['fetch_minutes']} min, "
                      f"in batches of {config.FETCH_BATCH_SIZE}; included in time).[/]")
    console.print("[dim]Estimates only — actual cost depends on the model and the number of follow-ups.[/]")
    while True:
        raw = input_fn(f"[bold]How many top-ranked stocks should get full proof?[/] "
                       f"[1-{total}, Enter = {default}, q = cancel] ").strip().lower()
        if raw == "":
            return default
        if raw in ("q", "quit", "cancel", "0"):
            return 0
        if raw.isdigit() and 1 <= int(raw) <= total:
            n = int(raw)
            est = service.estimate(n, info["uncached"], info.get("new_filings", 0))
            console.print(f"[dim]→ analysing top {n} of {total} (~${est['est_cost_usd']:.2f}, "
                          f"~{est['est_minutes']} min)[/]")
            return n
        console.print(f"[red]Please enter a number between 1 and {total}.[/]")


def _save_analysis(run: dict):
    """Auto-save after a full analysis: reports/<date>/<HHMMSS>_<INDEX>_top<N>.txt + <HHMMSS>_<INDEX>_ranking.csv."""
    plan = run.get("plan")
    if not plan or plan.get("cancelled") or not run["text"]:
        return
    now = datetime.now()
    ranking = None
    try:
        ranking = reports.export_ranking(plan["index"], now=now)
    except Exception as e:
        console.print(f"[yellow]Ranking CSV not saved:[/] {e}")
    meta = {**plan, **{k: run[k] for k in ("question", "seconds", "cost_usd", "model")},
            "as_of": ranking and ranking["as_of"]}
    path = reports.save_report(meta, run["text"], now=now)
    extra = f" (+ ranking.csv, {ranking['count']} stocks)" if ranking else ""
    console.print(f"[green]📄 saved:[/] {_rel(path)}{extra}\n")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(config.PROJECT_ROOT))
    except ValueError:
        return str(path)


def _list_reports():
    rows = reports.list_reports()
    if not rows:
        console.print(f"[dim]No saved reports yet in {_rel(config.REPORTS_DIR)}/[/]")
        return
    t = Table(title="Saved reports (newest first)", title_justify="left", box=None)
    for col in ("date", "time", "report", "type", "file"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["date"], r["time"], r["name"], r["kind"], _rel(r["path"]))
    console.print(t)


def _local_refresh(name: str):
    if not name:
        console.print("usage: /refresh <index or etf>")
        return
    done = {"n": 0}

    def progress(r):
        done["n"] += 1
        kind = r.get("kind", "prices")
        console.print(f"[dim]  {kind:<7} batch {r['batch']}/{r['batches']}  {done['n']:>3} {r['symbol']:<12} {r['status']}[/]")

    with console.status(f"Refreshing {name}…"):
        out = service.refresh(name, progress=progress)
    console.print_json(data=out, default=str)


async def chat():
    _setup_logging()
    _check_env()
    interaction.set_prompter(prompt_count)
    console.print(Panel.fit("[bold cyan]NSE Stock Research Agents[/]\n"
                            f"model: {config.MAIN_MODEL} · cache: {config.DB_PATH}", border_style="cyan"))
    console.print(HELP)
    client = ClaudeSDKClient(build_options())
    await client.connect()
    last: dict | None = None
    try:
        while True:
            try:
                text = (await asyncio.to_thread(console.input, "[bold green]you ›[/] ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            cmd, _, rest = text.partition(" ")
            if cmd in ("/quit", "/exit"):
                break
            if cmd == "/help":
                console.print(HELP)
            elif cmd == "/cache":
                console.print_json(data=db.cache_stats(), default=str)
            elif cmd == "/refresh":
                try:
                    await asyncio.to_thread(_local_refresh, rest.strip())
                except Exception as e:
                    console.print(f"[red]{e}[/]")
            elif cmd == "/reports":
                _list_reports()
            elif cmd == "/save":
                if not last or not last["text"]:
                    console.print("[dim]Nothing to save yet — ask a question first.[/]")
                    continue
                path = reports.save_report({k: last[k] for k in ("question", "seconds", "cost_usd", "model")},
                                           last["text"])
                console.print(f"[green]📄 saved:[/] {_rel(path)}")
            elif cmd == "/export":
                if not rest.strip():
                    console.print("usage: /export <index or etf>")
                    continue
                try:
                    with console.status(f"Ranking {rest.strip()} from cache…"):
                        out = await asyncio.to_thread(reports.export_ranking, rest.strip())
                    console.print(f"[green]📄 saved:[/] {_rel(out['path'])} ({out['count']} stocks, "
                                  f"data as of {out['as_of']})")
                except Exception as e:
                    console.print(f"[red]{e}[/]")
            elif cmd == "/analyze":
                if not rest.strip():
                    console.print("usage: /analyze <index or etf>")
                    continue
                try:
                    last = await _ask(client, ANALYZE_TEMPLATE.format(name=rest.strip()))
                    await asyncio.to_thread(_save_analysis, last)
                except Exception as e:
                    console.print(f"[red]Error:[/] {e}")
            elif cmd == "/clear":
                await client.disconnect()
                client = ClaudeSDKClient(build_options())
                await client.connect()
                console.print("[dim]New conversation started.[/]")
            else:
                try:
                    run = await _ask(client, text)
                    last = run if run["text"] else last
                    await asyncio.to_thread(_save_analysis, run)
                except KeyboardInterrupt:
                    await client.interrupt()
                except Exception as e:
                    console.print(f"[red]Error:[/] {e}")
    finally:
        await client.disconnect()


def main():
    try:
        asyncio.run(chat())
    except KeyboardInterrupt:
        pass
