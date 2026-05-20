"""FastAPI application."""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import structlog
import asyncio
import os
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
    fetch_current_price,
    fetch_overseas_price,
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
from jason_checks.decision_engine import run_decision_engine
from jason_checks.signal_journal import get_session_status

# Initialize logging
setup_logging()
logger = structlog.get_logger()

# Active market state
CURRENT_MARKET = "KR"
_PRICE_FETCH_LOCK = asyncio.Lock()


def _normalize_symbol(code: object) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        value = value[1:]
    if value.isdigit() and len(value) < 6:
        value = value.zfill(6)
    return value


def _theme_data_for_subscription(app: FastAPI) -> dict:
    theme_data = dict(getattr(app, "theme_data", {}) or {})
    alphaforge_theme = getattr(app, "alphaforge_theme_data", None)
    if alphaforge_theme:
        theme_data["AlphaForge"] = alphaforge_theme
    return theme_data


def _active_themes_for_subscription(app: FastAPI, active_themes: list[str]) -> list[str]:
    if getattr(app, "alphaforge_theme_data", None) and "AlphaForge" not in active_themes:
        return active_themes + ["AlphaForge"]
    return active_themes


def _build_watch_symbols(app: FastAPI) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for theme_data in (getattr(app, "theme_data", {}) or {}).values():
        for stock in theme_data.get("stocks", []):
            code = _normalize_symbol(stock.get("code", ""))
            if code and code not in seen:
                codes.append(code)
                seen.add(code)
    alphaforge_theme = getattr(app, "alphaforge_theme_data", {}) or {}
    for stock in alphaforge_theme.get("stocks", []):
        code = _normalize_symbol(stock.get("code", ""))
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


def _watch_symbol_names(app: FastAPI) -> dict[str, str]:
    names: dict[str, str] = {}
    for theme_data in (getattr(app, "theme_data", {}) or {}).values():
        for stock in theme_data.get("stocks", []):
            code = _normalize_symbol(stock.get("code", ""))
            if code:
                names.setdefault(code, str(stock.get("name", "")).strip())
    alphaforge_theme = getattr(app, "alphaforge_theme_data", {}) or {}
    for stock in alphaforge_theme.get("stocks", []):
        code = _normalize_symbol(stock.get("code", ""))
        if code:
            names.setdefault(code, str(stock.get("name", "")).strip())
    return names


def _apply_price_snapshot(code: str, price_data: dict | None) -> bool:
    code = _normalize_symbol(code)
    if not price_data:
        return False
    price = float(price_data.get("price") or 0)
    if price <= 0:
        return False
    update_data = {
        "price": price,
        "change_pct": float(price_data.get("change_pct") or 0),
        "cumulative_volume": int(price_data.get("volume") or 0),
        "cumulative_trading_value": int(price_data.get("trading_value") or 0),
        "market": price_data.get("market", "J"),
    }
    strength_raw = price_data.get("strength")
    strength = float(strength_raw or 0)
    if strength > 0:
        update_data["execution_strength"] = strength
    app_state.update_stock(code, **update_data)
    return True


async def _fetch_price_snapshot(code: str) -> dict | None:
    async with _PRICE_FETCH_LOCK:
        if CURRENT_MARKET == "KR":
            data = await fetch_current_price(code)
        else:
            data = await fetch_overseas_price(code)
        await asyncio.sleep(0.25)
        return data


