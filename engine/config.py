"""Central runtime configuration for the autonomous trader.

Everything here is either a *scale knob* or a *safety guardrail* — never a
trading strategy. The bot's strategy is decided entirely by the AI brain at
runtime; this module only controls how big it can scale and how it is kept safe.

All values come from environment variables (``.env``) with sensible defaults so
the system runs out of the box. Nothing here tells the bot *what* to trade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict


def _env_str(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (ValueError, AttributeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except (ValueError, AttributeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def gemini_keys() -> list[str]:
    """Collect every Gemini API key available for round-robin rotation.

    Supports ``GEMINI_API_KEY``, ``GEMINI_API_KEY_2`` … ``GEMINI_API_KEY_N`` and
    a comma-separated ``GEMINI_API_KEYS``. Rotation lets the bot fan out across
    many coins and agents without tripping per-key rate limits.
    """
    keys: list[str] = []
    primary = os.getenv("GEMINI_API_KEY", "").strip()
    if primary:
        keys.append(primary)
    # numbered keys GEMINI_API_KEY_2, _3, ...
    i = 2
    while True:
        k = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
        if not k:
            break
        keys.append(k)
        i += 1
    # comma-separated bundle
    bundle = os.getenv("GEMINI_API_KEYS", "")
    for k in bundle.split(","):
        k = k.strip()
        if k:
            keys.append(k)
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


@dataclass
class Settings:
    # ----- AI brain -----
    gemini_model: str = field(default_factory=lambda: _env_str("GEMINI_MODEL", "gemini-2.5-flash"))

    # ----- Exchange / data -----
    # "auto" tries a reachable exchange with a large universe automatically.
    exchange_id: str = field(default_factory=lambda: _env_str("EXCHANGE_ID", "auto"))
    quote_asset: str = field(default_factory=lambda: _env_str("QUOTE_ASSET", "USDT"))

    # ----- Scale knobs (bigger than the old fixed 6-coin/5-strategy setup) -----
    num_agents: int = field(default_factory=lambda: _env_int("NUM_AGENTS", 6))
    universe_size: int = field(default_factory=lambda: _env_int("UNIVERSE_SIZE", 60))
    deep_analysis_budget: int = field(default_factory=lambda: _env_int("DEEP_ANALYSIS_BUDGET", 25))
    cycle_minutes: int = field(default_factory=lambda: _env_int("CYCLE_MINUTES", 15))
    starting_balance: float = field(default_factory=lambda: _env_float("STARTING_BALANCE", 10_000.0))

    # ----- Safety guardrails (the AI operates *inside* these) -----
    trading_mode: str = field(default_factory=lambda: _env_str("TRADING_MODE", "paper").lower())
    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 50))
    max_daily_drawdown_pct: float = field(default_factory=lambda: _env_float("MAX_DAILY_DRAWDOWN_PCT", 25.0))
    max_alloc_pct_per_trade: float = field(default_factory=lambda: _env_float("MAX_ALLOC_PCT_PER_TRADE", 40.0))
    kill_switch: bool = field(default_factory=lambda: _env_bool("KILL_SWITCH", False))

    # ----- Notifications -----
    notify_telegram: bool = field(default_factory=lambda: _env_bool("NOTIFY_TELEGRAM", False))

    @property
    def is_paper(self) -> bool:
        return self.trading_mode != "live"

    def to_public_dict(self) -> dict:
        """Settings safe to expose to the dashboard (no secrets)."""
        d = asdict(self)
        d["gemini_keys_available"] = len(gemini_keys())
        return d


# Singleton-style accessor — re-read each call so tests/env changes take effect.
def get_settings() -> Settings:
    return Settings()
