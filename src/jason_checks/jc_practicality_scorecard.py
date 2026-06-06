"""JC Practicality & Accuracy Scorecard v1.

This module is intentionally read-only with respect to trading decisions. It
collects existing dashboard/API/file state and scores operational practicality.
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jason_checks.alphaforge_validation_loader import (
    load_alphaforge_validation as _load_av,
    AV_REPORT_CANDIDATES,
    find_alphaforge_scorecard_paths,
)


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports"
JSON_REPORT = REPORT_DIR / "jc_practicality_scorecard.json"
MD_REPORT = REPORT_DIR / "jc_practicality_scorecard.md"
LOCAL_API_BASE = "http://127.0.0.1:8000"

WEIGHTS = {
    "market_gate_score": 15,
    "decision_consistency_score": 15,
    "quote_coverage_score": 10,
    "realtime_strength_score": 15,
    "supply_quality_score": 10,
    "forward_test_score": 15,
    "jo_handoff_score": 8,
    "ops_reliability_score": 7,
    "setup_explainability_score": 3,
    "alphaforge_validation_link_score": 2,
}

CLOSED_SESSIONS = {"MARKET_CLOSED", "CLOSED", "WEEKEND", "HOLIDAY", "PRE_MARKET", "AFTER_MARKET", "AFTER"}
LIVE_SESSIONS = {"LIVE", "REGULAR", "REGULAR_SESSION"}
# Legacy alias for tests that monkeypatch this
ALPHAFORGE_VALIDATION_PATHS = [p for p, _ in AV_REPORT_CANDIDATES]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _http_json(path: str, timeout: float = 2.5) -> tuple[dict[str, Any], str]:
    url = f"{LOCAL_API_BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), "OK"
    except Exception as exc:
        return {}, f"DATA_INSUFFICIENT: {type(exc).__name__}: {exc}"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _score_from_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, min(100.0, numerator / denominator * 100.0))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _age_hours(value: Any, now: datetime) -> float | None:
    dt = _parse_dt(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - dt.astimezone(now.tzinfo)).total_seconds() / 3600)


def _session_text(context_or_decision: dict[str, Any]) -> str:
    if "decision_summary" in context_or_decision:
        decision = context_or_decision.get("decision_summary") or {}
        themes = context_or_decision.get("themes") or {}
    else:
        decision = context_or_decision
        themes = {}
    return str(decision.get("session") or themes.get("session_status") or "").strip().upper()


def _mode_text(context: dict[str, Any]) -> str:
    return str((context.get("themes") or {}).get("mode") or "").strip().upper()


def _score_context(context: dict[str, Any]) -> str:
    session = _session_text(context)
    if session in CLOSED_SESSIONS:
        return "CLOSED_REVIEW"
    if session in LIVE_SESSIONS:
        return "LIVE_TRADING_REVIEW"
    return "LIVE_TRADING_REVIEW" if _mode_text(context) == "LIVE" else "CLOSED_REVIEW"


def _is_closed_review(context: dict[str, Any]) -> bool:
    return _score_context(context) == "CLOSED_REVIEW"


@dataclass
class Score:
    value: float
    status: str
    notes: list[str]
    details: dict[str, Any]


def _decision_counts_from_results(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"BUY_NOW": 0, "STARTER_POSITION": 0, "CONDITIONAL_BUY": 0, "WATCH_ONLY": 0, "AVOID": 0}
    for row in results:
        decision = str(row.get("decision") or "")
        if decision in counts:
            counts[decision] += 1
    return counts


def _score_jo_handoff(themes: dict[str, Any], now: datetime) -> Score:
    picks = themes.get("alphaforge_picks") or []
    loaded = _to_int(themes.get("alphaforge_candidates_loaded"), len(picks))
    published = themes.get("alphaforge_candidates_published_at") or themes.get("alphaforge_candidates_generated_at")
    age = _age_hours(published, now)
    field_names = ("symbol", "name", "alert_type", "rs", "vcp_status", "box_upper_price")
    field_total = max(1, len(picks) * len(field_names))
    field_present = sum(1 for p in picks for f in field_names if p.get(f) not in (None, "", "-"))
    field_ratio = field_present / field_total if picks else 0.0

    score = 0.0
    notes = []
    if loaded > 0:
        score += 45
    else:
        notes.append("AlphaForge 후보 0개 또는 로드 실패")
    score += min(25, loaded * 5)
    if age is None:
        notes.append("후보 timestamp 확인 불가")
        score += 5
    elif age <= 36:
        score += 15
    elif age <= 96:
        score += 8
        notes.append(f"후보 timestamp 다소 오래됨: {age:.1f}h")
    else:
        notes.append(f"후보 stale: {age:.1f}h")
    score += field_ratio * 15
    if themes.get("alphaforge_candidates_is_stale"):
        score -= 20
        notes.append("AlphaForge 후보 stale flag")
    return Score(max(0, min(100, score)), "OK" if loaded else "DATA_INSUFFICIENT", notes, {
        "loaded": loaded,
        "pick_count": len(picks),
        "timestamp": published,
        "age_hours": age,
        "field_presence_ratio": round(field_ratio, 3),
    })


def _score_quote_coverage(themes: dict[str, Any]) -> Score:
    q = themes.get("quote_polling") or {}
    success = _to_int(q.get("success") or q.get("theme_row_price_count"))
    total = _to_int(q.get("total") or q.get("theme_row_total") or q.get("watch_symbols"))
    missing = _to_int(q.get("missing"), max(total - success, 0))
    score = _score_from_ratio(success, total)
    notes = []
    if missing:
        notes.append(f"missing quote {missing}/{total}")
    if _to_float(q.get("duration_sec")) > 180:
        score -= 8
        notes.append("quote polling duration long")
    return Score(max(0, min(100, score)), "OK" if total else "DATA_INSUFFICIENT", notes, {
        "success": success,
        "total": total,
        "missing": missing,
        "duration_sec": q.get("duration_sec"),
        "completed_at": q.get("completed_at"),
    })


def _score_realtime_strength(themes: dict[str, Any], decision: dict[str, Any], context: dict[str, Any]) -> Score:
    q = themes.get("quote_polling") or {}
    success = _to_int(q.get("strength_success") or q.get("theme_row_strength_count"))
    total = _to_int(q.get("strength_total") or q.get("theme_row_total") or q.get("total"))
    if not total:
        results = decision.get("results") or []
        total = len(results)
        success = sum(1 for r in results if r.get("has_strength") or _to_float(r.get("strength")) > 0)
    ratio = _score_from_ratio(success, total)
    score_context = _score_context(context)
    closed_review = score_context == "CLOSED_REVIEW"
    score = ratio
    notes = []
    status = "OK" if total else "DATA_INSUFFICIENT"
    reason = ""
    evaluable = True
    basis = "LIVE_STRENGTH_COVERAGE"

    # CLOSED_REVIEW: data=0 means not collectable (not a failure)
    if not total and closed_review:
        score = 0
        status = "NOT_EVALUATED_OFFLINE_CLOSED"
        reason = "폐장/API미연결 상태 — 체결강도 데이터 미수집, 평가 제외"
        evaluable = False
        basis = "EXCLUDED_SESSION_CLOSED"
        notes.append(reason)
    elif total and success == 0:
        if closed_review:
            score = 0
            status = "NOT_EVALUATED_SESSION_CLOSED"
            reason = "휴장/폐장 상태라 체결강도 기반 실시간 승격 판단은 평가할 수 없음"
            evaluable = False
            basis = "EXCLUDED_SESSION_CLOSED"
            notes.append(reason)
        else:
            score = 5
            status = "FAILED_LIVE_STRENGTH_COLLECTION"
            reason = "장중 LIVE인데 체결강도 수집 0%"
            notes.append(reason)
    elif ratio < 50:
        notes.append(f"체결강도 커버리지 낮음: {success}/{total}")
        reason = f"체결강도 커버리지 낮음: {success}/{total}"
    else:
        reason = f"체결강도 커버리지 {ratio:.1f}%"
    return Score(score, status, notes, {
        "success": success,
        "total": total,
        "coverage_pct": round(ratio, 2),
        "realtime_strength_status": status,
        "realtime_strength_reason": reason,
        "realtime_strength_evaluable": evaluable,
        "realtime_strength_coverage_pct": round(ratio, 2),
        "realtime_strength_score_basis": basis,
        "score_context": score_context,
    })


def _score_supply(themes: dict[str, Any], decision: dict[str, Any]) -> Score:
    sp = themes.get("supply_polling") or {}
    target = _to_int(sp.get("target_total"))
    ok = _to_int(sp.get("ok"))
    data_na = _to_int(sp.get("data_na"))
    rate_limit = _to_int(sp.get("rate_limit"))
    results = decision.get("results") or []
    has_supply = sum(1 for r in results if r.get("has_supply") or r.get("supply_status") == "OK")
    result_total = len(results)
    candidate_ratio = _score_from_ratio(max(ok, has_supply), max(target, result_total))
    score = 35 + candidate_ratio * 0.55
    notes = ["전체 섹터 수급은 미조회로 간주"]
    if data_na:
        score -= min(20, data_na * 4)
        notes.append(f"후보 수급 DATA_NA {data_na}")
    if rate_limit:
        score -= 15
        notes.append("수급 rate limit 감지")
    if "DATA_NA" in str(themes.get("supply_data_reason") or ""):
        score -= 8
    return Score(max(0, min(100, score)), "OK" if (target or result_total) else "DATA_INSUFFICIENT", notes, {
        "target_total": target,
        "ok": ok,
        "data_na": data_na,
        "decision_has_supply": has_supply,
        "decision_total": result_total,
        "supply_data_reason": themes.get("supply_data_reason", ""),
    })


def _score_market_gate(decision: dict[str, Any]) -> Score:
    counts = decision.get("decision_counts") or {}
    results = decision.get("results") or []
    session = decision.get("session")
    gate = decision.get("market_gate_level") or (decision.get("market_gate") or {}).get("level")
    buy_count = sum(_to_int(counts.get(k)) for k in ("BUY_NOW", "STARTER_POSITION", "CONDITIONAL_BUY"))
    max_pct_positive = sum(1 for r in results if _to_float(r.get("max_position_pct")) > 0)
    score = 75
    notes = []
    if gate in ("RISK_OFF", "CRASH") or session in ("MARKET_CLOSED", "AFTER_MARKET", "AFTER"):
        if buy_count == 0 and max_pct_positive == 0:
            score = 96
            notes.append("MARKET_CLOSED/CRASH에서 BUY 차단 정상")
        else:
            score = 20
            notes.append(f"시장 차단 구간에서 BUY 또는 비중 노출: buy={buy_count}, pct_rows={max_pct_positive}")
    return Score(score, "OK", notes, {
        "session": session,
        "market_gate_level": gate,
        "buy_count": buy_count,
        "max_position_positive_rows": max_pct_positive,
        "market_gate_reason": decision.get("market_gate_reason") or (decision.get("market_gate") or {}).get("reason"),
    })


def _score_decision_consistency(decision: dict[str, Any]) -> Score:
    results = decision.get("results") or []
    counts = decision.get("decision_counts") or {}
    actual = _decision_counts_from_results(results)
    mismatch = {k: {"board": _to_int(counts.get(k)), "results": actual.get(k, 0)} for k in actual if _to_int(counts.get(k)) != actual.get(k, 0)}
    buy_count = sum(_to_int(counts.get(k)) for k in ("BUY_NOW", "STARTER_POSITION", "CONDITIONAL_BUY"))
    max_pct_bad = [r.get("symbol") for r in results if buy_count == 0 and _to_float(r.get("max_position_pct")) > 0]
    missing_reason = [r.get("symbol") for r in results if r.get("decision") in ("WATCH_ONLY", "AVOID") and not r.get("no_buy_reason")]
    score = 96
    notes = []
    if mismatch:
        score -= 35
        notes.append(f"Action Board/후보 decision 불일치: {mismatch}")
    if max_pct_bad:
        score -= 35
        notes.append(f"BUY 0인데 max_position_pct > 0: {max_pct_bad[:5]}")
    if missing_reason:
        score -= min(20, len(missing_reason) * 4)
        notes.append(f"no_buy_reason 누락: {missing_reason[:5]}")
    return Score(max(0, score), "OK" if results else "DATA_INSUFFICIENT", notes, {
        "board_counts": counts,
        "result_counts": actual,
        "mismatch": mismatch,
        "max_pct_bad_symbols": max_pct_bad,
    })


def _score_setup_explainability(decision: dict[str, Any]) -> Score:
    results = decision.get("results") or []
    if not results:
        return Score(0, "DATA_INSUFFICIENT", ["Decision Engine results 없음"], {})
    fields = ("setup_score", "setup_label", "setup_reason", "reason_codes", "next_session_trigger")
    present = sum(1 for r in results for f in fields if r.get(f))
    ratio = _score_from_ratio(present, len(results) * len(fields))
    empty_entry = sum(1 for r in results if not r.get("entry_trigger"))
    empty_invalidation = sum(1 for r in results if not (r.get("invalidation_reason") or r.get("invalidation")))
    score = ratio
    notes = []
    if empty_entry:
        score -= min(10, empty_entry * 1.5)
        notes.append(f"entry_trigger 빈 값 {empty_entry}")
    if empty_invalidation:
        score -= min(10, empty_invalidation * 1.5)
        notes.append(f"invalidation 빈 값 {empty_invalidation}")
    return Score(max(0, min(100, score)), "OK", notes, {
        "field_presence_pct": round(ratio, 2),
        "empty_entry_trigger": empty_entry,
        "empty_invalidation": empty_invalidation,
    })


def _forward_sample_count(forward: dict[str, Any]) -> int:
    horizons = forward.get("horizons") or {}
    return sum(_to_int(v.get("count")) for v in horizons.values() if isinstance(v, dict))


def _score_forward(forward: dict[str, Any], score_data_mode: str = "API_CONNECTED") -> Score:
    # Distinguish API_UNAVAILABLE from genuine n=0
    if not forward or forward.get("status") != "OK":
        ft_status = "API_UNAVAILABLE" if score_data_mode in ("OFFLINE_FALLBACK", "PARTIAL_FALLBACK") else "DATA_INSUFFICIENT"
        msg = forward.get("message") or "Forward Test 데이터 부족"
        n = _forward_sample_count(forward or {})
        cap_msg = f"Forward Test {ft_status} 또는 DATA_INSUFFICIENT" if ft_status == "API_UNAVAILABLE" else f"Forward Test 표본 부족 n={n}"
        return Score(35, ft_status, [msg], {
            "sample_count": n,
            "status": ft_status,
            "forward_test_status": ft_status,
            "forward_test_sample_n": n,
            "forward_test_reason": msg,
            "_cap_note": cap_msg,
        })
    sample = _forward_sample_count(forward)
    blocked = forward.get("blocked_quality") or {}
    grade = str(blocked.get("quality_grade") or "")
    score = 50
    if grade == "우수":
        score += 25
    elif grade == "양호":
        score += 15
    elif grade == "점검 필요":
        score -= 10
    score += min(20, sample / 5)
    missed = _to_int(blocked.get("missed_opportunity_count"))
    score -= min(25, missed * 5)
    notes = []
    if sample < 100:
        notes.append(f"Forward Test 표본 부족 n={sample}")
    return Score(max(0, min(100, score)), "OK", notes, {
        "sample_count": sample,
        "blocked_quality": blocked,
        "status": "OK",
        "forward_test_status": "OK",
        "forward_test_sample_n": sample,
        "forward_test_reason": f"n={sample}",
    })


def _diagnose_telegram(telegram: dict[str, Any]) -> dict[str, Any]:
    """Parse telegram status dict into structured, sanitized diagnostic fields.

    Returns a dict with keys:
        telegram_status, telegram_auth_status, telegram_error_status,
        telegram_failed_today_count, telegram_sent_today_count,
        telegram_skipped_today_count, telegram_last_error_at,
        telegram_last_ok_at, telegram_last_error_reason_sanitized,
        telegram_reason, is_current_unauthorized
    민감정보(token/chat_id 값) 출력 금지.
    """
    if not telegram:
        return {
            "telegram_status": "TELEGRAM_DATA_NA",
            "telegram_auth_status": "DATA_NA",
            "telegram_error_status": "DATA_NA",
            "telegram_failed_today_count": None,
            "telegram_sent_today_count": None,
            "telegram_skipped_today_count": None,
            "telegram_last_error_at": None,
            "telegram_last_ok_at": None,
            "telegram_last_error_reason_sanitized": "",
            "telegram_reason": "Telegram 상태 데이터 없음",
            "is_current_unauthorized": False,
        }

    last_error_raw = str(telegram.get("last_error_reason") or telegram.get("send_error") or "")
    # Sanitize: strip actual token/chat_id values but keep error codes
    last_error_sanitized = last_error_raw  # no token/key values appear in error reason strings

    # Numeric timestamps (epoch ms) – handle both int and None
    last_error_at_raw = telegram.get("last_error_at")
    last_ok_at_raw = telegram.get("last_ok_at")
    try:
        last_error_at = int(last_error_at_raw) if last_error_at_raw is not None else None
    except (TypeError, ValueError):
        last_error_at = None
    try:
        last_ok_at = int(last_ok_at_raw) if last_ok_at_raw is not None else None
    except (TypeError, ValueError):
        last_ok_at = None

    failed_today = _to_int(telegram.get("failed_today_count"), 0)
    sent_today = _to_int(telegram.get("sent_today_count"), 0)
    skipped_today = _to_int(telegram.get("skipped_today_count"), 0)

    has_401 = "401" in last_error_sanitized or "unauthorized" in last_error_sanitized.lower()

    # Determine if 401 is CURRENT or HISTORICAL
    # HISTORICAL: last_ok_at exists AND last_ok_at > last_error_at AND failed_today == 0
    is_recovered = (
        has_401
        and last_ok_at is not None
        and last_error_at is not None
        and last_ok_at > last_error_at
        and failed_today == 0
    )
    is_current_unauthorized = has_401 and not is_recovered

    # Determine auth_status
    if has_401 and is_recovered:
        auth_status = "HISTORICAL_UNAUTHORIZED_RECOVERED"
    elif is_current_unauthorized:
        auth_status = "CURRENT_UNAUTHORIZED"
    elif telegram.get("configured", telegram.get("credentials_present", False)):
        auth_status = "OK"
    else:
        auth_status = "NOT_CONFIGURED"

    # Determine error_status
    if is_current_unauthorized:
        error_status = "CURRENT_ERROR_401"
    elif has_401 and is_recovered:
        error_status = "HISTORICAL_ERROR_RECOVERED"
    elif last_error_sanitized and not has_401:
        error_status = "RECENT_ERROR"
    else:
        error_status = "OK"

    # Overall status
    if is_current_unauthorized:
        tg_status = "TELEGRAM_UNAUTHORIZED"
    elif last_error_sanitized and not is_recovered:
        tg_status = "TELEGRAM_ERROR"
    elif has_401 and is_recovered:
        tg_status = "TELEGRAM_RECOVERED"
    elif telegram:
        tg_status = "TELEGRAM_OK"
    else:
        tg_status = "TELEGRAM_DATA_NA"

    # Human-readable reason
    if is_current_unauthorized:
        reason = "현재 Telegram 401 인증 오류 지속 중"
    elif has_401 and is_recovered:
        reason = f"과거 401 오류 발생 후 정상 복구됨 (last_ok_at > last_error_at, failed_today=0)"
    elif last_error_sanitized:
        reason = f"최근 오류: {last_error_sanitized[:80]}"
    else:
        reason = "정상"

    return {
        "telegram_status": tg_status,
        "telegram_auth_status": auth_status,
        "telegram_error_status": error_status,
        "telegram_failed_today_count": failed_today,
        "telegram_sent_today_count": sent_today,
        "telegram_skipped_today_count": skipped_today,
        "telegram_last_error_at": last_error_at,
        "telegram_last_ok_at": last_ok_at,
        "telegram_last_error_reason_sanitized": last_error_sanitized,
        "telegram_reason": reason,
        "is_current_unauthorized": is_current_unauthorized,
    }


def _score_ops(guard: dict[str, Any], telegram: dict[str, Any]) -> Score:
    score = 90
    notes = []
    guard_status = guard.get("overall_status") or guard.get("report_status")
    if guard_status == "STALE" or guard.get("is_stale"):
        score -= 35
        notes.append("JC Guard STALE")
    elif guard_status == "FAIL":
        score -= 45
        notes.append("JC Guard FAIL")
    elif guard_status == "WARN":
        score -= 15
    age_sec = _to_float(guard.get("age_sec"), -1)
    if age_sec > 24 * 3600:
        notes.append(f"Guard age 오래됨: {age_sec/60:.0f}분")

    tg_diag = _diagnose_telegram(telegram)
    is_current_unauth = tg_diag["is_current_unauthorized"]

    if is_current_unauth:
        score = min(score, 60)
        notes.append("Telegram 인증 오류: 현재 401 unauthorized 지속")
    elif tg_diag["telegram_auth_status"] == "HISTORICAL_UNAUTHORIZED_RECOVERED":
        # 과거 오류 + 회복: 약한 warning만
        score -= 5
        notes.append("Telegram 과거 401 오류 발생 이력 (현재 회복됨)")
    elif tg_diag["telegram_status"] == "TELEGRAM_ERROR":
        score -= 12
        notes.append("Telegram 최근 오류")

    if telegram and not telegram.get("configured", telegram.get("credentials_present", False)):
        score -= 25
        notes.append("Telegram credentials 미설정")

    # telegram_last_error_reason: sanitized, no token values
    tg_last_err_label = (
        "send_error: http_401_unauthorized (historical, recovered)"
        if tg_diag["telegram_auth_status"] == "HISTORICAL_UNAUTHORIZED_RECOVERED"
        else "send_error: http_401_unauthorized"
        if is_current_unauth
        else ("telegram_error" if tg_diag["telegram_error_status"] == "RECENT_ERROR" else "")
    )

    return Score(max(0, min(100, score)), "OK" if (guard or telegram) else "DATA_INSUFFICIENT", notes, {
        "guard_status": guard_status,
        "guard_age_sec": age_sec,
        "telegram_enabled": telegram.get("enabled"),
        "telegram_dry_run": telegram.get("dry_run"),
        "telegram_last_error_reason": tg_last_err_label,
        "telegram_status": tg_diag["telegram_status"],
        "telegram_auth_status": tg_diag["telegram_auth_status"],
        "telegram_error_status": tg_diag["telegram_error_status"],
        "is_current_unauthorized": is_current_unauth,
        "_tg_diag": tg_diag,
    })


def _score_alphaforge_validation(validation: dict[str, Any]) -> Score:
    """Score AlphaForge Validation link. Uses shared loader status."""
    # validation may come from API (has 'available') or from shared loader (has 'status')
    av_status = validation.get("status") or ("FOUND" if validation.get("available") else "DATA_NA")
    path_found = validation.get("path_found") or validation.get("alphaforge_validation_path_found") or []
    if isinstance(path_found, str):
        path_found_list = [path_found] if path_found else []
    else:
        path_found_list = list(path_found)
    paths_checked = validation.get("paths_checked") or [str(p) for p, _ in AV_REPORT_CANDIDATES]
    source_type = validation.get("source_type") or ("JC_API" if validation.get("available") else "DATA_NA")

    if av_status == "FOUND":
        return Score(90, "OK", [], {
            "alphaforge_validation_status": "FOUND",
            "alphaforge_validation_source_type": source_type,
            "alphaforge_validation_path_found": path_found_list,
            "alphaforge_validation_path_checked": paths_checked,
            "alphaforge_validation_reason": validation.get("reason") or "AlphaForge 검증 리포트 로드 성공",
        })

    if av_status == "PATH_MISMATCH":
        notes = ["JO 리포트는 존재하지만 JC 연동 경로 mismatch 가능"]
        return Score(20, "PATH_MISMATCH", notes, {
            "alphaforge_validation_status": "PATH_MISMATCH",
            "alphaforge_validation_source_type": source_type,
            "alphaforge_validation_path_found": path_found_list,
            "alphaforge_validation_path_checked": paths_checked,
            "alphaforge_validation_reason": validation.get("reason") or "file exists but not linked",
        })

    if av_status == "READ_ERROR":
        notes = [validation.get("reason") or "AlphaForge 검증 파일 읽기 오류"]
        return Score(30, "READ_ERROR", notes, {
            "alphaforge_validation_status": "READ_ERROR",
            "alphaforge_validation_source_type": source_type,
            "alphaforge_validation_path_found": path_found_list,
            "alphaforge_validation_path_checked": paths_checked,
            "alphaforge_validation_reason": validation.get("reason") or "READ_ERROR",
        })

    # DATA_NA — but also check ALPHAFORGE_VALIDATION_PATHS for file-existence
    # This preserves backward-compat with tests that monkeypatch ALPHAFORGE_VALIDATION_PATHS
    existing_legacy = [str(p) for p in ALPHAFORGE_VALIDATION_PATHS if p.exists()]
    if existing_legacy and not path_found_list:
        # Files exist on disk but were not loaded (PATH_MISMATCH or not yet parsed)
        notes = [validation.get("reason") or "JO 리포트는 존재하지만 JC 연동 경로 mismatch 가능"]
        notes.append("JO 리포트는 존재하지만 JC 연동 경로 mismatch 가능")
        return Score(20, "PATH_MISMATCH", notes, {
            "alphaforge_validation_status": "PATH_MISMATCH",
            "alphaforge_validation_source_type": source_type,
            "alphaforge_validation_path_found": existing_legacy,
            "alphaforge_validation_path_checked": paths_checked or [str(p) for p in ALPHAFORGE_VALIDATION_PATHS],
            "alphaforge_validation_reason": validation.get("reason") or "file exists but not linked",
        })
    notes = [validation.get("reason") or "AlphaForge Validation 리포트 없음"]
    return Score(45, "DATA_NA", notes, {
        "alphaforge_validation_status": "DATA_NA",
        "alphaforge_validation_source_type": "DATA_NA",
        "alphaforge_validation_path_found": path_found_list,
        "alphaforge_validation_path_checked": paths_checked,
        "alphaforge_validation_reason": validation.get("reason") or "candidate files not found",
    })


def _diagnose_guard_timestamp(guard: dict[str, Any], now: datetime) -> dict[str, Any]:
    raw = guard.get("timestamp")
    parsed = _parse_dt(raw)
    age_minutes = None
    parse_status = "OK"
    reason = ""
    anomaly = False
    if raw and not parsed:
        parse_status = "PARSE_FAILED"
        reason = "Guard timestamp ISO parse failed"
        anomaly = True
    elif parsed:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
            reason = "timestamp timezone missing; local timezone assumed"
        age_minutes = max(0.0, (now - parsed.astimezone(now.tzinfo)).total_seconds() / 60)
        if age_minutes > 7 * 24 * 60:
            anomaly = True
            reason = reason or "Guard report older than 7 days; stale age may look abnormal"
        elif age_minutes > 24 * 60:
            reason = reason or "Guard report older than 24 hours"
    else:
        parse_status = "MISSING"
        reason = "Guard timestamp missing"
    return {
        "guard_stale": bool(guard.get("is_stale") or guard.get("overall_status") == "STALE"),
        "guard_age_anomaly": anomaly,
        "guard_timestamp_parse_status": parse_status,
        "guard_timestamp_raw": raw,
        "guard_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "guard_age_reason": reason,
    }


def _find_anomalies(context: dict[str, Any], scores: dict[str, Score], guard_diag: dict[str, Any]) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    decision = context.get("decision_summary") or {}
    themes = context.get("themes") or {}
    q = themes.get("quote_polling") or {}
    strength = scores["realtime_strength_score"].details
    if _to_int(q.get("strength_success") or q.get("theme_row_strength_count")) == 0 and _to_int(q.get("strength_total") or q.get("theme_row_total")) > 0:
        if strength.get("realtime_strength_evaluable"):
            anomalies.append({"code": "STRENGTH_ZERO_COVERAGE", "severity": "WARN", "message": "장중 LIVE인데 체결강도 0/전체 상태"})
        else:
            anomalies.append({"code": "STRENGTH_NOT_EVALUATED_SESSION_CLOSED", "severity": "INFO", "message": strength.get("realtime_strength_reason")})
    guard = context.get("guard_status") or {}
    if guard.get("is_stale"):
        anomalies.append({"code": "GUARD_STALE", "severity": "WARN", "message": guard.get("summary", "Guard STALE")})
    if guard_diag.get("guard_age_anomaly") or guard_diag.get("guard_timestamp_parse_status") != "OK":
        anomalies.append({
            "code": "GUARD_AGE_ANOMALY",
            "severity": "WARN",
            "message": guard_diag.get("guard_age_reason") or "Guard timestamp/age 진단 필요",
        })
    telegram_notes = scores["ops_reliability_score"].notes
    if any("Telegram" in n for n in telegram_notes):
        anomalies.append({"code": "TELEGRAM_SEND_ERROR", "severity": "WARN", "message": "; ".join(telegram_notes)})
    validation = context.get("alphaforge_validation") or {}
    if not validation.get("available"):
        anomalies.append({"code": "ALPHAFORGE_VALIDATION_MISSING", "severity": "WARN", "message": validation.get("reason", "리포트 없음")})
    session = decision.get("session")
    if session == "MARKET_CLOSED" and str(context.get("themes", {}).get("mode", "")).upper() == "LIVE":
        anomalies.append({"code": "SESSION_LABEL_MISMATCH", "severity": "INFO", "message": "MARKET_CLOSED와 LIVE mode 표시 혼재 가능"})
    return anomalies


def _apply_caps(
    raw_score: float,
    context: dict[str, Any],
    scores: dict[str, Score],
    score_data_mode: str = "API_CONNECTED",
) -> tuple[float, list[str]]:
    caps: list[tuple[int, str]] = []
    strength = scores["realtime_strength_score"].details
    strength_cov = _to_float(strength.get("coverage_pct"))
    # Only cap strength when LIVE and evaluable — not during CLOSED_REVIEW
    if strength.get("realtime_strength_evaluable"):
        if strength_cov == 0 and _to_int(strength.get("total")) > 0:
            caps.append((75, "체결강도 커버리지 0%"))
        elif strength_cov < 50 and _to_int(strength.get("total")) > 0:
            caps.append((80, "체결강도 커버리지 < 50%"))
    guard = context.get("guard_status") or {}
    if guard.get("is_stale") or guard.get("overall_status") == "STALE":
        caps.append((78, "JC Guard STALE"))
    # Telegram 401 cap: CURRENT_UNAUTHORIZED only
    ops_details = scores["ops_reliability_score"].details
    if ops_details.get("is_current_unauthorized"):
        caps.append((78, "Telegram current 401 unauthorized"))
    # Forward Test cap: distinguish API_UNAVAILABLE from real n=0
    ft_details = scores["forward_test_score"].details
    ft_status = ft_details.get("forward_test_status") or ft_details.get("status") or "DATA_INSUFFICIENT"
    forward_n = _to_int(ft_details.get("sample_count", 0))
    if ft_status == "API_UNAVAILABLE":
        caps.append((85, f"Forward Test API_UNAVAILABLE 또는 DATA_INSUFFICIENT"))
    elif ft_status == "DATA_INSUFFICIENT" or (ft_status == "OK" and forward_n < 100):
        caps.append((85, f"Forward Test 표본 부족 n={forward_n}"))
    # AlphaForge Validation cap: use precise status
    av_status = scores["alphaforge_validation_link_score"].details.get("alphaforge_validation_status", "DATA_NA")
    if av_status == "DATA_NA":
        caps.append((88, "AlphaForge Validation DATA_NA"))
    elif av_status == "PATH_MISMATCH":
        caps.append((88, "AlphaForge Validation PATH_MISMATCH"))
    elif av_status == "READ_ERROR":
        caps.append((88, "AlphaForge Validation READ_ERROR"))
    # FOUND → no cap
    # Quote coverage cap: only when total > 0 and coverage confirmed < 90%
    quote_details = scores["quote_coverage_score"].details
    quote_total = _to_int(quote_details.get("total"))
    if quote_total > 0:
        quote_cov = _score_from_ratio(_to_int(quote_details.get("success")), quote_total)
        if quote_cov < 90:
            caps.append((82, "quote coverage < 90%"))
    elif quote_total == 0 and scores["quote_coverage_score"].status == "DATA_INSUFFICIENT":
        caps.append((82, "quote coverage DATA_INSUFFICIENT"))
    market_details = scores["market_gate_score"].details
    if market_details.get("market_gate_level") in ("RISK_OFF", "CRASH") and _to_int(market_details.get("buy_count")) > 0:
        caps.append((60, "MARKET_CLOSED/CRASH에서 BUY 발생"))
    if scores["decision_consistency_score"].details.get("mismatch"):
        caps.append((70, "후보 카드와 Action Board 불일치"))
    if not caps:
        return raw_score, []
    cap_value = min(c[0] for c in caps)
    return min(raw_score, cap_value), [f"max {cap}: {reason}" for cap, reason in caps]


def calculate_scorecard(context: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc).astimezone()
    themes = context.get("themes") or {}
    decision = context.get("decision_summary") or {}
    forward = context.get("forward_test_summary") or decision.get("forward_test_summary") or {}
    score_context = _score_context(context)
    score_data_mode = context.get("score_data_mode", "API_CONNECTED")
    scores = {
        "jo_handoff_score": _score_jo_handoff(themes, now),
        "quote_coverage_score": _score_quote_coverage(themes),
        "realtime_strength_score": _score_realtime_strength(themes, decision, context),
        "supply_quality_score": _score_supply(themes, decision),
        "market_gate_score": _score_market_gate(decision),
        "decision_consistency_score": _score_decision_consistency(decision),
        "setup_explainability_score": _score_setup_explainability(decision),
        "forward_test_score": _score_forward(forward, score_data_mode),
        "ops_reliability_score": _score_ops(context.get("guard_status") or {}, context.get("telegram_status") or {}),
        "alphaforge_validation_link_score": _score_alphaforge_validation(context.get("alphaforge_validation") or {}),
    }
    active_weights = dict(WEIGHTS)
    if not scores["realtime_strength_score"].details.get("realtime_strength_evaluable", True):
        active_weights.pop("realtime_strength_score", None)
    raw = sum(scores[key].value * weight for key, weight in active_weights.items()) / sum(active_weights.values())
    capped, cap_reasons = _apply_caps(raw, context, scores, score_data_mode)
    confidence = "MEDIUM"
    if capped < 70 or len(cap_reasons) >= 3:
        confidence = "LOW"
    elif capped < 80 or cap_reasons:
        confidence = "MEDIUM_LOW"
    if score_context == "CLOSED_REVIEW" and confidence == "MEDIUM":
        confidence = "MEDIUM_LOW"
    guard_diag = _diagnose_guard_timestamp(context.get("guard_status") or {}, now)
    anomalies = _find_anomalies(context, scores, guard_diag)
    realtime_details = scores["realtime_strength_score"].details
    ft_details = scores["forward_test_score"].details
    av_details = scores["alphaforge_validation_link_score"].details
    ops_details = scores["ops_reliability_score"].details
    tg_diag = ops_details.get("_tg_diag") or {}
    quote_details = scores["quote_coverage_score"].details
    quote_total = _to_int(quote_details.get("total"))
    quote_success = _to_int(quote_details.get("success"))
    quote_missing = _to_int(quote_details.get("missing"))
    quote_cov_pct = round(_score_from_ratio(quote_success, quote_total), 1) if quote_total > 0 else None
    quote_status = (
        "OK" if quote_total > 0 and quote_cov_pct is not None and quote_cov_pct >= 90
        else "LOW_COVERAGE" if quote_total > 0
        else "DATA_INSUFFICIENT"
    )
    quote_reason = (
        f"quote coverage {quote_cov_pct}% ({quote_success}/{quote_total})"
        if quote_total > 0
        else "quote 데이터 없음 (API 미연결 또는 미수집)"
    )
    quote_source = (
        "api:themes.quote_polling"
        if (context.get("themes") or {}).get("quote_polling")
        else "DATA_INSUFFICIENT"
    )
    return {
        "generated_at": _now_iso(),
        "score_context": score_context,
        "score_data_mode": score_data_mode,
        "overall_jc_practicality_score": round(capped, 1),
        "raw_overall_score": round(raw, 1),
        "confidence": confidence,
        # ── AlphaForge Validation ────────────────────────────────────────────
        "alphaforge_validation_status": av_details.get("alphaforge_validation_status", "DATA_NA"),
        "alphaforge_validation_link_score": round(scores["alphaforge_validation_link_score"].value, 1),
        "alphaforge_validation_source_type": av_details.get("alphaforge_validation_source_type", "DATA_NA"),
        "alphaforge_validation_path_found": av_details.get("alphaforge_validation_path_found", []),
        "alphaforge_validation_reason": av_details.get("alphaforge_validation_reason", ""),
        # ── Forward Test ─────────────────────────────────────────────────────
        "forward_test_status": ft_details.get("forward_test_status") or ft_details.get("status", "DATA_INSUFFICIENT"),
        "forward_test_sample_n": _to_int(ft_details.get("forward_test_sample_n") or ft_details.get("sample_count", 0)),
        "forward_test_reason": ft_details.get("forward_test_reason", ""),
        # ── Telegram (sanitized, no token/chat_id values) ────────────────────
        "telegram_status": tg_diag.get("telegram_status", ops_details.get("telegram_status", "TELEGRAM_DATA_NA")),
        "telegram_auth_status": tg_diag.get("telegram_auth_status", "DATA_NA"),
        "telegram_error_status": tg_diag.get("telegram_error_status", "DATA_NA"),
        "telegram_failed_today_count": tg_diag.get("telegram_failed_today_count"),
        "telegram_sent_today_count": tg_diag.get("telegram_sent_today_count"),
        "telegram_skipped_today_count": tg_diag.get("telegram_skipped_today_count"),
        "telegram_last_error_at": tg_diag.get("telegram_last_error_at"),
        "telegram_last_ok_at": tg_diag.get("telegram_last_ok_at"),
        "telegram_last_error_reason_sanitized": tg_diag.get("telegram_last_error_reason_sanitized", ""),
        "telegram_reason": tg_diag.get("telegram_reason", ""),
        # ── Quote Coverage ───────────────────────────────────────────────────
        "quote_coverage_score": round(scores["quote_coverage_score"].value, 1),
        "quote_coverage_pct": quote_cov_pct,
        "quote_total": quote_total if quote_total > 0 else None,
        "quote_success": quote_success if quote_total > 0 else None,
        "quote_missing": quote_missing if quote_total > 0 else None,
        "quote_status": quote_status,
        "quote_reason": quote_reason,
        "quote_source": quote_source,
        # ── Sub-scores ────────────────────────────────────────────────────────
        "scores": {
            key: {
                "score": None if key == "realtime_strength_score" and not score.details.get("realtime_strength_evaluable", True) else round(score.value, 1),
                "status": score.status,
                "notes": score.notes,
                "details": score.details,
            }
            for key, score in scores.items()
        },
        "weights": WEIGHTS,
        "active_weights": active_weights,
        "cap_reasons": cap_reasons,
        "anomalies": anomalies,
        "guard_timestamp_diagnostics": guard_diag,
        "alphaforge_validation_diagnostics": av_details,
        "telegram_diagnostics": {
            "telegram_status": tg_diag.get("telegram_status", ops_details.get("telegram_status")),
            "telegram_auth_status": tg_diag.get("telegram_auth_status"),
            "telegram_error_status": tg_diag.get("telegram_error_status"),
            "last_error_status": ops_details.get("telegram_last_error_reason"),
            "last_error_reason_sanitized": tg_diag.get("telegram_last_error_reason_sanitized"),
            "telegram_reason": tg_diag.get("telegram_reason"),
        },
        "realtime_strength_status": realtime_details.get("realtime_strength_status"),
        "realtime_strength_reason": realtime_details.get("realtime_strength_reason"),
        "realtime_strength_evaluable": realtime_details.get("realtime_strength_evaluable"),
        "realtime_strength_coverage_pct": realtime_details.get("realtime_strength_coverage_pct"),
        "realtime_strength_score_basis": realtime_details.get("realtime_strength_score_basis"),
        "source_status": context.get("source_status", {}),
        "summary": {
            "decision_counts": decision.get("decision_counts", {}),
            "market_gate_level": decision.get("market_gate_level") or (decision.get("market_gate") or {}).get("level"),
            "market_gate_reason": decision.get("market_gate_reason") or (decision.get("market_gate") or {}).get("reason"),
            "session": decision.get("session"),
            "quote_polling": themes.get("quote_polling", {}),
            "supply_polling": themes.get("supply_polling", {}),
            "guard_status": (context.get("guard_status") or {}).get("overall_status"),
            "telegram_error": ops_details.get("telegram_last_error_reason", ""),
            "alphaforge_validation_available": (context.get("alphaforge_validation") or {}).get("available", False),
        },
    }


def collect_context(fetch_live: bool = True) -> dict[str, Any]:
    source_status: dict[str, str] = {}
    context: dict[str, Any] = {"source_status": source_status}
    api_reachable_keys: set[str] = set()

    if fetch_live:
        for key, path in (
            ("themes", "/api/themes?sort=default"),
            ("decision_summary", "/api/decision-summary"),
            ("guard_status", "/api/guard/status"),
            ("telegram_status", "/api/telegram/status"),
            ("alphaforge_validation", "/api/alphaforge-validation"),
        ):
            data, status = _http_json(path)
            context[key] = data
            source_status[key] = f"api:{status}"
            if data and "DATA_INSUFFICIENT" not in status:
                api_reachable_keys.add(key)

    # Determine score_data_mode
    core_api_keys = {"themes", "decision_summary"}
    if not fetch_live:
        score_data_mode = "OFFLINE_FALLBACK"
    elif core_api_keys.issubset(api_reachable_keys):
        score_data_mode = "API_CONNECTED"
    elif api_reachable_keys:
        score_data_mode = "PARTIAL_FALLBACK"
    else:
        score_data_mode = "OFFLINE_FALLBACK"
    context["score_data_mode"] = score_data_mode

    if not context.get("themes"):
        context["themes"] = {}
    if not context.get("decision_summary"):
        context["decision_summary"] = {}
    if not context.get("guard_status"):
        context["guard_status"] = _read_json(ROOT / "data" / "reports" / "guard" / "latest_health.json", {})
    if not context.get("telegram_status"):
        context["telegram_status"] = _read_json(ROOT / "data" / "runtime" / "telegram_alert_state.json", {})

    # AlphaForge Validation: always try shared file loader as fallback
    av_data = context.get("alphaforge_validation") or {}
    if not av_data.get("available") and not av_data.get("status") == "FOUND":
        # Try shared loader directly from files
        av_from_file = _load_av()
        if av_from_file.get("status") == "FOUND":
            context["alphaforge_validation"] = av_from_file
            source_status["alphaforge_validation"] = f"file:{av_from_file.get('source_type', 'FILE')}"
        elif not av_data:
            context["alphaforge_validation"] = av_from_file  # keep DATA_NA with proper fields
            source_status.setdefault("alphaforge_validation", f"file:DATA_NA")

    if not context.get("forward_test_summary"):
        context["forward_test_summary"] = (
            (context.get("themes") or {}).get("forward_test_summary")
            or (context.get("decision_summary") or {}).get("forward_test_summary")
            or {}
        )
    return context


def render_markdown(report: dict[str, Any]) -> str:
    scores = report["scores"]
    strength_score = scores["realtime_strength_score"]["score"]
    strength_score_text = "N/A" if strength_score is None else str(strength_score)
    context_text = report.get("score_context") or "UNKNOWN"
    score_data_mode = report.get("score_data_mode", "API_CONNECTED")
    context_note = (
        "현재 점수는 폐장 후 점검 점수이며, 장중 실시간 매수판단 성능은 아직 평가 보류입니다."
        if context_text == "CLOSED_REVIEW"
        else "현재 점수는 장중 실시간 매수판단 점수입니다."
    )
    data_mode_note = (
        "현재 점수는 JC 서버 API 실시간 데이터를 기반으로 합니다."
        if score_data_mode == "API_CONNECTED"
        else "현재 점수는 제한된 fallback 점검 결과입니다. JC 서버가 실행 중이지 않아 일부 데이터는 미수집 상태입니다."
        if score_data_mode == "OFFLINE_FALLBACK"
        else "현재 점수는 일부 API 연결 성공 + fallback 혼합 기반입니다."
    )
    guard_diag = report.get("guard_timestamp_diagnostics") or {}
    av_diag = report.get("alphaforge_validation_diagnostics") or {}
    tg_diag = report.get("telegram_diagnostics") or {}
    av_status = report.get("alphaforge_validation_status") or av_diag.get("alphaforge_validation_status") or "DATA_NA"
    av_score = report.get("alphaforge_validation_link_score")
    ft_status = report.get("forward_test_status", "DATA_INSUFFICIENT")
    ft_n = report.get("forward_test_sample_n", 0)
    ft_reason = report.get("forward_test_reason", "")
    lines = [
        "# JC Practicality & Accuracy Scorecard v1",
        "",
        "## 요약",
        f"- 생성 시각: {report['generated_at']}",
        f"- 데이터 모드: **{score_data_mode}**",
        f"- {data_mode_note}",
        f"- 현재 평가 맥락: **{context_text}**",
        f"- {context_note}",
        f"- 현재 JC 실전성 점수: **{report['overall_jc_practicality_score']} / 100**",
        f"- raw score: {report['raw_overall_score']} / 100",
        f"- confidence: **{report['confidence']}**",
        f"- 시장 게이트: {report['summary'].get('market_gate_level') or '-'} / {report['summary'].get('market_gate_reason') or '-'}",
        "",
        "## 현재 JC 실전성 점수",
        f"- {report['overall_jc_practicality_score']}점: 결측/운영 리스크 cap 적용 후 점수입니다.",
        "",
        "## Closed Review Score",
        f"- score_context: {context_text}",
        f"- 폐장/휴장 점검 문구: {context_note}",
        "",
        "## Live Trading Score 평가 가능 여부",
        f"- realtime_strength_evaluable: {report.get('realtime_strength_evaluable')}",
        f"- reason: {report.get('realtime_strength_reason')}",
        "",
        "## 차단기 점수",
        f"- market_gate_score: {scores['market_gate_score']['score']} ({'; '.join(scores['market_gate_score']['notes']) or '특이사항 없음'})",
        f"- decision_consistency_score: {scores['decision_consistency_score']['score']} ({'; '.join(scores['decision_consistency_score']['notes']) or '특이사항 없음'})",
        "",
        "## 실시간 매수판단 점수",
        f"- realtime_strength_score: {strength_score_text} ({'; '.join(scores['realtime_strength_score']['notes']) or '특이사항 없음'})",
        f"- realtime_strength_status: {report.get('realtime_strength_status')}",
        f"- realtime_strength_score_basis: {report.get('realtime_strength_score_basis')}",
        "",
        "## 데이터 커버리지",
        f"- quote_coverage_score: {scores['quote_coverage_score']['score']}",
        f"- quote details: `{json.dumps(scores['quote_coverage_score']['details'], ensure_ascii=False)}`",
        "",
        "## JO 후보 수신/연동 품질",
        f"- jo_handoff_score: {scores['jo_handoff_score']['score']}",
        f"- details: `{json.dumps(scores['jo_handoff_score']['details'], ensure_ascii=False)}`",
        "",
        "## 시장 게이트 정확성",
        f"- {scores['market_gate_score']['score']}점",
        f"- details: `{json.dumps(scores['market_gate_score']['details'], ensure_ascii=False)}`",
        "",
        "## 체결강도/실시간성 품질",
        f"- {strength_score_text}",
        f"- details: `{json.dumps(scores['realtime_strength_score']['details'], ensure_ascii=False)}`",
        "",
        "## 수급 데이터 품질",
        f"- supply_quality_score: {scores['supply_quality_score']['score']}",
        f"- details: `{json.dumps(scores['supply_quality_score']['details'], ensure_ascii=False)}`",
        "",
        "## Forward Test 결과",
        f"- forward_test_score: {scores['forward_test_score']['score']}",
        f"- details: `{json.dumps(scores['forward_test_score']['details'], ensure_ascii=False)}`",
        "",
        "## Guard/Telegram 운영 상태",
        f"- ops_reliability_score: {scores['ops_reliability_score']['score']}",
        f"- notes: {', '.join(scores['ops_reliability_score']['notes']) or '-'}",
        "",
        "## Telegram 상태 진단",
        f"- telegram_status: {report.get('telegram_status')}",
        f"- telegram_auth_status: {report.get('telegram_auth_status')}",
        f"- telegram_error_status: {report.get('telegram_error_status')}",
        f"- telegram_failed_today_count: {report.get('telegram_failed_today_count')}",
        f"- telegram_sent_today_count: {report.get('telegram_sent_today_count')}",
        f"- telegram_skipped_today_count: {report.get('telegram_skipped_today_count')}",
        f"- telegram_last_error_at: {report.get('telegram_last_error_at')}",
        f"- telegram_last_ok_at: {report.get('telegram_last_ok_at')}",
        f"- telegram_last_error_reason_sanitized: {report.get('telegram_last_error_reason_sanitized') or '-'}",
        f"- telegram_reason: {report.get('telegram_reason')}",
        "- 민감 환경값은 리포트에 포함하지 않습니다.",
        "",
        "## Quote Coverage 진단",
        f"- quote_coverage_score: {report.get('quote_coverage_score')}",
        f"- quote_status: {report.get('quote_status')}",
        f"- quote_coverage_pct: {report.get('quote_coverage_pct')}",
        f"- quote_total: {report.get('quote_total')}",
        f"- quote_success: {report.get('quote_success')}",
        f"- quote_missing: {report.get('quote_missing')}",
        f"- quote_reason: {report.get('quote_reason')}",
        f"- quote_source: {report.get('quote_source')}",
        "",
        "## Guard timestamp 진단",
        f"- guard_timestamp_parse_status: {guard_diag.get('guard_timestamp_parse_status')}",
        f"- guard_timestamp_raw: {guard_diag.get('guard_timestamp_raw')}",
        f"- guard_age_minutes: {guard_diag.get('guard_age_minutes')}",
        f"- guard_age_anomaly: {guard_diag.get('guard_age_anomaly')}",
        f"- guard_age_reason: {guard_diag.get('guard_age_reason')}",
        "",
        "## AlphaForge Validation 연동 상태",
        f"- alphaforge_validation_status: **{av_status}**",
        f"- alphaforge_validation_link_score: {av_score}",
        f"- alphaforge_validation_source_type: {report.get('alphaforge_validation_source_type', 'DATA_NA')}",
        f"- alphaforge_validation_path_found: `{json.dumps(report.get('alphaforge_validation_path_found', []), ensure_ascii=False)}`",
        f"- notes: {', '.join(scores['alphaforge_validation_link_score']['notes']) or '-'}",
        "",
        "## AlphaForge Validation 경로 진단",
        f"- alphaforge_validation_reason: {report.get('alphaforge_validation_reason') or av_diag.get('alphaforge_validation_reason')}",
        "",
        "## Forward Test 상태",
        f"- forward_test_status: **{ft_status}**",
        f"- forward_test_sample_n: {ft_n}",
        f"- forward_test_reason: {ft_reason}",
        "",
        "## 90점 도달 제한 사유",
    ]
    lines.extend([f"- {reason}" for reason in report["cap_reasons"]] or ["- 제한 사유 없음"])
    lines.extend([
        "",
        "## session-aware 점수 cap 적용 여부",
        f"- 체결강도 cap 적용 여부: {'적용' if any('체결강도' in r for r in report['cap_reasons']) else '미적용'}",
        f"- score_context: {context_text}",
        "",
        "## 이상치/버그 진단",
    ])
    lines.extend([f"- [{a['severity']}] {a['code']}: {a['message']}" for a in report["anomalies"]] or ["- 이상치 없음"])
    lines.extend([
        "",
        "## 다음 개선 우선순위",
        "1. 체결강도 커버리지 회복 여부 확인",
        "2. Guard 최신 실행 및 stale 해소",
        "3. Telegram 인증 오류가 있으면 환경 설정을 별도로 점검",
        "4. AlphaForge Validation 리포트 경로 연동 확인",
        "5. Forward Test 표본 누적 후 cap 해제 가능성 재평가",
        "",
    ])
    return "\n".join(lines)


def write_reports(report: dict[str, Any]) -> dict[str, str]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MD_REPORT.write_text(render_markdown(report), encoding="utf-8")
    return {"json": str(JSON_REPORT), "markdown": str(MD_REPORT)}


def build_scorecard(fetch_live: bool = True, write: bool = True) -> dict[str, Any]:
    context = collect_context(fetch_live=fetch_live)
    report = calculate_scorecard(context)
    if write:
        report["report_paths"] = write_reports(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build JC Practicality & Accuracy Scorecard v1")
    parser.add_argument("--no-live", action="store_true", help="Do not call local dashboard APIs; use files/fallbacks only")
    parser.add_argument("--no-write", action="store_true", help="Print report JSON without writing reports/")
    args = parser.parse_args(argv)
    report = build_scorecard(fetch_live=not args.no_live, write=not args.no_write)
    print(json.dumps({
        "score_context": report["score_context"],
        "overall_jc_practicality_score": report["overall_jc_practicality_score"],
        "confidence": report["confidence"],
        "realtime_strength_status": report.get("realtime_strength_status"),
        "realtime_strength_evaluable": report.get("realtime_strength_evaluable"),
        "cap_reasons": report["cap_reasons"],
        "report_paths": report.get("report_paths", {}),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
