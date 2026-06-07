"""Oscar Telegram bot — command handlers and message routing."""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from oscar.analyzer import Analyzer
from oscar.market import MarketDataService
from oscar.paper_trader import PaperTrader
from oscar.voice import transcribe_voice
from engine.strategies_store import StrategyConfig, FALLBACK_STRATEGIES

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap — load config and shared services
# ---------------------------------------------------------------------------

_RULES_PATH = Path(__file__).parent.parent / "config" / "rules.json"
_DATA_DIR = Path(__file__).parent.parent / "data"

with _RULES_PATH.open() as _f:
    RULES: dict = json.load(_f)

_market: MarketDataService = MarketDataService()
_analyzer: Analyzer = Analyzer(RULES)

def get_arena_data() -> list[tuple[StrategyConfig, PaperTrader]]:
    strategies = []
    strategies_file = _DATA_DIR / "strategies.json"
    if strategies_file.exists():
        try:
            data = json.loads(strategies_file.read_text())
            strategies = [StrategyConfig.from_dict(d) for d in data]
        except Exception:
            pass
    if not strategies:
        strategies = FALLBACK_STRATEGIES

    res = []
    for strat in strategies:
        state_file = _DATA_DIR / f"arena_{strat.id}.json"
        res.append((strat, PaperTrader(state_file=state_file)))
    return res

_paper: PaperTrader = PaperTrader()

_WATCHLIST_MAJORS: list[str] = RULES["watchlist"]["majors"]
_WATCHLIST_ALTS: list[str] = RULES["watchlist"]["alts"]
_ALL_SYMBOLS: list[str] = _WATCHLIST_MAJORS + _WATCHLIST_ALTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bias_emoji(bias: str) -> str:
    return {"BULLISH": "🟢", "BEARISH": "🔴"}.get(bias, "🟡")


def _coin_to_tv(coin: str) -> str:
    """'BTC' or 'BTCUSDT' -> 'BINANCE:BTCUSDT'."""
    clean = coin.upper().replace("USDT", "").replace("/", "")
    return f"BINANCE:{clean}USDT"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hey, I'm <b>Oscar</b> — your AI crypto trading assistant.\n\n"
        "<b>Commands:</b>\n"
        "• /scan — Scan the full watchlist for setups\n"
        "• /analyse BTC — Full technical analysis of any coin\n"
        "• /price BTC — Quick price + bias snapshot\n"
        "• /clear — Reset conversation history\n"
        "• /help — Show this message\n\n"
        "Or just <i>talk to me naturally</i> — text or 🎤 voice.\n\n"
        "Let's find some setups 🔍",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Oscar — Command Reference</b>\n\n"
        "<b>Analysis</b>\n"
        "/scan — Full watchlist scan\n"
        "/analyse BTC — Deep technical analysis\n"
        "/price BTC — Quick price + bias\n"
        "/clear — Reset conversation\n\n"
        "<b>Arena &amp; Strategies</b>\n"
        "/arena — Live Strategy Arena Leaderboard (P&amp;L rankings)\n"
        "/positions — View open positions + live P&amp;L per strategy\n"
        "/balance — Account equity + trade stats for all strategies\n"
        "/history — Last 10 closed trades in the Arena\n\n"
        "<i>Or just talk / send a voice message.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)
    msg = await update.message.reply_text(
        "🔍 Scanning watchlist — pulling live data for all pairs…"
    )

    results = await _market.scan_symbols(_ALL_SYMBOLS, RULES)

    if not results:
        await msg.edit_text(
            "❌ Couldn't fetch market data. Check your connection and try again."
        )
        return

    summary = await _analyzer.scan_summary(results)
    await msg.edit_text(summary, parse_mode=ParseMode.HTML)


