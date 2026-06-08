"""Autonomous arena orchestrator — runs N fully-autonomous agents 24/7.

Each cycle it:
1. discovers the live universe (top coins by 24h volume — dynamic, not hardcoded),
2. builds a shared deep-technical snapshot (top movers ∪ what agents asked to watch),
3. lets every agent make its own AI decision on that data, and
4. runs a fast real-time exit monitor between cycles for stop/target hits.

There are no trading rules here — only orchestration, data plumbing and safety.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from engine.agent import AutonomousAgent, build_agents
from engine.brain import TraderBrain
from engine.config import Settings, get_settings
from oscar.market import MarketDataService

logger = logging.getLogger(__name__)

_EXIT_CHECK_SECONDS = 15


class AutonomousArena:
    def __init__(
        self,
        market: MarketDataService,
        settings: Optional[Settings] = None,
        brain: Optional[TraderBrain] = None,
    ) -> None:
        self._market = market
        self._settings = settings or get_settings()
        self._brain = brain or TraderBrain()
        self.agents: list[AutonomousAgent] = build_agents(market, self._brain, self._settings)

        self.running = False
        self.runs = 0
        self.last_run: Optional[str] = None
        self.next_run: Optional[str] = None
        self.last_universe: list[dict] = []
        self.last_universe_size = 0
        self._task: Optional[asyncio.Task] = None
        self._exit_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        self._exit_task = asyncio.create_task(self._exit_loop())
        logger.info(
            "Autonomous arena started — %d agents, %d Gemini key(s), cycle %d min, universe %d",
            len(self.agents), self._brain.num_keys, self._settings.cycle_minutes,
            self._settings.universe_size,
        )

    async def stop(self) -> None:
        self.running = False
        for t in (self._task, self._exit_task):
            if t:
                t.cancel()

    # ------------------------------------------------------------------
    # Data assembly (shared across agents each cycle)
    # ------------------------------------------------------------------

    async def _build_snapshot(self, universe: list[dict]) -> list[dict]:
        budget = max(1, self._settings.deep_analysis_budget)
        # base -> ccxt symbol, seeded from the discovered universe
        sym_map = {u["base"]: u["symbol"] for u in universe}

        # Start with the top movers by volume.
        bases: list[str] = [u["base"] for u in universe[:budget]]

        # Add whatever the agents asked to watch (so the AI steers its own focus).
        for ag in self.agents:
            for b in ag.watchlist:
                if b not in bases:
                    bases.append(b)
            for sym in ag._paper.positions:  # always analyse open positions
                if sym not in bases:
                    bases.append(sym)

        ccxt_syms = [sym_map.get(b, f"{b}/{self._settings.quote_asset}") for b in bases]
        summaries = await self._market.scan_symbols(ccxt_syms)
        return summaries

    async def _cycle(self) -> None:
        self.runs += 1
        self.last_run = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Cycle #%d starting — discovering universe (top %d by 24h volume)...",
            self.runs, self._settings.universe_size,
        )
        try:
            universe = await self._market.discover_universe(self._settings.universe_size)
        except Exception as exc:
            logger.warning("Universe discovery failed: %s — skipping cycle", repr(exc)[:120])
            return
        self.last_universe = universe
        self.last_universe_size = len(universe)

        snapshot = await self._build_snapshot(universe)
        logger.info(
            "Cycle #%d — universe=%d deep=%d; dispatching to %d agents",
            self.runs, len(universe), len(snapshot), len(self.agents),
        )

        for ag in self.agents:
            try:
                await ag.run_cycle(universe, snapshot)
            except Exception as exc:
                logger.exception("Agent %s cycle error: %s", ag.name, exc)

    async def _run_loop(self) -> None:
        while self.running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.exception("Cycle error: %s", exc)
            nxt = datetime.now(timezone.utc) + timedelta(minutes=self._settings.cycle_minutes)
            self.next_run = nxt.isoformat()
            await asyncio.sleep(self._settings.cycle_minutes * 60)

    async def _exit_loop(self) -> None:
        while self.running:
            for ag in self.agents:
                try:
                    await ag.check_exits()
                except Exception as exc:
                    logger.debug("Exit check error for %s: %s", ag.name, exc)
            await asyncio.sleep(_EXIT_CHECK_SECONDS)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def agent_by_id(self, sid: str) -> Optional[AutonomousAgent]:
        for ag in self.agents:
            if ag.id == sid:
                return ag
        return None

    async def run_one_cycle_now(self) -> None:
        """Run a single full cycle immediately (used for tests / manual triggers)."""
        await self._cycle()
