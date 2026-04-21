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
    bridge = get_bridge()
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
                # 즉시 브로드캐스트
                await bridge.broadcast_index_update(code, {
                    "name": info["name"],
                    "price": info["price"],
                    "change_pct": info["change_pct"],
                    "change_value": info["change_value"],
                    "investor_foreigner": app_state.indices.get(code).investor_foreigner if code in app_state.indices else 0,
                    "investor_institution": app_state.indices.get(code).investor_institution if code in app_state.indices else 0,
                    "investor_individual": app_state.indices.get(code).investor_individual if code in app_state.indices else 0,
                })
        except Exception as e:
            logger.warning("market_indices_loop_error", error=str(e))
        await asyncio.sleep(5)


async def _index_investor_loop():
    """Poll KOSPI/KOSDAQ investor trend every 30s.
    
    If API fails, aggregate from major stocks.
    """
    bridge = get_bridge()
    KOSPI_MAJORS = ["005930", "000660", "207940", "373220", "005380"]
    KOSDAQ_MAJORS = ["247540", "196170", "028300", "058470", "086520"]

    while True:
        try:
            for idx_code, majors in (("0001", KOSPI_MAJORS), ("1001", KOSDAQ_MAJORS)):
                tr = await fetch_index_investor_trend(idx_code)
                
                # If API returns 0 or fails, fallback to major aggregation
                if tr["foreigner"] == 0 and tr["institution"] == 0:
                    logger.info("index_inv_api_empty_falling_back", code=idx_code)
                    agg = {"foreigner": 0, "institution": 0, "individual": 0}
                    for code in majors:
                        mtr = await fetch_stock_investor_trend(code)
                        for k in agg:
                            agg[k] += mtr.get(k, 0)
                        await asyncio.sleep(0.2)
                    tr = agg

                app_state.update_index(
                    idx_code,
                    investor_foreigner=tr["foreigner"],
                    investor_institution=tr["institution"],
                    investor_individual=tr["individual"],
                )
                # 즉시 브로드캐스트
                idx = app_state.indices.get(idx_code)
                await bridge.broadcast_index_update(idx_code, {
                    "name": idx.name,
                    "price": idx.price,
                    "change_pct": idx.change_pct,
                    "change_value": idx.change_value,
                    "investor_foreigner": tr["foreigner"],
                    "investor_institution": tr["institution"],
                    "investor_individual": tr["individual"],
                })
                await asyncio.sleep(1.0)
        except Exception as e:
            logger.warning("index_investor_loop_error", error=str(e))
        await asyncio.sleep(30) # Relaxed to 30s as KIS limit is strict


async def _stock_investor_loop(app):
    """Poll per-stock investor trend every 15s for visible stocks (high priority)."""
    bridge = get_bridge()
    while True:
        try:
            # 우선순위 1: 현재 화면에 보이는 종목
            # 우선순위 2: 활성 테마의 리더들
            priority_codes = list(app_state.visible_codes)
            if not priority_codes:
                for tc in list(app.theme_data.keys())[:4]:
                    stocks = app.theme_data.get(tc, {}).get("stocks", [])[:4]
                    priority_codes.extend(s["code"] for s in stocks)
            
            unique_codes = list(dict.fromkeys(priority_codes))[:20]
            for code in unique_codes:
                tr = await fetch_stock_investor_trend(code)
                if tr:  # ★ CHANGED: tr이 None이 아닐 때만(정상 응답일 때만)
                    app_state.update_stock(
                        code,
                        investor_foreigner=tr["foreigner"],
                        investor_institution=tr["institution"],
                        investor_individual=tr["individual"],
                    )
                    await bridge.broadcast_investor_update(code, tr)
                # ★ CHANGED: 한투 수급 API 방화벽을 절대 피하는 안전한 딜레이(1.5초)
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.warning("stock_investor_loop_error", error=str(e))
        await asyncio.sleep(600)  # ★ CHANGED: 10초 -> 10분(600초) 단위로 대폭 완화


