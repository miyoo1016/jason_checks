"""FastAPI application."""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import structlog
import asyncio
from datetime import datetime

from jason_checks.web.ws_bridge import get_bridge, start_bridge
from jason_checks.config import get_settings
from jason_checks.state import app_state
from jason_checks.logging_config import setup_logging
from jason_checks.theme_manager import (
    load_themes,
    select_leaders,
    compute_theme_strength,
    compute_theme_avg_change_pct,
    get_all_stock_codes,
)
from jason_checks.theme_ranker import (
    rank_themes,
    get_active_stock_codes,
    get_expanded_subscription_codes,
    build_code_to_theme_map,
)
from jason_checks.kis_ws import get_ws
from jason_checks.kis_rest import (
    fetch_market_indices,
    fetch_stock_investor_trend,
    fetch_index_investor_trend,
)

# Initialize logging
setup_logging()
logger = structlog.get_logger()


async def _market_indices_loop():
    """Poll KOSPI/KOSDAQ index prices every 5s."""
    while True:
        try:
            data = await fetch_market_indices()
            for code, info in data.items():
                app_state.update_index(
                    code,
                    name=info["name"],
                    price=info["price"],
                    change_pct=info["change_pct"],
                    change_value=info["change_value"],
                )
        except Exception as e:
            logger.warning("market_indices_loop_error", error=str(e))
        await asyncio.sleep(5)


async def _index_investor_loop():
    """Poll KOSPI/KOSDAQ investor trend every 10s."""
    while True:
        try:
            for code in ("0001", "1001"):
                tr = await fetch_index_investor_trend(code)
                app_state.update_index(
                    code,
                    investor_foreigner=tr["foreigner"],
                    investor_institution=tr["institution"],
                    investor_individual=tr["individual"],
                )
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning("index_investor_loop_error", error=str(e))
        await asyncio.sleep(10)


async def _stock_investor_loop(app):
    """Poll per-stock investor trend every 30s for active grid stocks only."""
    while True:
        try:
            # 활성 4개 테마의 리더 종목만 (과다한 호출 방지)
            active_themes = list(getattr(app, "theme_data", {}).keys())[:4]
            codes: list[str] = []
            for tc in active_themes:
                stocks = app.theme_data.get(tc, {}).get("stocks", [])[:4]
                codes.extend(s["code"] for s in stocks)
            # 최대 16개 한정
            for code in codes[:16]:
                tr = await fetch_stock_investor_trend(code)
                app_state.update_stock(
                    code,
                    investor_foreigner=tr["foreigner"],
                    investor_institution=tr["institution"],
                    investor_individual=tr["individual"],
                )
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning("stock_investor_loop_error", error=str(e))
        await asyncio.sleep(30)


