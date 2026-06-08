"""Market data service — multi-exchange (ccxt) OHLCV, prices, indicators and
**dynamic universe discovery**.

The old version hit Binance.com directly with a hardcoded 6-coin watchlist.
This version talks to whatever exchange is reachable (auto-failover across
binance → kucoin → gate → okx → kraken → coinbase) and can discover the *entire*
tradeable universe (hundreds–thousands of pairs), so the AI brain — not the
code — chooses what to trade.

Method signatures used elsewhere (`get_ohlcv`, `get_price`, `get_market_summary`,
`scan_symbols`, `calculate_indicators`, `determine_bias`) are preserved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import pandas as pd

import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)

TIMEFRAME_MAP: dict[str, str] = {
    "1W": "1w", "1D": "1d", "4H": "4h", "1H": "1h", "15M": "15m", "15m": "15m",
}

# Tried in order when EXCHANGE_ID=auto. First one whose markets load wins.
# binance.com is intentionally NOT in this list: it is geo-blocked in many
# regions and, worse, in some networks the connection silently stalls instead
# of failing fast, which would hang startup. Pin EXCHANGE_ID=binance explicitly
# if you really want it. The defaults below are globally reachable and have the
# deepest USDT spot universes (kucoin ~885, gateio ~2000, okx ~300).
_AUTO_EXCHANGES = ["kucoin", "okx", "gateio", "binanceus", "kraken", "coinbase"]

# Hard cap on how long a single exchange's market load may take before we give
# up and try the next one. This guards against a stalled connection that never
# raises (the per-request ccxt timeout is not always enough on its own).
_EXCHANGE_LOAD_TIMEOUT = float(os.getenv("EXCHANGE_LOAD_TIMEOUT", "15"))

_SNAPSHOT_CONCURRENCY = 8

# How long a full tickers snapshot is reused before refetching. Keeps the
# dashboard snappy: one exchange round-trip serves every price lookup (all
# agents, all positions, every endpoint) within the window instead of one
# round-trip per position per request.
_PRICE_CACHE_TTL = float(os.getenv("PRICE_CACHE_TTL", "8"))


def parse_tv_symbol(tv_symbol: str) -> Optional[tuple[str, str]]:
    """Convert a TradingView symbol (EXCHANGE:BTCUSDT) to (base, ccxt_symbol)."""
    if ":" not in tv_symbol:
        return None
    _, pair = tv_symbol.split(":", 1)
    if pair.endswith("USDT"):
        base = pair[: -len("USDT")]
        return base, f"{base}/USDT"
    return None


# ---------------------------------------------------------------------------
# Pure-Python indicator maths
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=True).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=True).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def detect_structure(df: pd.DataFrame, window: int = 5) -> str:
    """Identify market structure using pivot high/low logic."""
    if len(df) < window * 2 + 2:
        return "Insufficient data"

    highs = df["high"].values
    lows = df["low"].values
    n = len(highs)

    pivot_highs: list[float] = []
    pivot_lows: list[float] = []

    for i in range(window, n - window):
        if highs[i] == max(highs[i - window : i + window + 1]):
            pivot_highs.append(float(highs[i]))
        if lows[i] == min(lows[i - window : i + window + 1]):
            pivot_lows.append(float(lows[i]))

    if len(pivot_highs) >= 2 and len(pivot_lows) >= 2:
        hh = pivot_highs[-1] > pivot_highs[-2]
        hl = pivot_lows[-1] > pivot_lows[-2]
        lh = pivot_highs[-1] < pivot_highs[-2]
        ll = pivot_lows[-1] < pivot_lows[-2]

        if hh and hl:
            return "HH/HL (Bullish)"
        if lh and ll:
            return "LH/LL (Bearish)"
        if hh and ll:
            return "Expanding range"
        if lh and hl:
            return "Contracting range"

    return "No clear structure"


def _normalize_symbol(x: str, quote: str = "USDT") -> str:
    """Accept 'BTC', 'BTCUSDT', 'BTC/USDT' or 'BINANCE:BTCUSDT' -> 'BTC/USDT'."""
    x = x.strip().upper()
    if "/" in x:
        return x
    if ":" in x:
        x = x.split(":", 1)[1]
    if x.endswith(quote):
        base = x[: -len(quote)]
        return f"{base}/{quote}"
    return f"{x}/{quote}"


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------


class MarketDataService:
    def __init__(self, exchange_id: str = "auto", quote: str = "USDT") -> None:
        self._exchange_id = exchange_id
        self._quote = quote
        self._ex: Optional[ccxt.Exchange] = None
        self._ex_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(_SNAPSHOT_CONCURRENCY)
        self.active_exchange: Optional[str] = None
        # Shared TTL cache of the full tickers snapshot (ccxt_sym -> last price).
        self._tickers_cache: dict[str, float] = {}
        self._tickers_ts: float = 0.0
        self._tickers_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Exchange lifecycle
    # ------------------------------------------------------------------

    async def _build(self, name: str) -> Optional[ccxt.Exchange]:
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 20000})
            await ex.load_markets()
            return ex
        except Exception as exc:
            logger.info("Exchange %s unavailable: %s", name, repr(exc)[:120])
            try:
                await ex.close()  # type: ignore[has-type]
            except Exception:
                pass
            return None

    async def _exchange(self) -> ccxt.Exchange:
        if self._ex is not None:
            return self._ex
        async with self._ex_lock:
            if self._ex is not None:
                return self._ex
            candidates = (
                _AUTO_EXCHANGES if self._exchange_id == "auto" else [self._exchange_id]
            )
            logger.info("Selecting exchange (candidates: %s)...", ", ".join(candidates))
            for name in candidates:
                logger.info("Connecting to exchange '%s' (loading markets)...", name)
                try:
                    ex = await asyncio.wait_for(
                        self._build(name), timeout=_EXCHANGE_LOAD_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Exchange %s stalled (>%.0fs) — skipping to next",
                        name, _EXCHANGE_LOAD_TIMEOUT,
                    )
                    ex = None
                if ex is not None:
                    self._ex = ex
                    self.active_exchange = name
                    logger.info("Market data exchange: %s (%d markets)", name, len(ex.markets))
                    return ex
            raise RuntimeError(
                f"No reachable exchange among {candidates}. Set EXCHANGE_ID to a "
                f"specific reachable exchange."
            )

    async def close(self) -> None:
        if self._ex is not None:
            try:
                await self._ex.close()
            except Exception:
                pass
            self._ex = None

    # ------------------------------------------------------------------
    # Universe discovery — the heart of "trades on many cryptos as it wishes"
    # ------------------------------------------------------------------

    async def discover_universe(self, limit: int = 60) -> list[dict]:
        """Return the top `limit` tradeable pairs ranked by 24h quote volume.

        No hardcoded coin list — this reflects whatever the exchange lists.
        """
        ex = await self._exchange()
        try:
            tickers = await ex.fetch_tickers()
        except Exception as exc:
            logger.warning("fetch_tickers failed: %s — falling back to markets list", repr(exc)[:120])
            tickers = {}

        rows: list[dict] = []
        for sym, m in ex.markets.items():
            if not m.get("spot", True):
                continue
            if m.get("quote") != self._quote:
                continue
            if not m.get("active", True):
                continue
            t = tickers.get(sym, {})
            qv = t.get("quoteVolume")
            if qv is None:
                bv = t.get("baseVolume") or 0
                last = t.get("last") or 0
                qv = (bv or 0) * (last or 0)
            rows.append({
                "symbol": sym,
                "base": m.get("base", sym.split("/")[0]),
                "last": t.get("last"),
                "quote_volume": float(qv or 0.0),
                "pct_change": t.get("percentage"),
            })

        rows.sort(key=lambda r: r["quote_volume"], reverse=True)
        return rows[: max(1, limit)]

    # ------------------------------------------------------------------
    # OHLCV + prices
    # ------------------------------------------------------------------

    async def get_ohlcv(
        self, symbol: str, timeframe: str = "1d", limit: int = 250
    ) -> Optional[pd.DataFrame]:
        ccxt_sym = _normalize_symbol(symbol, self._quote)
        tf = TIMEFRAME_MAP.get(timeframe, timeframe.lower())
        ex = await self._exchange()
        try:
            async with self._sem:
                raw = await ex.fetch_ohlcv(ccxt_sym, tf, limit=limit)
        except Exception as exc:
            logger.debug("OHLCV %s %s failed: %s", ccxt_sym, tf, repr(exc)[:100])
            return None
        if not raw:
            return None
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        df.set_index("ts", inplace=True)
        return df[["open", "high", "low", "close", "volume"]].astype(float)

    async def _all_tickers(self, force: bool = False) -> dict[str, float]:
        """Full tickers snapshot (ccxt_sym -> last price), cached for a few seconds.

        A single ``fetch_tickers()`` round-trip backs every price lookup within
        the TTL window, so the dashboard's combined poll (portfolio + arena +
        positions, many overlapping symbols) no longer fans out into dozens of
        sequential exchange calls.
        """
        now = time.monotonic()
        if not force and self._tickers_cache and now - self._tickers_ts < _PRICE_CACHE_TTL:
            return self._tickers_cache
        async with self._tickers_lock:
            now = time.monotonic()
            if not force and self._tickers_cache and now - self._tickers_ts < _PRICE_CACHE_TTL:
                return self._tickers_cache
            ex = await self._exchange()
            try:
                async with self._sem:
                    tickers = await ex.fetch_tickers()
                cache: dict[str, float] = {}
                for cs, t in tickers.items():
                    price = (t or {}).get("last") or (t or {}).get("close")
                    if price is not None:
                        cache[cs] = float(price)
                if cache:
                    self._tickers_cache = cache
                    self._tickers_ts = now
            except Exception as exc:
                logger.warning("fetch_tickers (cache) failed: %s", repr(exc)[:120])
        return self._tickers_cache

    async def get_price(self, symbol: str) -> Optional[float]:
        ccxt_sym = _normalize_symbol(symbol, self._quote)
        cache = await self._all_tickers()
        if ccxt_sym in cache:
            return cache[ccxt_sym]
        # Not in the snapshot (thin or newly listed pair) — one direct lookup.
        ex = await self._exchange()
        try:
            async with self._sem:
                t = await ex.fetch_ticker(ccxt_sym)
            price = t.get("last") or t.get("close")
            return float(price) if price is not None else None
        except Exception as exc:
            logger.debug("Price fetch failed %s: %s", ccxt_sym, repr(exc)[:100])
            return None

    async def get_prices(self, symbols: list[str]) -> dict[str, float]:
        """Batch price lookup; keys are bare bases (e.g. 'BTC')."""
        if not symbols:
            return {}
        ccxt_syms = [_normalize_symbol(s, self._quote) for s in symbols]
        cache = await self._all_tickers()
        out: dict[str, float] = {}
        missing: list[str] = []
        for cs in ccxt_syms:
            if cs in cache:
                out[cs.split("/")[0]] = cache[cs]
            else:
                missing.append(cs)

        if missing:
            async def _one(cs: str) -> None:
                p = await self.get_price(cs)
                if p is not None:
                    out[cs.split("/")[0]] = p

            await asyncio.gather(*[_one(cs) for cs in missing])
        return out

    # ------------------------------------------------------------------
    # Indicators + summaries
    # ------------------------------------------------------------------

    def calculate_indicators(self, df: pd.DataFrame) -> dict:
        closes = df["close"]
        volumes = df["volume"]

        rsi_s = _rsi(closes)
        ema50 = _ema(closes, 50)
        ema200 = _ema(closes, 200)
        macd_line, signal_line, histogram = _macd(closes)
        vol_ma20 = volumes.rolling(20).mean()

        price = float(closes.iloc[-1])
        rsi = float(rsi_s.iloc[-1])
        e50 = float(ema50.iloc[-1])
        e200 = float(ema200.iloc[-1])
        vol = float(volumes.iloc[-1])
        vol_avg = float(vol_ma20.iloc[-1]) if not pd.isna(vol_ma20.iloc[-1]) else vol

        return {
            "price": price,
            "rsi": rsi,
            "ema50": e50,
            "ema200": e200,
            "above_ema50": price > e50,
            "above_ema200": price > e200,
            "ema50_pct": ((price - e50) / e50) * 100 if e50 else 0.0,
            "macd": float(macd_line.iloc[-1]),
            "macd_signal": float(signal_line.iloc[-1]),
            "macd_hist": float(histogram.iloc[-1]),
            "volume": vol,
            "volume_avg_20": vol_avg,
            "volume_above_avg": vol > vol_avg,
        }

    def determine_bias(self, daily: dict, h4_structure: str) -> str:
        """A neutral, descriptive bias label (NOT a trade rule).

        This is only context handed to the AI — the AI is free to ignore it.
        """
        rsi = daily.get("rsi", 50.0)
        above_ema50 = daily.get("above_ema50", False)
        bullish_structure = "HH/HL" in h4_structure
        bearish_structure = "LH/LL" in h4_structure

        if above_ema50 and 45 <= rsi <= 70 and bullish_structure:
            return "BULLISH"
        if not above_ema50 and rsi < 45 and bearish_structure:
            return "BEARISH"
        return "NEUTRAL"

    async def get_market_summary(self, symbol: str, rules: dict | None = None) -> Optional[dict]:
        """Full analysis dict for one symbol. Accepts TV, ccxt or bare formats."""
        ccxt_sym = _normalize_symbol(symbol, self._quote)
        base = ccxt_sym.split("/")[0]

        daily_df, h4_df = await asyncio.gather(
            self.get_ohlcv(ccxt_sym, "1d", 250),
            self.get_ohlcv(ccxt_sym, "4h", 100),
        )

        if daily_df is None or len(daily_df) < 50:
            return {"symbol": symbol, "error": "Insufficient data"}

        daily = self.calculate_indicators(daily_df)
        h4 = self.calculate_indicators(h4_df) if h4_df is not None and len(h4_df) >= 50 else daily
        h4_structure = (
            detect_structure(h4_df) if h4_df is not None and len(h4_df) > 20 else "Unknown"
        )
        bias = self.determine_bias(daily, h4_structure)

        return {
            "symbol": f"EXCHANGE:{base}{self._quote}",
            "base": base,
            "ccxt_symbol": ccxt_sym,
            "price": daily["price"],
            "bias": bias,
            "daily": daily,
            "h4": h4,
            "h4_structure": h4_structure,
        }

    async def scan_symbols(self, symbols: list[str], rules: dict | None = None) -> list[dict]:
        tasks = [self.get_market_summary(s, rules) for s in symbols]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in raw if isinstance(r, dict) and "error" not in r]
