"""Autonomous trading engine — scans markets and manages paper positions 24/7."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx

from engine.strategy import ai_decision, calc_stop, get_signal, get_signal_with_config

logger = logging.getLogger(__name__)

NOTIFICATION_INTERVAL_HOURS = 4


class TradingLoop:
    def __init__(
        self,
        market,
        paper_trader,
        rules: dict,
        interval_minutes: int = 30,
        strategy_cfg=None,
        name: str = "main",
        initial_delay_seconds: int = 0,
        notify_telegram: bool = True,
    ) -> None:
        self._market = market
        self._paper = paper_trader
        self._rules = rules
        self.interval_minutes = interval_minutes
        self.strategy_cfg = strategy_cfg
        self.name = name
        self._initial_delay = initial_delay_seconds
        self._notify_telegram = notify_telegram
        self.running = False
        self.last_run: Optional[str] = None
        self.next_run: Optional[str] = None
        self.runs: int = 0
        self.log: list[dict] = []
        self._task: Optional[asyncio.Task] = None
        self._last_notify: Optional[datetime] = None
        self._last_bias: dict[str, str] = {}
        self.unleashed: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        self._exit_task = asyncio.create_task(self._exit_monitor_loop())
        logger.info("Trading engine started — interval %d min", self.interval_minutes)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
        if hasattr(self, "_exit_task") and self._exit_task:
            self._exit_task.cancel()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str, level: str = "INFO") -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "msg": msg,
            "level": level,
        }
        self.log.append(entry)
        if len(self.log) > 500:
            self.log = self.log[-500:]
        logger.info("[Engine] %s", msg)

    async def _notify(self, text: str) -> None:
        if not self._notify_telegram:
            return
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                )
        except Exception as exc:
            logger.warning("Telegram notify failed: %s", exc)

    async def _exit_monitor_loop(self) -> None:
        """Continuously check hard stop-losses and take-profits in real-time (every 15 seconds)."""
        # Add initial delay to match staggered start
        if self._initial_delay > 0:
            await asyncio.sleep(self._initial_delay)
        while self.running:
            try:
                await self._check_exits()
            except Exception as exc:
                logger.debug("Error in exit monitor loop: %s", exc)
            await asyncio.sleep(15)

    async def _run_loop(self) -> None:
        if self._initial_delay > 0:
            await asyncio.sleep(self._initial_delay)
        while self.running:
            await self._tick()
            next_dt = datetime.now(timezone.utc) + timedelta(minutes=self.interval_minutes)
            self.next_run = next_dt.isoformat()
            await asyncio.sleep(self.interval_minutes * 60)

    # ------------------------------------------------------------------
    # Main tick — exits first, then entries
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        self.runs += 1
        self.last_run = datetime.now(timezone.utc).isoformat()
        mode = "UNLEASHED" if self.unleashed else "restricted"
        self._log(
            f"Scan #{self.runs} [{mode}] started — {len(self._paper.positions)} open positions"
        )

        if self.unleashed:
            await self._tick_unleashed()
        else:
            # Let Oscar evaluate active exits and capital transfers first
            try:
                await self._run_oscar_portfolio_management()
            except Exception as exc:
                logger.warning("Error running Oscar Portfolio Management: %s", exc)

            await self._check_exits()
            await self._scan_entries()

        await self._maybe_send_summary()
        self._log(f"Scan #{self.runs} done — {len(self._paper.positions)} open positions")

    async def _run_oscar_portfolio_management(self) -> None:
        if not self.strategy_cfg:
            return  # Only run for active arena strategies

        # 1. Fetch live prices & build active position statistics
        positions_list = []
        for sym, pos in list(self._paper.positions.items()):
            price = await self._market.get_price(f"{sym}/USDT")
            if not price:
                price = pos.entry
            upnl = pos.unrealized_pnl(price)
            pct = pos.pnl_pct(price)
            positions_list.append({
                "symbol": sym,
                "side": pos.side,
                "entry": pos.entry,
                "size": pos.size,
                "stop": pos.stop,
                "target": pos.target,
                "current_price": price,
                "unrealized_pnl": upnl,
                "pnl_pct": pct,
            })

        # 2. Fetch market summaries for the watchlist
        market_summaries = []
        all_symbols = self._rules["watchlist"]["majors"] + self._rules["watchlist"]["alts"]
        for tv_symbol in all_symbols:
            parts = tv_symbol.split(":")
            if len(parts) == 2:
                summary = await self._market.get_market_summary(tv_symbol, self._rules)
                if summary and "error" not in summary:
                    market_summaries.append(summary)

        # 3. Request portfolio decision from Gemini (Oscar)
        prices = {}
        for pos in positions_list:
            prices[pos["symbol"]] = pos["current_price"]
        live_equity = self._paper.equity(prices)

        from engine.strategy import oscar_portfolio_decision
        decision = await oscar_portfolio_decision(
            strategy_name=self.strategy_cfg.name,
            strategy_style=self.strategy_cfg.style,
            strategy_desc=self.strategy_cfg.description,
            balance=self._paper.balance,
            initial_balance=self._paper.initial_balance,
            profit_purse=self._paper.profit_purse,
            positions=positions_list,
            market_summaries=market_summaries,
            equity=live_equity,
        )

        if not decision:
            return

        # 4. Handle early exits
        for exit_item in decision.get("exits", []):
            sym = exit_item.get("symbol", "").upper()
            reason = exit_item.get("reason", "No reason provided")
            if sym in self._paper.positions:
                price = await self._market.get_price(f"{sym}/USDT")
                if price:
                    result = self._paper.close(sym, price)
                    pnl = result["pnl"]
                    sign = "+" if pnl >= 0 else ""
                    self._log(
                        f"OSCAR DYNAMIC EXIT: Closed {sym} {result['side']} early @ ${price:,.4f}. Reason: {reason} | P&L: {sign}${pnl:,.2f}",
                        "TRADE"
                    )
                    emoji = "✅" if pnl >= 0 else "🟥"
                    await self._notify(
                        f"🤖 <b>Oscar Dynamic Exit (Early Sale)</b>\n"
                        f"Strategy: <code>{self.strategy_cfg.name}</code>\n"
                        f"Closed {sym} early @ <code>${price:,.4f}</code>\n"
                        f"Reason: <i>\"{reason}\"</i>\n"
                        f"P&amp;L: <code>{sign}${pnl:,.2f}</code>\n"
                        f"Balance: <code>${self._paper.balance:,.2f}</code>"
                    )

        # 5. Handle transfers (Sweeps/Re-capitalization)
        for xfer in decision.get("transfers", []):
            xtype = xfer.get("type", "").lower()
            try:
                amount = float(xfer.get("amount", 0.0))
            except ValueError:
                continue
            reason = xfer.get("reason", "No reason provided")

            if xtype == "sweep" and amount > 0:
                # Sweep excess profits from trading balance to locked purse
                if amount > self._paper.balance:
                    amount = self._paper.balance
                self._paper.balance -= amount
                self._paper.profit_purse += amount
                self._paper.save()
                self._log(
                    f"OSCAR PORTFOLIO MANAGEMENT: Swept ${amount:,.2f} into Profit Purse. Reason: {reason}",
                    "INFO"
                )
                await self._notify(
                    f"💰 <b>Oscar Profit Purse Sweep</b>\n"
                    f"Strategy: <code>{self.strategy_cfg.name}</code>\n"
                    f"Locked in <code>${amount:,.2f}</code> into Profit Purse!\n"
                    f"Reason: <i>\"{reason}\"</i>\n"
                    f"Trade Balance: <code>${self._paper.balance:,.2f}</code>\n"
                    f"Purse Balance: <code>${self._paper.profit_purse:,.2f}</code>"
                )
            elif xtype == "recapitalize" and amount > 0:
                # Move funds out of locked purse back into active balance
                if amount > self._paper.profit_purse:
                    amount = self._paper.profit_purse
                self._paper.profit_purse -= amount
                self._paper.balance += amount
                self._paper.save()
                self._log(
                    f"OSCAR PORTFOLIO MANAGEMENT: Re-capitalized ${amount:,.2f} from Profit Purse. Reason: {reason}",
                    "INFO"
                )
                await self._notify(
                    f"🔄 <b>Oscar Re-Capitalization</b>\n"
                    f"Strategy: <code>{self.strategy_cfg.name}</code>\n"
                    f"Transferred <code>${amount:,.2f}</code> out of Profit Purse to Trading Balance.\n"
                    f"Reason: <i>\"{reason}\"</i>\n"
                    f"Trade Balance: <code>${self._paper.balance:,.2f}</code>\n"
                    f"Purse Balance: <code>${self._paper.profit_purse:,.2f}</code>"
                )

    async def _check_exits(self) -> None:
        for sym, pos in list(self._paper.positions.items()):
            price = await self._market.get_price(f"{sym}/USDT")
            if not price:
                continue

            close_reason = None
            if pos.side == "LONG":
                if price <= pos.stop:
                    close_reason = "stop hit 🛑"
                elif price >= pos.target:
                    close_reason = "target hit 🎯"
            else:
                if price >= pos.stop:
                    close_reason = "stop hit 🛑"
                elif price <= pos.target:
                    close_reason = "target hit 🎯"

            if close_reason:
                result = self._paper.close(sym, price)
                pnl = result["pnl"]
                sign = "+" if pnl >= 0 else ""
                msg = (
                    f"CLOSED {sym} {pos.side} @ ${price:,.4f} "
                    f"— {close_reason} | P&L: {sign}${pnl:,.2f}"
                )
                self._log(msg, "TRADE")
                emoji = "✅" if pnl >= 0 else "🟥"
                await self._notify(
                    f"{emoji} <b>Paper Trade Closed</b>\n"
                    f"{sym} {pos.side} @ <code>${price:,.4f}</code>\n"
                    f"Reason: {close_reason}\n"
                    f"P&amp;L: <code>{sign}${pnl:,.2f}</code>\n"
                    f"Balance: <code>${self._paper.balance:,.2f}</code>"
                )

    async def _scan_entries(self) -> None:
        all_symbols = (
            self._rules["watchlist"]["majors"] + self._rules["watchlist"]["alts"]
        )
        for tv_symbol in all_symbols:
            parts = tv_symbol.split(":")
            if len(parts) != 2:
                continue
            ccxt_raw = parts[1]  # e.g. BTCUSDT
            base = ccxt_raw.replace("USDT", "")
            ccxt_sym = f"{base}/USDT"

            if base in self._paper.positions:
                continue

            summary = await self._market.get_market_summary(tv_symbol, self._rules)
            if not summary or "error" in summary:
                continue

            self._last_bias[base] = summary.get("bias", "NEUTRAL")
            if self.strategy_cfg is not None:
                signal = get_signal_with_config(summary, self.strategy_cfg)
            else:
                signal = get_signal(summary)
            if not signal:
                continue

            price = summary["price"]
            h4_df = await self._market.get_ohlcv(ccxt_sym, "4h", 20)
            stop = calc_stop(h4_df, signal, price)

            risk_pct = self.strategy_cfg.risk_pct if self.strategy_cfg else 0.01
            rr = self.strategy_cfg.rr_ratio if self.strategy_cfg else 2.0

            try:
                trade = self._paper.enter(base, signal, price, stop, risk_pct=risk_pct, rr=rr)
                msg = (
                    f"ENTERED {base} {signal} @ ${price:,.4f} "
                    f"| Stop: ${stop:,.4f} | Target: ${trade['target']:,.4f}"
                )
                self._log(msg, "TRADE")
                emoji = "🟢" if signal == "LONG" else "🔴"
                await self._notify(
                    f"{emoji} <b>Paper Trade Opened</b>\n"
                    f"{base} {signal} @ <code>${price:,.4f}</code>\n"
                    f"Stop: <code>${stop:,.4f}</code> | Target: <code>${trade['target']:,.4f}</code>\n"
                    f"Risk: <code>${trade['risk_usd']:,.2f}</code>"
                )
            except ValueError as exc:
                self._log(f"Entry skipped {base}: {exc}")

    async def _tick_unleashed(self) -> None:
        """Unleashed tick: Oscar (Gemini) decides everything — no rules applied."""
        all_symbols = (
            self._rules["watchlist"]["majors"] + self._rules["watchlist"]["alts"]
        )
        summaries = []
        for tv_sym in all_symbols:
            summary = await self._market.get_market_summary(tv_sym, self._rules)
            if summary and "error" not in summary:
                summaries.append(summary)
                sym_parts = tv_sym.split(":")
                base = sym_parts[1].replace("USDT", "") if len(sym_parts) == 2 else tv_sym
                self._last_bias[base] = summary.get("bias", "NEUTRAL")

        self._log(f"[UNLEASHED] Asking Oscar to decide on {len(summaries)} markets…")
        decisions = await ai_decision(
            summaries, self._paper.balance, self._paper.positions
        )

        for dec in decisions:
            action = dec.get("action", "hold")
            symbol = dec.get("symbol", "").upper()
            reason = dec.get("reason", "")

            if action == "close" and symbol in self._paper.positions:
                price = await self._market.get_price(f"{symbol}/USDT")
                if not price:
                    continue
                result = self._paper.close(symbol, price)
                pnl = result["pnl"]
                sign = "+" if pnl >= 0 else ""
                msg = (
                    f"[UNLEASHED] CLOSED {symbol} @ ${price:,.4f} "
                    f"P&L: {sign}${pnl:,.2f} — {reason}"
                )
                self._log(msg, "TRADE")
                emoji = "✅" if pnl >= 0 else "🟥"
                await self._notify(
                    f"{emoji} <b>[UNLEASHED] Oscar Closed</b>\n"
                    f"{symbol} @ <code>${price:,.4f}</code>\n"
                    f"P&amp;L: <code>{sign}${pnl:,.2f}</code>\n"
                    f"Oscar's reason: {reason}"
                )

            elif action == "enter" and symbol not in self._paper.positions:
                price = await self._market.get_price(f"{symbol}/USDT")
                if not price:
                    continue
                side = dec.get("side", "LONG").upper()
                size_usd = float(dec.get("size_usd", 0))
                stop = float(dec.get("stop", 0))
                target = float(dec.get("target", 0))
                if not all([size_usd, stop, target]):
                    self._log(f"[UNLEASHED] Skipping {symbol} — incomplete decision")
                    continue
                try:
                    trade = self._paper.enter_free(symbol, side, price, stop, target, size_usd)
                    alloc_pct = size_usd / (self._paper.balance + size_usd) * 100
                    msg = (
                        f"[UNLEASHED] ENTERED {symbol} {side} @ ${price:,.4f} "
                        f"size=${size_usd:,.0f} ({alloc_pct:.0f}% of balance) — {reason}"
                    )
                    self._log(msg, "TRADE")
                    emoji = "🟢" if side == "LONG" else "🔴"
                    await self._notify(
                        f"{emoji} <b>[UNLEASHED] Oscar Entered</b>\n"
                        f"{symbol} {side} @ <code>${price:,.4f}</code>\n"
                        f"Size: <code>${size_usd:,.0f}</code> ({alloc_pct:.0f}% of account)\n"
                        f"Stop: <code>${stop:,.4f}</code> | Target: <code>${target:,.4f}</code>\n"
                        f"Oscar's reason: {reason}"
                    )
                except ValueError as exc:
                    self._log(f"[UNLEASHED] Entry rejected {symbol}: {exc}")

            elif action == "hold":
                self._log(f"[UNLEASHED] HOLD {symbol} — {reason}")

    async def _maybe_send_summary(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_notify is None or (
            now - self._last_notify
        ).total_seconds() >= NOTIFICATION_INTERVAL_HOURS * 3600:
            self._last_notify = now
            await self._send_periodic_summary()

    async def _send_periodic_summary(self) -> None:
        st = self._paper.stats()
        lines = [
            "📊 <b>Oscar — Periodic Update</b>",
            f"Balance: <code>${self._paper.balance:,.2f}</code>",
            f"Open positions: <code>{len(self._paper.positions)}</code>",
        ]
        if st.get("count"):
            lines.append(
                f"Trades: {st['count']} | WR: {st['win_rate']:.0f}% "
                f"({st['wins']}W/{st['losses']}L)"
            )
            sign = "+" if st["total_pnl"] >= 0 else ""
            lines.append(f"Realised P&amp;L: <code>{sign}${st['total_pnl']:,.2f}</code>")
        if self._paper.positions:
            lines.append("\n<b>Open Positions:</b>")
            for sym, pos in self._paper.positions.items():
                lines.append(f"• {sym} {pos.side} @ ${pos.entry:,.4f}")
        await self._notify("\n".join(lines))
