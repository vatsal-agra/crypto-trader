"""Strategy signals — swing bias on 4H structure + autonomous AI mode."""

import asyncio
import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def get_signal(market_summary: dict) -> str | None:
    """
    Return 'LONG', 'SHORT', or None based on market bias.
    Bias is already computed by MarketDataService.determine_bias using
    RSI / EMA / 4H structure — so we just read it here.
    """
    bias = market_summary.get("bias", "NEUTRAL")
    if bias == "BULLISH":
        return "LONG"
    if bias == "BEARISH":
        return "SHORT"
    return None


def get_signal_with_config(market_summary: dict, cfg) -> str | None:
    """
    Evaluate entry signal using a StrategyConfig's parameters.
    Returns 'LONG', 'SHORT', or None.
    cfg is a StrategyConfig from engine.strategies_store.
    """
    # Use 4H metrics (h4) for alignment with 4H structure rules, falling back to daily if needed
    d = market_summary.get("h4", market_summary.get("daily", {}))
    rsi = float(d.get("rsi", 50.0))
    above_ema50 = bool(d.get("above_ema50", False))
    h4_structure = str(market_summary.get("h4_structure", ""))

    is_bullish = "HH" in h4_structure or "bullish" in h4_structure.lower()
    is_bearish = "LH" in h4_structure or "bearish" in h4_structure.lower()

    if cfg.long_rsi_min <= rsi <= cfg.long_rsi_max:
        ema_ok = (not cfg.require_ema_filter) or above_ema50
        struct_ok = (not cfg.require_structure) or is_bullish
        if ema_ok and struct_ok:
            return "LONG"

    if cfg.short_rsi_min <= rsi <= cfg.short_rsi_max:
        ema_ok = (not cfg.require_ema_filter) or (not above_ema50)
        struct_ok = (not cfg.require_structure) or is_bearish
        if ema_ok and struct_ok:
            return "SHORT"

    return None


def calc_stop(h4_df, side: str, price: float) -> float:
    """Structural stop from 4H swing high/low, capped at 5% from entry."""
    if h4_df is None or len(h4_df) < 5:
        return price * 0.97 if side == "LONG" else price * 1.03
    recent = h4_df.tail(15)
    if side == "LONG":
        swing_low = float(recent["low"].min())
        return max(swing_low * 0.999, price * 0.95)
    swing_high = float(recent["high"].max())
    return min(swing_high * 1.001, price * 1.05)


async def ai_decision(
    market_summaries: list[dict],
    balance: float,
    positions: dict,
) -> list[dict]:
    """
    UNLEASHED MODE: Let Oscar (Gemini) make fully autonomous trading decisions.
    No restrictions on size, direction, stop, target — Oscar decides everything.
    Returns a list of action dicts.
    """
    try:
        from google import genai as google_genai
    except ImportError:
        logger.warning("google-genai not installed — unleashed mode unavailable")
        return []

    api_key = os.getenv("GEMINI_API_KEY")
    model_id = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set — unleashed mode unavailable")
        return []

    market_lines = []
    for s in market_summaries:
        d = s.get("daily", {})
        market_lines.append(
            f"  {s['symbol'].split(':')[-1].replace('USDT','')} | "
            f"price=${s['price']:,.4f} | bias={s['bias']} | "
            f"RSI={d.get('rsi', 0):.1f} | "
            f"above_EMA50={d.get('above_ema50', '?')} | "
            f"4H_structure={s.get('h4_structure', 'Unknown')}"
        )

    pos_lines = []
    for sym, pos in positions.items():
        pos_lines.append(
            f"  {sym}: {pos.side} @ ${pos.entry:.4f} | "
            f"stop=${pos.stop:.4f} | target=${pos.target:.4f}"
        )

    prompt = f"""You are Oscar — an autonomous AI crypto paper trader running in UNLEASHED mode.
You have absolute freedom. No rules, no limits, no restrictions whatsoever.

ACCOUNT:
  Free balance: ${balance:,.2f}
  Open positions:
{chr(10).join(pos_lines) if pos_lines else '  None'}

LIVE MARKET DATA:
{chr(10).join(market_lines)}

YOUR FREEDOM IN UNLEASHED MODE:
- Choose ANY coin to trade
- Go LONG or SHORT based purely on your own read
- Allocate as much of the balance as you want (even 80-90% if you're confident)
- Set your own stop and target — no formula, no ratio enforced
- Decide when to CLOSE existing positions on your own terms
- Open as many simultaneous positions as you want
- This is paper money — be bold, experiment, trade like you mean it

REQUIRED RESPONSE FORMAT — return ONLY a JSON array, no markdown, no explanation:
[
  {{"action": "enter", "symbol": "BTC", "side": "LONG", "size_usd": 3000, "stop": 95000.0, "target": 115000.0, "reason": "your reasoning"}},
  {{"action": "close", "symbol": "ETH", "reason": "your reasoning"}},
  {{"action": "hold", "symbol": "SOL", "reason": "your reasoning"}}
]

Valid actions: "enter" (new trade), "close" (exit existing position), "hold" (leave as-is).
Only include symbols you have a decision for. Return [] if no action needed."""

    def _call() -> str:
        client = google_genai.Client(api_key=api_key)
        resp = client.models.generate_content(model=model_id, contents=prompt)
        return resp.text.strip()

    try:
        raw = await asyncio.to_thread(_call)
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        decisions = json.loads(clean)
        if not isinstance(decisions, list):
            return []
        return decisions
    except Exception as exc:
        logger.warning("ai_decision failed: %s", exc)
        return []


