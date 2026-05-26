"""Telegram alert dispatcher with real sendMessage support.

이전 버전은 dry-run 전용이었음.
이 버전은 KR_TELEGRAM_DRY_RUN=false 일 때 실제 발송을 수행한다.

환경변수:
  KR_TELEGRAM_ENABLED=true   → 알람 시스템 활성 (false면 전체 비활성)
  KR_TELEGRAM_DRY_RUN=true   → true: 로그만, false: 실제 발송
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID → .env 에서만 읽음, 로그 출력 금지

제한:
  - 토큰/CHAT_ID 절대 로그 출력 금지
  - 실패 시 앱 중단 금지 (예외 흡수)
  - 대량 발송 방지: global max/hour, per-symbol cooldown
"""

from __future__ import annotations

import os
import time
import json
from datetime import datetime
from typing import Any

import structlog

from jason_checks.config import get_settings

logger = structlog.get_logger()

# ── 쿨다운 상수 ──────────────────────────────────────────────────────────────
_COOLDOWN_SEC = 1800          # AlphaForge 종목+이벤트 쿨다운 5분
_INDEX_COOLDOWN_SEC = 1800    # 지수 알람 쿨다운 15분
_TEST_SENT_KEY = "__test__"  # 테스트 발송 중복 방지 키

# ── 인메모리 상태 ─────────────────────────────────────────────────────────────
_recent_alerts: dict[str, float] = {}   # (symbol, event_type) → ts
_hourly_sent: list[float] = []                       # 전역 발송 시각 목록
_daily_sent: dict[str, int] = {}
_daily_reset_date: str = ""
_recent_events: list[dict[str, Any]] = []
_STATE_FILE = "data/runtime/telegram_alert_state.json"

def _load_state():
    global _recent_alerts, _hourly_sent, _daily_sent, _daily_reset_date, _recent_events
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r") as f:
                d = json.load(f)
            _recent_alerts = d.get("recent_alerts", {})
            _hourly_sent = d.get("hourly_sent", [])
            _daily_sent = d.get("daily_sent", {})
            _daily_reset_date = d.get("daily_reset_date", "")
            _recent_events = d.get("recent_events", [])
    except Exception:
        pass

def _save_state():
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump({
                "recent_alerts": _recent_alerts,
                "hourly_sent": _hourly_sent,
                "daily_sent": _daily_sent,
                "daily_reset_date": _daily_reset_date,
                "recent_events": _recent_events
            }, f)
    except Exception:
        pass

_load_state()


# ── 설정 상수 ─────────────────────────────────────────────────────────────────


def get_telegram_config() -> dict:
    return {
        "enabled": _is_enabled(),
        "dry_run": _is_dry_run(),
        "mode": os.environ.get("KR_TELEGRAM_MODE", "instant"),
        "min_level": os.environ.get("KR_TELEGRAM_MIN_LEVEL", "L2"),
        "open_mute_minutes": int(os.environ.get("KR_TELEGRAM_OPEN_MUTE_MINUTES", "5")),
        "daily_max_per_symbol": _get_daily_max_per_symbol(),
        "global_max_per_hour": _get_max_per_hour(),
        "muted_codes": [c.strip() for c in os.environ.get("KR_TELEGRAM_MUTED_CODES", "").split(",") if c.strip()],
    }