async def _baseline_price_loop(app):
    """
    천천히(1초당 1개씩) REST API로 16개 종목의 기준 가격(NXT 포함)을 보충합니다.
    웹소켓은 '거래가 발생할 때만' 틱을 주기 때문에, 애프터장에 거래가 없는 종목은
    가격이 갱신되지 않는 문제를 해결하는 완벽한 하이브리드 안전망입니다.
    """
    from jason_checks.kis_rest import fetch_current_price
    import json
    from datetime import datetime as _dt
    
    bridge = get_bridge()
    while True:
        try:
            target_codes = list(app_state.visible_codes)
            for code in target_codes:
                try:
                    data = await fetch_current_price(code)
                    if data:
                        st = app_state.stocks.get(code)
                        # REST 데이터로 상태 업데이트
                        app_state.update_stock(
                            code,
                            price=data["price"],
                            change_pct=data["change_pct"],
                            cumulative_trading_value=data.get("trading_value", 0),
                            market=data.get("market", "J")
                        )
                        # 프론트엔드 브로드캐스트
                        strength = data.get("strength", 0) or (st.execution_strength if st else 0.0)
                        msg = {
                            "type": "tick",
                            "code": code,
                            "price": data["price"],
                            "change_pct": data["change_pct"],
                            "market": data.get("market", "J"),
                            "strength": strength,
                            "timestamp": _dt.now().strftime("%H%M%S"),
                        }
                        await bridge._broadcast(json.dumps(msg))
                    
                    # ★ 1초에 1개씩 아주 천천히 요청 (방화벽 절대 차단 안 당함)
                    await asyncio.sleep(1.0)
                except Exception as e:
                    logger.debug("baseline_price_single_error", code=code, error=str(e))

        except Exception as e:
            logger.warning("baseline_price_loop_error", error=str(e))
        
        # 16개 다 돌면 10초 휴식 (1사이클 약 26초 소요)
        await asyncio.sleep(10)


async def _subscription_manager_loop(app):
    """Manage WebSocket subscriptions in a stable background loop every 5s."""
    force_resub_interval = 0
    while True:
        try:
            if app_state.ws_connected:
                # 현재 상태에서 필요한 모든 종목 (화면 종목 + 테마 대표주)
                current_needed = list(app_state.visible_codes)
                if current_needed:
                    ws = get_ws()
                    # ★ NEW: 30초마다 (6회 × 5s) subscribed_codes를 강제 초기화해
                    # 조용히 실패한 구독들을 재시도
                    force_resub_interval += 1
                    if force_resub_interval >= 6:
                        force_resub_interval = 0
                        ws.subscribed_codes.clear()
                        logger.info("force_resub_clear", reason="periodic_retry")
                    
                    logger.info("sync_subscriptions", count=len(current_needed))
                    await ws.resubscribe(current_needed)
        except Exception as e:
            logger.error("sub_manager_error", error=str(e))
        await asyncio.sleep(5)  # ★ CHANGED: 15s → 5s


async def _theme_rank_supplement_loop(app):
    """30초마다 REST top_movers로 inactive 테마 종목들의 가격/등락 데이터 보충."""
    from jason_checks.kis_rest import fetch_top_movers_cached
    while True:
        try:
            top = await fetch_top_movers_cached(market="J", sort="0", limit=100)
            for mover in top:
                code = mover["code"]
                st = app_state.stocks.get(code)
                # ★ NEW: 이름은 무조건 업데이트 (코드만 나오는 현상 방지)
                if mover.get("name"):
                    app_state.get_or_create_stock(code).name = mover["name"]
                    
                # WS 틱이 없거나 가격이 0인 종목에 REST 데이터 보충
                if not st or st.price == 0:
                    app_state.update_stock(
                        code,
                        price=mover["price"],
                        change_pct=mover["change_pct"],
                        cumulative_trading_value=mover.get("cumulative_trading_value", 0),
                    )
                    logger.debug("rank_supplement_updated", code=code, price=mover["price"])
        except Exception as e:
            logger.warning("theme_rank_supplement_error", error=str(e))
        await asyncio.sleep(30)