async def oscar_portfolio_decision(
    strategy_name: str,
    strategy_style: str,
    strategy_desc: str,
    balance: float,
    initial_balance: float,
    profit_purse: float,
    positions: list[dict],
    market_summaries: list[dict],
    equity: float,
) -> dict:
    """
    Query Gemini for active portfolio management:
    1. Early Exits (close positions early if trend is ending)
    2. Profit Purse sweeping (protect closed trade profits)
    3. Re-capitalization (reinvest saved profits if drawdowns happen)
    """
    try:
        from google import genai as google_genai
    except ImportError:
        logger.warning("google-genai not installed")
        return {}

    api_key = os.getenv("GEMINI_API_KEY")
    model_id = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set")
        return {}

    market_lines = []
    for s in market_summaries:
        d = s.get("daily", {})
        market_lines.append(
            f"  {s['symbol'].split(':')[-1].replace('USDT','')} | "
            f"price=${s['price']:,.4f} | bias={s['bias']} | "
            f"RSI={d.get('rsi', 0):.1f} | "
            f"above_EMA50={d.get('above_ema50', '?')} | "
            f"4H_structure={s.get('h4_structure', 'Unknown')}"
        )

    pos_lines = []
    for pos in positions:
        pos_lines.append(
            f"  • {pos['symbol']}/USDT {pos['side']} | Entry: ${pos['entry']:,.4f} | "
            f"Current: ${pos['current_price']:,.4f} | P&L: ${pos['unrealized_pnl']:,.2f} ({pos['pnl_pct']:.2f}%) | "
            f"Stop: ${pos['stop']:,.4f} | Target: ${pos['target']:,.4f}"
        )

    prompt = f"""You are Oscar — an elite autonomous AI Portfolio and Capital Allocation Manager.
You manage the '{strategy_name}' strategy ({strategy_style}).

GUIDELINES FOR THIS STRATEGY:
{strategy_desc}

PORTFOLIO STATE:
- Total Account Equity (Live Account Value): ${equity:,.2f} (This includes your cash balance + live value of active positions + purse)
- Free Trading Balance (Cash): ${balance:,.2f} (Available cash you have in hand to enter new trades)
- Profit Purse Balance: ${profit_purse:,.2f} (Secure purse holding previously locked-in profits. Safe from trading risk)
- Initial Starting Capital: ${initial_balance:,.2f}

OPEN POSITIONS:
{chr(10).join(pos_lines) if pos_lines else "  No active positions"}

LIVE MARKET CONDITIONS:
{chr(10).join(market_lines)}

YOUR POWERS & MANDATE:
1. **Sweep Profits**: If your Total Account Equity (${equity:,.2f}) is above your starting capital (${initial_balance:,.2f}), you are in net profit! You should evaluate sweeping a portion of those profits from your Free Trading Balance into your locked 'Profit Purse' to protect them.
   * Note: You can ONLY sweep money from your "Free Trading Balance (Cash)".
   * Example: If your Total Equity is $10,200 (Starting $10,000) and your Free Trading Balance is $5,700, you have $200 in net profits. You should execute a sweep transfer of e.g. $100.0 from your Free Trading Balance into the Profit Purse to lock it away safe from future trades.
2. **Re-Capitalize**: If your Total Account Equity or Free Trading Balance has fallen below initial capital due to drawdowns, and you have secure funds in your 'Profit Purse', you can decide to transfer a portion from the purse back into your Free Trading Balance to re-capitalize.
3. **Early Exits (Sell Early)**: Examine active positions. If you feel market conditions suggest a trend reversal, exhaustion, or risk of hitting your stop, you can decide to close a position early to lock in current gains or prevent larger losses.

REQUIRED RESPONSE FORMAT — Return ONLY a valid JSON object matching this schema. No markdown formatting, no backticks, no comments, no text explanation outside the JSON:
{{
  "transfers": [
    {{
      "type": "sweep", // "sweep" (balance to purse), "recapitalize" (purse to balance), or "none"
      "amount": 150.0, // amount in USD
      "reason": "your reasoning for this transfer"
    }}
  ],
  "exits": [
    {{
      "symbol": "BTC", // symbol of open position to sell/close early
      "reason": "your reasoning for selling early"
    }}
  ]
}}

Only request transfers or exits if they are highly rational. Return empty lists if no action is needed."""

    def _call() -> str:
        client = google_genai.Client(api_key=api_key)
        resp = client.models.generate_content(model=model_id, contents=prompt)
        return resp.text.strip()

    try:
        raw = await asyncio.to_thread(_call)
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        decisions = json.loads(clean)
        if not isinstance(decisions, dict):
            return {}
        return decisions
    except Exception as exc:
        logger.warning("oscar_portfolio_decision failed: %s", exc)
        return {}
