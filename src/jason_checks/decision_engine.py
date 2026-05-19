"""Decision Engine v1 - 조건부 매수 의사결정 보조기.

Decisions: BUY_NOW / STARTER_POSITION / CONDITIONAL_BUY / WATCH_ONLY / AVOID
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from collections import deque

import structlog

logger = structlog.get_logger()
KST = ZoneInfo("Asia/Seoul")

# ── Signal history for stability (deque per symbol, max 5 entries) ──────────
_signal_history: dict[str, deque] = {}

# ── Decision journal path ────────────────────────────────────────────────────
def _decision_journal_path() -> Path:
    project_root = Path(__file__).parent.parent.parent
    today = datetime.now(KST).strftime("%Y-%m-%d")
    return project_root / "data" / "signal_journal" / "decision_signals.jsonl"


def _is_market_open(session: str) -> bool:
    return session in ("REGULAR",)


def _assess_market_gate(indices: dict[str, Any]) -> dict[str, Any]:
    """KOSPI+KOSDAQ 지수 상태를 판단하여 시장 게이트 결과를 반환."""
    if not indices:
        return {"ok": True, "reason": "지수 미확인", "level": "UNKNOWN"}

    pcts = []
    for idx in indices.values():
        pct = float(idx.get("change_pct") or 0)
        pcts.append(pct)

    if not pcts:
        return {"ok": True, "reason": "지수 미확인", "level": "UNKNOWN"}

    avg_pct = sum(pcts) / len(pcts)
    min_pct = min(pcts)

    if min_pct <= -2.0:
        return {"ok": False, "reason": f"지수 급락 ({min_pct:.2f}%)", "level": "CRASH"}
    if avg_pct <= -1.0:
        return {"ok": False, "reason": f"지수 약세 (평균 {avg_pct:.2f}%)", "level": "WEAK"}
    if avg_pct < 0:
        return {"ok": True, "reason": f"지수 소폭 약세 ({avg_pct:.2f}%)", "level": "SOFT"}
    return {"ok": True, "reason": f"지수 보통 ({avg_pct:.2f}%)", "level": "OK"}


def _assess_sector_gate(theme: dict[str, Any]) -> dict[str, Any]:
    """섹터 내 종목 상태를 판단."""
    leaders = theme.get("leaders", [])
    if not leaders:
        return {"confirmed": False, "reason": "섹터 데이터 없음", "avg_change": 0.0}

    avg_change = float(theme.get("avg_change_pct") or 0)
    positive_count = sum(1 for l in leaders if float(l.get("change_pct") or 0) > 0)
    live_count = sum(1 for l in leaders if float(l.get("price") or 0) > 0)

    if avg_change <= -2.0:
        return {"confirmed": False, "reason": f"섹터 급락 ({avg_change:.2f}%)", "avg_change": avg_change}
    if positive_count < 2 and live_count >= 2:
        return {"confirmed": False, "reason": f"섹터 내 강세 종목 부족 ({positive_count}/{live_count})", "avg_change": avg_change}
    return {"confirmed": True, "reason": f"섹터 OK ({positive_count}/{max(live_count,1)} 양호)", "avg_change": avg_change}


def _data_confidence(stock: dict[str, Any]) -> str:
    """데이터 신뢰도 판단: HIGH / MID / LOW"""
    price = float(stock.get("price") or 0)
    trading_value = int(stock.get("cumulative_trading_value") or 0)
    supply_status = stock.get("supply_status", "DATA_NA")

    if price <= 0:
        return "LOW"
    if supply_status == "DATA_NA" or trading_value <= 0:
        return "MID"
    return "HIGH"


def _chase_risk(change_pct: float, strength: float, box_price: float, price: float) -> bool:
    """과열/추격 위험 여부."""
    if change_pct >= 7.0:
        return True
    if box_price > 0 and price > 0:
        gap_pct = ((price - box_price) / box_price) * 100
        if gap_pct >= 5.0:
            return True
    if strength > 0 and change_pct >= 5.0 and strength < 110:
        return True
    return False


def _signal_stable(symbol: str, decision: str) -> bool:
    """신호 안정화: 최근 3회 중 2회 같은 decision 이어야 확정."""
    hist = _signal_history.get(symbol)
    if not hist:
        return False
    recent = list(hist)[-3:]
    count = sum(1 for d in recent if d == decision)
    return count >= 2


def _record_signal(symbol: str, decision: str):
    if symbol not in _signal_history:
        _signal_history[symbol] = deque(maxlen=5)
    _signal_history[symbol].append(decision)


def evaluate_stock(
    stock: dict[str, Any],
    session: str,
    market_gate: dict[str, Any],
    sector_gate: dict[str, Any],
    theme_name: str = "",
) -> dict[str, Any]:
    """단일 종목에 대한 Decision 계산."""

    symbol = str(stock.get("code") or stock.get("symbol") or "")
    name = str(stock.get("name") or symbol)
    price = float(stock.get("price") or 0)
    change_pct = float(stock.get("change_pct") or 0)
    strength = float(stock.get("strength") or 0)
    trading_value = int(stock.get("cumulative_trading_value") or 0)
    supply_status = str(stock.get("supply_status") or "DATA_NA")
    alert_type = str(stock.get("alert_type") or "")
    vcp_status = str(stock.get("vcp_status") or "")
    box_price = float(stock.get("box_upper_price") or 0)

    data_conf = _data_confidence(stock)
    chase = _chase_risk(change_pct, strength, box_price, price)

    no_buy_reasons: list[str] = []
    entry_trigger_parts: list[str] = []
    action_reason = ""
    invalidation = ""
    max_position_pct = 0
    required_confirmations: list[str] = []
    confidence_score = 50

    # ── MARKET_CLOSED 처리 ──────────────────────────────────────────────────
    if not _is_market_open(session):
        _record_signal(symbol, "WATCH_ONLY")
        return {
            "symbol": symbol, "name": name,
            "decision": "WATCH_ONLY",
            "decision_display": "내일 장중 확인",
            "confidence_score": 0,
            "data_confidence": data_conf,
            "action_reason": "장 마감",
            "no_buy_reason": "MARKET_CLOSED",
            "entry_trigger": "",
            "invalidation_reason": "",
            "chase_risk": False,
            "max_position_pct": 0,
            "required_confirmations": [],
            "stable": False,
            "theme": theme_name,
        }

    # ── Hard Gates ──────────────────────────────────────────────────────────
    if price <= 0:
        no_buy_reasons.append("현재가 없음")
        _record_signal(symbol, "AVOID")
        return _make_result(symbol, name, "AVOID", 0, data_conf,
                            "데이터 대기", "현재가 없음", "", "",
                            False, 0, [], theme_name, False)

    if alert_type == "RISK_WATCH":
        no_buy_reasons.append("RISK_WATCH 경보")
        confidence_score = max(0, confidence_score - 30)

    if vcp_status == "REVERSE_EXPANSION":
        no_buy_reasons.append("VCP 역수축")
        confidence_score = max(0, confidence_score - 30)

    if chase:
        no_buy_reasons.append(f"과열 추격 위험 (+{change_pct:.1f}%)")
        confidence_score = max(0, confidence_score - 20)

    if not market_gate["ok"]:
        no_buy_reasons.append(f"시장 게이트: {market_gate['reason']}")
        confidence_score = max(0, confidence_score - 25)

    if trading_value < 500_000_000:
        no_buy_reasons.append(f"거래대금 부족 ({trading_value // 100_000_000}억)")
        confidence_score = max(0, confidence_score - 20)

    if supply_status == "DATA_NA":
        confidence_score = max(0, confidence_score - 10)

    # ── 데이터 신뢰도 보너스 ─────────────────────────────────────────────────
    if data_conf == "HIGH":
        confidence_score = min(100, confidence_score + 15)
    elif data_conf == "LOW":
        confidence_score = max(0, confidence_score - 15)

    # ── 박스 위치 판단 ──────────────────────────────────────────────────────
    box_above = box_price > 0 and price > box_price
    box_gap_pct = ((price - box_price) / box_price * 100) if box_price > 0 else 0

    if box_price > 0:
        if box_above:
            confidence_score = min(100, confidence_score + 10)
            action_reason = f"BOX {box_price:,.0f} 돌파"
        else:
            no_buy_reasons.append(f"BOX {box_price:,.0f} 아래")
            confidence_score = max(0, confidence_score - 15)
            entry_trigger_parts.append(f"{box_price:,.0f} 돌파")
    else:
        no_buy_reasons.append("BOX 기준가 없음")
        entry_trigger_parts.append("BOX 설정 확인 필요")

    # ── 체결강도 보너스 ─────────────────────────────────────────────────────
    if strength >= 140:
        confidence_score = min(100, confidence_score + 15)
        action_reason += f" + 체결강도 {strength:.0f}"
    elif strength >= 110:
        confidence_score = min(100, confidence_score + 5)
    elif 0 < strength < 90:
        confidence_score = max(0, confidence_score - 10)
        no_buy_reasons.append(f"체결강도 부족 ({strength:.0f})")
        entry_trigger_parts.append("체결강도 110 이상 확인")

    # ── 거래대금 보너스 ─────────────────────────────────────────────────────
    if trading_value >= 5_000_000_000:
        confidence_score = min(100, confidence_score + 10)
    elif trading_value >= 1_000_000_000:
        confidence_score = min(100, confidence_score + 5)

    # ── 섹터 확인 ───────────────────────────────────────────────────────────
    if sector_gate["confirmed"]:
        confidence_score = min(100, confidence_score + 5)
    else:
        no_buy_reasons.append(f"섹터: {sector_gate['reason']}")
        confidence_score = max(0, confidence_score - 10)

    # ── entry_trigger 조립 ──────────────────────────────────────────────────
    if entry_trigger_parts:
        entry_trigger = " + ".join(entry_trigger_parts) + " + 거래대금 증가"
    else:
        entry_trigger = ""

    # ── invalidation ───────────────────────────────────────────────────────
    if box_price > 0:
        invalidation = f"전일 저점 이탈 또는 {box_price:,.0f} 재하향"
    else:
        invalidation = "섹터 약화 또는 거래대금 급감"

    # ── max_position_pct 결정 ──────────────────────────────────────────────
    if data_conf == "LOW":
        base_pct = 5
    elif data_conf == "MID":
        base_pct = 8
    else:
        base_pct = 15

    # ── Decision 분류 ──────────────────────────────────────────────────────
    fatal_gates = (
        alert_type == "RISK_WATCH"
        or vcp_status == "REVERSE_EXPANSION"
        or chase
        or not market_gate["ok"]
        or trading_value < 200_000_000
    )

    if fatal_gates:
        decision = "AVOID"
        max_position_pct = 0
        action_reason = action_reason or "조건 미충족"
    elif not box_above:
        decision = "CONDITIONAL_BUY"
        max_position_pct = 0
        action_reason = f"BOX {box_price:,.0f} 돌파 대기"
        required_confirmations = ["BOX 돌파", "거래대금 10억 이상", "체결강도 110 이상"]
    elif (
        box_above
        and not no_buy_reasons
        and market_gate["ok"]
        and sector_gate["confirmed"]
        and strength >= 110
        and trading_value >= 1_000_000_000
        and not chase
    ):
        decision = "BUY_NOW"
        max_position_pct = base_pct
        action_reason = action_reason or "모든 조건 충족"
    elif supply_status == "DATA_NA" or len(no_buy_reasons) <= 1:
        decision = "STARTER_POSITION"
        max_position_pct = min(base_pct, 8)
        action_reason = action_reason or "부분 조건 충족"
        required_confirmations = ["수급 확인", "거래대금 증가"]
    else:
        decision = "WATCH_ONLY"
        max_position_pct = 0
        action_reason = action_reason or "관찰 유지"

    stable = _signal_stable(symbol, decision)
    _record_signal(symbol, decision)

    return _make_result(
        symbol, name, decision, confidence_score, data_conf,
        action_reason, "; ".join(no_buy_reasons) if no_buy_reasons else "",
        entry_trigger, invalidation,
        chase, max_position_pct, required_confirmations,
        theme_name, stable,
    )


def _make_result(symbol, name, decision, score, data_conf,
                 action_reason, no_buy_reason, entry_trigger,
                 invalidation_reason, chase, max_pct, req_conf,
                 theme, stable) -> dict[str, Any]:
    decision_display = {
        "BUY_NOW": "매수 가능",
        "STARTER_POSITION": "소량 선취",
        "CONDITIONAL_BUY": "조건부 매수",
        "WATCH_ONLY": "관찰",
        "AVOID": "매수 금지",
    }.get(decision, decision)
    return {
        "symbol": symbol,
        "name": name,
        "decision": decision,
        "decision_display": decision_display,
        "confidence_score": max(0, min(100, int(score))),
        "data_confidence": data_conf,
        "action_reason": action_reason,
        "no_buy_reason": no_buy_reason,
        "entry_trigger": entry_trigger,
        "invalidation_reason": invalidation_reason,
        "chase_risk": chase,
        "max_position_pct": max_pct,
        "required_confirmations": req_conf,
        "stable": stable,
        "theme": theme,
    }


def run_decision_engine(
    alphaforge_picks: list[dict[str, Any]],
    themes: dict[str, Any],
    indices: dict[str, Any],
    session: str,
) -> dict[str, Any]:
    """AlphaForge 후보 전체에 대한 Decision 일괄 계산."""

    market_gate = _assess_market_gate(indices)
    results: list[dict[str, Any]] = []

    for stock in alphaforge_picks:
        # 해당 종목이 속한 테마 찾기
        code = str(stock.get("code") or "")
        theme_name = ""
        sector_gate = {"confirmed": True, "reason": "테마 없음", "avg_change": 0.0}
        for t_name, t_data in themes.items():
            leaders = t_data.get("leaders", [])
            if any(str(l.get("code") or "") == code for l in leaders):
                theme_name = t_name
                sector_gate = _assess_sector_gate(t_data)
                break

        result = evaluate_stock(stock, session, market_gate, sector_gate, theme_name)
        results.append(result)

    # Decision counts
    decision_counts = {
        "BUY_NOW": 0,
        "STARTER_POSITION": 0,
        "CONDITIONAL_BUY": 0,
        "WATCH_ONLY": 0,
        "AVOID": 0,
    }
    for r in results:
        d = r.get("decision", "WATCH_ONLY")
        if d in decision_counts:
            decision_counts[d] += 1

    top_actions = [r for r in results if r["decision"] in ("BUY_NOW", "STARTER_POSITION")]
    top_actions.sort(key=lambda x: x["confidence_score"], reverse=True)

    no_buy_reasons = [
        {"symbol": r["symbol"], "name": r["name"], "reason": r["no_buy_reason"]}
        for r in results if r.get("no_buy_reason")
    ]

    data_confidence_summary = {
        "HIGH": sum(1 for r in results if r["data_confidence"] == "HIGH"),
        "MID": sum(1 for r in results if r["data_confidence"] == "MID"),
        "LOW": sum(1 for r in results if r["data_confidence"] == "LOW"),
    }

    journal_status = _write_decision_journal(results, session, market_gate)

    return {
        "market_gate": market_gate,
        "session": session,
        "decision_counts": decision_counts,
        "results": results,
        "top_actions": top_actions,
        "no_buy_reasons": no_buy_reasons,
        "data_confidence_summary": data_confidence_summary,
        "journal_write_status": journal_status,
    }


def _write_decision_journal(results: list[dict], session: str, market_gate: dict) -> dict:
    """decision_signals.jsonl 에 기록."""
    try:
        path = _decision_journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        now_ts = datetime.now(KST).isoformat()
        written = 0
        with open(path, "a", encoding="utf-8") as f:
            for r in results:
                if r.get("decision") in ("AVOID",) and not r.get("confidence_score", 0):
                    continue
                row = {
                    "timestamp": now_ts,
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "decision": r["decision"],
                    "confidence_score": r["confidence_score"],
                    "price": None,
                    "trigger": r.get("entry_trigger", ""),
                    "invalidation": r.get("invalidation_reason", ""),
                    "max_position_pct": r["max_position_pct"],
                    "market_state": market_gate.get("level", ""),
                    "sector_state": r.get("theme", ""),
                    "no_buy_reason": r.get("no_buy_reason", ""),
                    "data_confidence": r["data_confidence"],
                }
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                written += 1
        return {"ok": True, "path": str(path), "written": written}
    except Exception as e:
        logger.warning("decision_journal_write_failed", error=str(e))
        return {"ok": False, "error": str(e)}
