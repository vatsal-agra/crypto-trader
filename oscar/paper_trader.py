"""Paper trading engine — simulated positions with live prices, persistent state."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent / "data" / "paper_account.json"
DEFAULT_BALANCE = 10_000.0
DEFAULT_RISK_PCT = 0.01  # 1% of free balance per trade
DEFAULT_RR = 2.0         # 2:1 reward:risk ratio


class Position:
    __slots__ = ("symbol", "side", "entry", "size", "stop", "target", "risk_usd", "opened_at")

    def __init__(
        self,
        symbol: str,
        side: str,
        entry: float,
        size: float,
        stop: float,
        target: float,
        risk_usd: float,
    ) -> None:
        self.symbol = symbol
        self.side = side          # "LONG" or "SHORT"
        self.entry = entry
        self.size = size          # quantity in base asset (e.g. BTC)
        self.stop = stop
        self.target = target
        self.risk_usd = risk_usd
        self.opened_at = datetime.now(timezone.utc).isoformat()

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "LONG":
            return (current_price - self.entry) * self.size
        return (self.entry - current_price) * self.size

    def pnl_pct(self, current_price: float) -> float:
        cost = self.entry * self.size
        return (self.unrealized_pnl(current_price) / cost * 100) if cost else 0.0

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        p = cls(
            d["symbol"], d["side"], d["entry"],
            d["size"], d["stop"], d["target"], d["risk_usd"],
        )
        p.opened_at = d.get("opened_at", p.opened_at)
        return p


class PaperTrader:
    def __init__(self, state_file: Path = STATE_FILE) -> None:
        self._state_file = state_file
        self.balance: float = DEFAULT_BALANCE
        self.initial_balance: float = DEFAULT_BALANCE
        self.profit_purse: float = 0.0
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            s = json.loads(self._state_file.read_text())
            self.balance = s.get("balance", DEFAULT_BALANCE)
            self.initial_balance = s.get("initial_balance", DEFAULT_BALANCE)
            self.profit_purse = s.get("profit_purse", 0.0)
            self.positions = {
                k: Position.from_dict(v)
                for k, v in s.get("positions", {}).items()
            }
            self.closed_trades = s.get("closed_trades", [])
            logger.info("Paper account loaded — balance $%.2f, purse $%.2f, %d open positions",
                        self.balance, self.profit_purse, len(self.positions))
        except Exception as exc:
            logger.warning("Could not load paper account: %s", exc)

    def save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps({
            "balance": self.balance,
            "initial_balance": self.initial_balance,
            "profit_purse": self.profit_purse,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "closed_trades": self.closed_trades,
        }, indent=2))

    def reset(self, starting: float = DEFAULT_BALANCE) -> None:
        self.balance = starting
        self.initial_balance = starting
        self.profit_purse = 0.0
        self.positions.clear()
        self.closed_trades.clear()
        self.save()

    # ------------------------------------------------------------------
    # Trading operations
    # ------------------------------------------------------------------

    def enter(
        self,
        symbol: str,
        side: str,
        price: float,
        stop: float,
        risk_pct: float = DEFAULT_RISK_PCT,
        rr: float = DEFAULT_RR,
    ) -> dict:
        """Open a paper position. Returns trade-detail dict."""
        if symbol in self.positions:
            raise ValueError(f"Already have an open position for {symbol}. Close it first.")

        if side == "LONG" and stop >= price:
            raise ValueError("Stop must be below entry for LONG.")
        if side == "SHORT" and stop <= price:
            raise ValueError("Stop must be above entry for SHORT.")

        stop_dist = abs(price - stop)
        risk_usd = self.balance * risk_pct
        size = risk_usd / stop_dist
        cost = size * price

        if cost > self.balance:
            raise ValueError(
                f"Not enough balance (need ${cost:,.2f}, have ${self.balance:,.2f})."
            )

        target = (price + stop_dist * rr) if side == "LONG" else (price - stop_dist * rr)

        pos = Position(symbol, side, price, size, stop, target, risk_usd)
        self.positions[symbol] = pos
        self.balance -= cost
        self.save()

        return {
            "entry": price, "size": size, "stop": stop,
            "target": target, "risk_usd": risk_usd, "cost": cost,
        }

    def enter_free(
        self,
        symbol: str,
        side: str,
        price: float,
        stop: float,
        target: float,
        size_usd: float,
    ) -> dict:
        """Open a paper position with Oscar's own sizing — no risk rules applied."""
        if symbol in self.positions:
            raise ValueError(f"Already have an open position for {symbol}.")
        if side == "LONG" and stop >= price:
            raise ValueError("Stop must be below entry for LONG.")
        if side == "SHORT" and stop <= price:
            raise ValueError("Stop must be above entry for SHORT.")
        if size_usd > self.balance:
            raise ValueError(
                f"Not enough balance (need ${size_usd:,.2f}, have ${self.balance:,.2f})."
            )

        size = size_usd / price
        pos = Position(symbol, side, price, size, stop, target, size_usd)
        self.positions[symbol] = pos
        self.balance -= size_usd
        self.save()

        return {
            "entry": price, "size": size, "stop": stop,
            "target": target, "risk_usd": size_usd, "cost": size_usd,
        }

    def close(self, symbol: str, price: float) -> dict:
        """Close an open position at current price. Returns result dict."""
        if symbol not in self.positions:
            raise ValueError(f"No open position for {symbol}.")

        pos = self.positions.pop(symbol)
        exit_value = pos.size * price
        pnl = (
            (price - pos.entry) * pos.size
            if pos.side == "LONG"
            else (pos.entry - price) * pos.size
        )
        self.balance += exit_value

        record = {
            **pos.to_dict(),
            "exit": price,
            "pnl": pnl,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.closed_trades.append(record)
        self.save()
        return record

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def equity(self, current_prices: dict[str, float]) -> float:
        """Free balance + profit purse + market value of all open positions."""
        total = self.balance + self.profit_purse
        for sym, pos in self.positions.items():
            price = current_prices.get(sym, pos.entry)
            total += pos.size * price
        return total

    def stats(self) -> dict:
        trades = self.closed_trades
        if not trades:
            return {}
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        return {
            "count": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100,
            "total_pnl": sum(t["pnl"] for t in trades),
            "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
        }
