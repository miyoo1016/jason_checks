"""FastAPI application."""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import FileResponse, JSONResponse
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
    KISClient,
)
from jason_checks.scanners.value_scanner import ValueScanner
from jason_checks.alphaforge_candidates import (
    build_alphaforge_theme,
    export_alphaforge_candidates,
    flatten_alphaforge_rows,
    load_alphaforge_candidates_with_meta,
)
from jason_checks.signal_journal import save_signal_journal

# Initialize logging
setup_logging()
logger = structlog.get_logger()

# Active market state
CURRENT_MARKET = "KR"


async def _unified_polling_loop(app):
    """Unified polling loop for indices and stocks to stay within rate limits (1-2 req/sec)."""
    while True:
        try:
            if not getattr(app, "theme_data", None):
                await asyncio.sleep(5)
                continue
                
            # 1. Update Market Indices (KOSPI/KOSDAQ)
            logger.info("polling_indices_start", market=CURRENT_MARKET)
            idx_data = await fetch_market_indices(market=CURRENT_MARKET)
            for code, info in idx_data.items():
                update_data = {
                    "name": info["name"], "price": info["price"],
                    "change_pct": info["change_pct"], "change_value": info["change_value"],
                }
                if CURRENT_MARKET == "KR":
                    inv = await fetch_index_investor_trend(code)
                    update_data.update({
                        "investor_foreigner": inv["foreigner"],
                        "investor_institution": inv["institution"],
                        "investor_individual": inv["individual"],
                    })
                    await asyncio.sleep(1.5)
                app_state.update_index(code, **update_data)
            
            # 2. Update Stock Investor Trends
            active_themes = rank_themes(app.theme_data, top_n=4, pinned=[])
            active_codes = get_expanded_subscription_codes(app.theme_data, active_themes, max_codes=40)
            asyncio.create_task(get_ws().resubscribe(active_codes))
            
            if CURRENT_MARKET == "KR":
                codes_set = set()
                for tc in active_themes:
                    for s in app.theme_data.get(tc, {}).get("stocks", [])[:4]:
                        codes_set.add(s["code"])
                for s in getattr(app, "surge_data", []):
                    if s.get("code"): codes_set.add(s["code"])
                
                unique_codes = list(codes_set)[:30]
                logger.info("polling_stocks_start", count=len(unique_codes))
                
                from jason_checks.kis_rest import fetch_current_price
                for code in unique_codes:
                    try:
                        tr = await fetch_stock_investor_trend(code)
                        pr = await fetch_current_price(code)
                        stock = app_state.stocks.get(code)
                        
                        update_data = {}
                        inv_f = tr["foreigner"] if tr["foreigner"] != 0 else (pr.get("foreigner_net_buy", 0) if pr else 0)
                        if inv_f != 0 or not stock or stock.investor_foreigner == 0:
                            update_data["investor_foreigner"] = inv_f
                        if tr["institution"] != 0 or not stock or stock.investor_institution == 0:
                            update_data["investor_institution"] = tr["institution"]
                        if tr["individual"] != 0 or not stock or stock.investor_individual == 0:
                            update_data["investor_individual"] = tr["individual"]
                            
                        str_val = tr.get("strength", (pr.get("strength", 0) if pr else 0))
                        if str_val != 0 or not stock or stock.execution_strength == 0:
                            update_data["execution_strength"] = str_val if str_val != 0 else 100.0
                        if pr:
                            update_data.update({
                                "price": pr["price"], "change_pct": pr["change_pct"],
                                "cumulative_trading_value": pr["trading_value"], "cumulative_volume": pr["volume"],
                            })
                        if update_data:
                            app_state.update_stock(code, **update_data)
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.warning("stock_poll_error", code=code, error=str(e))
                        await asyncio.sleep(1.5)
            else:
                # US Market
                from jason_checks.kis_rest import fetch_overseas_price
                codes = []
                for tc in active_themes:
                    for s in app.theme_data.get(tc, {}).get("stocks", [])[:4]:
                        codes.append(s["code"])
                for code in codes[:24]:
                    try:
                        pr = await fetch_overseas_price(code)
                        if pr:
                            app_state.update_stock(code, price=pr["price"], change_pct=pr["change_pct"], cum_volume_krw=pr["trading_value"])
                    except: pass
                    await asyncio.sleep(0.5)
            
            logger.info("polling_cycle_done")
        except Exception as e:
            logger.error("unified_loop_error", error=str(e))
        await asyncio.sleep(10)


async def _signal_journal_loop(app):
    """Save AlphaForge candidate snapshots once at startup, then every 5 minutes."""
    await asyncio.sleep(2)
    while True:
        save_signal_journal(getattr(app, "theme_data", {}), market=CURRENT_MARKET)
        await asyncio.sleep(300)


