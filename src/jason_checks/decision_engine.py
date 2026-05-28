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
from collections import Counter

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
        return {
            "ok": True,
            "reason": "지수 미확인",
            "level": "NORMAL",
            "market_gate_level": "NORMAL",
            "market_gate_reason": "지수 미확인",
            "market_gate_blocks_buy_now": False,
        }

    pcts = []
    for idx in indices.values():
        if idx.get("source") in ("dummy", "mock") or float(idx.get("price") or 0) <= 0:
            continue
        pct = float(idx.get("change_pct") or 0)
        pcts.append(pct)

    if not pcts:
        return {
            "ok": True,
            "reason": "지수 미확인",
            "level": "NORMAL",
            "market_gate_level": "NORMAL",
            "market_gate_reason": "지수 미확인",
            "market_gate_blocks_buy_now": False,
        }

    avg_pct = sum(pcts) / len(pcts)
    min_pct = min(pcts)

    if min_pct <= -3.0:
        level = "CRASH"
        reason = f"지수 급락 ({min_pct:.2f}%)"
    elif min_pct <= -2.0:
        level = "RISK_OFF"
        reason = f"지수 위험회피 ({min_pct:.2f}%)"
    elif min_pct <= -1.0 or avg_pct <= -0.5:
        level = "CAUTION"
        reason = f"지수 주의 (최저 {min_pct:.2f}%, 평균 {avg_pct:.2f}%)"
    else:
        level = "NORMAL"
        reason = f"지수 보통 ({avg_pct:.2f}%)"

    blocks = level in ("RISK_OFF", "CRASH")
    return {
        "ok": not blocks,
        "reason": reason,
        "level": level,
        "market_gate_level": level,
        "market_gate_reason": reason,
        "market_gate_blocks_buy_now": blocks,
    }


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


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _quote_age_sec(stock: dict[str, Any]) -> int | None:
    updated_at = stock.get("updated_at") or stock.get("quote_updated_at")
    if not updated_at:
        return None
    try:
        dt = datetime.fromisoformat(str(updated_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return max(0, int((datetime.now(KST) - dt.astimezone(KST)).total_seconds()))
    except (TypeError, ValueError):
        return None


def _data_quality(stock: dict[str, Any]) -> dict[str, Any]:
    """Data Confidence v2: quote/supply 품질을 구조화해서 반환."""
    price = _to_float(stock.get("price"))
    trading_value = int(_to_float(stock.get("cumulative_trading_value")))
    strength = _to_float(stock.get("strength"))
    supply_status = str(stock.get("supply_status") or "DATA_NA")
    supply_recency = str(stock.get("supply_recency") or "UNKNOWN")
    supply_timestamp = str(stock.get("supply_updated_at") or "")
    supply_age = _quote_age_sec({"updated_at": supply_timestamp}) if supply_timestamp else None
    quote_age = _quote_age_sec(stock)

    has_price = price > 0
    has_trading_value = trading_value > 0
    has_strength = strength > 0
    has_supply = supply_status == "OK" and supply_recency in ("TODAY", "PREV_DAY")
    quote_stale = quote_age is not None and quote_age > 180
    flags: list[str] = []

    if not has_price:
        flags.append("PRICE_MISSING")
    if not has_trading_value:
        flags.append("TRADING_VALUE_MISSING")
    if not has_strength:
        flags.append("STRENGTH_MISSING")
    if not has_supply:
        flags.append("SUPPLY_DATA_NA")
    if supply_status == "RATE_LIMIT":
        flags.append("SUPPLY_RATE_LIMIT")
    if supply_age is not None and supply_age > 30 * 60:
        flags.append("SUPPLY_STALE")
    if quote_age is None:
        flags.append("QUOTE_TIMESTAMP_MISSING")
    elif quote_stale:
        flags.append("QUOTE_STALE")

    if not has_price:
        confidence = "LOW"
    elif has_price and has_trading_value:
        confidence = "MID"
        if has_strength and has_supply and supply_recency == "TODAY" and not quote_stale:
            confidence = "HIGH"
    else:
        confidence = "LOW"

    # 수급 미확인은 절대 HIGH가 되지 않게 한다.
    if not has_supply and confidence == "HIGH":
        confidence = "MID"
    # 전영업일 수급은 HIGH가 되지 않게 한다.
    if supply_recency == "PREV_DAY" and confidence == "HIGH":
        confidence = "MID"
    if quote_stale and confidence == "HIGH":
        confidence = "MID"

    return {
        "data_confidence": confidence,
        "data_quality_flags": flags,
        "quote_age_sec": quote_age,
        "has_price": has_price,
        "has_trading_value": has_trading_value,
        "has_strength": has_strength,
        "has_supply": has_supply,
        "supply_status": supply_status,
        "supply_recency": supply_recency,
        "supply_date": str(stock.get("supply_date") or ""),
        "supply_timestamp": supply_timestamp,
        "supply_age_sec": supply_age,
    }


def _data_confidence(stock: dict[str, Any]) -> str:
    return str(_data_quality(stock)["data_confidence"])


def _base_reason_codes(
    price: float,
    strength: float,
    alert_type: str,
    vcp_status: str,
    box_price: float,
    market_gate: dict[str, Any],
    sector_gate: dict[str, Any],
    supply_status: str,
    stock: dict[str, Any],
) -> list[str]:
    codes: list[str] = []
    gate_level = market_gate.get("market_gate_level") or market_gate.get("level")
    if gate_level == "CRASH":
        codes.append("MARKET_CRASH")
    elif gate_level == "RISK_OFF":
        codes.append("MARKET_RISK_OFF")
    if alert_type == "RISK_WATCH":
        codes.append("RISK_WATCH")
    if vcp_status == "REVERSE_EXPANSION":
        codes.append("VCP_REVERSE_EXPANSION")
    if price <= 0:
        codes.append("PRICE_MISSING")
    if box_price > 0 and price > 0 and price < box_price:
        codes.append("BELOW_BOX")
    if not sector_gate.get("confirmed", True):
        codes.append("SECTOR_WEAK")
    supply_age = _quote_age_sec({"updated_at": stock.get("supply_updated_at")}) if stock.get("supply_updated_at") else None
    foreign_flow = int(_to_float(stock.get("foreign_flow")))
    institution_flow = int(_to_float(stock.get("institution_flow")))
    individual_flow = int(_to_float(stock.get("individual_flow")))

    supply_recency = str(stock.get("supply_recency") or "UNKNOWN")
    if supply_recency == "TODAY":
        codes.append("SUPPLY_TODAY")
    elif supply_recency == "PREV_DAY":
        codes.append("SUPPLY_PREV_DAY")

    if supply_status == "RATE_LIMIT":
        codes.append("SUPPLY_RATE_LIMIT")
    elif supply_status == "DATA_NA" or supply_recency == "UNKNOWN":
        codes.append("SUPPLY_DATA_NA")
    elif supply_age is not None and supply_age > 30 * 60:
        codes.append("SUPPLY_STALE")

    if supply_status == "OK" and supply_recency in ("TODAY", "PREV_DAY"):
        if foreign_flow > 0 and institution_flow > 0:
            codes.append("SUPPLY_POSITIVE")
        elif foreign_flow < 0 and institution_flow < 0:
            codes.append("SUPPLY_NEGATIVE")
        elif any(v != 0 for v in (foreign_flow, institution_flow, individual_flow)):
            codes.append("SUPPLY_MIXED")

    if 0 < strength < 90:
        codes.append("STRENGTH_WEAK")
    return list(dict.fromkeys(codes))


def _setup_label(score: int, risk_only: bool) -> str:
    if risk_only or score < 30:
        return "RISK_ONLY"
    if score >= 70:
        return "STRONG_SETUP"
    if score >= 50:
        return "SETUP_WATCH"
    return "WEAK_SETUP"


def _setup_profile(
    stock: dict[str, Any],
    data_conf: str,
    sector_gate: dict[str, Any],
) -> dict[str, Any]:
    """장마감 후 다음 세션 관찰 가치를 별도 점수로 평가."""
    price = _to_float(stock.get("price"))
    change_pct = _to_float(stock.get("change_pct"))
    strength = _to_float(stock.get("strength"))
    trading_value = int(_to_float(stock.get("cumulative_trading_value")))
    supply_status = str(stock.get("supply_status") or "DATA_NA")
    alert_type = str(stock.get("alert_type") or "")
    vcp_status = str(stock.get("vcp_status") or "")
    box_price = _to_float(stock.get("box_upper_price"))
    rs = _to_float(stock.get("rs"))
    reasons_text = " ".join(
        str(stock.get(key) or "")
        for key in ("short_reasons", "position_reasons", "horizon_label")
    )

    score = 35
    reasons: list[str] = []
    risk_only = False

    if price > 0:
        score += 8
        reasons.append("현재가 확인")
    else:
        score -= 20
        reasons.append("현재가 대기")

    if rs >= 95:
        score += 18
        reasons.append(f"RS 상위 {rs:.0f}")
    elif rs >= 85:
        score += 14
        reasons.append(f"RS 강함 {rs:.0f}")
    elif rs >= 70:
        score += 8
        reasons.append(f"RS 양호 {rs:.0f}")

    if trading_value >= 5_000_000_000:
        score += 18
        reasons.append("거래대금 강함")
    elif trading_value >= 1_000_000_000:
        score += 10
        reasons.append("거래대금 확인")
    elif trading_value > 0:
        score += 3

    if "정배열" in reasons_text:
        score += 8
        reasons.append("정배열")

    if box_price > 0 and price > 0:
        if price >= box_price:
            score += 10
            reasons.append("BOX 돌파권")
        else:
            gap_pct = ((box_price - price) / box_price) * 100
            if gap_pct <= 5:
                score += 8
                reasons.append(f"BOX 근접 {gap_pct:.1f}%")
            else:
                score -= 4
                reasons.append("BOX 아래")
    elif box_price <= 0:
        score -= 8
        reasons.append("BOX 기준가 없음")

    if strength >= 120:
        score += 8
        reasons.append("체결강도 강함")
    elif 0 < strength < 90:
        score -= 6
        reasons.append("체결강도 약함")

    if supply_status == "DATA_NA":
        score -= 8
        reasons.append("수급 DATA_NA")

    if change_pct >= 7:
        score -= 15
        reasons.append("급등 추격 위험")
    elif change_pct <= -5:
        score -= 8
        reasons.append("낙폭 큼")

    if alert_type == "RISK_WATCH":
        score -= 22
        reasons.append("RISK_WATCH")
    if vcp_status == "REVERSE_EXPANSION":
        score -= 22
        reasons.append("VCP 역수축")
    if alert_type == "RISK_WATCH" and vcp_status == "REVERSE_EXPANSION":
        score -= 18
        risk_only = True
        reasons.append("위험 신호 중첩")

    if not sector_gate.get("confirmed", True):
        score -= 6
        reasons.append(f"섹터 약함: {sector_gate.get('reason', '-')}")

    score = max(0, min(100, int(round(score))))
    trigger = ""
    if box_price > 0:
        trigger = f"{box_price:,.0f} 돌파 + 거래대금 증가"
        if strength <= 0 or strength < 110:
            trigger += " + 체결강도 110 이상"
    elif price > 0:
        trigger = "장중 고점 돌파 + 거래대금 증가"

    if risk_only:
        plan = "위험 신호 해소 전까지 매수 금지"
    elif box_price > 0 and price > 0 and price < box_price:
        plan = "내일 장중 박스 돌파 확인 전까지 매수 금지"
    elif score >= 70:
        plan = "내일 장중 거래대금과 체결강도 확인 후 소량 검토"
    else:
        plan = "내일 장중 가격/거래대금 재확인"

    return {
        "setup_score": score,
        "setup_label": _setup_label(score, risk_only),
        "next_session_trigger": trigger,
        "next_session_plan": plan,
        "setup_reason": " · ".join(reasons[:6]) or "관찰 데이터 대기",
    }


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

    merge_failed = stock.get("merge_failed", False)
    polling_in_progress = stock.get("polling_in_progress", False)

    data_quality = _data_quality(stock)
    data_conf = str(data_quality["data_confidence"])
    chase = _chase_risk(change_pct, strength, box_price, price)
    setup = _setup_profile(stock, data_conf, sector_gate)
    reason_codes = _base_reason_codes(
        price, strength, alert_type, vcp_status, box_price,
        market_gate, sector_gate, supply_status, stock,
    )

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
            "price": price,
            "box_upper_price": box_price,
            "reason_codes": reason_codes,
            **data_quality,
            **setup,
            "merge_failed": merge_failed,
        }

    # ── Hard Gates ──────────────────────────────────────────────────────────
    if price <= 0:
        if polling_in_progress:
            _record_signal(symbol, "DATA_WAIT")
            result = _make_result(symbol, name, "DATA_WAIT", 10, "LOW",
                                  "현재가 폴링 대기 중", "DATA_WAIT", "", "",
                                  False, 0, [], theme_name, False,
                                  price, box_price, reason_codes, data_quality,
                                  merge_failed=merge_failed)
            result.update(setup)
            return result
        else:
            no_buy_reasons.append("현재가 없음")
            _record_signal(symbol, "AVOID")
            result = _make_result(symbol, name, "AVOID", 0, data_conf,
                                  "데이터 대기", "현재가 없음", "", "",
                                  False, 0, [], theme_name, False,
                                  price, box_price, reason_codes, data_quality,
                                  merge_failed=merge_failed)
            result.update(setup)
            return result

    if alert_type == "RISK_WATCH":
        no_buy_reasons.append("RISK_WATCH 경보")
        confidence_score = max(0, confidence_score - 30)

    if vcp_status == "REVERSE_EXPANSION":
        no_buy_reasons.append("VCP 역수축")
        confidence_score = max(0, confidence_score - 30)

    if chase:
        no_buy_reasons.append(f"과열 추격 위험 ({change_pct:+.1f}%)")
        confidence_score = max(0, confidence_score - 20)

    if not market_gate["ok"]:
        no_buy_reasons.append(f"시장 게이트: {market_gate['reason']}")
        confidence_score = max(0, confidence_score - 25)

    if trading_value < 500_000_000:
        no_buy_reasons.append(f"거래대금 부족 ({trading_value // 100_000_000}억)")
        confidence_score = max(0, confidence_score - 20)

    if not data_quality.get("has_supply"):
        confidence_score = max(0, confidence_score - 10)
    elif data_quality.get("supply_recency") == "PREV_DAY":
        confidence_score = min(100, confidence_score + 3)

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
        or market_gate.get("market_gate_blocks_buy_now", not market_gate["ok"])
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
        and not market_gate.get("market_gate_blocks_buy_now", not market_gate["ok"])
        and sector_gate["confirmed"]
        and strength >= 110
        and trading_value >= 1_000_000_000
        and not chase
    ):
        decision = "BUY_NOW"
        max_position_pct = base_pct
        action_reason = action_reason or "모든 조건 충족"
    elif not data_quality.get("has_supply") or len(no_buy_reasons) <= 1:
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

    result = _make_result(
        symbol, name, decision, confidence_score, data_conf,
        action_reason, "; ".join(no_buy_reasons) if no_buy_reasons else "",
        entry_trigger, invalidation,
        chase, max_position_pct, required_confirmations,
        theme_name, stable, price, box_price, reason_codes, data_quality,
        merge_failed=merge_failed
    )
    result.update(setup)
    return result


