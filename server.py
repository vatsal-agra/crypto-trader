"""FastAPI web server — serves dashboard UI and REST API for Oscar."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine.strategies_store import StrategyConfig, get_or_create_strategies
from engine.trading_loop import TradingLoop
from oscar.market import MarketDataService, parse_tv_symbol
from oscar.paper_trader import PaperTrader

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap shared state
# ---------------------------------------------------------------------------

_RULES_PATH = Path(__file__).parent / "config" / "rules.json"
_DATA_DIR   = Path(__file__).parent / "data"

with _RULES_PATH.open() as _f:
    RULES: dict = json.load(_f)

market_service = MarketDataService()

# Arena — populated in lifespan
arena_strategies: list[StrategyConfig] = []
arena_papers: list[PaperTrader] = []
arena_engines: list[TradingLoop] = []

_WEB_DIR = Path(__file__).parent / "web"

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global arena_strategies, arena_papers, arena_engines

    arena_strategies = await get_or_create_strategies()
    for i, strat in enumerate(arena_strategies):
        state_file = _DATA_DIR / f"arena_{strat.id}.json"
        paper = PaperTrader(state_file=state_file)
        loop = TradingLoop(
            market=market_service,
            paper_trader=paper,
            rules=RULES,
            interval_minutes=30,
            strategy_cfg=strat,
            name=strat.id,
            initial_delay_seconds=i * 360,
            notify_telegram=False,
        )
        arena_papers.append(paper)
        arena_engines.append(loop)
        await loop.start()
        logger.info("Arena engine started: %s (%s)", strat.id, strat.name)

    yield

    for loop in arena_engines:
        await loop.stop()
    logger.info("All arena engines stopped")


app = FastAPI(title="Oscar Trading Dashboard", lifespan=lifespan)

# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


@app.get("/api/portfolio")
async def api_portfolio(sid: str = "all"):
    # If a specific strategy is selected
    if sid != "all":
        idx = _arena_idx(sid)
        if idx is None:
            return JSONResponse({"error": "strategy not found"}, status_code=404)
        paper = arena_papers[idx]
        prices: dict[str, float] = {}
        for sym in paper.positions:
            p = await market_service.get_price(f"{sym}/USDT")
            if p:
                prices[sym] = p
        equity = paper.equity(prices)
        initial = paper.initial_balance
        total_pnl = equity - initial
        total_pnl_pct = (total_pnl / initial * 100) if initial else 0
        return {
            "balance": paper.balance,
            "initial_balance": initial,
            "equity": equity,
            "profit_purse": paper.profit_purse,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "stats": paper.stats(),
        }

    # Combined total of all 5 strategies
    tot_balance = 0.0
    tot_initial = 0.0
    tot_equity = 0.0
    tot_profit_purse = 0.0
    agg_count = 0
    agg_wins = 0
    agg_losses = 0
    agg_total_pnl = 0.0

    for i, paper in enumerate(arena_papers):
        prices = {}
        for sym in paper.positions:
            p = await market_service.get_price(f"{sym}/USDT")
            if p:
                prices[sym] = p
        tot_balance += paper.balance
        tot_initial += paper.initial_balance
        tot_equity += paper.equity(prices)
        tot_profit_purse += paper.profit_purse

        st = paper.stats()
        agg_count += st.get("count", 0)
        agg_wins += st.get("wins", 0)
        agg_losses += st.get("losses", 0)
        agg_total_pnl += st.get("total_pnl", 0.0)

    tot_pnl = tot_equity - tot_initial
    tot_pnl_pct = (tot_pnl / tot_initial * 100) if tot_initial else 0

    return {
        "balance": tot_balance,
        "initial_balance": tot_initial,
        "equity": tot_equity,
        "profit_purse": tot_profit_purse,
        "total_pnl": tot_pnl,
        "total_pnl_pct": tot_pnl_pct,
        "stats": {
            "count": agg_count,
            "wins": agg_wins,
            "losses": agg_losses,
            "win_rate": (agg_wins / (agg_wins + agg_losses) * 100) if (agg_wins + agg_losses) > 0 else 0.0,
            "total_pnl": agg_total_pnl,
        },
    }


@app.get("/api/positions")
async def api_positions(sid: str = "all"):
    # Specific strategy positions
    if sid != "all":
        idx = _arena_idx(sid)
        if idx is None:
            return JSONResponse({"error": "strategy not found"}, status_code=404)
        paper = arena_papers[idx]
        strat = arena_strategies[idx]
        result = []
        for sym, pos in paper.positions.items():
            price = await market_service.get_price(f"{sym}/USDT")
            cur = price or pos.entry
            result.append({
                "symbol": sym,
                "side": pos.side,
                "entry": pos.entry,
                "current_price": cur,
                "stop": pos.stop,
                "target": pos.target,
                "size": pos.size,
                "risk_usd": pos.risk_usd,
                "unrealized_pnl": pos.unrealized_pnl(cur),
                "pnl_pct": pos.pnl_pct(cur),
                "opened_at": pos.opened_at,
                "strategy_id": strat.id,
                "strategy_name": strat.name,
            })
        return {"positions": result}

    # All positions combined
    result = []
    for i, paper in enumerate(arena_papers):
        strat = arena_strategies[i]
        for sym, pos in paper.positions.items():
            price = await market_service.get_price(f"{sym}/USDT")
            cur = price or pos.entry
            result.append({
                "symbol": sym,
                "side": pos.side,
                "entry": pos.entry,
                "current_price": cur,
                "stop": pos.stop,
                "target": pos.target,
                "size": pos.size,
                "risk_usd": pos.risk_usd,
                "unrealized_pnl": pos.unrealized_pnl(cur),
                "pnl_pct": pos.pnl_pct(cur),
                "opened_at": pos.opened_at,
                "strategy_id": strat.id,
                "strategy_name": strat.name,
            })
    return {"positions": result}


@app.get("/api/trades")
async def api_trades(sid: str = "all"):
    # Specific strategy closed trades
    if sid != "all":
        idx = _arena_idx(sid)
        if idx is None:
            return JSONResponse({"error": "strategy not found"}, status_code=404)
        return {"trades": arena_papers[idx].closed_trades}

    # All closed trades combined
    result = []
    for i, paper in enumerate(arena_papers):
        strat = arena_strategies[i]
        for t in paper.closed_trades:
            t_copy = dict(t)
            t_copy["strategy_id"] = strat.id
            t_copy["strategy_name"] = strat.name
            result.append(t_copy)

    # Sort combined trades by exit time descending (or index)
    result.sort(key=lambda x: x.get("opened_at", ""), reverse=True)
    return {"trades": result}


@app.get("/api/prices")
async def api_prices():
    result: dict[str, dict] = {}
    all_symbols = RULES["watchlist"]["majors"] + RULES["watchlist"]["alts"]
    for tv_sym in all_symbols:
        parsed = parse_tv_symbol(tv_sym)
        if not parsed:
            continue
        base, ccxt_sym = parsed
        price = await market_service.get_price(ccxt_sym)
        # Use first arena engine's bias as a proxy
        bias = "NEUTRAL"
        if arena_engines:
            bias = arena_engines[0]._last_bias.get(base, "NEUTRAL")
        result[base] = {
            "price": price,
            "bias": bias,
        }
    return {"prices": result}


@app.get("/api/candles/{symbol}")
async def api_candles(symbol: str, tf: str = "4h", limit: int = 200):
    symbol = symbol.upper()
    if "/" not in symbol:
        if symbol.endswith("USDT"):
            ccxt_sym = symbol[:-4] + "/USDT"
        else:
            ccxt_sym = symbol + "/USDT"
    else:
        ccxt_sym = symbol

    df = await market_service.get_ohlcv(ccxt_sym, tf, limit)
    if df is None or df.empty:
        return {"candles": []}

    candles = [
        {
            "time": int(ts.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for ts, row in df.iterrows()
    ]
    return {"candles": candles}


@app.get("/api/engine")
async def api_engine():
    # Return details for s1 or combined
    running = any(eng.running for eng in arena_engines) if arena_engines else False
    runs = max(eng.runs for eng in arena_engines) if arena_engines else 0
    next_run = arena_engines[0].next_run if arena_engines else None
    last_run = arena_engines[0].last_run if arena_engines else None

    # Merge logs
    merged_log = []
    for eng in arena_engines:
        for entry in eng.log:
            entry_copy = dict(entry)
            entry_copy["msg"] = f"[{eng.name.upper()}] {entry_copy['msg']}"
            merged_log.append(entry_copy)
    merged_log.sort(key=lambda x: x.get("time", ""))

    return {
        "running": running,
        "unleashed": False, # Mode removed as strategies are autonomous
        "interval_minutes": 30,
        "last_run": last_run,
        "next_run": next_run,
        "runs": runs,
        "log": merged_log[-100:],
    }


# ---------------------------------------------------------------------------
# Arena API — 5 independent strategy instances
# ---------------------------------------------------------------------------


def _arena_idx(sid: str) -> int | None:
    for i, s in enumerate(arena_strategies):
        if s.id == sid:
            return i
    return None


@app.get("/api/arena/strategies")
async def api_arena_strategies():
    return {"strategies": [s.to_dict() for s in arena_strategies]}


@app.get("/api/arena")
async def api_arena_overview():
    result = []
    for i, strat in enumerate(arena_strategies):
        paper = arena_papers[i]
        eng = arena_engines[i]
        prices: dict[str, float] = {}
        for sym in paper.positions:
            p = await market_service.get_price(f"{sym}/USDT")
            if p:
                prices[sym] = p
        equity = paper.equity(prices)
        initial = paper.initial_balance
        result.append({
            "strategy": strat.to_dict(),
            "balance": paper.balance,
            "equity": equity,
            "profit_purse": paper.profit_purse,
            "pnl": equity - initial,
            "pnl_pct": (equity - initial) / initial * 100 if initial else 0,
            "open_positions": len(paper.positions),
            "stats": paper.stats(),
            "running": eng.running,
            "runs": eng.runs,
            "last_run": eng.last_run,
            "log": eng.log[-30:],
        })
    return {"arena": result}


@app.get("/api/arena/{sid}/positions")
async def api_arena_positions(sid: str):
    idx = _arena_idx(sid)
    if idx is None:
        return JSONResponse({"error": "strategy not found"}, status_code=404)
    paper = arena_papers[idx]
    result = []
    for sym, pos in paper.positions.items():
        price = await market_service.get_price(f"{sym}/USDT")
        cur = price or pos.entry
        result.append({
            "symbol": sym,
            "side": pos.side,
            "entry": pos.entry,
            "current_price": cur,
            "stop": pos.stop,
            "target": pos.target,
            "size": pos.size,
            "risk_usd": pos.risk_usd,
            "unrealized_pnl": pos.unrealized_pnl(cur),
            "pnl_pct": pos.pnl_pct(cur),
            "opened_at": pos.opened_at,
        })
    return {"positions": result}


@app.get("/api/arena/{sid}/trades")
async def api_arena_trades(sid: str):
    idx = _arena_idx(sid)
    if idx is None:
        return JSONResponse({"error": "strategy not found"}, status_code=404)
    return {"trades": arena_papers[idx].closed_trades}


# ---------------------------------------------------------------------------
# Serve React dashboard
# ---------------------------------------------------------------------------


@app.get("/")
async def serve_ui():
    return FileResponse(_WEB_DIR / "index.html")
