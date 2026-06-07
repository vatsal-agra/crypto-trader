"""FastAPI web server — dashboard UI + REST API for the autonomous arena."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from engine.config import get_settings
from engine.trading_loop import AutonomousArena
from oscar.market import MarketDataService

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"

SETTINGS = get_settings()
market_service = MarketDataService(exchange_id=SETTINGS.exchange_id, quote=SETTINGS.quote_asset)
arena: AutonomousArena = AutonomousArena(market_service, SETTINGS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await arena.start()
    logger.info("Arena live with %d autonomous agents", len(arena.agents))
    yield
    await arena.stop()
    await market_service.close()
    logger.info("Arena stopped")


app = FastAPI(title="Autonomous Crypto Trader", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_strategy_block(ov: dict) -> dict:
    """Back-compat 'strategy' object the dashboard expects, enriched with memo."""
    memo = ov.get("strategy_memo") or ""
    return {
        "id": ov["id"],
        "name": ov["name"],
        "style": ov["persona"],
        "description": (memo[:240] + "…") if len(memo) > 240 else (memo or ov["persona"]),
        "memo": memo,
        "watchlist": ov.get("watchlist", []),
    }


async def _all_overviews() -> list[dict]:
    return await asyncio.gather(*[ag.overview() for ag in arena.agents])


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

@app.get("/api/portfolio")
async def api_portfolio(sid: str = "all"):
    if sid != "all":
        ag = arena.agent_by_id(sid)
        if ag is None:
            return JSONResponse({"error": "agent not found"}, status_code=404)
        ov = await ag.overview()
        return {
            "balance": ov["balance"],
            "initial_balance": SETTINGS.starting_balance,
            "equity": ov["equity"],
            "profit_purse": ov["profit_purse"],
            "total_pnl": ov["pnl"],
            "total_pnl_pct": ov["pnl_pct"],
            "stats": ov["stats"],
        }

    ovs = await _all_overviews()
    tot_balance = sum(o["balance"] for o in ovs)
    tot_equity = sum(o["equity"] for o in ovs)
    tot_purse = sum(o["profit_purse"] for o in ovs)
    tot_initial = SETTINGS.starting_balance * len(ovs)
    agg_count = sum(o["stats"].get("count", 0) for o in ovs)
    agg_wins = sum(o["stats"].get("wins", 0) for o in ovs)
    agg_losses = sum(o["stats"].get("losses", 0) for o in ovs)
    agg_pnl = sum(o["stats"].get("total_pnl", 0.0) for o in ovs)
    tot_pnl = tot_equity - tot_initial
    return {
        "balance": tot_balance,
        "initial_balance": tot_initial,
        "equity": tot_equity,
        "profit_purse": tot_purse,
        "total_pnl": tot_pnl,
        "total_pnl_pct": (tot_pnl / tot_initial * 100) if tot_initial else 0,
        "stats": {
            "count": agg_count,
            "wins": agg_wins,
            "losses": agg_losses,
            "win_rate": (agg_wins / (agg_wins + agg_losses) * 100) if (agg_wins + agg_losses) else 0.0,
            "total_pnl": agg_pnl,
        },
    }


# ---------------------------------------------------------------------------
# Positions / trades
# ---------------------------------------------------------------------------

async def _positions_for(ag) -> list[dict]:
    rows = await ag.positions_view()
    for r in rows:
        r["strategy_id"] = ag.id
        r["strategy_name"] = ag.name
    return rows


@app.get("/api/positions")
async def api_positions(sid: str = "all"):
    if sid != "all":
        ag = arena.agent_by_id(sid)
        if ag is None:
            return JSONResponse({"error": "agent not found"}, status_code=404)
        return {"positions": await _positions_for(ag)}
    nested = await asyncio.gather(*[_positions_for(ag) for ag in arena.agents])
    return {"positions": [r for sub in nested for r in sub]}


@app.get("/api/trades")
async def api_trades(sid: str = "all"):
    if sid != "all":
        ag = arena.agent_by_id(sid)
        if ag is None:
            return JSONResponse({"error": "agent not found"}, status_code=404)
        return {"trades": ag.trades_view()}
    result = []
    for ag in arena.agents:
        for t in ag.trades_view():
            tc = dict(t)
            tc["strategy_id"] = ag.id
            tc["strategy_name"] = ag.name
            result.append(tc)
    result.sort(key=lambda x: x.get("opened_at", ""), reverse=True)
    return {"trades": result}


# ---------------------------------------------------------------------------
# Prices / candles (now driven by the dynamic universe)
# ---------------------------------------------------------------------------

@app.get("/api/prices")
async def api_prices():
    result: dict[str, dict] = {}
    for u in arena.last_universe[:12]:
        chg = u.get("pct_change")
        bias = "NEUTRAL"
        if isinstance(chg, (int, float)):
            bias = "BULLISH" if chg > 1 else "BEARISH" if chg < -1 else "NEUTRAL"
        result[u["base"]] = {"price": u.get("last"), "bias": bias}
    return {"prices": result}


@app.get("/api/universe")
async def api_universe():
    return {"universe": arena.last_universe, "size": arena.last_universe_size}


@app.get("/api/candles/{symbol}")
async def api_candles(symbol: str, tf: str = "4h", limit: int = 200):
    df = await market_service.get_ohlcv(symbol, tf, limit)
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


# ---------------------------------------------------------------------------
# Engine status
# ---------------------------------------------------------------------------

@app.get("/api/engine")
async def api_engine():
    merged_log = []
    for ag in arena.agents:
        for entry in ag.log[-40:]:
            ec = dict(entry)
            ec["msg"] = f"[{ag.name}] {ec['msg']}"
            merged_log.append(ec)
    merged_log.sort(key=lambda x: x.get("time", ""))
    return {
        "running": arena.running,
        "ai_available": arena._brain.ai_available,
        "gemini_keys": arena._brain.num_keys,
        "exchange": market_service.active_exchange,
        "universe_size": arena.last_universe_size,
        "interval_minutes": SETTINGS.cycle_minutes,
        "num_agents": len(arena.agents),
        "last_run": arena.last_run,
        "next_run": arena.next_run,
        "runs": arena.runs,
        "mode": "paper" if SETTINGS.is_paper else "live",
        "log": merged_log[-120:],
    }


@app.get("/api/config")
async def api_config():
    return {"settings": SETTINGS.to_public_dict(), "exchange": market_service.active_exchange}


# ---------------------------------------------------------------------------
# Arena
# ---------------------------------------------------------------------------

@app.get("/api/arena/strategies")
async def api_arena_strategies():
    ovs = await _all_overviews()
    return {"strategies": [_agent_strategy_block(o) for o in ovs]}


@app.get("/api/arena")
async def api_arena_overview():
    ovs = await _all_overviews()
    result = []
    for o in ovs:
        result.append({
            "strategy": _agent_strategy_block(o),
            "persona": o["persona"],
            "strategy_memo": o["strategy_memo"],
            "watchlist": o["watchlist"],
            "balance": o["balance"],
            "equity": o["equity"],
            "profit_purse": o["profit_purse"],
            "pnl": o["pnl"],
            "pnl_pct": o["pnl_pct"],
            "open_positions": o["open_positions"],
            "stats": o["stats"],
            "running": arena.running,
            "runs": o["runs"],
            "last_run": o["last_run"],
            "last_commentary": o["last_commentary"],
            "safety": o["safety"],
            "log": o["log"],
        })
    return {"arena": result}


@app.get("/api/arena/{sid}/positions")
async def api_arena_positions(sid: str):
    ag = arena.agent_by_id(sid)
    if ag is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    return {"positions": await ag.positions_view()}


@app.get("/api/arena/{sid}/trades")
async def api_arena_trades(sid: str):
    ag = arena.agent_by_id(sid)
    if ag is None:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    return {"trades": ag.trades_view()}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_ui():
    return FileResponse(_WEB_DIR / "index.html")