def _make_result(symbol, name, decision, score, data_conf,
                 action_reason, no_buy_reason, entry_trigger,
                 invalidation_reason, chase, max_pct, req_conf,
                 theme, stable, price=0.0, box_price=0.0,
                 reason_codes=None, data_quality=None, merge_failed=False) -> dict[str, Any]:
    decision_display = {
        "BUY_NOW": "매수 가능",
        "STARTER_POSITION": "소량 선취",
        "CONDITIONAL_BUY": "조건부 매수",
        "WATCH_ONLY": "관찰",
        "AVOID": "매수 금지",
        "DATA_WAIT": "데이터 대기",
    }.get(decision, decision)
    quality = dict(data_quality or {})
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
        "price": price,
        "box_upper_price": box_price,
        "reason_codes": list(reason_codes or []),
        **quality,
        "setup_score": 0,
        "setup_label": "RISK_ONLY",
        "next_session_trigger": entry_trigger,
        "next_session_plan": action_reason or "장중 조건 확인",
        "setup_reason": action_reason or no_buy_reason or "관찰 데이터 대기",
        "merge_failed": merge_failed,
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
    sector_audit = _audit_sectors(themes)

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
        "DATA_WAIT": 0,
    }
    for r in results:
        d = r.get("decision", "WATCH_ONLY")
        if d in decision_counts:
            decision_counts[d] += 1

    top_actions = [r for r in results if r["decision"] in ("BUY_NOW", "STARTER_POSITION")]
    top_actions.sort(key=lambda x: x["confidence_score"], reverse=True)
    setup_candidates = [
        r for r in results
        if r.get("setup_label") != "RISK_ONLY" and r.get("setup_score", 0) > 0
    ]
    setup_top3 = sorted(
        setup_candidates,
        key=lambda x: x.get("setup_score", 0),
        reverse=True,
    )[:3]

    no_buy_reasons = [
        {"symbol": r["symbol"], "name": r["name"], "reason": r["no_buy_reason"]}
        for r in results if r.get("no_buy_reason")
    ]

    data_confidence_summary = {
        "HIGH": sum(1 for r in results if r["data_confidence"] == "HIGH"),
        "MID": sum(1 for r in results if r["data_confidence"] == "MID"),
        "LOW": sum(1 for r in results if r["data_confidence"] == "LOW"),
    }
    reason_code_counts = dict(Counter(
        code for r in results for code in r.get("reason_codes", [])
    ))
    decision_quality_summary = {
        "total": len(results),
        "has_price": sum(1 for r in results if r.get("has_price")),
        "has_trading_value": sum(1 for r in results if r.get("has_trading_value")),
        "has_strength": sum(1 for r in results if r.get("has_strength")),
        "has_supply": sum(1 for r in results if r.get("has_supply")),
        "quote_stale": sum(1 for r in results if "QUOTE_STALE" in r.get("data_quality_flags", [])),
        "price_missing": sum(1 for r in results if "PRICE_MISSING" in r.get("data_quality_flags", [])),
    }

    journal_status = _write_decision_journal(results, session, market_gate)

    forward_test_summary = {
        "status": "DATA_INSUFFICIENT",
        "message": "평가기 로드 실패"
    }
    try:
        from jason_checks.forward_test import run_forward_test
        forward_test_summary = run_forward_test()
    except Exception as e:
        logger.exception("forward_test_integration_failed", error=str(e))
        forward_test_summary = {
            "status": "DATA_INSUFFICIENT",
            "message": f"평가 중 예외 발생: {str(e)}",
            "horizons": {},
            "by_decision": {},
            "by_setup_label": {},
            "by_market_gate_level": {},
            "by_reason_code": {},
            "blocked_quality": {
                "blocked_count": 0,
                "good_block_count": 0,
                "missed_opportunity_count": 0,
                "missed_opportunity_symbols": [],
                "quality_grade": "아직 데이터 부족"
            }
        }

    return {
        "market_gate": market_gate,
        "market_gate_level": market_gate.get("market_gate_level"),
        "market_gate_reason": market_gate.get("market_gate_reason"),
        "market_gate_blocks_buy_now": market_gate.get("market_gate_blocks_buy_now"),
        "session": session,
        "decision_counts": decision_counts,
        "results": results,
        "top_actions": top_actions,
        "setup_top3": setup_top3,
        "no_buy_reasons": no_buy_reasons,
        "data_confidence_summary": data_confidence_summary,
        "data_confidence_counts": data_confidence_summary,
        "reason_code_counts": reason_code_counts,
        "decision_quality_summary": decision_quality_summary,
        "sector_audit_warnings": sector_audit["sector_audit_warnings"],
        "duplicated_symbols": sector_audit["duplicated_symbols"],
        "suspicious_sector_members": sector_audit["suspicious_sector_members"],
        "journal_write_status": journal_status,
        "journal_status": journal_status,
        "forward_test_summary": forward_test_summary,
    }