async def _get_initial_themes_by_movers(theme_data: dict) -> list:
    """REST top_movers로 오늘 강세 테마를 파악해 초기 active themes 설정."""
    from jason_checks.kis_rest import fetch_top_movers
    try:
        movers = await fetch_top_movers(market="J", sort="0", limit=100)
        code_to_pct = {m["code"]: m["change_pct"] for m in movers}

        theme_scores = {}
        for theme_code, config in theme_data.items():
            stocks = config.get("stocks", [])[:5]
            pcts = [code_to_pct[s["code"]] for s in stocks if s["code"] in code_to_pct]
            if pcts:
                theme_scores[theme_code] = sum(pcts) / len(pcts)

        sorted_themes = sorted(theme_scores.items(), key=lambda x: x[1], reverse=True)
        top4 = [code for code, _ in sorted_themes[:4]]
        fallback = [t for t in theme_data.keys() if t not in top4]
        result = (top4 + fallback)[:4]
        logger.info("initial_themes_by_movers", themes=result)
        return result
    except Exception as e:
        logger.warning("initial_theme_fetch_failed", error=str(e))
        return list(theme_data.keys())[:4]


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
            # REST top_movers로 오늘 강세 테마를 파악해 초기 active themes 결정
            initial_themes = await _get_initial_themes_by_movers(app.theme_data)
            # ★ CHANGED: max_codes=40 → max_codes=16 (4테마 × 상위 4종목)
            # 서버 구독 한도 초과 방지, 나머지는 _subscription_manager_loop가 담당
            codes_to_sub = get_expanded_subscription_codes(
                app.theme_data, initial_themes, max_codes=16
            )
            codes_to_sub = codes_to_sub if codes_to_sub else ["005930"]
            logger.info("initial_subscription", count=len(codes_to_sub))
            # ★ NEW: visible_codes를 시작부터 세팅 (바로 _baseline_price_loop이 즉시 실행되도록)
            app_state.set_visible_codes(codes_to_sub)
            await start_bridge(codes_to_sub)
        except Exception as e:
            logger.error("startup_failed", error=str(e))

        # Background: market indices every 5s
        asyncio.create_task(_market_indices_loop())
        # Background: investor trends
        asyncio.create_task(_index_investor_loop())
        asyncio.create_task(_stock_investor_loop(app))
        # Background: Subscription Sync
        asyncio.create_task(_subscription_manager_loop(app))
        # Background: REST data supplement for theme ranking (inactive 테마 종목 커버리지 개선)
        asyncio.create_task(_theme_rank_supplement_loop(app))
        # Background: Baseline price filler (1초 1건 안전 보충)
        asyncio.create_task(_baseline_price_loop(app))

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

        # Update visible codes in state so background loops know what to prioritize
        # ★ CHANGED: max_codes=40 → max_codes=16 (웹소켓 40개 한도 초과 방지 핵심)
        active_codes = get_expanded_subscription_codes(app.theme_data, active_themes, max_codes=16)
        app_state.set_visible_codes(active_codes)
        
        # REMOVED: resubscribe from here. Now handled by background _subscription_manager_loop

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
                # Force update app_state with what we need to track
                app_state.get_or_create_stock(code)
                
                # ★ CHANGED: price==0 조건 → 60초 이상 오래된 데이터도 갱신 (종가 잘못 표시 방지)
                existing_st = app_state.stocks.get(code)
                last_ts = existing_st.last_tick_ts if existing_st else None
                age_secs = (now_ts - last_ts).total_seconds() if last_ts else 9999
                is_stale = (code not in stock_ticks) or (stock_ticks.get(code, {}).get("price", 0) == 0) or (age_secs > 60)
                if is_stale:
                    try:
                        await asyncio.sleep(0.1) # Avoid rate limit
                        data = await fetch_current_price(code)
                        if data:
                            existing = app_state.stocks.get(code)
                            # REST strength는 0일 수 있음 → WS 값이 있으면 우선 유지
                            rest_strength = data.get("strength", 0) or 0
                            ws_strength = existing.execution_strength if existing else 0.0
                            strength = rest_strength if rest_strength > 0 else ws_strength

                            stock_ticks[code] = {
                                "price": data["price"],
                                "change_pct": data["change_pct"],
                                "cumulative_volume": data["volume"],
                                "cumulative_trading_value": data.get("trading_value", 0),
                                "strength": strength,
                                "timestamp": now_ts.strftime("%H%M%S"),
                                "investor_foreigner": stock.get("investor_foreigner", 0) if isinstance(stock, dict) else (stock.investor_foreigner if hasattr(stock, "investor_foreigner") else 0),
                                "investor_institution": 0,
                                "investor_individual": 0,
                            }
                            # Update app_state so it persists
                            # REST strength=0이면 WS 값을 유지하도록 조건부 업데이트
                            update_kwargs = {
                                "price": data["price"],
                                "change_pct": data["change_pct"],
                                "cumulative_trading_value": data.get("trading_value", 0),
                            }
                            if rest_strength > 0:
                                update_kwargs["execution_strength"] = rest_strength
                            app_state.update_stock(code, **update_kwargs)
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
