"""Telegram alert evaluator in dry-run mode.

This module intentionally does not call Telegram sendMessage. It evaluates the
same event-gating rules as the worktree notifier and logs would-send results so
the dashboard can verify alert candidates without sending real messages.
"""

from __future__ import annotations

import os
import time
from typing import Any

import structlog

from jason_checks.config import get_settings


logger = structlog.get_logger()

_COOLDOWN_SEC = 300
_recent_alerts: dict[tuple[str, str], float] = {}


def _credentials_present() -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        return True
    try:
        settings = get_settings()
        token = token or getattr(settings, "telegram_bot_token", "") or ""
        chat = chat or getattr(settings, "telegram_chat_id", "") or ""
    except Exception:
        return False
    return bool(token) and bool(chat)


def telegram_enabled() -> bool:
    """Return whether credentials exist, without exposing them."""
    return _credentials_present()


def _should_send(symbol: str, event_type: str, now: float | None = None, mark: bool = True) -> bool:
    now = now if now is not None else time.time()
    key = (symbol, event_type)
    last = _recent_alerts.get(key, 0.0)
    if now - last < _COOLDOWN_SEC:
        return False
    if mark:
        _recent_alerts[key] = now
    return True


def evaluate_event(row: dict[str, Any], *, mark_cooldown: bool = False) -> dict[str, Any]:
    """Evaluate Telegram alert gates without sending anything."""
    symbol = str(row.get("symbol") or row.get("code") or "")
    event_type = str(row.get("event_type") or "")
    level = str(row.get("event_level") or "")
    session = str(row.get("session_status") or "")

    checks = {
        "credentials_present": telegram_enabled(),
        "event_should_alert": bool(row.get("event_should_alert")),
        "level_allowed": level in ("L2", "L3", "RISK"),
        "has_symbol": bool(symbol),
        "has_event_type": bool(event_type),
        "session_allows_alert": session.upper() == "REGULAR",
    }
    checks["cooldown_allows_alert"] = (
        _should_send(symbol, event_type, mark=mark_cooldown)
        if checks["has_symbol"] and checks["has_event_type"]
        else False
    )
    would_send = all(checks.values())
    blocked_by = [key for key, ok in checks.items() if not ok]
    return {
        "symbol": symbol,
        "name": row.get("name") or symbol,
        "event_level": level,
        "event_type": event_type,
        "session_status": session,
        "would_send_telegram": would_send,
        "dry_run": True,
        "blocked_by": blocked_by,
    }


async def maybe_send_event(row: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """Dry-run Telegram dispatch.

    The dry_run argument is kept explicit and defaults to True. Passing False is
    still blocked in this recovery build to satisfy the no-send requirement.
    """
    result = evaluate_event(row, mark_cooldown=False)
    logger.info(
        "telegram_alert_dry_run",
        symbol=result["symbol"],
        event_level=result["event_level"],
        event_type=result["event_type"],
        session_status=result["session_status"],
        would_send_telegram=result["would_send_telegram"],
        blocked_by=result["blocked_by"],
        dry_run=True,
    )
    return result


def reset_cooldown_for_tests() -> None:
    _recent_alerts.clear()
