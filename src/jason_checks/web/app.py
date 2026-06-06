"""FastAPI application."""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import structlog
import asyncio
import os
import json
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
    fetch_index_investor_trend,
    fetch_current_price,
    fetch_overseas_price,
    KISClient,
)
from jason_checks.supply_poller import run_selective_supply_poller
from jason_checks.scanners.value_scanner import ValueScanner
from jason_checks.alphaforge_candidates import (
    build_alphaforge_theme,
    export_alphaforge_candidates,
    flatten_alphaforge_rows,
    load_alphaforge_candidates_with_meta,
)
from jason_checks.signal_journal import save_signal_journal, build_signal_rows
from jason_checks.decision_engine import run_decision_engine
from jason_checks.signal_journal import get_session_status
from jason_checks.telegram_notifier import (
    maybe_send_event,
    maybe_send_index_alert,
    send_test_message,
    telegram_enabled,
)

# Initialize logging
setup_logging()
logger = structlog.get_logger()

# Active market state
CURRENT_MARKET = "KR"
_PRICE_FETCH_LOCK = asyncio.Lock()
_GUARD_REPORT_DIR = Path(__file__).parent.parent.parent.parent / "data" / "reports" / "guard"
_GUARD_HEALTH_PATH = _GUARD_REPORT_DIR / "latest_health.json"
_GUARD_INCIDENT_PATH = _GUARD_REPORT_DIR / "latest_incident.md"
_GUARD_CODEX_PROMPT_PATH = _GUARD_REPORT_DIR / "latest_codex_prompt.txt"


def _normalize_symbol(code: object) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        value = value[1:]
    if value.isdigit() and len(value) < 6:
        value = value.zfill(6)
    return value


_US_MACRO_TICKERS = {
    "VIX": {
        "ticker": "^VIX",
        "name": "CBOE Volatility Index",
        "source": "yahoo:^VIX",
    },
    "US10Y": {
        "ticker": "^TNX",
        "name": "US 10Y Treasury Yield",
        "source": "yahoo:^TNX",
    },
    "USDKRW": {
        "ticker": "KRW=X",
        "name": "USD/KRW",
        "source": "yahoo:KRW=X",
    },
}


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _as_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        parsed = float(value)
        return parsed if parsed == parsed else None
    except Exception:
        return None


def _normalize_us10y_price(symbol: str, price: float | None) -> float | None:
    if price is None:
        return None
    # Yahoo ^TNX may be reported as 44.7 for 4.47%.
    if symbol == "US10Y" and price > 20:
        return price / 10.0
    return price


async def _fetch_us_macro_row(symbol: str, item: dict, session_status: str) -> dict:
    import httpx

    cfg = _US_MACRO_TICKERS[symbol]
    ticker = cfg["ticker"]
    source = cfg["source"]
    updated_at = _utc_now_iso()
    row = {
        "symbol": symbol,
        "name": item.get("name") or cfg["name"],
        "category": item.get("category"),
        "benchmark": item.get("benchmark"),
        "role": item.get("role"),
        "price": None,
        "change_pct": None,
        "status": "DATA_NA",
        "source": source,
        "updated_at": updated_at,
        "session_status": session_status,
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "5d", "interval": "1d"}
    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            row["error"] = f"fetch_failed: http_{resp.status_code}"
            logger.warning("us_watchlist_macro", symbol=symbol, status="DATA_NA", source=source, reason=row["error"])
            return row

        payload = resp.json()
        result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
        closes = [_as_float(v) for v in (quote.get("close") or [])]
        closes = [v for v in closes if v is not None and v > 0]

        raw_price = (
            _as_float(meta.get("regularMarketPrice"))
            or _as_float(meta.get("postMarketPrice"))
            or _as_float(meta.get("preMarketPrice"))
            or (closes[-1] if closes else None)
        )
        raw_prev = (
            _as_float(meta.get("chartPreviousClose"))
            or _as_float(meta.get("previousClose"))
            or (closes[-2] if len(closes) >= 2 else None)
        )
        price = _normalize_us10y_price(symbol, raw_price)
        prev = _normalize_us10y_price(symbol, raw_prev)

        if price is None or price <= 0:
            row["error"] = "fetch_failed: missing_price"
            logger.warning("us_watchlist_macro", symbol=symbol, status="DATA_NA", source=source, reason=row["error"])
            return row

        row["price"] = round(price, 4 if symbol in ("US10Y", "USDKRW") else 2)
        row["previous_close"] = round(prev, 4 if symbol in ("US10Y", "USDKRW") else 2) if prev else None
        if prev and prev > 0:
            row["change_pct"] = round(((price - prev) / prev) * 100.0, 4)
        row["status"] = "OK"
        logger.info("us_watchlist_macro", symbol=symbol, status="OK", price=row["price"], source=source)
        return row
    except Exception as e:
        row["error"] = f"fetch_failed: {type(e).__name__}: {str(e)[:160]}"
        logger.warning("us_watchlist_macro", symbol=symbol, status="DATA_NA", source=source, reason=row["error"])
        return row


