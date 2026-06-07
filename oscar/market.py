"""Market data service — fetches OHLCV via direct Binance REST + indicators."""

import asyncio
import logging
from typing import Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

TIMEFRAME_MAP: dict[str, str] = {
    "1W": "1w",
    "1D": "1d",
    "4H": "4h",
    "1H": "1h",
    "15M": "15m",
    "15m": "15m",
}


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
# Pure-Python indicator maths (no extra deps beyond pandas/numpy)
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


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

_BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
_HTTP_TIMEOUT = httpx.Timeout(15.0)


class MarketDataService:
    def __init__(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_ohlcv(
        self, symbol: str, timeframe: str = "1d", limit: int = 250
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV candles from Binance REST API — no market-loading needed."""
        base, quote = symbol.split("/")
        bsym = f"{base}{quote}"  # BTCUSDT
        interval = TIMEFRAME_MAP.get(timeframe, timeframe.lower())
        params = {"symbol": bsym, "interval": interval, "limit": limit}

        for host in _BINANCE_HOSTS:
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    r = await client.get(f"{host}/api/v3/klines", params=params)
                    r.raise_for_status()
                    raw = r.json()
                if not raw:
                    return None
                df = pd.DataFrame(
                    raw,
                    columns=[
                        "ts", "open", "high", "low", "close", "volume",
                        "ct", "qvol", "trades", "tbbv", "tbqv", "ignore",
                    ],
                )
                df["ts"] = pd.to_datetime(df["ts"], unit="ms")
                df.set_index("ts", inplace=True)
                return df[["open", "high", "low", "close", "volume"]].astype(float)
            except Exception as exc:
                logger.debug("OHLCV %s failed on %s: %s", symbol, host, exc)
                continue

        logger.warning("OHLCV fetch failed for %s/%s after all hosts", symbol, timeframe)
        return None

    def calculate_indicators(self, df: pd.DataFrame) -> dict:
        """Compute all indicators: RSI, MACD, 50/200 EMA, volume MA."""
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
        vol_avg = float(vol_ma20.iloc[-1])

        return {
            "price": price,
            "rsi": rsi,
            "ema50": e50,
            "ema200": e200,
            "above_ema50": price > e50,
            "above_ema200": price > e200,
            "ema50_pct": ((price - e50) / e50) * 100,
            "macd": float(macd_line.iloc[-1]),
            "macd_signal": float(signal_line.iloc[-1]),
            "macd_hist": float(histogram.iloc[-1]),
            "volume": vol,
            "volume_avg_20": vol_avg,
            "volume_above_avg": vol > vol_avg,
        }

    def determine_bias(self, daily: dict, h4_structure: str) -> str:
        """Apply the swing-trading bias rules from config/rules.json."""
        rsi = daily.get("rsi", 50.0)
        above_ema50 = daily.get("above_ema50", False)
        bullish_structure = "HH/HL" in h4_structure
        bearish_structure = "LH/LL" in h4_structure

        if above_ema50 and 45 <= rsi <= 70 and bullish_structure:
            return "BULLISH"
        if not above_ema50 and rsi < 45 and bearish_structure:
            return "BEARISH"
        return "NEUTRAL"

    async def get_market_summary(
        self, tv_symbol: str, rules: dict
    ) -> Optional[dict]:
        """Return a full analysis dict for a single TradingView symbol."""
        parsed = parse_tv_symbol(tv_symbol)
        if not parsed:
            logger.debug("Skipping non-exchange symbol: %s", tv_symbol)
            return None
        base, ccxt_symbol = parsed

        daily_df, h4_df = await asyncio.gather(
            self.get_ohlcv(ccxt_symbol, "1d", 250),
            self.get_ohlcv(ccxt_symbol, "4h", 100),
        )

        if daily_df is None or len(daily_df) < 50:
            return {"symbol": tv_symbol, "error": "Insufficient data"}

        daily = self.calculate_indicators(daily_df)
        h4 = self.calculate_indicators(h4_df) if h4_df is not None and len(h4_df) >= 50 else daily
        h4_structure = (
            detect_structure(h4_df)
            if h4_df is not None and len(h4_df) > 20
            else "Unknown"
        )
        bias = self.determine_bias(daily, h4_structure)

        return {
            "symbol": tv_symbol,
            "ccxt_symbol": ccxt_symbol,
            "price": daily["price"],
            "bias": bias,
            "daily": daily,
            "h4": h4,
            "h4_structure": h4_structure,
        }

    async def get_price(self, ccxt_symbol: str) -> Optional[float]:
        """Fetch the current price for a symbol via Binance REST API."""
        base, quote = ccxt_symbol.split("/")
        bsym = f"{base}{quote}"

        for host in _BINANCE_HOSTS:
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    r = await client.get(
                        f"{host}/api/v3/ticker/price", params={"symbol": bsym}
                    )
                    r.raise_for_status()
                    return float(r.json()["price"])
            except Exception:
                continue

        logger.warning("Price fetch failed for %s", ccxt_symbol)
        return None

    async def scan_symbols(self, symbols: list[str], rules: dict) -> list[dict]:
        """Scan a list of TradingView symbols concurrently."""
        tradeable = [s for s in symbols if parse_tv_symbol(s) is not None]
        tasks = [self.get_market_summary(s, rules) for s in tradeable]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            r
            for r in raw
            if isinstance(r, dict) and "error" not in r
        ]