def create_app() -> FastAPI:
    """Create and configure FastAPI app."""
    app = FastAPI(
        title="CHECKS Terminal",
        description="Real-time stock trading (KR/US)",
        version="0.3.0",
    )

    static_path = Path(__file__).parent.parent / "static"
    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    def reload_market_themes(market: str):
        global CURRENT_MARKET
        CURRENT_MARKET = market
        try:
            filename = "themes.yaml" if market == "KR" else "themes_us.yaml"
            # Path(__file__).parent is web/, .parent.parent is jason_checks/, .parent.parent.parent is src/, .parent.parent.parent.parent is root
            yaml_path = Path(__file__).parent.parent.parent.parent / filename
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            app.theme_data = data.get("themes", {})
            app.alphaforge_candidates_loaded = 0
            app.alphaforge_candidates_path = ""
            app.alphaforge_candidates_generated_at = ""
            if market == "KR":
                alphaforge_candidates, alphaforge_meta = load_alphaforge_candidates_with_meta()
                app.alphaforge_candidates_path = alphaforge_meta.get("path", "")
                app.alphaforge_candidates_generated_at = alphaforge_meta.get("generated_at", "")
                if alphaforge_candidates:
                    app.theme_data = build_alphaforge_theme(alphaforge_candidates)
                    app.alphaforge_candidates_loaded = len(alphaforge_candidates)
            app.stock_codes = get_all_stock_codes(app.theme_data)
            app.code_theme_map = build_code_to_theme_map(app.theme_data)
            logger.info(
                "market_switched",
                market=market,
                themes=len(app.theme_data),
                alphaforge_candidates=app.alphaforge_candidates_loaded,
                alphaforge_path=app.alphaforge_candidates_path,
            )
        except Exception as e:
            logger.error("market_switch_failed", error=str(e), path=str(yaml_path))

    # Initial load
    reload_market_themes("KR")

    @app.on_event("startup")
    async def startup():
        logger.info("app_startup")
        if not hasattr(app, "theme_data"):
            logger.error("startup_failed_no_theme_data")
            return
            
        initial_themes = list(app.theme_data.keys())[:4]
        codes_to_sub = get_expanded_subscription_codes(app.theme_data, initial_themes, max_codes=40)
        await start_bridge(codes_to_sub)
        asyncio.create_task(_unified_polling_loop(app))
        asyncio.create_task(_signal_journal_loop(app))

    @app.on_event("shutdown")
    async def shutdown():
        await get_bridge().stop()

    @app.get("/")
    async def root():
        template_path = Path(__file__).parent / "templates" / "index.html"
        return FileResponse(template_path)

    @app.post("/api/market")
    async def switch_market(payload: dict = Body(...)):
        """Switch active market and re-subscribe WebSocket."""
        market = payload.get("market")
        if market not in ["KR", "US"]:
            return JSONResponse({"error": "Invalid market"}, status_code=400)
            
        logger.info("request_market_switch", target=market)
        reload_market_themes(market)
        
        # Immediate resubscribe
        active_themes = list(app.theme_data.keys())[:4]
        new_codes = get_expanded_subscription_codes(app.theme_data, active_themes, max_codes=40)
        
        # Clear old data to avoid confusion
        app_state.stocks.clear()
        app_state.indices.clear()
        
        # Stop and restart bridge with new codes
        bridge = get_bridge()
        await bridge.stop()
        await start_bridge(new_codes)
        
        return {"status": "ok", "market": market, "codes": len(new_codes)}
        
        # Tell WebSocket to flush and re-sub
        ws = get_ws()
        if ws.connected:
            await ws.flush_all(list(ws.subscribed_codes))
            await ws.subscribe(new_codes)
            
        return {"status": "ok", "market": market, "subscribed": len(new_codes)}

    @app.get("/api/state")
    async def get_state():
        return {
            "market": CURRENT_MARKET,
            "ws_connected": app_state.ws_connected,
            "mode": get_settings().kis_mode,
        }

    @app.post("/api/signal-journal")
    async def save_signal_journal_endpoint():
        return save_signal_journal(getattr(app, "theme_data", {}), market=CURRENT_MARKET)

    @app.get("/api/themes")
    async def get_themes(sort: str = "strength", pinned: str = ""):
        if not app.theme_data: return {"themes": {}}
        pinned_list = [p for p in pinned.split(",") if p.strip()]
        active_themes = rank_themes(app.theme_data, top_n=4, pinned=pinned_list)
        
        stock_ticks = {}
        for code, stock in app_state.stocks.items():
            stock_ticks[code] = {
                "price": stock.price,
                "change_pct": stock.change_pct,
                "cumulative_volume": stock.cumulative_volume,
                "cumulative_trading_value": stock.cumulative_trading_value,
                "strength": stock.execution_strength,
                "investor_foreigner": stock.investor_foreigner,
                "investor_institution": stock.investor_institution,
                "investor_individual": stock.investor_individual,
                "foreign_flow": stock.foreign_flow,
                "institution_flow": stock.institution_flow,
                "individual_flow": stock.individual_flow,
                "supply_status": stock.supply_status,
                "supply_updated_at": stock.supply_updated_at,
            }

        themes_result = {}
        for theme_code in active_themes:
            theme_config = app.theme_data.get(theme_code, {})
            leaders = select_leaders(theme_code, stock_ticks, app.theme_data, sort_mode=sort)
            strength = compute_theme_strength(leaders, sort_mode=sort)
            avg_change_pct = compute_theme_avg_change_pct(leaders)

            leader_list = []
            for leader in leaders:
                ld = {
                    "code": leader["code"],
                    "name": leader["name"],
                    "score": leader["score"],
                    "tier": leader.get("tier", ""),
                    "alert_type": leader.get("alert_type", ""),
                    "rs": leader.get("rs", ""),
                    "vcp_status": leader.get("vcp_status", ""),
                    "box_upper_price": leader.get("box_upper_price", ""),
                    "short_swing_score": leader.get("short_swing_score", "-"),
                    "position_swing_score": leader.get("position_swing_score", "-"),
                    "horizon_label": leader.get("horizon_label", "-"),
                    "short_reasons": leader.get("short_reasons", "-"),
                    "position_reasons": leader.get("position_reasons", "-"),
                    "foreign_flow": None,
                    "institution_flow": None,
                    "individual_flow": None,
                    "supply_status": "DATA_NA",
                    "supply_updated_at": "",
                }
                if leader["code"] in stock_ticks: ld.update(stock_ticks[leader["code"]])
                leader_list.append(ld)

            themes_result[theme_code] = {
                "display_name": theme_config.get("display_name", theme_code),
                "strength": strength,
                "avg_change_pct": avg_change_pct,
                "leaders": leader_list,
            }

        return {
            "themes": themes_result,
            "market": CURRENT_MARKET,
            "ws_connected": app_state.ws_connected,
            "mode": get_settings().kis_mode,
            "alphaforge_candidates_loaded": getattr(app, "alphaforge_candidates_loaded", 0),
            "alphaforge_candidates_path": getattr(app, "alphaforge_candidates_path", ""),
            "alphaforge_candidates_generated_at": getattr(app, "alphaforge_candidates_generated_at", ""),
        }

    @app.get("/api/indices")
    async def get_indices():
        result = {}
        for code, idx in app_state.indices.items():
            result[code] = {
                "name": idx.name, "price": idx.price, "change_pct": idx.change_pct,
                "investor_foreigner": idx.investor_foreigner,
                "investor_institution": idx.investor_institution,
                "investor_individual": idx.investor_individual,
            }
        return {"indices": result}

    @app.get("/api/surges")
    async def get_surges(sort: str = "trading_value", limit: int = 10):
        """Get top movers / high volume stocks."""
        from jason_checks.kis_rest import fetch_top_movers_cached
        
        # Mapping frontend sort to KIS sort
        # 0=상승률, 1=하락률
        kis_sort = "0" if sort == "change_pct" else "0" # Defaulting to volume/change for now
        
        # Currently fetch_top_movers only supports KR, need US version later
        if CURRENT_MARKET == "KR":
            data = await fetch_top_movers_cached(market="J", sort=kis_sort, limit=limit)
        else:
            from jason_checks.kis_rest import fetch_us_top_movers
            data = await fetch_us_top_movers(limit=limit)
            
        return {"surges": data, "market": CURRENT_MARKET}
        
    @app.get("/api/scan")
    async def run_scan(max_symbols: int = Query(30, ge=1, le=500)):
        """Run value investment scanners."""
        from jason_checks.kis_rest import get_rest_client
        scanner = ValueScanner(get_rest_client())
        
        results_a = await scanner.scan_park_sung_jin()
        results_b = await scanner.scan_byun_doo_shik()
        results_c = await scanner.scan_seohee_father()
        export_source = flatten_alphaforge_rows([results_a, results_b, results_c])[:max_symbols]
        alphaforge_export_stats = export_alphaforge_candidates(export_source)
        
        return {
            "results": {
                "a": results_a,
                "b": results_b,
                "c": results_c
            },
            "alphaforge_export_count": alphaforge_export_stats["exported_count"],
            "alphaforge_export_stats": alphaforge_export_stats,
        }

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        bridge = get_bridge()
        await bridge.add_client(websocket)
        try:
            while True: await websocket.receive_text()
        except WebSocketDisconnect:
            await bridge.remove_client(websocket)

    return app