def _audit_sectors(themes: dict[str, Any]) -> dict[str, Any]:
    """섹터 구성 이상 징후를 경고만 한다. 자동 수정하지 않는다."""
    seen: dict[str, list[str]] = {}
    suspicious: list[dict[str, Any]] = []
    for theme_name, theme in (themes or {}).items():
        leaders = theme.get("leaders") or theme.get("stocks") or []
        for row in leaders:
            code = str(row.get("code") or row.get("symbol") or "")
            name = str(row.get("name") or "")
            if not code or not name:
                suspicious.append({"theme": theme_name, "code": code, "name": name, "reason": "missing_code_or_name"})
                continue
            seen.setdefault(code, []).append(theme_name)

    duplicated = [
        {"code": code, "themes": themes_for_code}
        for code, themes_for_code in seen.items()
        if len(set(themes_for_code)) > 1
    ]
    suspicious.extend(
        {"code": item["code"], "themes": item["themes"], "reason": "appears_in_3_or_more_sectors"}
        for item in duplicated
        if len(set(item["themes"])) >= 3
    )
    warnings: list[str] = []
    if duplicated:
        warnings.append(f"중복 섹터 종목 {len(duplicated)}개")
    if suspicious:
        warnings.append(f"섹터 구성 점검 필요 {len(suspicious)}건")
    return {
        "sector_audit_warnings": warnings,
        "duplicated_symbols": duplicated[:30],
        "suspicious_sector_members": suspicious[:30],
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
                price = float(r.get("price") or 0.0)
                row = {
                    "timestamp": now_ts,
                    "session": session,
                    "market_gate_level": market_gate.get("market_gate_level", market_gate.get("level", "")),
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "decision": r["decision"],
                    "confidence_score": r["confidence_score"],
                    "setup_score": r.get("setup_score", 0),
                    "setup_label": r.get("setup_label", ""),
                    "price": r.get("price"),
                    "box_upper_price": r.get("box_upper_price"),
                    "reason_codes": r.get("reason_codes", []),
                    "no_buy_reason": r.get("no_buy_reason", ""),
                    "entry_trigger": r.get("entry_trigger", ""),
                    "invalidation": r.get("invalidation_reason", ""),
                    "max_position_pct": r["max_position_pct"],
                    "data_confidence": r["data_confidence"],
                    "data_quality_flags": r.get("data_quality_flags", []),
                }
                if price <= 0.0:
                    row["excluded_reason"] = "PRICE_MISSING"
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                written += 1
        return {"ok": True, "path": str(path), "written": written}
    except Exception as e:
        logger.warning("decision_journal_write_failed", error=str(e))
        return {"ok": False, "error": str(e)}