def _sanitize_guard_value(value):
    """Remove sensitive-looking keys before exposing guard reports."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in {"token", "chat_id", "telegram_bot_token", "telegram_chat_id"}:
                continue
            if "token" in key_text or key_text.endswith("chat_id"):
                continue
            sanitized[key] = _sanitize_guard_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_guard_value(item) for item in value]
    return value


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except Exception as e:
        logger.warning("guard_report_read_failed", path=str(path), error=str(e))
        return ""


def _guard_issues_from_incident(incident: str) -> list[dict]:
    issues = []
    for line in incident.splitlines():
        text = line.strip()
        if not text.startswith("- [") and not text.startswith("- ["):
            continue
        if "]" not in text or ":" not in text:
            continue
        try:
            severity = text.split("]", 1)[0].lstrip("- [")
            rest = text.split("]", 1)[1].strip()
            code, summary = rest.split(":", 1)
            issues.append({
                "severity": severity.strip(),
                "code": code.strip(),
                "summary": summary.strip(),
            })
        except Exception:
            continue
    return issues


def _guard_report_age(timestamp: object) -> tuple[float | None, bool]:
    if not timestamp:
        return None, False
    try:
        checked_at = datetime.fromisoformat(str(timestamp))
        now = datetime.now(checked_at.tzinfo) if checked_at.tzinfo else datetime.now()
        age_sec = max(0.0, (now - checked_at).total_seconds())
        return age_sec, age_sec > 15 * 60
    except Exception:
        return None, False


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


def _run_dashboard_decision_engine_cached(
    app,
    *,
    alphaforge_picks: list[dict],
    themes: dict,
    indices: dict,
    session: str,
    ttl_sec: float = 10.0,
) -> dict:
    now_ts = datetime.now().timestamp()
    cached_at = float(getattr(app, "_decision_summary_cached_at", 0) or 0)
    cached = getattr(app, "latest_decision_summary", None)
    if cached and now_ts - cached_at < ttl_sec:
        return cached

    summary = run_decision_engine(
        alphaforge_picks=alphaforge_picks,
        themes=themes,
        indices=indices,
        session=session,
    )
    app.latest_decision_summary = summary
    app._decision_summary_cached_at = now_ts
    return summary


async def _fetch_price_snapshot(code: str, timeout_sec: float | None = None) -> dict | None:
    async with _PRICE_FETCH_LOCK:
        coro = fetch_current_price(code) if CURRENT_MARKET == "KR" else fetch_overseas_price(code)
        if timeout_sec:
            data = await asyncio.wait_for(coro, timeout=timeout_sec)
        else:
            data = await coro
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
            last_exception = ""
            stuck_warned = False
            symbol_names = _watch_symbol_names(app)
            started_at = datetime.now()
            estimated_sec = max(30, min(90, round(len(symbols) * 0.7)))
            app.quote_polling_status = {
                "total": len(symbols),
                "checked": 0,
                "success": 0,
                "missing": 0,
                "in_progress": True,
                "started_at": started_at.isoformat(),
                "updated_at": started_at.isoformat(),
                "completed_at": "",
                "duration_sec": getattr(app, "last_quote_polling_duration_sec", 0),
                "estimated_sec": estimated_sec,
                "missing_symbols": [],
                "last_exception": "",
            }
            logger.info("theme_quote_polling_start", count=len(symbols), market=CURRENT_MARKET)
            for code in symbols:
                try:
                    data = await _fetch_price_snapshot(code, timeout_sec=2.0)
                    if _apply_price_snapshot(code, data):
                        hydrated += 1
                    else:
                        missing_codes.append(code)
                except Exception as e:
                    last_exception = f"{code}: {type(e).__name__}: {e}"
                    missing_codes.append(code)
                    logger.warning("theme_quote_symbol_error", code=code, error=str(e), error_type=type(e).__name__)
                duration_live = round((datetime.now() - started_at).total_seconds(), 1)
                app.quote_polling_status.update({
                    "checked": hydrated + len(missing_codes),
                    "success": hydrated,
                    "missing": len(missing_codes),
                    "updated_at": datetime.now().isoformat(),
                    "duration_sec": duration_live,
                    "last_exception": last_exception,
                })
                if not stuck_warned and duration_live >= 60 and hydrated < 80:
                    stuck_warned = True
                    logger.warning(
                        "theme_quote_polling_slow",
                        elapsed_sec=duration_live,
                        quote_success=hydrated,
                        price_missing=len(missing_codes),
                        missing_symbols=[
                            {"code": c, "name": symbol_names.get(c, "")}
                            for c in missing_codes[:20]
                        ],
                        last_exception=last_exception,
                    )
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
                "last_exception": last_exception,
            })
            if hydrated < 80:
                logger.warning(
                    "theme_quote_polling_low_success",
                    elapsed_sec=duration_sec,
                    quote_success=hydrated,
                    price_missing=len(missing_codes),
                    missing_symbols=missing_preview,
                    last_exception=last_exception,
                )
            logger.info(
                "theme_quote_polling_done",
                watch_symbols=len(symbols),
                quote_success=hydrated,
                price_missing=len(missing_codes),
                missing_symbols=missing_preview,
            )
        except Exception as e:
            logger.warning("theme_quote_polling_error", error=str(e))
            status = dict(getattr(app, "quote_polling_status", {}) or {})
            status.update({
                "in_progress": False,
                "last_exception": str(e),
                "updated_at": datetime.now().isoformat(),
            })
            app.quote_polling_status = status
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
                    "source": info.get("source", "live"),
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

            # 2. Update WS targets and price snapshots. Broad supply polling is
            # intentionally disabled; Selective Supply Poller handles max 10.
            active_themes = rank_themes(app.theme_data, top_n=4, pinned=[])
            sub_theme_data = _theme_data_for_subscription(app)
            sub_active_themes = _active_themes_for_subscription(app, active_themes)
            active_codes = get_expanded_subscription_codes(sub_theme_data, sub_active_themes, max_codes=40)
            get_bridge().target_codes = list(active_codes)
            asyncio.create_task(get_ws().resubscribe(active_codes))

            quote_status = getattr(app, "quote_polling_status", {}) or {}
            quote_busy = bool(quote_status.get("in_progress")) and int(quote_status.get("success") or 0) < 80

            if CURRENT_MARKET == "KR" and not quote_busy:
                codes_dict = {}

                # 1. AlphaForge first to ensure they are always polled
                for s in sub_theme_data.get("AlphaForge", {}).get("stocks", []):
                    code_norm = _normalize_symbol(s["code"])
                    if code_norm:
                        codes_dict[code_norm] = True

                # 2. Other active themes
                for tc in sub_active_themes:
                    if tc == "AlphaForge":
                        continue
                    for s in sub_theme_data.get(tc, {}).get("stocks", []):
                        code_norm = _normalize_symbol(s["code"])
                        if code_norm:
                            codes_dict[code_norm] = True

                # 3. Surge data
                for s in getattr(app, "surge_data", []):
                    code_norm = _normalize_symbol(s.get("code") or "")
                    if code_norm:
                        codes_dict[code_norm] = True

                unique_codes = list(codes_dict.keys())[:30]
                logger.info("polling_stocks_start", count=len(unique_codes))

                for code in unique_codes:
                    try:
                        pr = await _fetch_price_snapshot(code)
                        update_data = {}
                        str_val = pr.get("strength", 0) if pr else 0
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
            elif CURRENT_MARKET == "KR":
                logger.info(
                    "stock_poll_deferred_for_full_quote_scan",
                    quote_success=int(quote_status.get("success") or 0),
                    quote_total=int(quote_status.get("total") or 0),
                )
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


async def _telegram_alert_loop(app):
    """AlphaForge Telegram alert scan (dry-run or real, per env flag)."""
    await asyncio.sleep(5)
    while True:
        try:
            rows = build_signal_rows(_theme_data_for_subscription(app), market=CURRENT_MARKET)
            results = []
            for row in rows:
                try:
                    # dry_run=None → telegram_notifier가 KR_TELEGRAM_DRY_RUN env로 결정
                    results.append(await maybe_send_event(row, dry_run=None))
                except Exception as e:
                    logger.warning("telegram_alert_row_failed", error=str(e))
            would_send_count = sum(1 for item in results if item.get("would_send_telegram"))
            session_blocked_count = sum(
                1 for item in results
                if "session_allows_alert" in item.get("blocked_by", [])
            )
            import os
            from jason_checks.telegram_notifier import _is_enabled, _is_dry_run
            enabled_flag = _is_enabled()
            dry_run_flag = _is_dry_run()
            min_level = os.environ.get("KR_TELEGRAM_MIN_LEVEL", "L2")
            mode = os.environ.get("KR_TELEGRAM_MODE", "instant")
            logger.info(
                "telegram_alert_loop_cycle",
                rows=len(rows),
                credentials_present=telegram_enabled(),
                would_send_telegram=would_send_count,
                session_blocked=session_blocked_count,
                enabled=enabled_flag,
                dry_run=dry_run_flag,
                min_level=min_level,
                mode=mode,
            )
        except Exception as e:
            logger.warning("telegram_loop_error", error=str(e))
        await asyncio.sleep(30)


# ─────────────────────────────────────────────────────────────────────────────
# AlphaForge Validation Report (read-only, from JO)
# ─────────────────────────────────────────────────────────────────────────────

_AV_REPORT_CANDIDATES = [
    (Path("/Users/miyoo1016/jason_octopus/reports/alphaforge_performance_scorecard.json"), "JO_SCORECARD_JSON"),
    (Path("/Users/miyoo1016/jason_octopus/reports/alphaforge_performance_scorecard.md"), "JO_SCORECARD_MD"),
    (Path("/Users/miyoo1016/jason_octopus/reports/alphaforge_validation/summary.json"), "JO_VALIDATION_SUMMARY_JSON"),
    (Path("reports/alphaforge_performance_scorecard.json"), "JC_INTERNAL_JSON"),
    (Path("data/reports/alphaforge_performance_scorecard.json"), "JC_INTERNAL_JSON"),
    (Path("data/exports/alphaforge_validation.json"), "JC_INTERNAL_JSON"),
]
_av_cache: dict = {}          # 마지막으로 읽은 summary 딕셔너리
_av_last_mtime: float = -1.0  # os.stat mtime 추적용
_av_last_path: str = ""


def _safe_ratio(value):
    try:
        if value is None:
            return None
        number = float(value)
        return number / 100 if abs(number) > 1 else number
    except Exception:
        return None


def _av_source_candidates() -> list[tuple[Path, str]]:
    return [(path if path.is_absolute() else Path.cwd() / path, source_type) for path, source_type in _AV_REPORT_CANDIDATES]


def _empty_alphaforge_validation(reason: str = "AlphaForge 검증 리포트 없음") -> dict:
    checked = [str(path) for path, _ in _av_source_candidates()]
    return {
        "available": False,
        "status": "DATA_NA",
        "reason": reason,
        "source_path": "",
        "source_type": "DATA_NA",
        "overall_practicality_score": None,
        "confidence": "DATA_NA",
        "generated_at": "",
        "report_created_at": "",
        "summary": reason,
        "cap_reasons": [],
        "sample_status": "DATA_NA",
        "path_checked": checked,
        "path_found": [],
    }


def _window_from_performance_row(row: dict) -> dict:
    sample_status = row.get("sample_status") or ("DATA_INSUFFICIENT" if row.get("n", 0) == 0 else "OK")
    return {
        "insufficient": sample_status != "OK",
        "n": row.get("n", 0),
        "note": sample_status if sample_status != "OK" else "",
        "avg_return": _safe_ratio(row.get("avg_return")),
        "avg_alpha": _safe_ratio(row.get("excess_return")),
        "avg_net_alpha": _safe_ratio(row.get("excess_return")),
        "win_rate": _safe_ratio(row.get("win_rate")),
        "sample_status": sample_status,
    }


def _normalize_performance_scorecard(data: dict, source_path: Path, source_type: str) -> dict:
    scores = data.get("scores") or {}
    input_counts = data.get("input_counts") or {}
    horizons_raw = data.get("horizons") or ["1d", "3d", "5d", "10d"]
    windows = []
    for item in horizons_raw:
        text = str(item).lower().replace("d", "")
        try:
            windows.append(int(text))
        except Exception:
            continue

    by_label = ((data.get("performance") or {}).get("by_label") or {})
    label_counts = data.get("label_counts") or {}
    by_alert_type = {}
    for label, label_data in by_label.items():
        windows_map = {}
        for window in windows:
            row = label_data.get(f"{window}d", {}) if isinstance(label_data, dict) else {}
            windows_map[str(window)] = _window_from_performance_row(row)
        by_alert_type[label] = {
            "n": label_counts.get(label, max((w.get("n", 0) for w in windows_map.values()), default=0)),
            "windows": windows_map,
        }

    overall = {}
    for window in windows:
        rows = [
            (label_data or {}).get(f"{window}d", {})
            for label_data in by_label.values()
            if isinstance(label_data, dict)
        ]
        n_total = sum(int(row.get("n") or 0) for row in rows)
        valid = [row for row in rows if int(row.get("n") or 0) > 0 and row.get("sample_status") == "OK"]
        if not valid:
            overall[str(window)] = {
                "insufficient": True,
                "n": n_total,
                "note": "sample insufficient",
                "sample_status": "DATA_INSUFFICIENT",
            }
            continue
        avg_net_alpha = sum((_safe_ratio(row.get("excess_return")) or 0) * int(row.get("n") or 0) for row in valid) / max(sum(int(row.get("n") or 0) for row in valid), 1)
        win_rate = sum((_safe_ratio(row.get("win_rate")) or 0) * int(row.get("n") or 0) for row in valid) / max(sum(int(row.get("n") or 0) for row in valid), 1)
        overall[str(window)] = {
            "insufficient": False,
            "n": n_total,
            "avg_net_alpha": avg_net_alpha,
            "win_rate": win_rate,
            "sample_status": "OK",
        }

    cap_reasons = scores.get("cap_reasons") or []
    sample_status = "LOW_CONFIDENCE" if cap_reasons else "OK"
    return {
        "available": True,
        "status": "FOUND",
        "source_path": str(source_path),
        "source_type": source_type,
        "overall_practicality_score": scores.get("overall_practicality_score"),
        "confidence": scores.get("confidence") or "DATA_NA",
        "generated_at": data.get("generated_at", ""),
        "report_created_at": data.get("generated_at", ""),
        "summary": scores.get("reason") or data.get("version") or "AlphaForge Performance Scorecard",
        "cap_reasons": cap_reasons,
        "sample_status": sample_status,
        "path_checked": [str(path) for path, _ in _av_source_candidates()],
        "path_found": [str(source_path)],
        "signal_count": input_counts.get("candidate_rows", 0),
        "evaluated_count": input_counts.get("snapshot_rows", 0),
        "benchmark": "DATA_NA",
        "transaction_cost_rt": 0,
        "windows": windows,
        "min_n": 20,
        "overall": {"n": input_counts.get("snapshot_rows", 0), **overall},
        "by_alert_type": by_alert_type,
        "raw_scores": scores,
    }


def _normalize_legacy_validation_summary(data: dict, source_path: Path, source_type: str) -> dict:
    if not isinstance(data, dict) or "overall" not in data:
        return {}
    result = dict(data)
    result.update({
        "available": True,
        "status": "FOUND",
        "source_path": str(source_path),
        "source_type": source_type,
        "overall_practicality_score": data.get("overall_practicality_score"),
        "confidence": data.get("confidence") or "DATA_NA",
        "report_created_at": data.get("generated_at", ""),
        "summary": data.get("summary", ""),
        "cap_reasons": data.get("cap_reasons", []),
        "sample_status": data.get("sample_status", "DATA_NA"),
        "path_checked": [str(path) for path, _ in _av_source_candidates()],
        "path_found": [str(source_path)],
    })
    return result


def _load_alphaforge_validation() -> dict:
    """JO AlphaForge validation/performance reports를 안전하게 읽어 반환한다."""
    global _av_cache, _av_last_mtime, _av_last_path
    try:
        checked = []
        for path, source_type in _av_source_candidates():
            checked.append(str(path))
            if not path.exists():
                continue
            mtime = path.stat().st_mtime
            cache_key = str(path)
            if mtime == _av_last_mtime and cache_key == _av_last_path and _av_cache:
                return _av_cache
            if source_type == "JO_SCORECARD_MD":
                result = {
                    **_empty_alphaforge_validation("JO AlphaForge markdown scorecard found; JSON report unavailable"),
                    "available": True,
                    "status": "FOUND",
                    "source_path": str(path),
                    "source_type": source_type,
                    "confidence": "DATA_NA",
                    "summary": "Markdown 리포트만 존재합니다. JSON 점수 필드는 DATA_NA입니다.",
                    "path_checked": checked,
                    "path_found": [str(path)],
                }
            else:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if source_type == "JO_SCORECARD_JSON":
                    result = _normalize_performance_scorecard(data, path, source_type)
                else:
                    result = _normalize_legacy_validation_summary(data, path, source_type)
                if not result:
                    return {
                        **_empty_alphaforge_validation("AlphaForge validation schema mismatch"),
                        "status": "READ_ERROR",
                        "source_path": str(path),
                        "source_type": source_type,
                        "path_checked": checked,
                        "path_found": [str(path)],
                    }
            _av_last_mtime = mtime
            _av_last_path = cache_key
            _av_cache = result
            logger.info("alphaforge_validation_loaded", source_path=str(path), source_type=source_type, status=result.get("status"))
            return result
        return _empty_alphaforge_validation()
    except Exception as e:
        logger.warning("alphaforge_validation_load_error", error=str(e))
        return {
            **_empty_alphaforge_validation("AlphaForge 검증 리포트 읽기 실패"),
            "status": "READ_ERROR",
            "error": f"read_error:{type(e).__name__}",
        }


async def _alphaforge_validation_loop(app):
    """60초마다 summary.json mtime 변경 감지 후 캐시 갱신."""
    await asyncio.sleep(10)
    while True:
        try:
            _load_alphaforge_validation()
        except Exception as e:
            logger.warning("alphaforge_validation_loop_error", error=str(e))
        await asyncio.sleep(60)


async def _index_alert_loop(app):
    """KOSPI/KOSDAQ 급등락 Telegram 알람 루프. 15분 쿨다운 내장."""
    await asyncio.sleep(15)  # 지수 polling이 먼저 뜰 때까지 대기
    while True:
        try:
            for code, idx in list(app_state.indices.items()):
                source = getattr(idx, "source", "live")
                name = getattr(idx, "name", code) or code
                chg = float(getattr(idx, "change_pct", 0) or 0)
                price = float(getattr(idx, "price", 0) or 0)
                if source not in ("dummy", "mock") and price > 0 and abs(chg) >= 0.1:  # 0 방어
                    try:
                        session_now = get_session_status("KR")
                        await maybe_send_index_alert(
                            index_code=code,
                            index_name=name,
                            change_pct=chg,
                            price=price,
                            threshold_pct=3.0,
                            session_status=session_now,
                        )
                    except Exception as e:
                        logger.warning("index_alert_error", code=code, error=str(e))
        except Exception as e:
            logger.warning("index_alert_loop_error", error=str(e))
        await asyncio.sleep(60)


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
            app.alphaforge_candidates_source = ""
            app.alphaforge_candidates_mode = ""
            app.alphaforge_candidates_published_at = ""
            app.alphaforge_candidates_is_stale = False
            app.alphaforge_candidates_stale_age_hours = -1.0
            app.alphaforge_candidates_fallback_warning = ""
            app.alphaforge_theme_data = {}
            if market == "KR":
                alphaforge_candidates, alphaforge_meta = load_alphaforge_candidates_with_meta()
                app.alphaforge_candidates_path = alphaforge_meta.get("path", "")
                app.alphaforge_candidates_generated_at = alphaforge_meta.get("generated_at", "")
                app.alphaforge_candidates_source = alphaforge_meta.get("source", "")
                app.alphaforge_candidates_mode = alphaforge_meta.get("mode", "")
                app.alphaforge_candidates_published_at = alphaforge_meta.get("published_at", "")
                app.alphaforge_candidates_is_stale = alphaforge_meta.get("is_stale", False)
                app.alphaforge_candidates_stale_age_hours = alphaforge_meta.get("stale_age_hours", -1.0)
                app.alphaforge_candidates_fallback_warning = alphaforge_meta.get("fallback_warning", "")
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
            app.supply_polling_status = {
                "target_total": 0,
                "target_codes": [],
                "checked": 0,
                "ok": 0,
                "data_na": 0,
                "rate_limit": 0,
                "error": 0,
                "last_updated_at": "",
                "paused_until": "",
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

        from jason_checks.telegram_notifier import telegram_healthcheck
        telegram_healthcheck()

        app_state.load_sparkline_history()
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
        asyncio.create_task(run_selective_supply_poller(app, app_state))
        asyncio.create_task(_signal_journal_loop(app))
        asyncio.create_task(_telegram_alert_loop(app))
        asyncio.create_task(_index_alert_loop(app))
        asyncio.create_task(_alphaforge_validation_loop(app))

    @app.on_event("shutdown")
    async def shutdown():
        await get_bridge().stop()

    @app.get("/")
    async def root():
        template_path = Path(__file__).parent / "templates" / "index.html"
        return FileResponse(template_path)

    @app.get("/api/alphaforge-validation")
    async def get_alphaforge_validation():
        """JO의 AlphaForge validation/performance report를 읽기 전용으로 반환한다."""
        try:
            data = _load_alphaforge_validation()
            if not data:
                return _empty_alphaforge_validation()
            return data
        except Exception as e:
            logger.warning("alphaforge_validation_api_error", error=str(e))
            return {
                **_empty_alphaforge_validation("AlphaForge 검증 리포트 읽기 실패"),
                "status": "READ_ERROR",
                "error": f"read_error:{type(e).__name__}",
            }

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

    _us_cache = {"data": [], "updated_at": 0, "session_status": "UNKNOWN"}

    @app.get("/api/us/watchlist")
    async def get_us_watchlist():
        import time
        import json
        now = time.time()
        if now - _us_cache["updated_at"] < 60 and _us_cache["data"]:
            return {
                "session_status": _us_cache["session_status"],
                "updated_at": datetime.fromtimestamp(_us_cache["updated_at"]).isoformat() + "Z",
                "rows": _us_cache["data"],
                "available": True
            }

        p = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../data/us_watchlist.json"))
        try:
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f)
            items = meta.get("items", [])
        except Exception as e:
            return {"available": False, "reason": f"Failed to read us_watchlist: {e}"}

        if not items:
            return {"available": False, "reason": "us_watchlist.json is empty"}

        sess = get_session_status(market="US")

        async def _fetch(item):
            sym = str(item.get("symbol") or "").strip().upper()
            if sym in ("VIX", "US10Y", "USDKRW"):
                return await _fetch_us_macro_row(sym, item, sess)
            try:
                res = await fetch_overseas_price(sym)
                if res:
                    return {
                        "symbol": sym,
                        "price": res.get("price"),
                        "change_pct": res.get("change_pct"),
                    }
            except Exception:
                pass
            return {"symbol": sym, "price": None, "change_pct": None}

        tasks = [_fetch(item) for item in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rows = [r for r in results if isinstance(r, dict)]

        _us_cache["data"] = rows
        _us_cache["updated_at"] = now
        _us_cache["session_status"] = sess

        return {
            "session_status": sess,
            "updated_at": datetime.fromtimestamp(now).isoformat() + "Z",
            "rows": rows,
            "available": True
        }

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
        supply_polling_status = getattr(app, "supply_polling_status", {}) or {}
        supply_reason = (
            f"KIS 선택수급: 전일수급 {int(supply_polling_status.get('ok') or 0)}/"
            f"{int(supply_polling_status.get('target_total') or 0)} · "
            "전체 섹터 수급: 미조회 · KRX 전체수급: 미사용"
        )
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
                "alphaforge_candidates_source": getattr(app, "alphaforge_candidates_source", ""),
                "alphaforge_candidates_mode": getattr(app, "alphaforge_candidates_mode", ""),
                "alphaforge_candidates_published_at": getattr(app, "alphaforge_candidates_published_at", ""),
                "alphaforge_candidates_is_stale": getattr(app, "alphaforge_candidates_is_stale", False),
                "alphaforge_candidates_stale_age_hours": getattr(app, "alphaforge_candidates_stale_age_hours", -1.0),
                "alphaforge_candidates_fallback_warning": getattr(app, "alphaforge_candidates_fallback_warning", ""),
            "alphaforge_picks": [],
            "quote_polling": quote_status,
            "supply_data_reason": supply_reason,
            "supply_polling": getattr(app, "supply_polling_status", {}),
            "symbol_names": _watch_symbol_names(app),
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
                "supply_source": stock.supply_source,
                "supply_recency": stock.supply_recency,
                "supply_date": stock.supply_date,
                "supply_error": stock.supply_error,
                "updated_at": stock.last_tick_ts.isoformat(),
                "sparkline": {
                    "status": "OK" if len(stock.sparkline_points) >= 2 else "collecting",
                    "points": [{"t": t, "p": p} for t, p in sorted(stock.sparkline_points.items())],
                    "baseline": stock.price / (1 + stock.change_pct / 100) if stock.change_pct > -99.9 and stock.price > 0 else stock.price,
                } if stock.sparkline_points else None
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
                    "supply_source": "",
                    "supply_recency": "UNKNOWN",
                    "supply_date": "",
                    "supply_error": "",
                }
                if code_norm in stock_ticks:
                    ld.update(stock_ticks[code_norm])
                leader_list.append(ld)

            themes_result[theme_code] = {
                "display_name": theme_config.get("display_name", theme_code),
                "strength": strength,
                "avg_change_pct": avg_change_pct,
                "price_count": sum(1 for stock in leader_list if float(stock.get("price") or 0) > 0),
                "leader_count": len(leader_list),
                "leaders": leader_list,
            }

        alphaforge_picks = []
        alphaforge_theme = getattr(app, "alphaforge_theme_data", {}) or {}
        if alphaforge_theme:
            alpha_data = {"AlphaForge": alphaforge_theme}
            alpha_leaders = select_leaders("AlphaForge", stock_ticks, alpha_data, sort_mode=sort)

            quote_status = getattr(app, "quote_polling_status", {})
            polling_in_progress = quote_status.get("in_progress", False)
            if not quote_status.get("completed_at"):
                polling_in_progress = True

            for leader in alpha_leaders:
                code_norm = _normalize_symbol(leader.get("code") or leader.get("symbol") or "")
                ld = {
                    "code": code_norm,
                    "symbol": code_norm,
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
                    "supply_source": "",
                    "supply_recency": "UNKNOWN",
                    "supply_date": "",
                    "supply_error": "",
                }
                if code_norm in stock_ticks:
                    ld.update(stock_ticks[code_norm])

                app_stock = app_state.stocks.get(code_norm)
                app_state_price = app_stock.price if app_stock else 0.0
                price_in_ld = float(ld.get("price") or 0.0)
                merge_failed = False
                if app_state_price > 0.0 and price_in_ld <= 0.0:
                    merge_failed = True
                    logger.warning("merge_failed_detected_in_themes", symbol=code_norm, app_state_price=app_state_price)

                ld["merge_failed"] = merge_failed
                ld["polling_in_progress"] = polling_in_progress
                alphaforge_picks.append(ld)

        theme_rows = [
            stock
            for theme in themes_result.values()
            for stock in theme.get("leaders", [])
        ]
        theme_row_total = len(theme_rows)
        theme_row_price_count = sum(1 for stock in theme_rows if float(stock.get("price") or 0) > 0)
        theme_row_strength_count = sum(1 for stock in theme_rows if float(stock.get("strength") or 0) > 0)
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
            "checked": theme_row_total,
            "success": theme_row_price_count,
            "missing": max(theme_row_total - theme_row_price_count, 0),
            "watch_symbols": len(watch_symbols),
            "app_state_price_count": app_state_price_count,
            "theme_row_total": theme_row_total,
            "theme_row_price_count": theme_row_price_count,
            "theme_row_strength_count": theme_row_strength_count,
            "strength_success": theme_row_strength_count,
            "strength_total": theme_row_total,
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
        decision_summary = _run_dashboard_decision_engine_cached(
            app,
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
            pick["de_supply_recency"] = dec.get("supply_recency", "UNKNOWN")
            pick["de_supply_date"] = dec.get("supply_date", "")
            pick["de_supply_status"] = dec.get("supply_status", "DATA_NA")

            # Base pick keys for UI ease of access
            pick["supply_recency"] = pick.get("supply_recency") or "UNKNOWN"
            pick["supply_date"] = pick.get("supply_date") or ""

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
            "alphaforge_candidates_source": getattr(app, "alphaforge_candidates_source", ""),
            "alphaforge_candidates_mode": getattr(app, "alphaforge_candidates_mode", ""),
            "alphaforge_candidates_published_at": getattr(app, "alphaforge_candidates_published_at", ""),
            "alphaforge_candidates_is_stale": getattr(app, "alphaforge_candidates_is_stale", False),
            "alphaforge_candidates_stale_age_hours": getattr(app, "alphaforge_candidates_stale_age_hours", -1.0),
            "alphaforge_candidates_fallback_warning": getattr(app, "alphaforge_candidates_fallback_warning", ""),
            "alphaforge_picks": alphaforge_picks,
            "symbol_names": _watch_symbol_names(app),
            "quote_polling": display_quote_status,
            "supply_data_reason": supply_reason,
            "supply_polling": getattr(app, "supply_polling_status", {}),
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
            "forward_test_summary": decision_summary.get("forward_test_summary", {}),
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
                "supply_source": s.supply_source,
                "supply_recency": s.supply_recency,
                "supply_date": s.supply_date,
                "supply_error": s.supply_error,
                "foreign_flow": s.foreign_flow,
                "institution_flow": s.institution_flow,
                "individual_flow": s.individual_flow,
                "updated_at": s.last_tick_ts.isoformat(),
            }
            for code, s in app_state.stocks.items()
        }

        quote_status = getattr(app, "quote_polling_status", {})
        polling_in_progress = quote_status.get("in_progress", False)
        if not quote_status.get("completed_at"):
            polling_in_progress = True

        enriched_picks = []
        for p in picks_raw:
            code = _normalize_symbol(p.get("code") or p.get("symbol") or p.get("ticker") or "")
            tick = stock_ticks.get(code, {})

            app_stock = app_state.stocks.get(code)
            app_state_price = app_stock.price if app_stock else 0.0
            price_in_tick = float(tick.get("price") or 0.0)
            merge_failed = False
            if app_state_price > 0.0 and price_in_tick <= 0.0:
                merge_failed = True
                logger.warning("merge_failed_detected_in_summary", symbol=code, app_state_price=app_state_price)

            enriched_picks.append({
                **p,
                **tick,
                "code": code,
                "symbol": code,
                "merge_failed": merge_failed,
                "polling_in_progress": polling_in_progress
            })

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

    @app.get("/api/telegram/status")
    async def get_telegram_status():
        from jason_checks.telegram_notifier import (
            get_telegram_config, _credentials_present, _get_credentials, _recent_alerts, _hourly_sent,
            _daily_sent, _daily_reset_date, _recent_events, _last_ok_at, _last_error_at, _last_error_reason
        )
        cfg = get_telegram_config()
        token, chat_id = _get_credentials()
        token_present = bool(str(token or "").strip())
        chat_id_present = bool(str(chat_id or "").strip())

        sent_today = len([e for e in _recent_events if e.get("result") == "sent"])
        failed_today = len([e for e in _recent_events if e.get("result") == "failed"])
        skipped_today = len([e for e in _recent_events if e.get("result") in ("skipped", "dry_run")])

        last_sent = next((e.get("timestamp") for e in _recent_events if e.get("result") == "sent"), None)
        last_error_event = next((e for e in _recent_events if e.get("result") == "failed"), None)

        return {
            "enabled": cfg["enabled"],
            "dry_run": cfg["dry_run"],
            "mode": cfg["mode"],
            "min_level": cfg["min_level"],
            "credentials_present": _credentials_present(),
            "configured": token_present and chat_id_present,
            "token_present": token_present,
            "chat_id_present": chat_id_present,
            "recent_alerts_count": len(_recent_alerts),
            "hourly_sent_count": len(_hourly_sent),
            "daily_sent": _daily_sent,
            "daily_reset_date": _daily_reset_date,
            "eligible_events_count": len(_recent_alerts),
            "last_skip_reasons": [],
            "recent_events": _recent_events[:20],
            "sent_today_count": sent_today,
            "failed_today_count": failed_today,
            "skipped_today_count": skipped_today,
            "last_sent_at": last_sent,
            "last_ok_at": _last_ok_at or last_sent,
            "last_error_at": _last_error_at or (last_error_event.get("timestamp") if last_error_event else None),
            "last_error_reason": _last_error_reason or (last_error_event.get("reason") if last_error_event else None),
            "runtime_last_error_at": _last_error_at,
        }

    @app.get("/api/guard/status")
    async def get_guard_status():
        """Read JC Guard report files without running or repairing guard."""
        if not _GUARD_HEALTH_PATH.exists():
            return {
                "overall_status": "NOT_RUN",
                "timestamp": None,
                "summary": "Guard has not run yet",
                "issues": [],
                "health": {},
                "server": {},
                "indices": {},
                "telegram": {},
                "themes": {},
                "logs": {},
                "auto_repair": {},
                "incident": "",
                "codex_prompt": "",
            }

        try:
            raw_health = json.loads(_GUARD_HEALTH_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("guard_health_read_failed", error=str(e))
            raw_health = {
                "overall_status": "FAIL",
                "timestamp": None,
                "server": {},
                "indices": {},
                "telegram": {},
                "themes": {},
                "logs": {},
                "auto_repair": {},
            }

        health = _sanitize_guard_value(raw_health)
        incident = _read_text_file(_GUARD_INCIDENT_PATH)
        codex_prompt = _read_text_file(_GUARD_CODEX_PROMPT_PATH)
        age_sec, stale = _guard_report_age(health.get("timestamp"))
        issues = health.get("issues") if isinstance(health.get("issues"), list) else []
        if not issues:
            issues = _guard_issues_from_incident(incident)
        report_status = health.get("overall_status", "NOT_RUN")
        historical_issues = issues if stale else []
        display_issues = [] if stale else issues
        display_status = "STALE" if stale else report_status
        if stale:
            summary = f"Guard 미실행: {int(age_sec // 60) if age_sec is not None else '?'}분 전 과거 {report_status} 리포트"
        else:
            summary = "OK" if not display_issues else "; ".join(
                str(item.get("code") or item.get("summary") or item) for item in display_issues[:3]
            )

        return {
            "overall_status": display_status,
            "report_status": report_status,
            "timestamp": health.get("timestamp"),
            "age_sec": age_sec,
            "is_stale": stale,
            "summary": summary or "OK",
            "issues": display_issues,
            "stale_report_issues": historical_issues,
            "historical_issues": historical_issues,
            "health": health,
            "server": health.get("server", {}),
            "indices": health.get("indices", {}),
            "telegram": health.get("telegram", {}),
            "themes": health.get("themes", {}),
            "logs": health.get("logs", {}),
            "auto_repair": health.get("auto_repair", {}),
            "incident": incident,
            "codex_prompt": codex_prompt,
        }

    @app.get("/api/indices")
    async def get_indices():
        result = {}
        for code, idx in app_state.indices.items():
            source = getattr(idx, "source", "live")
            if source in ("dummy", "mock") or idx.price <= 0:
                sparkline = {"status": "DATA_NA", "points": [], "baseline": 0.0}
            else:
                sparkline = {
                    "status": "OK" if len(idx.sparkline_points) >= 2 else "collecting",
                    "points": [{"t": t, "p": p} for t, p in sorted(idx.sparkline_points.items())],
                    "baseline": idx.price / (1 + idx.change_pct / 100) if idx.change_pct > -99.9 and idx.price > 0 else idx.price,
                } if idx.sparkline_points else None

            result[code] = {
                "name": idx.name, "price": idx.price, "change_pct": idx.change_pct,
                "source": source,
                "investor_foreigner": idx.investor_foreigner,
                "investor_institution": idx.investor_institution,
                "investor_individual": idx.investor_individual,
                "sparkline": sparkline
            }
        return {"indices": result}

    @app.get("/api/surges")
    async def get_surges(sort: str = "trading_value", limit: int = 10):
        """Get top movers / high volume stocks."""
        from jason_checks.kis_rest import fetch_top_movers_cached

        quote_status = getattr(app, "quote_polling_status", {}) or {}
        if quote_status.get("in_progress") and int(quote_status.get("success") or 0) < 80:
            return {"surges": getattr(app, "surge_data", []), "market": CURRENT_MARKET, "deferred": True}

        # Mapping frontend sort to KIS sort
        # 0=상승률, 1=하락률
        kis_sort = "0" if sort == "change_pct" else "0" # Defaulting to volume/change for now

        # Currently fetch_top_movers only supports KR, need US version later
        if CURRENT_MARKET == "KR":
            data = await fetch_top_movers_cached(market="J", sort=kis_sort, limit=limit)
        else:
            from jason_checks.kis_rest import fetch_us_top_movers
            data = await fetch_us_top_movers(limit=limit)

        if data:
            app.surge_data = data
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

    @app.post("/api/telegram-test")
    async def telegram_test():
        """CHECKS Telegram 테스트 메시지 1회 발송 (프로세스당 1회, 쿨다운 1h)."""
        result = await send_test_message()
        if result.get("error"):
            return JSONResponse({"ok": False, **result}, status_code=400)
        return {"ok": True, **result}

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
