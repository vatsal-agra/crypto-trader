"""The autonomous AI brain (Google Gemini).

This is where *all* trading intelligence lives. The brain is handed the full
picture — account state, the live tradeable universe, deep indicators for a
subset, open positions with P&L, its own past trades and the strategy memo it
wrote for itself last time — and returns:

1. concrete actions (enter / close / hold) with its *own* coin picks, sizing,
   direction, stop and target — nothing is dictated by code, and
2. a rewritten ``strategy_memo`` — its evolving, self-authored thesis. This is
   how the bot "analyzes, creates, and implements on its own": it literally
   edits its own playbook every cycle based on what worked.

Multiple API keys are rotated round-robin to sustain many coins × many agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from engine.config import gemini_keys, get_settings

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    actions: list[dict] = field(default_factory=list)
    strategy_memo: str = ""
    watchlist: list[str] = field(default_factory=list)
    commentary: str = ""
    ai_used: bool = False
    raw: str = ""


class GeminiPool:
    """Round-robin pool of Gemini clients across all available API keys."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._keys = gemini_keys()
        self._clients: list[Any] = []
        self._idx = 0
        if self._keys:
            try:
                from google import genai
                self._clients = [genai.Client(api_key=k) for k in self._keys]
                logger.info("GeminiPool ready — %d key(s), model=%s", len(self._clients), model)
            except Exception as exc:  # pragma: no cover - import/SDK failure
                logger.warning("Could not init Gemini clients: %s", exc)
                self._clients = []
        else:
            logger.warning("No Gemini API keys found — brain runs in no-AI fallback mode")

    @property
    def available(self) -> bool:
        return bool(self._clients)

    @property
    def num_keys(self) -> int:
        return len(self._clients)

    async def generate(self, prompt: str, max_output_tokens: int = 8192) -> Optional[str]:
        """Call Gemini, rotating keys on rate-limit/quota and retrying on 503.

        Thinking is disabled (``thinking_budget=0``) so the whole output budget
        goes to the JSON answer — 2.5 models otherwise spend it "thinking" and
        truncate the response.
        """
        if not self._clients:
            return None

        from google.genai import types
        cfg = types.GenerateContentConfig(
            max_output_tokens=max_output_tokens,
            temperature=0.7,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

        n = len(self._clients)
        max_attempts = n + 3
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            client = self._clients[self._idx]
            key_no = self._idx + 1
            self._idx = (self._idx + 1) % n  # advance for next call / next attempt

            def _call() -> str:
                resp = client.models.generate_content(
                    model=self._model, contents=prompt, config=cfg
                )
                return (resp.text or "").strip()

            try:
                out = await asyncio.to_thread(_call)
                if out:
                    return out
                last_exc = RuntimeError("empty response")
            except Exception as exc:
                last_exc = exc
                msg = repr(exc).lower()
                if any(t in msg for t in ("429", "rate", "quota", "exhaust", "resource")):
                    logger.warning("Gemini key #%d rate-limited — rotating (%s)", key_no, repr(exc)[:90])
                    continue
                if any(t in msg for t in ("503", "unavailable", "overload", "500", "deadline", "timeout")):
                    backoff = 1.5 * (attempt + 1)
                    logger.warning("Gemini key #%d transient error — retry in %.1fs (%s)", key_no, backoff, repr(exc)[:80])
                    await asyncio.sleep(backoff)
                    continue
                logger.warning("Gemini key #%d error: %s", key_no, repr(exc)[:120])
                continue
        logger.error("All Gemini attempts failed: %s", repr(last_exc)[:140] if last_exc else "?")
        return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _fmt_universe(universe: list[dict], limit: int) -> str:
    lines = []
    for r in universe[:limit]:
        chg = r.get("pct_change")
        chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "?"
        vol = r.get("quote_volume", 0) or 0
        last = r.get("last")
        last_s = f"{last:g}" if isinstance(last, (int, float)) else "?"
        lines.append(f"  {r['base']:<10} last={last_s:<12} 24h_vol={vol:,.0f} chg={chg_s}")
    return "\n".join(lines) if lines else "  (universe unavailable)"


def _fmt_snapshot(snapshot: list[dict]) -> str:
    lines = []
    for s in snapshot:
        d = s.get("daily", {})
        lines.append(
            f"  {s.get('base', '?'):<10} price=${s.get('price', 0):,.6g} bias={s.get('bias','?'):<8} "
            f"RSI={d.get('rsi', 0):.1f} aboveEMA50={d.get('above_ema50','?')} "
            f"aboveEMA200={d.get('above_ema200','?')} MACDhist={d.get('macd_hist', 0):+.4g} "
            f"4Hstruct={s.get('h4_structure','?')}"
        )
    return "\n".join(lines) if lines else "  (no deep analysis this cycle)"


def _fmt_positions(positions: list[dict]) -> str:
    if not positions:
        return "  None"
    lines = []
    for p in positions:
        lines.append(
            f"  {p['symbol']:<10} {p['side']:<5} entry=${p['entry']:,.6g} now=${p['current_price']:,.6g} "
            f"size=${p.get('size_usd', p.get('size', 0)):,.0f} uPnL=${p['unrealized_pnl']:,.2f} ({p['pnl_pct']:+.2f}%) "
            f"stop=${p['stop']:,.6g} target=${p['target']:,.6g}"
        )
    return "\n".join(lines)


def _fmt_recent_trades(trades: list[dict], limit: int = 10) -> str:
    if not trades:
        return "  None yet"
    lines = []
    for t in trades[-limit:]:
        pnl = t.get("pnl", 0.0)
        lines.append(
            f"  {t.get('symbol','?'):<10} {t.get('side','?'):<5} "
            f"entry=${t.get('entry',0):,.6g} exit=${t.get('exit',0):,.6g} pnl=${pnl:,.2f}"
        )
    return "\n".join(lines)


SYSTEM_PREAMBLE = """You are an ELITE fully-autonomous crypto trader running a paper-trading account.
You have COMPLETE freedom and full responsibility. There are NO hardcoded rules telling you what to do:
- You choose ANY coins from the live universe below — as many or as few as you want.
- You decide LONG or SHORT, your own position size in USD, your own stop and target.
- You invent and refine your OWN strategy. The indicators shown are just context — use, combine, or ignore them as you see fit.
- You manage the whole portfolio: open, hold, close, scale — your call, every cycle.
Your single objective: grow account equity over time while surviving (don't blow up)."""


def build_prompt(ctx: dict, settings) -> str:
    safety = ctx["safety"]
    deep_budget = settings.deep_analysis_budget
    return f"""{SYSTEM_PREAMBLE}

# YOUR IDENTITY
You are agent "{ctx['agent_name']}" (persona seed: {ctx['persona']}).

# YOUR CURRENT SELF-WRITTEN STRATEGY MEMO (you wrote this last cycle — evolve it)
{ctx['strategy_memo'] or '(empty — this is your first cycle; create your initial thesis)'}

# ACCOUNT STATE
- Mode: {safety['mode'].upper()} (simulated money)
- Total equity: ${ctx['equity']:,.2f}  (started at ${ctx['initial_balance']:,.2f}, all-time PnL {ctx['total_pnl_pct']:+.2f}%)
- Free cash to deploy: ${ctx['free_balance']:,.2f}
- Open positions: {ctx['open_count']} / max {safety['max_open_positions']}

# PERFORMANCE SO FAR
- Closed trades: {ctx['stats'].get('count', 0)} | win rate {ctx['stats'].get('win_rate', 0):.0f}% | realised PnL ${ctx['stats'].get('total_pnl', 0):,.2f}
Recent closed trades:
{_fmt_recent_trades(ctx['recent_trades'])}

# SAFETY GUARDRAILS (you operate INSIDE these — proposals outside get clamped/rejected)
- Per-trade size capped at {safety['max_alloc_pct_per_trade']:.0f}% of equity.
- Daily drawdown breaker at {safety['max_daily_drawdown_pct']:.0f}% (today: {safety['daily_drawdown_pct']:.1f}%). If tripped, new entries are blocked till next day.
- A stop is REQUIRED for every entry (risk management).

# OPEN POSITIONS (live)
{_fmt_positions(ctx['positions'])}

# LIVE TRADEABLE UNIVERSE — top {len(ctx['universe'])} by 24h volume (pick from ANY of these)
{_fmt_universe(ctx['universe'], len(ctx['universe']))}

# DEEP TECHNICALS (full indicators for {deep_budget} coins you/your memo flagged + top movers)
{_fmt_snapshot(ctx['snapshot'])}

# YOUR TASK
Decide what to do RIGHT NOW. If you want technicals on coins not shown above, list them in "watchlist" and you'll get them next cycle.

Return ONLY valid JSON (no markdown, no prose outside the JSON) of this exact shape:
{{
  "strategy_memo": "Your evolving thesis & plan in your own words. What's your edge right now? What are you watching? What did you learn from recent trades? This persists to next cycle.",
  "watchlist": ["SYMBOLS","you","want","deep","data","on","next","cycle"],
  "actions": [
    {{"action": "enter", "symbol": "BTC", "side": "LONG", "size_usd": 2500, "stop": 95000.0, "target": 112000.0, "reason": "why"}},
    {{"action": "close", "symbol": "ETH", "reason": "why"}},
    {{"action": "hold", "symbol": "SOL", "reason": "why"}}
  ],
  "commentary": "one-line summary of your stance this cycle"
}}
Rules for the JSON: symbols are bare bases (e.g. "BTC" not "BTC/USDT"). For LONG, stop < entry < target; for SHORT, target < entry < stop. Only include "enter" for coins you do NOT already hold. Return "actions": [] if you choose to do nothing this cycle."""


# ---------------------------------------------------------------------------
# Brain
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except Exception:
            return None
    return None


class TraderBrain:
    def __init__(self, model: Optional[str] = None, pool: Optional[GeminiPool] = None) -> None:
        settings = get_settings()
        self._model = model or settings.gemini_model
        self._pool = pool or GeminiPool(self._model)

    @property
    def ai_available(self) -> bool:
        return self._pool.available

    @property
    def num_keys(self) -> int:
        return self._pool.num_keys

    async def decide(self, ctx: dict) -> Decision:
        settings = get_settings()
        if not self._pool.available:
            return Decision(
                actions=[],
                strategy_memo=ctx.get("strategy_memo", ""),
                commentary="No Gemini key configured — brain idle (no autonomous decisions made).",
                ai_used=False,
            )

        prompt = build_prompt(ctx, settings)
        raw = await self._pool.generate(prompt)
        if not raw:
            return Decision(
                actions=[],
                strategy_memo=ctx.get("strategy_memo", ""),
                commentary="Gemini call failed this cycle — held all positions.",
                ai_used=False,
            )

        parsed = _extract_json(raw)
        if not parsed:
            logger.warning("Brain returned unparseable JSON: %s", raw[:200])
            return Decision(
                actions=[],
                strategy_memo=ctx.get("strategy_memo", ""),
                commentary="Brain output unparseable — held all positions.",
                ai_used=True,
                raw=raw,
            )

        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []
        return Decision(
            actions=actions,
            strategy_memo=str(parsed.get("strategy_memo", ctx.get("strategy_memo", ""))),
            watchlist=[str(s).upper() for s in (parsed.get("watchlist") or []) if s],
            commentary=str(parsed.get("commentary", "")),
            ai_used=True,
            raw=raw,
        )
