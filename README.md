Copyright (c) 2026 Shubham Dawra
# indian-stock-researcher

An interactive, multi-agent research assistant for **Indian equities (NSE)**, built on the
[Claude Agent SDK](https://docs.claude.com/en/docs/agent-sdk/overview) and
[jugaad-data](https://github.com/jugaad-py/jugaad-data).

> Educational research only — not SEBI-registered investment advice.

> Please Note this project is still WIP, use it at your own risk.

You give it an **index or ETF** (for example `NIFTY 50`, `Nifty Bank`, `NIFTYBEES`, `MID150BEES`). It then:

1. Works out the stock universe. An ETF is mapped to the index it tracks, and the constituents are pulled live from NSE.
2. Downloads daily price, volume and delivery history into a local SQLite cache. Only the sessions that are missing get fetched.
3. Downloads each stock's quarterly results (NSE integrated-filing XBRL), archives the raw files under `data/filings/`, and scores earnings quality.
4. Scores every constituent on a deterministic model: 85% technical (0–100 scaled to 85) plus up to 15 points for fundamentals. A fundamental red flag (loss, earnings collapse, weak debt service, NPA slip, …) caps the verdict at WATCH.
5. Hands off to specialist agents, which dig into the leaders, filter out false positives and add NSE context.
6. Writes a **ranked report in chat**. Each pick gets a verdict, the technical proof behind it (exact indicator values), a fundamentals line from the latest quarter and an ATR-based trade plan.


## Setup

```bash
# 1. Python 3.12 via uv (system Python 3.9 is too old for the SDK)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

# 2. Bedrock credentials
cp .env.example .env      # then fill in AWS_REGION + AWS_BEARER_TOKEN_BEDROCK (or AWS_PROFILE)
```

Shell environment variables take precedence over values in `.env`. If you have already exported
`CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION` and your AWS credentials, `.env` is optional.

## Usage

```bash
uv run stock-researcher            # or: uv run python -m stock_researcher
```

```
you › Analyze NIFTY 200 and give me stocks I can hold for a few days for profit, with proof

NIFTY 200 — 200 stocks (max 200)
 stocks  est. Claude cost  est. time  agent pages
     10            ~$1.10    ~11 min            1
     25            ~$2.15    ~16 min            3
     50            ~$3.90    ~21 min            5
    200           ~$14.40    ~59 min           20
150 stocks need a price download first (~6.8 min, in batches of 25; included in time).
How many top-ranked stocks should get full proof? [1-200, Enter = 10, q = cancel] 50

you › Analyze NIFTY 50 and give me the top 5 swing picks
you › Research BANKBEES
you › Why is HDFCBANK not in the list?
you › Compare TCS vs INFY — which has the better setup?
you › What's a sensible stop for the #1 pick?
```

Local commands don't call the LLM, so they cost nothing:

| Command | What it does |
|---|---|
| `/analyze <index or etf>` | Run the standard swing-pick analysis; asks how many stocks you want |
| `/refresh <index or etf>` | Pre-warm or update the price cache and quarterly results, with per-stock progress |
| `/cache` | Show row counts and last dates in the SQLite cache |
| `/export <index or etf>` | Save the full ranking of every constituent as CSV, using only the local cache |
| `/save` | Save the last answer as a `.txt` file |
| `/reports` | List saved reports, newest first |
| `/clear` | Start a new conversation (the cache is kept) |
| `/help`, `/quit` | Show help, or exit |

The first run on an index downloads about 18 months of history per stock. That takes roughly 3 s per stock (about 1 min for NIFTY 50), throttled to stay polite to NSE. After that, a refresh only fetches the new sessions.

Quarterly results add about 6 XBRL files per stock on the first run (roughly 3–4 s per stock). After that the app makes no filing calls at all until a new quarter has ended, and then at most one listing call per stock every 12 h until the new result appears.

## Saved reports

Every full analysis is saved automatically into one folder per day, under `REPORTS_DIR` (default `reports/`):

```
reports/
  2026-10-07/
    160102_NIFTY-50_top1.txt       final report as plain text, with a header (universe, N, data date, model, cost)
    160102_NIFTY-50_ranking.csv    all constituents ranked: score, verdict, RSI, returns, entry, stop, T1, T2
    160110_answer.txt              a follow-up answer saved with /save
```

The `.txt` file holds only the final report, without the interim "waiting for agents" messages.

## How many stocks get analysed

Every constituent is always scored in Python, which costs nothing. Whenever you ask for a new index or ETF, the
app asks **how many of the top-ranked stocks should get full written proof**. The maximum is the index size. Each
choice comes with an estimated Claude cost and duration, and the app checks that your number is in range.
Processing is paginated at every step, which keeps NSE load and agent context bounded:

| Stage | Pagination |
|---|---|
| Price download | Only stale stocks are fetched, in batches of `FETCH_BATCH_SIZE` (25). There is a `FETCH_BATCH_PAUSE` (3 s) rest between batches, on top of a global 0.35 s request throttle |
| Ranking | Computed once per data date and cached. `screen_universe` returns at most 25 rows per call (`offset`/`next_offset`) |
| Deep-dive | The N picks are split into pages of 10. Each page gets its own technical-analyst and market-context subagent, and all of them run in parallel using batch tools (`technical_snapshots`, `market_context_batch`) |
| Report | Written in parts: the overview and full ranked table first, then one message per page of full proof sections |

The estimates use `EST_BASE_COST`, `EST_PER_STOCK_COST` and the related `EST_*` settings. Tune them in `.env` if your actual costs differ.

## Architecture

```
CLI chat (rich) ──► Orchestrator agent (ClaudeSDKClient)
                      │ delegates via Agent tool
     ┌────────────────┼────────────────────────┐
 data-collector   technical-analyst   market-context-analyst
     └──── in-process MCP server "market" (Python tools) ────┘
                      │
        NSE client (jugaad-data, throttled + retry) ◄─► SQLite cache (data/market.db)
```

- **Deterministic numbers, LLM reasoning.** Python computes every indicator, signal and score. The agents only cite tool output and are told never to quote a number from memory.
- **Scoring (0–100):** 0.85 × technical (trend 30, momentum 25, volume/delivery 15, relative strength vs benchmark 20, breakout/volatility 10) + fundamentals 0–15 (growth 6, profitability trend 4, quality 5). Without a parsable filing a stock gets a neutral 7.5.
  - Any fundamental red flag caps BUY/ACCUMULATE at **WATCH**.
  - Verdicts: **BUY** ≥ 70 (the stock must also be above SMA200 with RSI ≤ 75), **ACCUMULATE** ≥ 55, **WATCH** ≥ 40, otherwise **AVOID**.
- **Trade plan:**
  - Stop = max(close − 2×ATR, 10-session swing low − 0.25×ATR), and never tighter than 1 ATR below the close.
  - Targets: T1 = 1.5R, T2 = 3R.
  - You get a warning when resistance sits below T1.
- **Indicators** are implemented directly in pandas, with no TA library: SMA/EMA 20/50/200, RSI 14, MACD 12/26/9, ADX/DI 14, Bollinger 20/2, Stochastic 14/3, ATR 14, OBV, volume ratios, delivery %, 52-week levels and swing support/resistance.

| Module | Role |
|---|---|
| `data/nse_client.py` | jugaad-data wrapper: one shared session, rate limit, retry with backoff, 90-day request chunks |
| `data/universe.py` | Index/ETF name → canonical NSE index → constituents (ETF map, aliases, fuzzy suggestions) |
| `data/prices.py` | Incremental refresh logic (compares against the expected last trading session) |
| `data/filings.py` | Quarterly results: list, download, archive to `data/filings/<SYMBOL>/`, parse XBRL, store; `rebuild_from_disk()` restores the table offline |
| `analysis/` | Indicators, signals with evidence strings, scoring and trade plan |
| `service.py` | Screen, single-stock analysis, NSE context (quote, sector P/E, announcements, index P/E percentile) |
| `tools.py` / `agents.py` / `orchestrator.py` | MCP tools, subagent definitions and prompts, SDK options |

### Cache rules

| Data | Freshness |
|---|---|
| Daily prices (stocks and indices) | Fetched when the last stored date is older than the latest completed NSE session, at most once every 2 h |
| Index constituents | 7 days |
| Live quote | 15 min |
| Corporate announcements, index P/E history | 1 day |
| Quarterly results | No call until the next quarter has ended; then one listing call per 12 h until it appears. Raw XBRL is never downloaded twice |
| Computed analysis | Reused until a new price date or a new/revised filing arrives |

To start from a completely clean cache, delete `data/market.db`. The archived filings in `data/filings/` survive that: `filings.rebuild_from_disk()` reloads them with no network calls.

## Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `MAIN_MODEL` | `us.anthropic.claude-opus-5-5` | Bedrock model / inference-profile ID |
| `SUBAGENT_MODEL` | `inherit` | For example `us.anthropic.claude-sonnet-5-5`, to cut cost |
| `DB_PATH` | `data/market.db` | Relative paths are resolved from the project root |
| `HISTORY_DAYS` | `550` | Calendar days of history per stock |
| `FILINGS_DIR` | `data/filings` | Archive of raw quarterly-results XBRL + metadata JSON |
| `MAX_TURNS` | `60` | Agent turn cap per question |

Logs go to `data/app.log`.

## Tests

```bash
uv run pytest      # offline: indicators, signals, fundamentals/XBRL parsing, filings archive, DB, universe matching
```

## Limitations

- NSE has no API for ETF holdings. ETFs are therefore analysed through the index they track, using a static map plus NSE's ETF list.
- NSE endpoints change from time to time and can rate-limit you. If fetches fail, wait a bit and run `/refresh`.
- Fundamentals come only from NSE integrated filings, which start at the March 2025 quarter (about 6 quarters). There is no long-term trend, no forecasts, and no news from outside NSE announcements.
