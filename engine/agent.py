"""An autonomous trading agent.

agent = AI brain + its own paper account + its own evolving strategy memo +
the shared safety guard. Each agent is fully independent and free to do whatever
it wants; together they form a self-competing "arena". Nothing about *how* they
trade is hardcoded — only the safety rails and the bookkeeping live in code.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.brain import Decision, TraderBrain
from engine.config import Settings
from engine.safety import SafetyGuard
from oscar.market import MarketDataService
from oscar.paper_trader import PaperTrader

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Initial persona *seeds* only — agents rewrite their own strategy every cycle.
DEFAULT_PERSONAS: list[tuple[str, str]] = [
    ("Momentum Hawk", "hunts strong trending coins and rides momentum"),
    ("Mean Reverter", "fades overextended moves, patient and contrarian"),
    ("Macro Trender", "follows the dominant multi-day market direction"),
    ("Volatility Breakout", "strikes on volatility expansion and breakouts"),
    ("Deep Value Contrarian", "buys fear, takes profit into euphoria"),
    ("Balanced All-Rounder", "adapts style to whatever the market is paying"),
    ("Alt Rotation Specialist", "rotates capital into the strongest alts"),
    ("Risk-Off Defender", "trades small, protects capital, strikes selectively"),
]


class AutonomousAgent:
    def __init__(
        self,
        agent_id: str,
        name: str,
        persona: str,
        market: MarketDataService,
        brain: TraderBrain,
        settings: Settings,
    ) -> None:
        self.id = agent_id
        self.name = name
        self.persona = persona
        self._market = market
        self._brain = brain
        self._settings = settings

        self._meta_file = _DATA_DIR / f"agent_{agent_id}.json"
        self._paper = PaperTrader(
            state_file=_DATA_DIR / f"agent_{agent_id}_account.json",
            starting_balance=settings.starting_balance,
        )
        self.safety = SafetyGuard(settings, name=name)

        self.strategy_memo: str = ""
        self.watchlist: list[str] = []
        self.log: list[dict] = []
        self.runs: int = 0
        self.last_run: Optional[str] = None
        self.last_commentary: str = ""
        self._load_meta()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_meta(self) -> None:
        if not self._meta_file.exists():
            return
        try:
            d = json.loads(self._meta_file.read_text())
            self.strategy_memo = d.get("strategy_memo", "")
            self.watchlist = d.get("watchlist", [])
            self.log = d.get("log", [])
            self.runs = d.get("runs", 0)
            self.last_commentary = d.get("last_commentary", "")
        except Exception as exc:
            logger.warning("[%s] could not load meta: %s", self.name, exc)

    def _save_meta(self) -> None:
        self._meta_file.parent.mkdir(parents=True, exist_ok=True)
        self._meta_file.write_text(json.dumps({
            "id": self.id,
            "name": self.name,
            "persona": self.persona,
            "strategy_memo": self.strategy_memo,
            "watchlist": self.watchlist,
            "log": self.log[-200:],
            "runs": self.runs,
            "last_commentary": self.last_commentary,
        }, indent=2))

    def _logmsg(self, msg: str, level: str = "INFO") -> None:
        self.log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "msg": msg,
            "level": level,
        })
        if len(self.log) > 300:
            self.log = self.log[-300:]
        logger.info("[%s] %s", self.name, msg)

    # ------------------------------------------------------------------
    # Account snapshots
    # ------------------------------------------------------------------

    async def _prices_for_positions(self) -> dict[str, float]:
        syms = list(self._paper.positions.keys())
        if not syms:
            return {}
        return await self._market.get_prices(syms)

    def _positions_ctx(self, prices: dict[str, float]) -> list[dict]:
        out = []
        for sym, pos in self._paper.positions.items():
            cur = prices.get(sym, pos.entry)
            out.append({
                "symbol": sym,
                "side": pos.side,
                "entry": pos.entry,
                "current_price": cur,
                "size": pos.size,
                "size_usd": pos.size * pos.entry,
                "stop": pos.stop,
                "target": pos.target,
                "unrealized_pnl": pos.unrealized_pnl(cur),
                "pnl_pct": pos.pnl_pct(cur),
            })
        return out

    # ------------------------------------------------------------------
    # Real-time exit monitor (stop / target)
    # ------------------------------------------------------------------

    async def check_exits(self) -> None:
        for sym, pos in list(self._paper.positions.items()):
            price = await self._market.get_price(sym)
            if not price:
                continue
            reason = None
            if pos.side == "LONG":
                if price <= pos.stop:
                    reason = "stop hit"
                elif price >= pos.target:
                    reason = "target hit"
            else:
                if price >= pos.stop:
                    reason = "stop hit"
                elif price <= pos.target:
                    reason = "target hit"
            if reason:
                res = self._paper.close(sym, price)
                pnl = res["pnl"]
                self._logmsg(
                    f"CLOSED {sym} {pos.side} @ ${price:,.6g} — {reason} | PnL {'+' if pnl>=0 else ''}${pnl:,.2f}",
                    "TRADE",
                )
                self._save_meta()

    # ------------------------------------------------------------------
    # Main decision cycle
    # ------------------------------------------------------------------

    async def run_cycle(self, universe: list[dict], snapshot: list[dict]) -> Decision:
        self.runs += 1
        self.last_run = datetime.now(timezone.utc).isoformat()

        prices = await self._prices_for_positions()
        equity = self._paper.equity(prices)
        self.safety.roll_day(equity)

        positions_ctx = self._positions_ctx(prices)
        initial = self._paper.initial_balance
        ctx = {
            "agent_name": self.name,
            "persona": self.persona,
            "strategy_memo": self.strategy_memo,
            "equity": equity,
            "initial_balance": initial,
            "total_pnl_pct": (equity - initial) / initial * 100 if initial else 0.0,
            "free_balance": self._paper.balance,
            "open_count": len(self._paper.positions),
            "positions": positions_ctx,
            "universe": universe,
            "snapshot": snapshot,
            "recent_trades": self._paper.closed_trades,
            "stats": self._paper.stats(),
            "safety": self.safety.status(equity),
        }

        decision = await self._brain.decide(ctx)

        # Persist the agent's self-authored evolving thesis.
        if decision.strategy_memo:
            self.strategy_memo = decision.strategy_memo
        if decision.watchlist:
            self.watchlist = decision.watchlist[:40]
        self.last_commentary = decision.commentary

        if not decision.ai_used:
            self._logmsg(f"Cycle #{self.runs}: {decision.commentary}", "INFO")
            self._save_meta()
            return decision

        self._logmsg(
            f"Cycle #{self.runs} [{self._brain.num_keys}-key brain]: {decision.commentary or 'decided'}",
            "INFO",
        )
        await self._apply(decision, equity)
        self._save_meta()
        return decision

    async def _apply(self, decision: Decision, equity: float) -> None:
        # Closes first to free capital.
        for act in decision.actions:
            if not isinstance(act, dict) or act.get("action") != "close":
                continue
            sym = str(act.get("symbol", "")).upper()
            if sym not in self._paper.positions:
                continue
            price = await self._market.get_price(sym)
            if not price:
                continue
            res = self._paper.close(sym, price)
            pnl = res["pnl"]
            self._logmsg(
                f"CLOSED {sym} @ ${price:,.6g} | PnL {'+' if pnl>=0 else ''}${pnl:,.2f} — {act.get('reason','')}",
                "TRADE",
            )

        # Then entries (safety-checked + size-clamped).
        for act in decision.actions:
            if not isinstance(act, dict) or act.get("action") != "enter":
                continue
            sym = str(act.get("symbol", "")).upper()
            if not sym or sym in self._paper.positions:
                continue

            ok, why = self.safety.can_open(equity, len(self._paper.positions))
            if not ok:
                self._logmsg(f"Entry {sym} blocked by safety: {why}", "WARN")
                continue

            price = await self._market.get_price(sym)
            if not price:
                self._logmsg(f"Entry {sym} skipped — no price (not on exchange?)", "WARN")
                continue

            side = str(act.get("side", "LONG")).upper()
            try:
                size_usd = float(act.get("size_usd", 0) or 0)
                stop = float(act.get("stop", 0) or 0)
                target = float(act.get("target", 0) or 0)
            except (TypeError, ValueError):
                self._logmsg(f"Entry {sym} skipped — malformed numbers", "WARN")
                continue

            if size_usd <= 0 or stop <= 0 or target <= 0:
                self._logmsg(f"Entry {sym} skipped — missing size/stop/target", "WARN")
                continue
            if side == "LONG" and not (stop < price < target):
                self._logmsg(f"Entry {sym} LONG skipped — need stop<price<target", "WARN")
                continue
            if side == "SHORT" and not (target < price < stop):
                self._logmsg(f"Entry {sym} SHORT skipped — need target<price<stop", "WARN")
                continue

            size_usd, note = self.safety.clamp_size(size_usd, equity, self._paper.balance)
            if size_usd <= 0:
                self._logmsg(f"Entry {sym} skipped — no free balance", "WARN")
                continue

            try:
                self._paper.enter_free(sym, side, price, stop, target, size_usd)
            except ValueError as exc:
                self._logmsg(f"Entry {sym} rejected: {exc}", "WARN")
                continue

            extra = f" ({note})" if note else ""
            self._logmsg(
                f"ENTERED {sym} {side} @ ${price:,.6g} size=${size_usd:,.0f}{extra} "
                f"stop=${stop:,.6g} target=${target:,.6g} — {act.get('reason','')}",
                "TRADE",
            )

    # ------------------------------------------------------------------
    # Dashboard views
    # ------------------------------------------------------------------

    async def overview(self) -> dict:
        prices = await self._prices_for_positions()
        equity = self._paper.equity(prices)
        initial = self._paper.initial_balance
        return {
            "id": self.id,
            "name": self.name,
            "persona": self.persona,
            "strategy_memo": self.strategy_memo,
            "watchlist": self.watchlist,
            "balance": self._paper.balance,
            "equity": equity,
            "profit_purse": self._paper.profit_purse,
            "pnl": equity - initial,
            "pnl_pct": (equity - initial) / initial * 100 if initial else 0.0,
            "open_positions": len(self._paper.positions),
            "stats": self._paper.stats(),
            "runs": self.runs,
            "last_run": self.last_run,
            "last_commentary": self.last_commentary,
            "safety": self.safety.status(equity),
            "log": self.log[-40:],
        }

    async def positions_view(self) -> list[dict]:
        prices = await self._prices_for_positions()
        return self._positions_ctx(prices)

    def trades_view(self) -> list[dict]:
        return self._paper.closed_trades


def build_agents(
    market: MarketDataService, brain: TraderBrain, settings: Settings
) -> list[AutonomousAgent]:
    agents: list[AutonomousAgent] = []
    n = max(1, settings.num_agents)
    for i in range(n):
        name, persona = DEFAULT_PERSONAS[i % len(DEFAULT_PERSONAS)]
        if i >= len(DEFAULT_PERSONAS):
            name = f"{name} {i // len(DEFAULT_PERSONAS) + 1}"
        agents.append(
            AutonomousAgent(
                agent_id=f"a{i+1}",
                name=name,
                persona=persona,
                market=market,
                brain=brain,
                settings=settings,
            )
        )
    return agents
