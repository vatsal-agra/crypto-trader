# Autonomous Arena — AI Crypto Trader

A fully **LLM-driven** autonomous crypto trading system. Multiple independent AI
agents (powered by Google Gemini) each run their own paper-trading account,
**decide everything themselves** — which coins to trade, long or short, position
size, stops, targets, and their own evolving strategy — and compete in a live
arena. **Nothing about the trading is hardcoded.** The code only discovers market
data, executes the AI's decisions on paper, and enforces a thin safety layer.

> Trading mode is **paper only**. No live-exchange order execution is implemented.

---

## What makes it "autonomous"

| Old (rule-driven) | New (AI-driven) |
|---|---|
| Fixed 6-coin watchlist | **Dynamic universe** — top N coins by 24h volume, discovered live |
| Hardcoded RSI/EMA entry rules | **AI authors & evolves its own strategy** every cycle |
| Code-coded 1% risk sizing | **AI sizes freely** (within a safety cap) |
| One strategy | **N independent agents** competing, each with its own persona & memo |
| Binance.com only | **Multi-exchange** via ccxt with auto-failover |

Each cycle every agent is handed its account state, the live universe, deep
technicals, its open positions, recent trades, and **its own last strategy memo**.
It returns a JSON decision: which positions to open/close, sizes, stops, targets,
a rewritten strategy memo, and a watchlist for next cycle. The indicators shown
to the AI are *context only* — it may use, combine, or ignore them.

---

## Architecture

```
├── main.py                  # Entry point — python main.py (web) / --bot (legacy Telegram)
├── server.py                # FastAPI server + dashboard REST API (the autonomous arena)
├── engine/
│   ├── config.py            # Runtime knobs + safe defaults + Gemini key collection (NO strategy params)
│   ├── brain.py             # Gemini decision engine: multi-key rotation, self-evolving memo, JSON parse, no-AI fallback
│   ├── agent.py             # An autonomous agent = brain + paper account + evolving memo + safety
│   ├── safety.py            # The ONLY code-enforced rules: paper mode, kill switch, drawdown breaker, caps
│   └── trading_loop.py      # AutonomousArena orchestrator (universe discovery, snapshot, dispatch, exit monitor)
├── oscar/
│   ├── market.py            # ccxt multi-exchange OHLCV + indicators + dynamic universe discovery
│   ├── paper_trader.py      # Paper account mechanics (free-form entries)
│   ├── analyzer.py          # Legacy Gemini analyzer (Telegram)
│   ├── bot.py               # Legacy Telegram bot
│   └── voice.py             # OpenAI Whisper transcription (legacy)
└── web/index.html           # Live dashboard (React, single file)
```

---

## Quick Start

### 1. Install
```bash
pip install -r requirements.txt
```

### 2. Configure
Copy `.env.example` to `.env` and add at least one Gemini API key:
```env
GEMINI_API_KEY=your_key_here
GEMINI_API_KEY_2=optional_second_key      # round-robin rotation for higher throughput
GEMINI_MODEL=gemini-2.5-flash
```
Get free keys at <https://aistudio.google.com/app/apikey>.

> **Model note:** some free-tier keys have **quota 0** on `gemini-2.0-flash`, and
> the `1.5` models are retired. `gemini-2.5-flash` is the verified default.

### 3. Run
```bash
python main.py
```
Open <http://localhost:8000> for the dashboard. The arena starts immediately and
runs its first decision cycle within ~1 minute.

If no Gemini key is set, the system still runs in **degraded mode** (agents hold
positions passively and the exit monitor still honors stops/targets).

---

## Configuration (all via env vars — all optional)

**Scale**
| Var | Default | Meaning |
|---|---|---|
| `NUM_AGENTS` | 6 | independent competing agents |
| `UNIVERSE_SIZE` | 60 | coins discovered per cycle (by 24h volume) |
| `DEEP_ANALYSIS_BUDGET` | 25 | coins with full technicals fetched per cycle |
| `CYCLE_MINUTES` | 15 | minutes between decision cycles |
| `STARTING_BALANCE` | 10000 | paper $ per agent |

**Exchange / data**
| Var | Default | Meaning |
|---|---|---|
| `EXCHANGE_ID` | `auto` | `auto` failover, or a specific ccxt id (e.g. `binance`, `kucoin`) |
| `QUOTE_ASSET` | `USDT` | quote currency for the universe |

Auto-failover order: `binance → binanceus → kucoin → gateio → okx → kraken → coinbase`.

**Safety (the only code-enforced rules)**
| Var | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `paper` | only paper is implemented |
| `MAX_OPEN_POSITIONS` | 50 | per agent |
| `MAX_DAILY_DRAWDOWN_PCT` | 25 | halt new entries for the day if breached |
| `MAX_ALLOC_PCT_PER_TRADE` | 40 | clamp any single position size |
| `KILL_SWITCH` | `false` | set true (or create a `STOP` file) to halt all new entries |

**Gemini keys** — any of: `GEMINI_API_KEY`, `GEMINI_API_KEY_2..N`, or a
comma-separated `GEMINI_API_KEYS` bundle. All are pooled and rotated round-robin.

---

## Dashboard

The single-file React dashboard (`web/index.html`) shows:
- Live header (exchange, agents, keys, universe size, cycle, run #)
- Aggregate equity curve + portfolio KPIs
- **Live universe heatmap** (top coins colored by 24h change)
- **Agent cards** — equity sparkline, win rate, and each agent's evolving memo
- **Agent detail** — full self-authored strategy memo, watchlist, open positions, closed trades, and a per-trade activity log with the AI's reasoning
- Candle charts for any coin, and a safety panel

---

## Safety model

The AI has complete freedom over *what* to trade. The code never tells it a
strategy. The only hard limits the code enforces are in `engine/safety.py`:
paper-mode (live execution refused), kill switch / `STOP` file, daily drawdown
circuit breaker, max open positions, and a per-trade allocation cap. Everything
else — coin selection, direction, sizing, stops, targets, entry/exit timing — is
the AI's own decision.

---

## Legacy Telegram bot

The original Telegram assistant is still available:
```bash
python main.py --bot
```
Requires `TELEGRAM_BOT_TOKEN` (and optionally `OPENAI_API_KEY` for voice).