def create_app() -> FastAPI:
    """Create and configure FastAPI app."""
    app = FastAPI(
        title="CHECKS Terminal",
        description="Real-time stock trading TUI (web version)",
        version="0.2.0",
    )

    # Static files
    static_path = Path(__file__).parent.parent / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    # Load themes at startup
    try:
        app.theme_data = load_themes()
        app.stock_codes = get_all_stock_codes(app.theme_data)
        app.code_theme_map = build_code_to_theme_map(app.theme_data)
        logger.info("themes_loaded", count=len(app.theme_data), stocks=len(app.stock_codes))
    except Exception as e:
        logger.error("themes_load_failed", error=str(e))
        app.theme_data = {}
        app.stock_codes = []

    # Startup event
    @app.on_event("startup")
    async def startup():
        logger.info("app_startup")
        settings = get_settings()
        logger.info("mode", mode=settings.kis_mode)
        try:
            initial_themes = list(app.theme_data.keys())[:4]
            # Use expanded subscription: active themes + 1 per inactive theme
            codes_to_sub = get_expanded_subscription_codes(
                app.theme_data, initial_themes, max_codes=40
            )
            codes_to_sub = codes_to_sub if codes_to_sub else ["005930"]
            logger.info("initial_subscription", count=len(codes_to_sub))
            await start_bridge(codes_to_sub)
        except Exception as e:
            logger.error("startup_failed", error=str(e))

        # Background: market indices every 5s
        asyncio.create_task(_market_indices_loop())
        # Background: investor trends every 30s (stocks) and 10s (indices)
        asyncio.create_task(_index_investor_loop())
        asyncio.create_task(_stock_investor_loop(app))

    # Shutdown event
    @app.on_event("shutdown")
    async def shutdown():
        logger.info("app_shutdown")
        await get_bridge().stop()

    # HTTP endpoints
    @app.get("/")
    async def root():
        """Serve main page."""
        template_path = Path(__file__).parent / "templates" / "index.html"
        if template_path.exists():
            return FileResponse(template_path, media_type="text/html")
        return {"message": "CHECKS Terminal (Phase 2)"}

    @app.get("/api/state")
    async def get_state():
        """Get current app state."""
        return {
            "stocks": {
                code: {
                    "price": stock.price,
                    "change_pct": stock.change_pct,
                    "cumulative_volume": stock.cum_volume_krw,
                    "strength": stock.execution_strength,
                    "last_tick_ts": stock.last_tick_ts.isoformat(),
                }
                for code, stock in app_state.stocks.items()
            },
            "ws_connected": app_state.ws_connected,
            "mode": get_settings().kis_mode,
        }

    @app.get("/api/themes")
    async def get_themes(sort: str = "strength", pinned: str = ""):
        """Get current theme state with top 4 stocks each (dynamically ranked)."""
        if not app.theme_data:
            return {"themes": {}, "ws_connected": app_state.ws_connected}

        if sort not in ("strength", "change_pct", "trading_value"):
            sort = "strength"

        pinned_list = [p for p in pinned.split(",") if p.strip()]
        active_themes = rank_themes(app.theme_data, top_n=4, pinned=pinned_list)

        active_codes = get_expanded_subscription_codes(app.theme_data, active_themes, max_codes=40)
        asyncio.create_task(get_ws().resubscribe(active_codes))

        stock_ticks = {}
        for code, stock in app_state.stocks.items():
            stock_ticks[code] = {
                "price": stock.price,
                "change_pct": stock.change_pct,
                "cumulative_volume": stock.cum_volume_krw,
                "cumulative_trading_value": stock.cumulative_trading_value,
                "strength": stock.execution_strength,
                "timestamp": stock.last_tick_ts.strftime("%H%M%S"),
                "investor_foreigner": stock.investor_foreigner,
                "investor_institution": stock.investor_institution,
                "investor_individual": stock.investor_individual,
            }

        # Data Supplement: If any active grid stock is missing data, fetch it using standard API
        from jason_checks.kis_rest import fetch_current_price
        now_ts = datetime.now()
        for theme_code in active_themes:
            theme_stocks = app.theme_data.get(theme_code, {}).get("stocks", [])
            for stock in theme_stocks[:4]:
                code = stock["code"]
                if code not in stock_ticks or stock_ticks[code].get("price", 0) == 0:
                    try:
                        await asyncio.sleep(0.1) # Avoid rate limit
                        data = await fetch_current_price(code)
                        if data:
                            stock_ticks[code] = {
                                "price": data["price"],
                                "change_pct": data["change_pct"],
                                "cumulative_volume": data["volume"],
                                "cumulative_trading_value": data.get("trading_value", 0),
                                "strength": 100.0,
                                "timestamp": now_ts.strftime("%H%M%S"),
                                "investor_foreigner": 0,
                                "investor_institution": 0,
                                "investor_individual": 0,
                            }
                            # Update app_state so it persists
                            app_state.update_stock(
                                code,
                                price=data["price"],
                                change_pct=data["change_pct"],
                                cumulative_trading_value=data.get("trading_value", 0),
                            )
                    except Exception as e:
                        logger.error("individual_fallback_failed", code=code, error=str(e))

        themes_result = {}
        for theme_code in active_themes:
            theme_config = app.theme_data.get(theme_code, {})
            leaders = select_leaders(theme_code, stock_ticks, app.theme_data, sort_mode=sort)
            strength = compute_theme_strength(leaders, sort_mode=sort)
            avg_change_pct = compute_theme_avg_change_pct(leaders)

            leader_list = []
            for leader in leaders:
                leader_dict = {"code": leader["code"], "name": leader["name"], "score": leader["score"]}
                if leader["code"] in stock_ticks:
                    leader_dict.update(stock_ticks[leader["code"]])
                leader_list.append(leader_dict)

            themes_result[theme_code] = {
                "display_name": theme_config.get("display_name", theme_code),
                "description": theme_config.get("description", ""),
                "strength": strength,
                "avg_change_pct": avg_change_pct,
                "leaders": leader_list,
            }

        return {
            "themes": themes_result,
            "ws_connected": app_state.ws_connected,
            "mode": get_settings().kis_mode,
        }

    _active_leaders_cache = {"codes": set(), "updated_at": None}
    _LEADERS_CACHE_TTL = 1.5

    @app.get("/api/surges")
    async def get_surges(sort: str = "change_pct", limit: int = 10):
        """Return top N individual surge stocks (NXT-aware)."""
        from jason_checks.kis_rest import fetch_top_movers_cached, filter_non_theme_stocks

        now = datetime.now()
        current_min = now.hour * 100 + now.minute
        is_market_closed = not (900 <= current_min <= 1530)  # KRX 정규장 종료
        is_nxt_hour = now.weekday() < 5 and (
            (800 <= current_min < 850) or (1530 <= current_min < 2000)
        )

        active_leader_codes = _active_leaders_cache["codes"]
        settings = get_settings()

        # 정규장 시간: top_movers (KRX 기준 등락률 순위)
        if not is_market_closed and settings.kis_mode == "live":
            try:
                rest_stocks = await fetch_top_movers_cached(market="J", sort="0", limit=50)
                if rest_stocks:
                    filtered = [s for s in rest_stocks if s["code"] not in active_leader_codes]
                    return {"surges": filtered[:limit], "sort": sort, "source": "rest_api"}
            except Exception as e:
                logger.warning("rest_surge_failed", error=str(e))

        # NXT 시간: WebSocket pool에서 가져오기 (ranking API가 NXT를 지원 안 함)
        fallback = filter_non_theme_stocks(
            app_state.stocks, active_leader_codes, getattr(app, "code_theme_map", {})
        )
        # Sort
        if sort == "change_pct":
            fallback.sort(key=lambda s: s.get("change_pct", 0), reverse=True)
        elif sort == "strength":
            fallback.sort(key=lambda s: s.get("strength", 0), reverse=True)
        elif sort == "trading_value":
            fallback.sort(key=lambda s: s.get("cumulative_trading_value", 0), reverse=True)

        return {
            "surges": fallback[:limit],
            "sort": sort,
            "source": "ws_pool",
            "is_nxt": is_nxt_hour,
        }

    @app.get("/api/indices")
    async def get_indices():
        """Return KOSPI/KOSDAQ snapshot with investor trends."""
        result = {}
        for code, idx in app_state.indices.items():
            result[code] = {
                "name": idx.name,
                "price": idx.price,
                "change_pct": idx.change_pct,
                "change_value": idx.change_value,
                "investor_foreigner": idx.investor_foreigner,
                "investor_institution": idx.investor_institution,
                "investor_individual": idx.investor_individual,
            }
        return {"indices": result}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for browser clients."""
        await websocket.accept()
        bridge = get_bridge()
        await bridge.add_client(websocket)
        try:
            import json
            await websocket.send_text(json.dumps({"type": "init", "mode": get_settings().kis_mode, "ws_connected": app_state.ws_connected}))
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await bridge.remove_client(websocket)
        except Exception as e:
            logger.error("ws_error", error=str(e))
            await bridge.remove_client(websocket)

    return app
