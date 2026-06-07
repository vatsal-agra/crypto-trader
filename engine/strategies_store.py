"""Strategy arena — Oscar generates 5 diverse trading strategies via Gemini."""

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

STRATEGIES_FILE = Path(__file__).parent.parent / "data" / "strategies.json"
N_STRATEGIES = 5


@dataclass
class StrategyConfig:
    id: str
    name: str
    description: str
    style: str  # trend_follow | reversal | momentum | aggressive | breakout

    long_rsi_min: float
    long_rsi_max: float
    short_rsi_min: float
    short_rsi_max: float
    require_ema_filter: bool
    require_structure: bool

    risk_pct: float
    rr_ratio: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyConfig":
        fields = {
            "id", "name", "description", "style",
            "long_rsi_min", "long_rsi_max", "short_rsi_min", "short_rsi_max",
            "require_ema_filter", "require_structure", "risk_pct", "rr_ratio",
        }
        return cls(**{k: v for k, v in d.items() if k in fields})


FALLBACK_STRATEGIES: list[StrategyConfig] = [
    StrategyConfig(
        id="s1", name="Trend Rider",
        description="All three filters must align: EMA side, RSI 45–70, and bullish/bearish 4H structure. Low frequency, high conviction.",
        style="trend_follow",
        long_rsi_min=45, long_rsi_max=70, short_rsi_min=30, short_rsi_max=55,
        require_ema_filter=True, require_structure=True,
        risk_pct=0.01, rr_ratio=3.0,
    ),
    StrategyConfig(
        id="s2", name="Reversal Hunter",
        description="Fades RSI extremes — buys oversold (<35) and shorts overbought (>65). Counter-trend, no EMA or structure filter.",
        style="reversal",
        long_rsi_min=20, long_rsi_max=35, short_rsi_min=65, short_rsi_max=85,
        require_ema_filter=False, require_structure=False,
        risk_pct=0.015, rr_ratio=2.0,
    ),
    StrategyConfig(
        id="s3", name="Momentum Surfer",
        description="Rides mid-RSI momentum continuation (50–75 long, 25–50 short) with EMA filter but no structure requirement.",
        style="momentum",
        long_rsi_min=50, long_rsi_max=75, short_rsi_min=25, short_rsi_max=50,
        require_ema_filter=True, require_structure=False,
        risk_pct=0.008, rr_ratio=2.5,
    ),
    StrategyConfig(
        id="s4", name="Swing Sniper",
        description="High conviction with all filters on, doubles the risk to 2% but accepts quick 1.5:1 exits. Fewer trades, bigger bets.",
        style="aggressive",
        long_rsi_min=42, long_rsi_max=68, short_rsi_min=32, short_rsi_max=58,
        require_ema_filter=True, require_structure=True,
        risk_pct=0.02, rr_ratio=1.5,
    ),
    StrategyConfig(
        id="s5", name="Structure Pure",
        description="EMA side + 4H structure only — ignores RSI range entirely. Widest entry window, catches more moves at the cost of some noise.",
        style="breakout",
        long_rsi_min=25, long_rsi_max=80, short_rsi_min=20, short_rsi_max=75,
        require_ema_filter=True, require_structure=True,
        risk_pct=0.012, rr_ratio=2.0,
    ),
]


async def generate_strategies() -> list[StrategyConfig]:
    """Ask Oscar (Gemini) to design 5 diverse trading strategies from scratch."""
    try:
        from google import genai as google_genai
    except ImportError:
        logger.warning("google-genai not installed — using fallback strategies")
        return FALLBACK_STRATEGIES

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("No GEMINI_API_KEY — using fallback strategies")
        return FALLBACK_STRATEGIES

    model_id = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    prompt = """You are Oscar — an autonomous AI crypto trading system architect.

Design exactly 5 GENUINELY DIVERSE trading strategies for a paper trading arena. Each must have a distinct edge, thrive in different market conditions, and have a different risk personality.

Signal evaluation parameters available:
- long_rsi_min/long_rsi_max: RSI must be in this window to enter LONG (0–100)
- short_rsi_min/short_rsi_max: RSI must be in this window to enter SHORT (0–100)
- require_ema_filter: true = LONG needs price above EMA50, SHORT needs below. false = no check
- require_structure: true = LONG needs 4H bullish structure (HH/HL), SHORT needs bearish (LH/LL). false = no check
- risk_pct: fraction of free balance to risk per trade (0.005 to 0.03)
- rr_ratio: reward:risk ratio for setting the profit target (1.0 to 5.0)

Return ONLY a JSON array of exactly 5 objects, no markdown, no explanation:
[
  {
    "id": "s1",
    "name": "Short catchy name",
    "description": "One precise sentence about the edge and when it works best.",
    "style": "trend_follow",
    "long_rsi_min": 45, "long_rsi_max": 70,
    "short_rsi_min": 30, "short_rsi_max": 55,
    "require_ema_filter": true,
    "require_structure": true,
    "risk_pct": 0.01,
    "rr_ratio": 3.0
  }
]

Use ids s1 through s5. Make strategies genuinely different — mix trend-following with reversal, strict filters with permissive, conservative sizing with aggressive."""

    def _call() -> str:
        client = google_genai.Client(api_key=api_key)
        resp = client.models.generate_content(model=model_id, contents=prompt)
        return resp.text.strip()

    try:
        raw = await asyncio.to_thread(_call)
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        data = json.loads(clean)
        strategies = [StrategyConfig.from_dict(d) for d in data]
        if len(strategies) == N_STRATEGIES:
            logger.info("Oscar designed %d strategies via Gemini", len(strategies))
            return strategies
        logger.warning(
            "Gemini returned %d strategies, expected %d — using fallbacks",
            len(strategies), N_STRATEGIES,
        )
    except Exception as exc:
        logger.warning("Strategy generation failed: %s — using fallbacks", exc)

    return FALLBACK_STRATEGIES


async def get_or_create_strategies() -> list[StrategyConfig]:
    """Load from disk if present, otherwise generate via Gemini and persist."""
    if STRATEGIES_FILE.exists():
        try:
            data = json.loads(STRATEGIES_FILE.read_text())
            strategies = [StrategyConfig.from_dict(d) for d in data]
            if len(strategies) == N_STRATEGIES:
                logger.info("Loaded %d arena strategies from disk", len(strategies))
                return strategies
        except Exception as exc:
            logger.warning("Failed to load strategies.json: %s", exc)

    logger.info("Generating arena strategies via Oscar (Gemini)…")
    strategies = await generate_strategies()
    STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    STRATEGIES_FILE.write_text(json.dumps([s.to_dict() for s in strategies], indent=2))
    logger.info("Saved %d strategies to %s", len(strategies), STRATEGIES_FILE)
    return strategies


def reset_strategies() -> None:
    """Delete saved strategies so they are regenerated on next startup."""
    if STRATEGIES_FILE.exists():
        STRATEGIES_FILE.unlink()
        logger.info("Deleted strategies.json — will regenerate on next start")
