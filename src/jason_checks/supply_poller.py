"""Selective low-frequency supply polling for AlphaForge/setup candidates."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import structlog

from jason_checks.kis_rest import fetch_stock_supply_snapshot

logger = structlog.get_logger()

ALPHA_TTL_SEC = 5 * 60
SETUP_TTL_SEC = 12 * 60
FAIL_BACKOFF_SEC = 20 * 60
RATE_LIMIT_BACKOFF_SEC = 30 * 60
MAX_TARGETS = 10
CALL_INTERVAL_SEC = 2.0


def normalize_symbol(code: object) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        value = value[1:]
    if value.isdigit() and len(value) < 6:
        value = value.zfill(6)
    return value


def _target_name_map(app: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for theme in (getattr(app, "theme_data", {}) or {}).values():
        for row in theme.get("stocks", []):
            code = normalize_symbol(row.get("code"))
            if code:
                names.setdefault(code, str(row.get("name") or ""))
    for row in (getattr(app, "alphaforge_theme_data", {}) or {}).get("stocks", []):
        code = normalize_symbol(row.get("code"))
        if code:
            names.setdefault(code, str(row.get("name") or ""))
    return names


def select_supply_targets(app: Any) -> list[dict[str, Any]]:
    """Return max 10 selective supply targets: AlphaForge first, then setup/near-decision."""
    names = _target_name_map(app)
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(code: object, group: str, score: int = 0) -> None:
        norm = normalize_symbol(code)
        if not norm or norm in seen or len(targets) >= MAX_TARGETS:
            return
        seen.add(norm)
        targets.append({"code": norm, "name": names.get(norm, ""), "group": group, "setup_score": score})

    for row in (getattr(app, "alphaforge_theme_data", {}) or {}).get("stocks", [])[:5]:
        add(row.get("code"), "alphaforge")

    summary = getattr(app, "latest_decision_summary", {}) or {}
    results = list(summary.get("results", []) or [])
    for row in sorted(results, key=lambda x: int(x.get("setup_score") or 0), reverse=True):
        if int(row.get("setup_score") or 0) > 0:
            add(row.get("symbol"), "setup", int(row.get("setup_score") or 0))

    for row in results:
        if row.get("decision") in ("CONDITIONAL_BUY", "STARTER_POSITION"):
            add(row.get("symbol"), "near_decision", int(row.get("setup_score") or 0))

    return targets[:MAX_TARGETS]


def _supply_age_sec(timestamp: str) -> int | None:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
        return max(0, int((datetime.now() - dt).total_seconds()))
    except ValueError:
        return None


def apply_supply_snapshot(app_state: Any, code: str, data: dict[str, Any]) -> None:
    """Merge supply-only fields into app_state. Never touch price/change/value/strength."""
    status = data.get("supply_status") or "DATA_NA"
    update = {
        "supply_status": status,
        "supply_source": data.get("supply_source", "KIS"),
        "supply_recency": data.get("supply_recency", "UNKNOWN"),
        "supply_date": data.get("supply_date", ""),
        "supply_error": data.get("supply_error", ""),
    }
    if status == "OK":
        update.update({
            "foreign_flow": int(data.get("foreigner_net_buy") or 0),
            "institution_flow": int(data.get("institution_net_buy") or 0),
            "individual_flow": int(data.get("individual_net_buy") or 0),
            "supply_updated_at": str(data.get("supply_timestamp") or ""),
        })
    app_state.update_stock(code, **update)


def _target_supply_status(app_state: Any, targets: list[dict[str, Any]]) -> dict[str, Any]:
    codes = [t["code"] for t in targets]
    counts = {
        "target_total": len(codes),
        "target_codes": codes,
        "checked": 0,
        "ok": 0,
        "data_na": 0,
        "rate_limit": 0,
        "error": 0,
        "last_errors": [],
    }
    for code in codes:
        stock = app_state.stocks.get(code)
        status = getattr(stock, "supply_status", "DATA_NA") if stock else "DATA_NA"
        source = getattr(stock, "supply_source", "") if stock else ""
        if status != "DATA_NA" or source:
            counts["checked"] += 1
        if status == "OK":
            counts["ok"] += 1
        elif status == "RATE_LIMIT":
            counts["rate_limit"] += 1
        elif status in ("ERROR", "NOT_SUPPORTED"):
            counts["error"] += 1
            err = getattr(stock, "supply_error", "") if stock else ""
            if err:
                counts.setdefault("last_errors", []).append({"code": code, "status": status, "error": err[:240]})
        else:
            counts["data_na"] += 1
    return counts


async def run_selective_supply_poller(app: Any, app_state: Any) -> None:
    """Background poller with TTL/backoff and global rate-limit pause."""
    await asyncio.sleep(5)
    last_success: dict[str, datetime] = {}
    backoff_until: dict[str, datetime] = {}
    global_backoff_until: datetime | None = None
    app.supply_polling_status = {
        "target_total": 0,
        "target_codes": [],
        "checked": 0,
        "ok": 0,
        "data_na": 0,
        "rate_limit": 0,
        "error": 0,
        "last_errors": [],
        "last_updated_at": "",
        "paused_until": "",
    }

    while True:
        try:
            now = datetime.now()
            if global_backoff_until and now < global_backoff_until:
                app.supply_polling_status["paused_until"] = global_backoff_until.isoformat(timespec="seconds")
                await asyncio.sleep(30)
                continue

            targets = select_supply_targets(app)
            app.supply_polling_status.update({
                **_target_supply_status(app_state, targets),
            })
            if not targets:
                await asyncio.sleep(30)
                continue

            quote_status = getattr(app, "quote_polling_status", {}) or {}
            if quote_status.get("in_progress") and int(quote_status.get("success") or 0) < 80:
                await asyncio.sleep(10)
                continue

            called = False
            for target in targets:
                code = target["code"]
                ttl = ALPHA_TTL_SEC if target["group"] == "alphaforge" else SETUP_TTL_SEC
                if code in backoff_until and now < backoff_until[code]:
                    continue
                if code in last_success and (now - last_success[code]).total_seconds() < ttl:
                    continue

                logger.info("selective_supply_poll", code=code, group=target["group"])
                data = await fetch_stock_supply_snapshot(code)
                apply_supply_snapshot(app_state, code, data)
                status = str(data.get("supply_status") or "DATA_NA")
                stat = dict(getattr(app, "supply_polling_status", {}) or {})
                stat.update(_target_supply_status(app_state, targets))
                stat["last_updated_at"] = datetime.now().isoformat(timespec="seconds")
                if status == "OK":
                    last_success[code] = datetime.now()
                    backoff_until.pop(code, None)
                elif status == "RATE_LIMIT":
                    backoff_until[code] = datetime.now() + timedelta(seconds=RATE_LIMIT_BACKOFF_SEC)
                    global_backoff_until = datetime.now() + timedelta(seconds=RATE_LIMIT_BACKOFF_SEC)
                    logger.warning("selective_supply_rate_limited", code=code, paused_until=global_backoff_until.isoformat())
                elif status in ("ERROR", "NOT_SUPPORTED"):
                    backoff_until[code] = datetime.now() + timedelta(seconds=FAIL_BACKOFF_SEC)
                else:
                    backoff_until[code] = datetime.now() + timedelta(seconds=FAIL_BACKOFF_SEC)
                stat["paused_until"] = global_backoff_until.isoformat(timespec="seconds") if global_backoff_until else ""
                app.supply_polling_status = stat
                called = True
                await asyncio.sleep(CALL_INTERVAL_SEC)

            if not called:
                await asyncio.sleep(30)
        except Exception as e:
            logger.warning("selective_supply_poller_error", error=str(e))
            await asyncio.sleep(30)
