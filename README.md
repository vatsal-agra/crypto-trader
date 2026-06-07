# Oscar — AI Crypto Trading Assistant

Oscar is an AI-powered Telegram bot for swing-trading crypto. Send text or voice messages and get actionable trading insights — powered by Google Gemini AI and live Binance data.

---

## What Oscar Does

| Feature | Detail |
|---------|--------|
| 🔍 Watchlist scan | Scans BTC, ETH, SOL, LINK, AVAX, SUI concurrently |
| 📊 Technical analysis | RSI(14), MACD(12/26/9), 50/200 EMA, 4H market structure |
| 🎤 Voice messages | Whisper transcription → same AI response |
| 🧠 Conversation memory | Remembers context within a session per user |
| ⚡ Live data | Real-time OHLCV from Binance via CCXT |

---

## Project Structure

```
crypto trader attempt 2/
├── main.py                  # Entry point — python main.py
├── requirements.txt
├── .env.example             # Copy to .env and fill in keys
├── config/
│   └── rules.json           # Swing-trading rules (watchlist, bias, risk)
├── oscar/
│   ├── __init__.py
│   ├── bot.py               # Telegram bot — all commands and handlers
│   ├── analyzer.py          # Gemini API integration (Oscar persona)
│   ├── market.py            # CCXT data fetching + indicator engine
│   └── voice.py             # OpenAI Whisper transcription
└── scripts/
    └── setup_mcp.ps1        # Windows: sets up TradingView MCP for Claude Code
```

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- A Telegram Bot Token → create one via [@BotFather](https://t.me/BotFather)
- A [Google Gemini API key](https://aistudio.google.com/app/apikey) — free tier available
- *(Optional)* An [OpenAI API key](https://platform.openai.com/) for voice transcription

### 2. Install

```powershell
cd "crypto trader attempt 2"
pip install -r requirements.txt
```

### 3. Configure

```powershell
copy .env.example .env
# Open .env and fill in your API keys
```

```env
TELEGRAM_BOT_TOKEN=your_token_here
GEMINI_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here        # optional, for voice
GEMINI_MODEL=gemini-1.5-flash       # or gemini-1.5-pro for more power
```

### 4. Run Oscar

```powershell
python main.py
```

Open your Telegram bot and type `/start`.

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and capabilities |
| `/scan` | Scan full watchlist — majors + alts |
| `/analyse BTC` | Full technical breakdown of any coin |
| `/price ETH` | Quick price, bias, RSI snapshot |
| `/clear` | Reset your conversation history |
| `/help` | List all commands |

You can also just **talk naturally** — ask anything in text or send a voice message.

**Example voice prompts:**
> *"Oscar, anything interesting in the majors right now?"*
> *"Oscar, give me a quick read on BTC and ETH."*
> *"Oscar, what setups are triggering on my watchlist today?"*

---

## Trading Rules

Oscar's analysis follows `config/rules.json`:

- **Bias (Bullish):** Price above 50D EMA + RSI 45–70 + 4H HH/HL structure
- **Bias (Bearish):** Price below 50D EMA + RSI < 45 + 4H LH/LL structure
- **Bias (Neutral):** Anything else
- **Min R/R:** 2:1 — Oscar won't suggest a trade below this threshold
- **Max risk:** 1% of portfolio per trade
- **Avoid:** Major CPI, FOMC, weekend thin liquidity

Edit `config/rules.json` to change any of these at any time.

---

## TradingView MCP Setup (for Claude Code)

If you also want to use Claude Code with TradingView Desktop (live chart analysis, drawing tools, Pine Script injection):

```powershell
.\scripts\setup_mcp.ps1
```

This will:
1. Clone the `tradingview-mcp` server to `~/tradingview-mcp`
2. Install npm dependencies
3. Add the MCP entry to `~/.claude/mcp.json`
4. Pre-approve TradingView tools in `~/.claude/settings.json`
5. Copy `config/rules.json` into the MCP directory

After running: restart Claude Code, open TradingView Desktop, then run `tv_health_check` in Claude Code.

---

## Symbols Tracked

| Group | Symbols |
|-------|---------|
| Majors | BTC, ETH, SOL |
| Alts | LINK, AVAX, SUI |

Add or remove symbols in `config/rules.json` → `watchlist.majors` / `watchlist.alts`.

> **Note:** `CRYPTOCAP:TOTAL`, `CRYPTOCAP:TOTAL3`, `CRYPTOCAP:BTC.D` are kept in rules.json for Claude Code / TradingView reference but are skipped in the bot's live data scan (not available via exchange APIs).

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `TELEGRAM_BOT_TOKEN is not set` | Create `.env` from `.env.example` and add your token |
| `/scan` returns no data | Check internet connection; Binance may be rate-limiting |
| Voice messages fail | Add `OPENAI_API_KEY` to `.env` |
| Claude returns an error | Check `ANTHROPIC_API_KEY` and your account credits |
| `tv_health_check` fails in Claude Code | Close TradingView fully, re-run `setup_mcp.ps1`, restart Claude Code |
