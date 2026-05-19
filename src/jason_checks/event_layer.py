"""Intraday event classification shared by server-side signal rows."""

from __future__ import annotations

from typing import Any


LEVEL_PRIORITY = {
    "DATA_WAIT": 0,
    "OFF": 1,
    "L1": 2,
    "L2": 3,
    "L3": 4,
    "RISK": 5,
}


def _candidate_get(candidate: Any, key: str, default: Any = "") -> Any:
    if candidate is None:
        return default
    getter = getattr(candidate, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        return default if value is None else value
    return default


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_box_price(value: float) -> str:
    if value <= 0:
        return "-"
    return f"{value:,.15g}"


def _event(level: str, event_type: str, reason: str, should_alert: bool) -> dict[str, Any]:
    return {
        "event_level": level,
        "event_type": event_type,
        "event_reason": reason,
        "event_should_alert": bool(should_alert),
    }


def compute_event(
    candidate: Any,
    current_price: Any,
    change_rate: Any,
    trade_strength: Any,
    session_status: Any,
) -> dict[str, Any]:
    """Compute the minimal server-side event used by signal_journal rows."""
    price = _to_float(current_price)
    chg = _to_float(change_rate)
    strength = _to_float(trade_strength)
    box = _to_float(_candidate_get(candidate, "box_upper_price"))
    horizon = str(_candidate_get(candidate, "horizon_label", "") or "")
    alert_type = str(
        _candidate_get(
            candidate,
            "alert_type",
            _candidate_get(
                candidate,
                "watch_alert_type",
                _candidate_get(candidate, "final_label", ""),
            ),
        )
        or ""
    )

    if str(session_status or "").lower() != "regular":
        return _event("OFF", "MARKET_CLOSED", "장마감/비정규 세션", False)
    if price <= 0:
        return _event("DATA_WAIT", "NO_PRICE", "현재가 대기", False)

    candidates: list[dict[str, Any]] = []
    box_text = _format_box_price(box)

    if box > 0:
        if price < box:
            gap_pct = ((box - price) / box) * 100
            if gap_pct <= 1.0:
                candidates.append(
                    _event(
                        "L1",
                        "BOX_NEAR",
                        f"BOX {box_text} 상단 근접 (이격 {gap_pct:.2f}%)",
                        False,
                    )
                )
        else:
            if strength < 100:
                candidates.append(
                    _event(
                        "RISK",
                        "FAKEOUT_RISK",
                        f"BOX {box_text} 돌파했지만 체결강도 약함 (체결강도 {strength:.0f})",
                        True,
                    )
                )
            elif strength < 120:
                candidates.append(
                    _event(
                        "L2",
                        "BREAKOUT_WATCH",
                        f"BOX {box_text} 돌파 관찰 · 체결강도 {strength:.0f}",
                        True,
                    )
                )
            else:
                candidates.append(
                    _event(
                        "L3",
                        "BREAKOUT_STRONG",
                        f"BOX {box_text} 돌파 + 강한 체결강도 {strength:.0f}",
                        True,
                    )
                )

    if chg >= 7.0 and strength < 110:
        candidates.append(
            _event(
                "RISK",
                "WEAK_CHASE_RISK",
                f"급등(+{chg:.2f}%)했지만 체결강도 부족({strength:.0f}) · 추격주의",
                True,
            )
        )
    if chg <= -3.0:
        candidates.append(
            _event(
                "RISK",
                "DROP_WATCH",
                f"후보 종목 장중 급락 관찰 ({chg:.2f}%)",
                True,
            )
        )

    if not candidates:
        chosen = _event("L1", "OBSERVING", "특이 신호 없음", False)
    else:
        chosen = max(candidates, key=lambda item: LEVEL_PRIORITY.get(item["event_level"], -1))

    if (
        (horizon == "CHASE_RISK" or alert_type == "RISK_WATCH")
        and "추격주의" not in chosen["event_reason"]
    ):
        chosen = {**chosen, "event_reason": chosen["event_reason"] + " · 추격주의"}

    return chosen
