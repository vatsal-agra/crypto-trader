"""Gemini AI integration — Oscar's brain (google-genai SDK)."""

import json
import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

OSCAR_SYSTEM_PROMPT = """\
You are Oscar, an expert crypto swing-trading analyst. You're direct, data-driven, and concise — like a sharp trading colleague who never wastes words.

You strictly follow these trading rules:
{rules_json}

Analysis principles:
- Ground every call in the specific data provided (price, RSI, EMA levels, structure).
- Only recommend trades with at minimum a 2:1 R/R ratio.
- Flag CPI, FOMC, or weekend sessions as risky periods.
- Be honest — "no clean setup right now" is a valid and important answer.
- For quick questions give brief answers. For explicit analysis requests go deeper.

Formatting:
- Use Telegram HTML: <b>bold</b>, <i>italic</i>, <code>inline</code>.
- Use emoji bias indicators: 🟢 BULLISH | 🔴 BEARISH | 🟡 NEUTRAL.
- Keep responses under ~400 words unless a full analysis is requested.
"""


class Analyzer:
    def __init__(self, rules: dict) -> None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set in your .env file.")

        self._client = genai.Client(api_key=api_key)
        self._model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        system = OSCAR_SYSTEM_PROMPT.format(rules_json=json.dumps(rules, indent=2))
        self._config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=1200,
            temperature=0.4,
        )
        self._histories: dict[int, list[dict]] = {}

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def clear_history(self, user_id: int) -> None:
        self._histories[user_id] = []

    def _get_history(self, user_id: int) -> list[dict]:
        return self._histories.setdefault(user_id, [])

    def _push(self, user_id: int, role: str, content: str) -> None:
        history = self._get_history(user_id)
        history.append({"role": role, "content": content})
        if len(history) > 20:
            self._histories[user_id] = history[-20:]

    # ------------------------------------------------------------------
    # SDK format helpers
    # google-genai uses types.Content(role, parts) objects.
    # Role must be "user" or "model" ("assistant" is not valid).
    # ------------------------------------------------------------------

    @staticmethod
    def _to_contents(messages: list[dict]) -> list[types.Content]:
        """Convert internal history to google-genai Content objects."""
        result = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            result.append(
                types.Content(role=role, parts=[types.Part.from_text(msg["content"])])
            )
        return result

    @staticmethod
    def _format_market_block(data_list: list[dict]) -> str:
        if not data_list:
            return "No market data available."

        lines: list[str] = ["<b>Current Market Snapshot:</b>"]
        for d in data_list:
            if not isinstance(d, dict) or "error" in d:
                continue
            sym = d.get("ccxt_symbol", d.get("symbol", "?"))
            price = d.get("price", 0)
            bias = d.get("bias", "NEUTRAL")
            daily = d.get("daily", {})
            rsi = daily.get("rsi")
            ema50 = daily.get("ema50")
            above_ema50 = daily.get("above_ema50")
            macd_hist = daily.get("macd_hist")
            vol_above = daily.get("volume_above_avg")
            structure = d.get("h4_structure", "Unknown")

            price_str = f"${price:,.4f}" if price < 1 else f"${price:,.2f}"

            lines.append(f"\n<b>{sym}</b>")
            lines.append(f"Price: {price_str} | Bias: {bias}")
            if rsi is not None:
                lines.append(f"RSI(14): {rsi:.1f}")
            if ema50 is not None:
                direction = "above" if above_ema50 else "below"
                lines.append(f"50 EMA: ${ema50:,.2f} — price {direction}")
            if macd_hist is not None:
                lines.append(f"MACD hist: {macd_hist:+.4f}")
            lines.append(f"4H structure: {structure}")
            if vol_above is not None:
                lines.append(
                    f"Volume: {'above' if vol_above else 'below'} 20-bar avg"
                )

        return "\n".join(lines)

    async def _ask_gemini(self, messages: list[dict]) -> str:
        """Send the full conversation to Gemini and return the response text."""
        try:
            contents = self._to_contents(messages)
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=contents,
                config=self._config,
            )
            return response.text
        except Exception as exc:
            logger.error("Gemini API error: %s", exc)
            return "⚠️ Analysis unavailable right now — try again in a moment."

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def analyze(
        self, user_id: int, message: str, market_data: list[dict] | None = None
    ) -> str:
        """Respond to a free-form user message, optionally with market context."""
        content = message
        if market_data:
            market_block = self._format_market_block(market_data)
            content = f"{market_block}\n\nUser: {message}"

        self._push(user_id, "user", content)
        reply = await self._ask_gemini(self._get_history(user_id))
        self._push(user_id, "assistant", reply)
        return reply

    async def analyze_symbol(self, coin: str, market_data: dict) -> str:
        """Deep single-symbol analysis (no conversation history needed)."""
        data_block = self._format_market_block([market_data])
        prompt = (
            f"{data_block}\n\n"
            f"Give me a full technical breakdown of <b>{coin}</b>:\n"
            "1. Overall bias and the reasons behind it\n"
            "2. Key support and resistance levels (specific prices)\n"
            "3. Any clean setups forming right now\n"
            "4. What would invalidate the bull/bear case\n\n"
            "Be specific with price levels. Keep it punchy."
        )
        return await self._ask_gemini([{"role": "user", "content": prompt}])

    async def scan_summary(self, results: list[dict]) -> str:
        """Summarise a full watchlist scan into a Telegram-ready message."""
        data_block = self._format_market_block(results)
        prompt = (
            f"{data_block}\n\n"
            "Give me a watchlist summary. For each coin one line: bias emoji + ticker + "
            "one-sentence setup or 'no setup'. "
            "Then at the bottom: which 1–2 coins have the cleanest setups right now and why."
        )
        return await self._ask_gemini([{"role": "user", "content": prompt}])
