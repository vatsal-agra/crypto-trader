"""Safety layer — the only code-enforced guardrails around the AI.

The AI brain decides *what* to trade, *how much*, and *which strategy*. This
module never makes a trading decision; it only refuses actions that would breach
hard safety limits the operator configured. Think of it as the seatbelt, not the
driver.

Guards:
- **Mode**: paper by default. Live execution is not implemented and is refused.
- **Kill switch**: env ``KILL_SWITCH=1`` or a ``STOP`` file in the repo root.
- **Daily drawdown breaker**: if equity falls more than ``MAX_DAILY_DRAWDOWN_PCT``
  below the day's opening equity, new entries are halted until the next UTC day.
- **Position cap**: never exceed ``MAX_OPEN_POSITIONS`` simultaneous positions.
- **Per-trade allocation cap**: a single trade can't exceed
  ``MAX_ALLOC_PCT_PER_TRADE`` of current equity (sizes above are clamped down).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.config import Settings

logger = logging.getLogger(__name__)

_STOP_FILE = Path(__file__).resolve().parent.parent / "STOP"


class SafetyGuard:
    def __init__(self, settings: Settings, name: str = "guard") -> None:
        self._s = settings
        self.name = name
        self._day: Optional[str] = None
        self._day_open_equity: float = 0.0
        self._drawdown_halt: bool = False

    # ------------------------------------------------------------------
    # Global state
    # ------------------------------------------------------------------

    def kill_switch_active(self) -> bool:
        return self._s.kill_switch or _STOP_FILE.exists()

    @property
    def is_paper(self) -> bool:
        return self._s.is_paper

    def roll_day(self, equity: float) -> None:
        """Call once per cycle. Resets the daily drawdown breaker at UTC midnight."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._day != today:
            self._day = today
            self._day_open_equity = equity
            self._drawdown_halt = False
            logger.info("[%s] New trading day %s — opening equity $%.2f", self.name, today, equity)

    def daily_drawdown_pct(self, equity: float) -> float:
        if self._day_open_equity <= 0:
            return 0.0
        return (self._day_open_equity - equity) / self._day_open_equity * 100.0

    def check_drawdown(self, equity: float) -> bool:
        """Returns True if the daily drawdown breaker is (now) tripped."""
        dd = self.daily_drawdown_pct(equity)
        if dd >= self._s.max_daily_drawdown_pct:
            if not self._drawdown_halt:
                logger.warning(
                    "[%s] DAILY DRAWDOWN BREAKER TRIPPED — down %.1f%% (limit %.1f%%). "
                    "Halting new entries until next UTC day.",
                    self.name, dd, self._s.max_daily_drawdown_pct,
                )
            self._drawdown_halt = True
        return self._drawdown_halt

    # ------------------------------------------------------------------
    # Per-action checks
    # ------------------------------------------------------------------

    def can_open(self, equity: float, open_count: int) -> tuple[bool, str]:
        if self.kill_switch_active():
            return False, "kill switch active"
        if not self.is_paper:
            return False, "live mode not implemented — refusing real orders"
        if self.check_drawdown(equity):
            return False, f"daily drawdown breaker tripped ({self.daily_drawdown_pct(equity):.1f}%)"
        if open_count >= self._s.max_open_positions:
            return False, f"max open positions reached ({self._s.max_open_positions})"
        return True, "ok"

    def clamp_size(self, size_usd: float, equity: float, free_balance: float) -> tuple[float, Optional[str]]:
        """Clamp a requested trade size to allocation + balance limits."""
        note: Optional[str] = None
        cap = equity * (self._s.max_alloc_pct_per_trade / 100.0)
        if cap > 0 and size_usd > cap:
            note = f"clamped from ${size_usd:,.0f} to alloc cap ${cap:,.0f} ({self._s.max_alloc_pct_per_trade:.0f}% of equity)"
            size_usd = cap
        if size_usd > free_balance:
            note = f"clamped to free balance ${free_balance:,.0f}"
            size_usd = free_balance
        return max(0.0, size_usd), note

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def status(self, equity: float) -> dict:
        return {
            "mode": "paper" if self.is_paper else "live",
            "kill_switch": self.kill_switch_active(),
            "day": self._day,
            "day_open_equity": self._day_open_equity,
            "daily_drawdown_pct": round(self.daily_drawdown_pct(equity), 2),
            "max_daily_drawdown_pct": self._s.max_daily_drawdown_pct,
            "drawdown_halt": self._drawdown_halt,
            "max_open_positions": self._s.max_open_positions,
            "max_alloc_pct_per_trade": self._s.max_alloc_pct_per_trade,
        }