async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/analyse BTC</code> or <code>/analyse ETHUSDT</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    coin = context.args[0]
    tv_symbol = _coin_to_tv(coin)

    await update.effective_chat.send_action(ChatAction.TYPING)
    ticker = coin.upper().replace("USDT", "")
    msg = await update.message.reply_text(f"📊 Pulling data for {ticker}…")

    data = await _market.get_market_summary(tv_symbol, RULES)

    if not data or "error" in data:
        await msg.edit_text(
            f"❌ No data found for <b>{ticker}</b>. "
            "Double-check the ticker and try again.",
            parse_mode=ParseMode.HTML,
        )
        return

    response = await _analyzer.analyze_symbol(ticker, data)
    await msg.edit_text(response, parse_mode=ParseMode.HTML)


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/price BTC</code>", parse_mode=ParseMode.HTML
        )
        return

    coin = context.args[0]
    tv_symbol = _coin_to_tv(coin)
    ticker = coin.upper().replace("USDT", "")

    await update.effective_chat.send_action(ChatAction.TYPING)

    data = await _market.get_market_summary(tv_symbol, RULES)

    if not data or "error" in data:
        await update.message.reply_text(f"❌ Couldn't get price for <b>{ticker}</b>.", parse_mode=ParseMode.HTML)
        return

    daily = data.get("daily", {})
    price = data.get("price", 0.0)
    bias = data.get("bias", "NEUTRAL")
    rsi = daily.get("rsi", 0.0)
    above_ema50 = daily.get("above_ema50", False)
    ema50 = daily.get("ema50", 0.0)
    structure = data.get("h4_structure", "Unknown")

    price_fmt = f"${price:,.4f}" if price < 1 else f"${price:,.2f}"
    ema_fmt = f"${ema50:,.2f}"

    text = (
        f"<b>{ticker}/USDT</b>\n"
        f"Price: <code>{price_fmt}</code>\n"
        f"Bias: {_bias_emoji(bias)} <b>{bias}</b>\n"
        f"RSI(14): <code>{rsi:.1f}</code>\n"
        f"50 EMA: {ema_fmt} — {'Above ✅' if above_ema50 else 'Below ❌'}\n"
        f"4H: {structure}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    _analyzer.clear_history(user_id)
    await update.message.reply_text("🗑️ Conversation history cleared.")


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pass free-text questions to Oscar with BTC+ETH as quick context."""
    user_id = update.effective_user.id
    text = update.message.text

    await update.effective_chat.send_action(ChatAction.TYPING)

    context_data = await _market.scan_symbols(
        ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"], RULES
    )

    response = await _analyzer.analyze(user_id, text, context_data)
    await update.message.reply_text(response, parse_mode=ParseMode.HTML)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe a voice message then handle it like text."""
    if not os.getenv("OPENAI_API_KEY"):
        await update.message.reply_text(
            "⚠️ Voice transcription requires <code>OPENAI_API_KEY</code> in your .env file.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    status_msg = await update.message.reply_text("🎤 Transcribing…")

    tmp_dir = tempfile.gettempdir()
    voice_path = Path(tmp_dir) / f"oscar_voice_{update.update_id}.ogg"

    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(str(voice_path))

        transcribed = await transcribe_voice(str(voice_path))

        await status_msg.edit_text(
            f'🎤 <i>"{transcribed}"</i>\n\n⏳ Thinking…',
            parse_mode=ParseMode.HTML,
        )

        user_id = update.effective_user.id
        context_data = await _market.scan_symbols(
            ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT"], RULES
        )

        response = await _analyzer.analyze(user_id, transcribed, context_data)

        await status_msg.edit_text(
            f'🎤 <i>"{transcribed}"</i>\n\n{response}',
            parse_mode=ParseMode.HTML,
        )

    except RuntimeError as exc:
        await status_msg.edit_text(f"❌ {exc}")
    except Exception as exc:
        logger.error("Voice handler error: %s", exc)
        await status_msg.edit_text("❌ Something went wrong processing your voice message.")
    finally:
        if voice_path.exists():
            voice_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Paper trading helpers
# ---------------------------------------------------------------------------

async def _auto_stop(h4_df, side: str, price: float) -> float:
    """Derive a structural stop from the last 15 x 4H candles."""
    if h4_df is None or len(h4_df) < 5:
        return price * 0.97 if side == "LONG" else price * 1.03
    recent = h4_df.tail(15)
    if side == "LONG":
        swing_low = float(recent["low"].min())
        stop = swing_low * 0.999
        return max(stop, price * 0.95)   # cap at 5 % below entry
    else:
        swing_high = float(recent["high"].max())
        stop = swing_high * 1.001
        return min(stop, price * 1.05)   # cap at 5 % above entry


async def cmd_long(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚠️ Manual trades are disabled.\n"
        "Oscar now manages 5 independent, fully autonomous strategies in the Strategy Arena.\n"
        "Use <code>/arena</code> to see live rankings and stats!",
        parse_mode=ParseMode.HTML,
    )


async def cmd_short(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚠️ Manual trades are disabled.\n"
        "Oscar now manages 5 independent, fully autonomous strategies in the Strategy Arena.\n"
        "Use <code>/arena</code> to see live rankings and stats!",
        parse_mode=ParseMode.HTML,
    )


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚠️ Manual trades are disabled.\n"
        "Oscar now manages 5 independent, fully autonomous strategies in the Strategy Arena.\n"
        "Use <code>/arena</code> to see live rankings and stats!",
        parse_mode=ParseMode.HTML,
    )


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)
    arena_data = get_arena_data()
    
    total_positions_count = sum(len(p.positions) for _, p in arena_data)
    if total_positions_count == 0:
        await update.message.reply_text("📋 No open positions across any strategy in the Arena.")
        return

    lines = ["<b>📋 Open Positions (Arena)</b>\n"]
    total_upnl = 0.0

    for strat, paper in arena_data:
        if not paper.positions:
            continue
        
        lines.append(f"🤖 <b>{strat.name}</b>")
        symbols = [f"{sym}/USDT" for sym in paper.positions]
        prices_list = await asyncio.gather(*[_market.get_price(s) for s in symbols])
        current_prices = {
            sym: (prices_list[i] or pos.entry)
            for i, (sym, pos) in enumerate(paper.positions.items())
        }

        for sym, pos in paper.positions.items():
            cur = current_prices[sym]
            upnl = pos.unrealized_pnl(cur)
            upnl_pct = pos.pnl_pct(cur)
            total_upnl += upnl
            sign = "+" if upnl >= 0 else ""
            emoji = "🟢" if pos.side == "LONG" else "🔴"
            pnl_color = "↗️" if upnl >= 0 else "↘️"

            lines.append(
                f"  {emoji} <b>{sym}/USDT {pos.side}</b>\n"
                f"    Entry: <code>${pos.entry:,.4f}</code> | Now: <code>${cur:,.4f}</code>\n"
                f"    P\u0026L: <code>{sign}${upnl:,.2f}</code> ({sign}{upnl_pct:.2f}%) {pnl_color}"
            )
        lines.append("")

    total_sign = "+" if total_upnl >= 0 else ""
    lines.append(f"Combined Unrealised: <code>{total_sign}${total_upnl:,.2f}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)
    arena_data = get_arena_data()

    tot_balance = 0.0
    tot_initial = 0.0
    tot_equity = 0.0
    agg_count = 0
    agg_wins = 0
    agg_losses = 0

    lines = ["<b>💰 Combined Arena Balance</b>\n"]

    for strat, paper in arena_data:
        symbols = [f"{sym}/USDT" for sym in paper.positions]
        if symbols:
            prices_list = await asyncio.gather(*[_market.get_price(s) for s in symbols])
            current_prices = {
                sym: (prices_list[i] or pos.entry)
                for i, (sym, pos) in enumerate(paper.positions.items())
            }
        else:
            current_prices = {}

        eq = paper.equity(current_prices)
        tot_balance += paper.balance
        tot_initial += paper.initial_balance
        tot_equity += eq

        st = paper.stats()
        agg_count += st.get("count", 0)
        agg_wins += st.get("wins", 0)
        agg_losses += st.get("losses", 0)

        pnl = eq - paper.initial_balance
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"• <b>{strat.name}</b>: "
            f"<code>${eq:,.2f}</code> ({sign}${pnl:,.2f})"
        )

    tot_pnl = tot_equity - tot_initial
    tot_pnl_pct = (tot_pnl / tot_initial * 100) if tot_initial else 0.0
    tot_sign = "+" if tot_pnl >= 0 else ""

    lines += [
        "\n<b>📊 Aggregate Metrics</b>",
        f"Total Equity: <code>${tot_equity:,.2f}</code>",
        f"Free Balance: <code>${tot_balance:,.2f}</code>",
        f"Overall P\u0026L: <code>{tot_sign}${tot_pnl:,.2f}</code> ({tot_sign}{tot_pnl_pct:.2f}%)",
    ]

    if (agg_wins + agg_losses) > 0:
        win_rate = (agg_wins / (agg_wins + agg_losses)) * 100
        lines.append(f"Combined WR:  <code>{win_rate:.1f}%</code> ({agg_wins}W / {agg_losses}L)")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arena_data = get_arena_data()
    combined_trades = []

    for strat, paper in arena_data:
        for t in paper.closed_trades:
            t_copy = dict(t)
            t_copy["strategy_name"] = strat.name
            combined_trades.append(t_copy)

    if not combined_trades:
        await update.message.reply_text("📜 No closed trades across the Arena yet.")
        return

    # Sort combined trades by exit time descending (most recent first)
    combined_trades.sort(key=lambda x: x.get("closed_at", ""), reverse=True)
    trades = combined_trades[:10]

    lines = ["<b>📜 Last 10 Closed Trades (Arena)</b>\n"]
    for t in trades:
        pnl = t["pnl"]
        sign = "+" if pnl >= 0 else ""
        emoji = "✅" if pnl >= 0 else "🟥"
        lines.append(
            f"{emoji} <b>{t['symbol']} {t['side']}</b>  "
            f"<code>{sign}${pnl:,.2f}</code>\n"
            f"  {t['entry']:,.4f} → {t['exit']:,.4f}  "
            f"({t['strategy_name']} | {t['closed_at'][:10]})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_arena(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)
    arena_data = get_arena_data()

    results = []
    for strat, paper in arena_data:
        symbols = [f"{sym}/USDT" for sym in paper.positions]
        if symbols:
            prices_list = await asyncio.gather(*[_market.get_price(s) for s in symbols])
            current_prices = {
                sym: (prices_list[i] or pos.entry)
                for i, (sym, pos) in enumerate(paper.positions.items())
            }
        else:
            current_prices = {}

        eq = paper.equity(current_prices)
        pnl = eq - paper.initial_balance
        pnl_pct = (pnl / paper.initial_balance) * 100 if paper.initial_balance else 0.0
        st = paper.stats()
        results.append({
            "name": strat.name,
            "style": strat.style,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "equity": eq,
            "stats": st,
            "open": len(paper.positions)
        })

    # Sort by P&L descending for Leaderboard ranking
    results.sort(key=lambda x: x["pnl"], reverse=True)
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["<b>🏟️ Strategy Arena Leaderboard</b>\n"]

    for rank, r in enumerate(results):
        sign = "+" if r["pnl"] >= 0 else ""
        wr_text = f"{r['stats']['win_rate']:.0f}%" if r["stats"].get("count") else "—"
        lines.append(
            f"{medals[rank]} <b>{r['name']}</b> ({r['style'].replace('_', ' ')})\n"
            f"  Equity: <code>${r['equity']:,.2f}</code> | P\u0026L: <code>{sign}${r['pnl']:,.2f}</code> ({sign}{r['pnl_pct']:.2f}%)\n"
            f"  Trades: <code>{r['stats'].get('count', 0)}</code> | WR: <code>{wr_text}</code> | Active: <code>{r['open']}</code>\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or context.args[0].lower() != "confirm":
        await update.message.reply_text(
            "⚠️ This will wipe all positions and trade history.\n"
            "Type <code>/reset confirm</code> to proceed.",
            parse_mode=ParseMode.HTML,
        )
        return
    _paper.reset()
    await update.message.reply_text(
        "🔄 Paper account reset. Starting balance: <code>$10,000.00</code>",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_bot() -> None:
    """Build the Telegram Application and start polling."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is not set. Add it to your .env file."
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("analyse", cmd_analyse))
    app.add_handler(CommandHandler("analyze", cmd_analyse))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("long", cmd_long))
    app.add_handler(CommandHandler("short", cmd_short))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("arena", cmd_arena))
    app.add_handler(CommandHandler("leaderboard", cmd_arena))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Oscar is online. Polling for updates…")
    app.run_polling(drop_pending_updates=True)