async def _theme_quote_polling_loop(app):
    """Slowly hydrate the full dashboard universe via REST without expanding WS."""
    await asyncio.sleep(1)
    while True:
        try:
            symbols = list(getattr(app, "watch_symbols", []) or [])
            if not symbols:
                await asyncio.sleep(5)
                continue

            hydrated = 0
            missing_codes: list[str] = []
            symbol_names = _watch_symbol_names(app)
            started_at = datetime.now()
            estimated_sec = max(30, min(90, round(len(symbols) * 0.7)))
            app.quote_polling_status = {
                "total": len(symbols),
                "success": 0,
                "missing": 0,
                "in_progress": True,
                "started_at": started_at.isoformat(),
                "updated_at": started_at.isoformat(),
                "completed_at": "",
                "duration_sec": getattr(app, "last_quote_polling_duration_sec", 0),
                "estimated_sec": estimated_sec,
                "missing_symbols": [],
            }
            logger.info("theme_quote_polling_start", count=len(symbols), market=CURRENT_MARKET)
            for code in symbols:
                data = await _fetch_price_snapshot(code)
                if _apply_price_snapshot(code, data):
                    hydrated += 1
                else:
                    missing_codes.append(code)
                app.quote_polling_status.update({
                    "success": hydrated,
                    "missing": len(missing_codes),
                    "updated_at": datetime.now().isoformat(),
                })
            missing_preview = [
                {"code": code, "name": symbol_names.get(code, "")}
                for code in missing_codes[:20]
            ]
            duration_sec = round((datetime.now() - started_at).total_seconds(), 1)
            app.last_quote_polling_duration_sec = duration_sec
            app.quote_polling_status.update({
                "in_progress": False,
                "completed_at": datetime.now().isoformat(),
                "duration_sec": duration_sec,
                "estimated_sec": duration_sec,
                "missing_symbols": missing_preview,
            })
            logger.info(
                "theme_quote_polling_done",
                watch_symbols=len(symbols),
                quote_success=hydrated,
                price_missing=len(missing_codes),
                missing_symbols=missing_preview,
            )
        except Exception as e:
            logger.warning("theme_quote_polling_error", error=str(e))
        await asyncio.sleep(60)


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
            sub_theme_data = _theme_data_for_subscription(app)
            sub_active_themes = _active_themes_for_subscription(app, active_themes)
            active_codes = get_expanded_subscription_codes(sub_theme_data, sub_active_themes, max_codes=40)
            get_bridge().target_codes = list(active_codes)
            asyncio.create_task(get_ws().resubscribe(active_codes))
            
            if CURRENT_MARKET == "KR":
                codes_set = set()
                for tc in sub_active_themes:
                    for s in sub_theme_data.get(tc, {}).get("stocks", []):
                        codes_set.add(_normalize_symbol(s["code"]))
                for s in getattr(app, "surge_data", []):
                    if s.get("code"): codes_set.add(_normalize_symbol(s["code"]))
                
                unique_codes = list(codes_set)[:30]
                logger.info("polling_stocks_start", count=len(unique_codes))
                
                for code in unique_codes:
                    try:
                        tr = await fetch_stock_investor_trend(code)
                        pr = await _fetch_price_snapshot(code)
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
                        if str_val not in (None, "", 0, 0.0):
                            update_data["execution_strength"] = float(str_val)
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
                codes = []
                for tc in sub_active_themes:
                    for s in sub_theme_data.get(tc, {}).get("stocks", [])[:4]:
                        codes.append(s["code"])
                for code in codes[:24]:
                    try:
                        pr = await _fetch_price_snapshot(code)
                        if pr:
                            _apply_price_snapshot(code, pr)
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
        yaml_path = None
        try:
            filename = "themes.yaml" if market == "KR" else "themes_us.yaml"
            # Path(__file__).parent is web/, .parent.parent is jason_checks/, .parent.parent.parent is src/, .parent.parent.parent.parent is root
            yaml_path = Path(__file__).parent.parent.parent.parent / filename
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            app.theme_data = data.get("themes", {})
            app.theme_load_status = "ok"
            app.theme_load_reason = ""
            app.alphaforge_candidates_loaded = 0
            app.alphaforge_candidates_path = ""
            app.alphaforge_candidates_generated_at = ""
            app.alphaforge_theme_data = {}
            if market == "KR":
                alphaforge_candidates, alphaforge_meta = load_alphaforge_candidates_with_meta()
                app.alphaforge_candidates_path = alphaforge_meta.get("path", "")
                app.alphaforge_candidates_generated_at = alphaforge_meta.get("generated_at", "")
                if alphaforge_candidates:
                    app.alphaforge_theme_data = build_alphaforge_theme(alphaforge_candidates).get("AlphaForge", {})
                    app.alphaforge_candidates_loaded = len(alphaforge_candidates)
            app.stock_codes = get_all_stock_codes(_theme_data_for_subscription(app))
            app.theme_symbols = get_all_stock_codes(app.theme_data)
            app.watch_symbols = _build_watch_symbols(app)
            app.last_quote_polling_duration_sec = 0
            app.quote_polling_status = {
                "total": len(app.watch_symbols),
                "success": 0,
                "missing": 0,
                "in_progress": False,
                "started_at": "",
                "updated_at": "",
                "completed_at": "",
                "duration_sec": 0,
                "estimated_sec": max(30, min(90, round(len(app.watch_symbols) * 0.7))),
                "missing_symbols": [],
            }
            app.code_theme_map = build_code_to_theme_map(app.theme_data)
            logger.info(
                "market_switched",
                market=market,
                themes=len(app.theme_data),
                theme_symbols=len(app.theme_symbols),
                watch_symbols=len(app.watch_symbols),
                alphaforge_candidates=app.alphaforge_candidates_loaded,
                alphaforge_path=app.alphaforge_candidates_path,
            )
        except Exception as e:
            if not getattr(app, "theme_data", None):
                app.theme_data = {}
                app.stock_codes = []
                app.theme_symbols = []
                app.watch_symbols = []
                app.code_theme_map = {}
            app.theme_load_status = "error"
            app.theme_load_reason = str(e)
            app.alphaforge_theme_data = getattr(app, "alphaforge_theme_data", {})
            logger.warning("market_switch_failed_keep_existing_themes", error=str(e), path=str(yaml_path))

    # Initial load
    reload_market_themes("KR")

    @app.on_event("startup")
    async def startup():
        logger.info("app_startup")
        if not hasattr(app, "theme_data"):
            logger.error("startup_failed_no_theme_data")
            return
            
        initial_themes = list(app.theme_data.keys())[:4]
        sub_theme_data = _theme_data_for_subscription(app)
        sub_initial_themes = _active_themes_for_subscription(app, initial_themes)
        codes_to_sub = get_expanded_subscription_codes(sub_theme_data, sub_initial_themes, max_codes=40)
        await start_bridge(codes_to_sub)
        asyncio.create_task(_unified_polling_loop(app))
        asyncio.create_task(_theme_quote_polling_loop(app))
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
        sub_theme_data = _theme_data_for_subscription(app)
        sub_active_themes = _active_themes_for_subscription(app, active_themes)
        new_codes = get_expanded_subscription_codes(sub_theme_data, sub_active_themes, max_codes=40)
        
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
    async def get_themes(sort: str = "default", pinned: str = ""):
        quote_status = getattr(app, "quote_polling_status", {})
        supply_reason = ""
        if not os.getenv("KRX_ID") or not os.getenv("KRX_PW"):
            supply_reason = "KRX 로그인 정보 없음"
        if not app.theme_data:
            return {
                "themes": {},
                "market": CURRENT_MARKET,
                "ws_connected": app_state.ws_connected,
                "mode": get_settings().kis_mode,
                "theme_load_status": getattr(app, "theme_load_status", "empty"),
                "theme_load_reason": getattr(app, "theme_load_reason", "산업군 데이터 없음"),
                "alphaforge_candidates_loaded": getattr(app, "alphaforge_candidates_loaded", 0),
                "alphaforge_candidates_path": getattr(app, "alphaforge_candidates_path", ""),
                "alphaforge_candidates_generated_at": getattr(app, "alphaforge_candidates_generated_at", ""),
                "alphaforge_picks": [],
                "quote_polling": quote_status,
                "supply_data_reason": supply_reason,
            }
        pinned_list = [p for p in pinned.split(",") if p.strip()]
        if sort == "default":
            active_themes = list(app.theme_data.keys())
            active_themes = [p for p in pinned_list if p in active_themes] + [
                t for t in active_themes if t not in pinned_list
            ]
        else:
            active_themes = rank_themes(app.theme_data, top_n=len(app.theme_data), pinned=pinned_list)
        
        stock_ticks = {}
        for code, stock in app_state.stocks.items():
            norm_code = _normalize_symbol(code)
            stock_ticks[norm_code] = {
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
                "updated_at": stock.last_tick_ts.isoformat(),
            }

        themes_result = {}
        for theme_code in active_themes:
            theme_config = app.theme_data.get(theme_code, {})
            leaders = select_leaders(theme_code, stock_ticks, app.theme_data, sort_mode=sort)
            strength = compute_theme_strength(leaders, sort_mode=sort)
            avg_change_pct = compute_theme_avg_change_pct(leaders)

            leader_list = []
            for leader in leaders:
                code_norm = _normalize_symbol(leader["code"])
                ld = {
                    "code": code_norm,
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
                if code_norm in stock_ticks:
                    ld.update(stock_ticks[code_norm])
                leader_list.append(ld)

            themes_result[theme_code] = {
                "display_name": theme_config.get("display_name", theme_code),
                "strength": strength,
                "avg_change_pct": avg_change_pct,
                "leaders": leader_list,
            }

        alphaforge_picks = []
        alphaforge_theme = getattr(app, "alphaforge_theme_data", {}) or {}
        if alphaforge_theme:
            alpha_data = {"AlphaForge": alphaforge_theme}
            alpha_leaders = select_leaders("AlphaForge", stock_ticks, alpha_data, sort_mode=sort)
            for leader in alpha_leaders:
                code_norm = _normalize_symbol(leader["code"])
                ld = {
                    "code": code_norm,
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
                if code_norm in stock_ticks:
                    ld.update(stock_ticks[code_norm])
                alphaforge_picks.append(ld)

        theme_rows = [
            stock
            for theme in themes_result.values()
            for stock in theme.get("leaders", [])
        ]
        theme_row_total = len(theme_rows)
        theme_row_price_count = sum(1 for stock in theme_rows if float(stock.get("price") or 0) > 0)
        watch_symbols = [_normalize_symbol(code) for code in (getattr(app, "watch_symbols", []) or [])]
        app_state_price_count = sum(
            1
            for code in set(watch_symbols)
            if float(getattr(app_state.stocks.get(code), "price", 0) or 0) > 0
        )
        app_state_price_codes = {
            _normalize_symbol(code)
            for code, stock in app_state.stocks.items()
            if float(getattr(stock, "price", 0) or 0) > 0
        }
        missing_quote_rows = [
            {"code": stock.get("code"), "name": stock.get("name")}
            for stock in theme_rows
            if float(stock.get("price") or 0) <= 0
        ][:20]
        ui_merge_failed = [
            {"code": stock.get("code"), "name": stock.get("name")}
            for stock in theme_rows
            if stock.get("code") in app_state_price_codes and float(stock.get("price") or 0) <= 0
        ][:20]
        symbol_mismatch = [
            {"raw": code, "normalized": _normalize_symbol(code)}
            for code in app_state.stocks.keys()
            if code != _normalize_symbol(code)
        ][:20]
        display_quote_status = dict(quote_status or {})
        display_quote_status.update({
            "total": theme_row_total,
            "success": theme_row_price_count,
            "missing": max(theme_row_total - theme_row_price_count, 0),
            "watch_symbols": len(watch_symbols),
            "app_state_price_count": app_state_price_count,
            "theme_row_total": theme_row_total,
            "theme_row_price_count": theme_row_price_count,
            "symbol_mismatch_count": len(symbol_mismatch),
            "symbol_mismatch": symbol_mismatch,
            "missing_symbols": missing_quote_rows,
            "ui_merge_failed": ui_merge_failed,
        })
        now_ts = datetime.now().timestamp()
        last_diag_ts = getattr(app, "_last_theme_quote_diag_ts", 0)
        if now_ts - last_diag_ts > 15:
            app._last_theme_quote_diag_ts = now_ts
            logger.info(
                "theme_quote_display_diag",
                watch_symbols=len(watch_symbols),
                app_state_price_count=app_state_price_count,
                theme_row_total=theme_row_total,
                theme_row_price_count=theme_row_price_count,
                symbol_mismatch_count=len(symbol_mismatch),
                missing_quote_rows=missing_quote_rows,
                ui_merge_failed=ui_merge_failed,
            )

        # Decision Engine
        indices_snapshot = {
            code: {"change_pct": idx.change_pct, "price": idx.price}
            for code, idx in app_state.indices.items()
        }
        session_now = get_session_status(market=CURRENT_MARKET)
        decision_summary = run_decision_engine(
            alphaforge_picks=alphaforge_picks,
            themes=themes_result,
            indices=indices_snapshot,
            session=session_now,
        )
        # Enrich alphaforge_picks with decision fields
        decision_by_symbol = {r["symbol"]: r for r in decision_summary["results"]}
        for pick in alphaforge_picks:
            code_k = str(pick.get("code") or "")
            dec = decision_by_symbol.get(code_k, {})
            pick["decision_engine"] = dec
            pick["de_decision"] = dec.get("decision", "")
            pick["de_decision_display"] = dec.get("decision_display", "")
            pick["de_confidence"] = dec.get("confidence_score", 0)
            pick["de_data_confidence"] = dec.get("data_confidence", "")
            pick["de_action_reason"] = dec.get("action_reason", "")
            pick["de_no_buy_reason"] = dec.get("no_buy_reason", "")
            pick["de_entry_trigger"] = dec.get("entry_trigger", "")
            pick["de_invalidation"] = dec.get("invalidation_reason", "")
            pick["de_invalidation_reason"] = dec.get("invalidation_reason", "")
            pick["de_required_confirmations"] = dec.get("required_confirmations", [])
            pick["de_chase_risk"] = dec.get("chase_risk", False)
            pick["de_max_pct"] = dec.get("max_position_pct", 0)
            pick["de_stable"] = dec.get("stable", False)
            pick["de_setup_score"] = dec.get("setup_score", 0)
            pick["de_setup_label"] = dec.get("setup_label", "")
            pick["de_next_session_trigger"] = dec.get("next_session_trigger", "")
            pick["de_next_session_plan"] = dec.get("next_session_plan", "")
            pick["de_setup_reason"] = dec.get("setup_reason", "")
            pick["de_reason_codes"] = dec.get("reason_codes", [])
            pick["de_data_quality_flags"] = dec.get("data_quality_flags", [])
            pick["de_quote_age_sec"] = dec.get("quote_age_sec")
            pick["de_has_price"] = dec.get("has_price", False)
            pick["de_has_trading_value"] = dec.get("has_trading_value", False)
            pick["de_has_strength"] = dec.get("has_strength", False)
            pick["de_has_supply"] = dec.get("has_supply", False)
            pick["de_supply_timestamp"] = dec.get("supply_timestamp", "")

        return {
            "themes": themes_result,
            "market": CURRENT_MARKET,
            "ws_connected": app_state.ws_connected,
            "mode": get_settings().kis_mode,
            "theme_load_status": getattr(app, "theme_load_status", "ok"),
            "theme_load_reason": getattr(app, "theme_load_reason", ""),
            "alphaforge_candidates_loaded": getattr(app, "alphaforge_candidates_loaded", 0),
            "alphaforge_candidates_path": getattr(app, "alphaforge_candidates_path", ""),
            "alphaforge_candidates_generated_at": getattr(app, "alphaforge_candidates_generated_at", ""),
            "alphaforge_picks": alphaforge_picks,
            "quote_polling": display_quote_status,
            "supply_data_reason": supply_reason,
            "decision_counts": decision_summary["decision_counts"],
            "decision_session": session_now,
            "decision_market_gate": decision_summary["market_gate"],
            "decision_setup_top3": decision_summary.get("setup_top3", []),
            "decision_quality_summary": decision_summary.get("decision_quality_summary", {}),
            "data_confidence_counts": decision_summary.get("data_confidence_counts", {}),
            "reason_code_counts": decision_summary.get("reason_code_counts", {}),
            "market_gate_level": decision_summary.get("market_gate_level", ""),
            "market_gate_reason": decision_summary.get("market_gate_reason", ""),
            "market_gate_blocks_buy_now": decision_summary.get("market_gate_blocks_buy_now", False),
            "journal_status": decision_summary.get("journal_status", {}),
            "sector_audit_warnings": decision_summary.get("sector_audit_warnings", []),
            "duplicated_symbols": decision_summary.get("duplicated_symbols", []),
            "suspicious_sector_members": decision_summary.get("suspicious_sector_members", []),
        }

    @app.get("/api/decision-summary")
    async def get_decision_summary():
        """Decision Engine 요약 API."""
        alphaforge_theme = getattr(app, "alphaforge_theme_data", {}) or {}
        picks_raw = alphaforge_theme.get("stocks", [])
        stock_ticks = {
            _normalize_symbol(code): {
                "price": s.price, "change_pct": s.change_pct,
                "cumulative_trading_value": s.cumulative_trading_value,
                "strength": s.execution_strength,
                "supply_status": s.supply_status,
                "supply_updated_at": s.supply_updated_at,
                "updated_at": s.last_tick_ts.isoformat(),
            }
            for code, s in app_state.stocks.items()
        }
        enriched_picks = []
        for p in picks_raw:
            code = _normalize_symbol(p.get("code", ""))
            tick = stock_ticks.get(code, {})
            enriched_picks.append({**p, **tick, "code": code})

        indices_snapshot = {
            code: {"change_pct": idx.change_pct, "price": idx.price}
            for code, idx in app_state.indices.items()
        }
        session_now = get_session_status(market=CURRENT_MARKET)

        # Lightweight themes for sector gate
        themes_lite = {
            k: {"leaders": v.get("stocks", [])[:4], "avg_change_pct": 0}
            for k, v in (getattr(app, "theme_data", {}) or {}).items()
        }
        summary = run_decision_engine(
            alphaforge_picks=enriched_picks,
            themes=themes_lite,
            indices=indices_snapshot,
            session=session_now,
        )
        return summary

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
