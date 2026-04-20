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

# Initialize logging
setup_logging()
logger = structlog.get_logger()


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
        """Get current theme state with top 4 stocks each (dynamically ranked).

        Args:
            sort: Sorting mode - "strength" | "change_pct" | "trading_value"
            pinned: Comma-separated theme codes to pin (always included)
        """
        if not app.theme_data:
            return {"themes": {}, "ws_connected": app_state.ws_connected}

        # Validate sort mode
        if sort not in ("strength", "change_pct", "trading_value"):
            sort = "strength"

        # Parse pinned themes
        pinned_list = [p for p in pinned.split(",") if p.strip()]

        # Rank themes dynamically (pinned first, then top 4)
        active_themes = rank_themes(app.theme_data, top_n=4, pinned=pinned_list)

        # Re-subscribe WebSocket to active theme stocks (background task)
        active_codes = get_expanded_subscription_codes(
            app.theme_data, active_themes, max_codes=40
        )
        asyncio.create_task(get_ws().resubscribe(active_codes))

        # Build current tick dict from app_state
        stock_ticks = {}
        for code, stock in app_state.stocks.items():
            stock_ticks[code] = {
                "price": stock.price,
                "change_pct": stock.change_pct,
                "cumulative_volume": stock.cum_volume_krw,
                "cumulative_trading_value": stock.cumulative_trading_value,
                "strength": stock.execution_strength,
                "timestamp": stock.last_tick_ts.strftime("%H%M%S"),
            }

        # Build response ONLY for active themes
        themes_result = {}
        for theme_code in active_themes:
            theme_config = app.theme_data.get(theme_code, {})
            leaders = select_leaders(theme_code, stock_ticks, app.theme_data, sort_mode=sort)
            strength = compute_theme_strength(leaders, sort_mode=sort)
            avg_change_pct = compute_theme_avg_change_pct(leaders)

            # Build leader list with full tick data
            leader_list = []
            for leader in leaders:
                leader_dict = {
                    "code": leader["code"],
                    "name": leader["name"],
                    "score": leader["score"],
                }
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

    # Cache for active leader codes to avoid redundant calculation across multiple client requests
    _active_leaders_cache = {"codes": set(), "updated_at": None}
    _LEADERS_CACHE_TTL = 1.5  # seconds

    @app.get("/api/surges")
    async def get_surges(sort: str = "change_pct", limit: int = 10):
        """Return top N individual surge stocks NOT in the active theme grid."""
        from jason_checks.kis_rest import (
            fetch_top_movers_cached,
            filter_non_theme_stocks,
        )

        if sort not in ("change_pct", "strength", "trading_value"):
            sort = "change_pct"
        limit = max(1, min(limit, 30))

        # Step 1: Get active leader codes (with 1.5s cache)
        now = datetime.now()
        if (
            not _active_leaders_cache["updated_at"]
            or (now - _active_leaders_cache["updated_at"]).total_seconds() > _LEADERS_CACHE_TTL
        ):
            active_leader_codes = set()
            active_themes = rank_themes(app.theme_data, top_n=4, pinned=[])
            
            # Build minimal tick dict
            stock_ticks = {
                code: {
                    "price": s.price,
                    "change_pct": s.change_pct,
                    "cumulative_trading_value": s.cumulative_trading_value,
                    "strength": s.execution_strength,
                }
                for code, s in app_state.stocks.items()
            }

            for theme_code in active_themes:
                leaders = select_leaders(
                    theme_code, stock_ticks, app.theme_data, sort_mode="strength"
                )
                for leader in leaders:
                    active_leader_codes.add(leader["code"])
            
            _active_leaders_cache["codes"] = active_leader_codes
            _active_leaders_cache["updated_at"] = now
        else:
            active_leader_codes = _active_leaders_cache["codes"]

        code_map = getattr(app, "code_theme_map", {})
        settings = get_settings()

        # Step 2: Try KIS REST API (ONLY in LIVE mode, skip in PAPER to avoid known 403 errors)
        if settings.kis_mode == "live":
            try:
                rest_stocks = await fetch_top_movers_cached(
                    market="J", sort="0", limit=50
                )
                if rest_stocks:
                    filtered = [s for s in rest_stocks if s["code"] not in active_leader_codes]
                    
                    if sort == "change_pct":
                        filtered.sort(key=lambda s: s.get("change_pct", 0), reverse=True)
                    elif sort == "strength":
                        filtered.sort(key=lambda s: s.get("strength", 0), reverse=True)
                    elif sort == "trading_value":
                        filtered.sort(key=lambda s: s.get("cumulative_trading_value", 0) or s.get("trading_value", 0), reverse=True)

                    return {
                        "surges": filtered[:limit],
                        "sort": sort,
                        "total_tracked": len(filtered),
                        "source": "rest_api",
                    }
            except Exception as e:
                logger.warning("rest_surge_failed", error=str(e))

        # Step 3: Fallback (or Primary for Paper) — filter from WebSocket pool
        fallback = filter_non_theme_stocks(
            app_state.stocks, active_leader_codes, code_map
        )

        if sort == "change_pct":
            fallback.sort(key=lambda s: s.get("change_pct", 0), reverse=True)
        elif sort == "strength":
            fallback.sort(key=lambda s: s.get("strength", 0), reverse=True)
        elif sort == "trading_value":
            fallback.sort(key=lambda s: s.get("cumulative_trading_value", 0), reverse=True)

        return {
            "surges": fallback[:limit],
            "sort": sort,
            "total_tracked": len(fallback),
            "source": "ws_pool",
        }


    # WebSocket endpoint
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for browser clients."""
        await websocket.accept()
        bridge = get_bridge()
        await bridge.add_client(websocket)

        try:
            # Send initial state
            import json
            initial = {
                "type": "init",
                "mode": get_settings().kis_mode,
                "ws_connected": app_state.ws_connected,
            }
            await websocket.send_text(json.dumps(initial))

            # Keep connection alive
            while True:
                msg = await websocket.receive_text()
                logger.debug("ws_client_message", msg=msg[:50])

        except WebSocketDisconnect:
            await bridge.remove_client(websocket)
            logger.info("ws_client_disconnected")
        except Exception as e:
            logger.error("ws_error", error=str(e))
            await bridge.remove_client(websocket)

    return app
