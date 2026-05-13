"""Signal Journal v1 - append AlphaForge candidate snapshots to JSONL."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from jason_checks.state import app_state


logger = structlog.get_logger()
KST = ZoneInfo("Asia/Seoul")


def get_session_status(now: datetime | None = None, market: str = "KR") -> str:
    now = now or datetime.now(KST)
    if now.weekday() >= 5:
        return "MARKET_CLOSED"

    hhmm = now.hour * 100 + now.minute
    if market == "KR":
        if 800 <= hhmm < 850:
            return "PRE_MARKET"
        if 900 <= hhmm <= 1530:
            return "REGULAR"
        if 1530 < hhmm < 2000:
            return "AFTER_MARKET"
        return "MARKET_CLOSED"

    if 1700 <= hhmm < 2230:
        return "PRE_MARKET"
    if hhmm >= 2230 or hhmm < 500:
        return "REGULAR"
    if 500 <= hhmm < 900:
        return "AFTER_MARKET"
    return "MARKET_CLOSED"


def signal_journal_path(root: Path | None = None, now: datetime | None = None) -> Path:
    project_root = root or Path(__file__).parent.parent.parent
    today = (now or datetime.now(KST)).strftime("%Y-%m-%d")
    return project_root / "data" / "signal_journal" / f"{today}_signals.jsonl"


def get_alphaforge_candidates(theme_data: dict[str, Any]) -> list[dict[str, Any]]:
    theme = theme_data.get("AlphaForge")
    if not theme:
        return []
    stocks = theme.get("stocks", [])
    return [stock for stock in stocks if isinstance(stock, dict)]


def _fmt_reasons(value: Any) -> list[str]:
    if value in (None, "", "-"):
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _decision(candidate: dict[str, Any], current_price: float, trade_strength: float, trading_value: int, session_status: str) -> dict[str, Any]:
    reasons: list[str] = []
    failed_conditions: list[str] = []
    box = candidate.get("box_upper_price")

    try:
        box_price = float(box)
    except (TypeError, ValueError):
        box_price = 0.0

    if session_status == "MARKET_CLOSED":
        return {"decision": "MARKET_CLOSED", "reasons": ["장중판정 비활성"], "failed_conditions": []}
    if current_price <= 0:
        return {"decision": "DATA_WAIT", "reasons": ["현재가 대기"], "failed_conditions": ["현재가 없음"]}
    if box_price <= 0:
        return {"decision": "DATA_WAIT", "reasons": ["BOX 기준가 대기"], "failed_conditions": ["box_upper_price 없음"]}

    if candidate.get("vcp_status") == "RALLY_EXHAUSTION":
        reasons.append("추격주의")
    if candidate.get("alert_type") == "ACTION_ALERT":
        reasons.append("우선관찰")

    if current_price <= box_price:
        reasons.insert(0, f"현재가가 BOX {box_price:,.0f} 아래")
        failed_conditions.append("BOX 돌파 전")
        return {"decision": "IN_BOX_WAIT", "reasons": reasons, "failed_conditions": failed_conditions}

    reasons.insert(0, f"BOX {box_price:,.0f} 돌파")
    trading_value_ok = trading_value >= 1_000_000_000
    if trade_strength >= 140 and trading_value_ok:
        reasons.extend([f"체결강도 {trade_strength:.0f}", "거래대금 양호"])
        return {"decision": "ENTRY_OK", "reasons": reasons, "failed_conditions": failed_conditions}
    if trade_strength >= 120:
        reasons.append(f"체결강도 {trade_strength:.0f}")
        if trade_strength < 140:
            failed_conditions.append("체결강도 140 미만")
        if not trading_value_ok:
            failed_conditions.append("거래대금 부족")
        return {"decision": "BREAKOUT_WATCH", "reasons": reasons, "failed_conditions": failed_conditions}

    reasons.append(f"체결강도 {trade_strength:.0f}")
    failed_conditions.append("체결강도 120 미만")
    return {"decision": "FAKEOUT_RISK", "reasons": reasons, "failed_conditions": failed_conditions}


def build_signal_rows(theme_data: dict[str, Any], market: str = "KR", now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(KST)
    timestamp = now.isoformat()
    session_status = get_session_status(now, market)
    rows: list[dict[str, Any]] = []

    for candidate in get_alphaforge_candidates(theme_data):
        symbol = str(candidate.get("symbol") or candidate.get("code") or "")
        stock = app_state.stocks.get(symbol)
        current_price = float(stock.price) if stock else 0.0
        change_rate = float(stock.change_pct) if stock else 0.0
        trade_strength = float(stock.execution_strength) if stock else 0.0
        trading_value = int(stock.cumulative_trading_value) if stock else 0
        supply_status = stock.supply_status if stock else "DATA_NA"
        decision = _decision(candidate, current_price, trade_strength, trading_value, session_status)

        rows.append({
            "timestamp": timestamp,
            "market": market,
            "session_status": session_status,
            "symbol": symbol,
            "name": candidate.get("name", symbol),
            "tier": candidate.get("tier", ""),
            "alert_type": candidate.get("alert_type", ""),
            "rs": candidate.get("rs", ""),
            "vcp_status": candidate.get("vcp_status", ""),
            "box_upper_price": candidate.get("box_upper_price"),
            "short_swing_score": candidate.get("short_swing_score", "-"),
            "position_swing_score": candidate.get("position_swing_score", "-"),
            "horizon_label": candidate.get("horizon_label", "-"),
            "short_reasons": _fmt_reasons(candidate.get("short_reasons")),
            "position_reasons": _fmt_reasons(candidate.get("position_reasons")),
            "current_price": current_price,
            "change_rate": change_rate,
            "trade_strength": trade_strength,
            "trading_value": trading_value,
            "foreign_flow": stock.foreign_flow if stock and supply_status == "OK" else None,
            "institution_flow": stock.institution_flow if stock and supply_status == "OK" else None,
            "individual_flow": stock.individual_flow if stock and supply_status == "OK" else None,
            "supply_status": supply_status,
            "supply_updated_at": stock.supply_updated_at if stock else "",
            "decision": decision["decision"],
            "reasons": decision["reasons"],
            "failed_conditions": decision["failed_conditions"],
            "generated_at": candidate.get("generated_at", ""),
        })
    return rows


def _valid_signal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if float(row.get("current_price") or 0) > 0
        and row.get("decision") != "DATA_WAIT"
    ]


def save_signal_journal(theme_data: dict[str, Any], market: str = "KR", path: Path | None = None) -> dict[str, Any]:
    try:
        now = datetime.now(KST)
        rows = build_signal_rows(theme_data, market=market, now=now)
        valid_rows = _valid_signal_rows(rows)
        journal_path = path or signal_journal_path(now=now)
        if not valid_rows:
            result = {
                "ok": True,
                "path": str(journal_path),
                "saved_count": 0,
                "skipped_count": len(rows),
                "skip_reason": "no_valid_rows",
            }
            logger.info("signal_journal_skipped", **result)
            return result

        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(journal_path, "a", encoding="utf-8") as f:
            for row in valid_rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        result = {
            "ok": True,
            "path": str(journal_path),
            "saved_count": len(valid_rows),
            "skipped_count": len(rows) - len(valid_rows),
        }
        logger.info("signal_journal_saved", **result)
        return result
    except Exception as e:
        logger.warning("signal_journal_save_failed", error=str(e))
        return {"ok": False, "path": str(path or signal_journal_path()), "saved_count": 0, "error": str(e)}