def telegram_healthcheck() -> None:
    cfg = get_telegram_config()
    logger.info(
        "telegram_healthcheck",
        env_loaded="KR_TELEGRAM_ENABLED" in os.environ,
        enabled=cfg["enabled"],
        dry_run=cfg["dry_run"],
        mode=cfg["mode"],
        min_level=cfg["min_level"],
        credentials_present=_credentials_present(),
        state_file_exists=os.path.exists(_STATE_FILE),
        daily_reset_date=_daily_reset_date,
        today=datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d"),
        daily_state_reset_needed=(_daily_reset_date != datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d")),
    )


def _get_max_per_hour() -> int:
    try: return int(os.environ.get("KR_TELEGRAM_GLOBAL_MAX_PER_HOUR", 10))
    except ValueError: return 10

def _get_daily_max_per_symbol() -> int:
    try: return int(os.environ.get("KR_TELEGRAM_DAILY_MAX_PER_SYMBOL", 2))
    except ValueError: return 2



# ─────────────────────────────────────────────────────────────────────────────
# 자격증명 / 플래그 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _get_credentials() -> tuple[str, str]:
    """Return (token, chat_id) from env. Never log them."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        try:
            settings = get_settings()
            token = token or getattr(settings, "telegram_bot_token", "") or ""
            chat = chat or str(getattr(settings, "telegram_chat_id", "") or "")
        except Exception:
            pass
    return token, chat


from dotenv import load_dotenv
load_dotenv()


def _parse_bool(val: str | None, default: bool) -> bool:
    if val is None or str(val).strip() == "":
        return default
    v = str(val).lower().strip()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _credentials_present() -> bool:
    token, chat = _get_credentials()
    return bool(token) and bool(chat)


def telegram_enabled() -> bool:
    """Return whether credentials exist, without exposing them."""
    return _credentials_present()


def _is_enabled() -> bool:
    """KR_TELEGRAM_ENABLED=true 이어야 활성."""
    return _parse_bool(os.environ.get("KR_TELEGRAM_ENABLED"), False)


def _is_dry_run() -> bool:
    """KR_TELEGRAM_DRY_RUN=false 일 때만 실발송. 기본값 true(안전 우선)."""
    return _parse_bool(os.environ.get("KR_TELEGRAM_DRY_RUN"), True)


# ─────────────────────────────────────────────────────────────────────────────
# 쿨다운 / 폭주 방지
# ─────────────────────────────────────────────────────────────────────────────

def _check_daily_reset(now: float | None = None) -> None:
    global _daily_sent, _daily_reset_date, _recent_alerts, _hourly_sent
    now_ts = now if now is not None else time.time()
    today = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
    if _daily_reset_date != today:
        _daily_sent.clear()
        cutoff = now_ts - 86400
        _recent_alerts = {k: v for k, v in _recent_alerts.items() if v >= cutoff}
        _hourly_sent = [v for v in _hourly_sent if v >= cutoff]
        _daily_reset_date = today
        _save_state()

def _should_send(
    symbol: str,
    event_type: str,
    event_level: str = "",
    now: float | None = None,
    mark: bool = True,
    cooldown_sec: float = _COOLDOWN_SEC,
) -> bool:
    now_ts = now if now is not None else time.time()
    _check_daily_reset(now_ts)
    key = f"{symbol}:{event_type}:{event_level}"
    last = _recent_alerts.get(key, 0.0)
    if now_ts - last < cooldown_sec:
        return False
    if mark:
        _recent_alerts[key] = now_ts
        if symbol and not symbol.startswith("IDX_") and symbol != "__test__":
            _daily_sent[symbol] = _daily_sent.get(symbol, 0) + 1
        _save_state()
    return True

def _daily_check(symbol: str, now: float | None = None) -> bool:
    now_ts = now if now is not None else time.time()
    _check_daily_reset(now_ts)
    if not symbol or symbol.startswith("IDX_") or symbol == "__test__":
        return True
    return _daily_sent.get(symbol, 0) < _get_daily_max_per_symbol()

def _global_hourly_check(now: float | None = None) -> bool:
    now_ts = now if now is not None else time.time()
    cutoff = now_ts - 3600
    while _hourly_sent and _hourly_sent[0] < cutoff:
        _hourly_sent.pop(0)
    return len(_hourly_sent) < _get_max_per_hour()

def _record_sent(now: float | None = None) -> None:
    _hourly_sent.append(now if now is not None else time.time())
    _save_state()

# ─────────────────────────────────────────────────────────────────────────────
# 실제 HTTP 발송
# ─────────────────────────────────────────────────────────────────────────────

async def _send_message(text: str) -> bool:
    """실제 Telegram sendMessage 호출. 토큰/chat_id 절대 로그 금지."""
    token, chat_id = _get_credentials()
    if not (token and chat_id):
        logger.warning("telegram_send_skipped", reason="no_credentials")
        return False
    try:
        import httpx
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        if ok:
            logger.info("telegram_sent", chars=len(text))
        else:
            # 오류 코드만 기록, 응답 body에 token 없으므로 안전
            logger.warning(
                "telegram_send_error",
                status=resp.status_code,
                error_code=resp.json().get("error_code"),
                description=resp.json().get("description", "")
            )
        return ok
    except Exception as e:
        logger.warning("telegram_send_exception", error=str(e))
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 이벤트 평가 (dry-run / 실발송 공용)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_event(row: dict[str, Any], *, mark_cooldown: bool = False) -> dict[str, Any]:
    """Evaluate Telegram alert gates. Does NOT send anything."""
    symbol = str(row.get("symbol") or row.get("code") or "")
    event_type = str(row.get("event_type") or "")
    level = str(row.get("event_level") or "")
    session = str(row.get("session_status") or "")

    def _get_blocked_by() -> list[str]:
        if not _is_enabled(): return ["disabled"]
        if not telegram_enabled(): return ["no_credentials"]
        cfg = get_telegram_config()
        min_level = cfg["min_level"].upper()
        allowed = ["RISK"] if min_level == "RISK" else ["L3", "RISK"] if min_level == "L3" else ["L2", "L3", "RISK"]
        if level not in allowed: return ["below_min_level"]
        if symbol in cfg["muted_codes"]: return ["muted_symbol"]
        if session.upper() != "REGULAR": return ["session_blocked"]
        if not _should_send(symbol, event_type, level, mark=mark_cooldown): return ["cooldown"]
        if not _daily_check(symbol): return ["daily_max"]
        if not _global_hourly_check(): return ["global_hourly_max"]
        return []

    blocked_by = _get_blocked_by()

    return {
        "symbol": symbol,
        "name": row.get("name") or symbol,
        "event_level": level,
        "event_type": event_type,
        "session_status": session,
        "would_send_telegram": len(blocked_by) == 0,
        "dry_run": _is_dry_run(),
        "blocked_by": blocked_by,
    }


def _record_event(row: dict[str, Any], result: str, reason: str, message: str = "") -> None:
    event = {
        "timestamp": int(time.time() * 1000),
        "symbol": str(row.get("symbol") or row.get("code") or ""),
        "name": str(row.get("name") or row.get("symbol") or ""),
        "event_type": str(row.get("event_type") or ""),
        "event_level": str(row.get("event_level") or ""),
        "result": result,
        "reason": reason,
        "message": message
    }
    _recent_events.insert(0, event)
    while len(_recent_events) > 100:
        _recent_events.pop()
    _save_state()

async def maybe_send_event(row: dict[str, Any], *, dry_run: bool | None = None) -> dict[str, Any]:
    """Evaluate and optionally send a Telegram alert for an AlphaForge event."""
    effective_dry_run = _is_dry_run() if dry_run is None else dry_run
    result = evaluate_event(row, mark_cooldown=False)

    if not result["would_send_telegram"]:
        reason = result["blocked_by"][0] if result["blocked_by"] else "unknown"
        logger.info("telegram_skip", reason=reason, symbol=result["symbol"], event_type=result["event_type"])
        _record_event(row, "skipped", reason)
        return result

    if effective_dry_run:
        symbol = str(row.get("symbol") or row.get("code") or "")
        event_type = str(row.get("event_type") or "")
        _should_send(symbol, event_type, result.get('event_level', ''), mark=True)
        _record_sent()
        logger.info("telegram_skip", reason="dry_run", symbol=result["symbol"], event_type=result["event_type"])
        result["would_send_telegram"] = False  # To mark that it was skipped
        _record_event(row, "dry_run", "dry_run")
        return result

    symbol = str(row.get("symbol") or row.get("code") or "")
    event_type = str(row.get("event_type") or "")

    # Actually send
    text = _build_message(row)
    sent_ok = await _send_message(text)
    if sent_ok:
        _should_send(symbol, event_type, result.get('event_level', ''), mark=True)
        _record_sent()
        _record_event(row, "sent", "success")
    else:
        _record_event(row, "failed", "send_error")

    return result

    if effective_dry_run:
        symbol = str(row.get("symbol") or row.get("code") or "")
        event_type = str(row.get("event_type") or "")
        _should_send(symbol, event_type, result.get('event_level', ''), mark=True)
        _record_sent()
        logger.info("telegram_skip", reason="dry_run", symbol=result["symbol"], event_type=result["event_type"])
        result["would_send_telegram"] = False  # To mark that it was skipped
        return result

    symbol = str(row.get("symbol") or row.get("code") or "")
    event_type = str(row.get("event_type") or "")

    # Actually send
    text = _build_message(row)
    sent_ok = await _send_message(text)
    if sent_ok:
        _should_send(symbol, event_type, result.get('event_level', ''), mark=True)
        _record_sent()

    return result

    if effective_dry_run:
        symbol = str(row.get("symbol") or row.get("code") or "")
        event_type = str(row.get("event_type") or "")
        _should_send(symbol, event_type, result.get('event_level', ''), mark=True)
        _record_sent()
        logger.info(
            "telegram_dry_run_would_send",
            symbol=result["symbol"],
            event_type=result["event_type"],
            event_level=result["event_level"],
        )
        return result

    # ── 실제 발송 경로 ─────────────────────────────────────────────────────
    # 쿨다운 마킹 (발송 직전에 찍어야 중복 방지)
    symbol = str(row.get("symbol") or row.get("code") or "")
    event_type = str(row.get("event_type") or "")
    _should_send(symbol, event_type, result.get('event_level', ''), mark=True)
    _record_sent()

    text = _build_alphaforge_message(result, row)
    sent = await _send_message(text)
    result["sent"] = sent
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 메시지 포맷터
# ─────────────────────────────────────────────────────────────────────────────

def _build_alphaforge_message(result: dict, row: dict) -> str:
    level = result.get("event_level", "")
    etype = result.get("event_type", "")
    sym = result.get("symbol", "")
    name = result.get("name", sym)
    reason = row.get("event_reason", "")
    price = row.get("current_price", 0)
    change = row.get("change_rate", 0)
    strength = row.get("trade_strength", 0)
    session = result.get("session_status", "")

    level_emoji = {"L2": "🟡", "L3": "🟢", "RISK": "🔴"}.get(level, "⚪")
    lines = [
        f"{level_emoji} <b>[{level}] {etype}</b>",
        f"📌 {name} ({sym})",
        f"💰 {price:,.0f}원  {change:+.2f}%  강도 {strength:.0f}",
        f"📋 {reason}",
        f"⏰ {session}",
    ]
    return "\n".join(lines)


def _build_index_message(index_name: str, change_pct: float, price: float, direction: str) -> str:
    emoji = "🚀" if direction == "UP" else "📉"
    lines = [
        f"{emoji} <b>[지수알람] {index_name}</b>",
        f"등락률 {change_pct:+.2f}%  현재 {price:,.2f}",
        f"{'급등' if direction == 'UP' else '급락'} 감지 · 장중 모니터링 권장",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 지수 알람
# ─────────────────────────────────────────────────────────────────────────────

async def maybe_send_index_alert(
    index_code: str,
    index_name: str,
    change_pct: float,
    price: float,
    threshold_pct: float = 3.0,
) -> dict[str, Any]:
    """KOSPI/KOSDAQ 급등락 알람. |change_pct| >= threshold_pct 이면 후보."""
    abs_chg = abs(change_pct)
    direction = "UP" if change_pct >= 0 else "DOWN"
    key_symbol = f"IDX_{index_code}"
    key_event = direction

    result: dict[str, Any] = {
        "index_code": index_code,
        "index_name": index_name,
        "change_pct": change_pct,
        "price": price,
        "direction": direction,
        "threshold_met": abs_chg >= threshold_pct,
    }

    if not _is_enabled():
        result["blocked_reason"] = "system_disabled"
        return result

    if not telegram_enabled():
        result["blocked_reason"] = "no_credentials"
        return result

    if abs_chg < threshold_pct:
        result["blocked_reason"] = f"below_threshold({threshold_pct:.1f}%)"
        logger.info(
            "would_send_telegram",
            symbol=key_symbol,
            event_type=key_event,
            would_send_telegram=False,
            blocked_reason=result["blocked_reason"],
            change_pct=change_pct,
            dry_run=_is_dry_run(),
        )
        return result

    if not _should_send(key_symbol, key_event, '', mark=False, cooldown_sec=_INDEX_COOLDOWN_SEC):
        result["blocked_reason"] = "index_cooldown_15m"
        logger.info(
            "would_send_telegram",
            symbol=key_symbol,
            event_type=key_event,
            would_send_telegram=False,
            blocked_reason="index_cooldown_15m",
            change_pct=change_pct,
            dry_run=_is_dry_run(),
        )
        return result

    if not _global_hourly_check():
        result["blocked_reason"] = "hourly_cap_reached"
        logger.info(
            "would_send_telegram",
            symbol=key_symbol,
            event_type=key_event,
            would_send_telegram=False,
            blocked_reason="hourly_cap_reached",
            dry_run=_is_dry_run(),
        )
        return result

    result["would_send"] = True
    logger.info(
        "would_send_telegram",
        symbol=key_symbol,
        event_type=key_event,
        would_send_telegram=True,
        blocked_reason="",
        change_pct=change_pct,
        dry_run=_is_dry_run(),
    )

    if _is_dry_run():
        result["dry_run"] = True
        return result

    # 실발송
    _should_send(key_symbol, key_event, '', mark=True, cooldown_sec=_INDEX_COOLDOWN_SEC)
    _record_sent()
    text = _build_index_message(index_name, change_pct, price, direction)
    sent = await _send_message(text)
    result["sent"] = sent
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 테스트 발송
# ─────────────────────────────────────────────────────────────────────────────

_test_sent_flag: bool = False   # 프로세스당 1회만


async def send_test_message() -> dict[str, Any]:
    """CHECKS Telegram 테스트 메시지 1회 발송. 중복 방지."""
    global _test_sent_flag

    result: dict[str, Any] = {"test": True}

    if not _is_enabled():
        result["error"] = "KR_TELEGRAM_ENABLED is not true"
        return result

    if not telegram_enabled():
        result["error"] = "no_credentials"
        return result

    if _test_sent_flag:
        result["error"] = "already_sent_this_process"
        result["skipped"] = True
        return result

    if _is_dry_run():
        result["dry_run"] = True
        result["would_send"] = True
        logger.info(
            "would_send_telegram",
            symbol="__test__",
            event_type="TEST",
            would_send_telegram=True,
            blocked_reason="",
            dry_run=True,
        )
        return result

    # 중복 방지 (쿨다운 재사용)
    if not _should_send("__test__", "TEST", '', mark=False, cooldown_sec=3600):
        result["error"] = "test_already_sent_within_1h"
        result["skipped"] = True
        return result

    _test_sent_flag = True
    _should_send("__test__", "TEST", '', mark=True, cooldown_sec=3600)
    _record_sent()

    text = (
        "✅ <b>CHECKS Telegram test</b>\n"
        "알람 시스템 연결 확인용 메시지입니다.\n"
        "이 메시지가 오면 실발송 경로가 정상입니다."
    )
    sent = await _send_message(text)
    result["sent"] = sent
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────────────────────

def reset_cooldown_for_tests() -> None:
    _recent_alerts.clear()
    _hourly_sent.clear()
