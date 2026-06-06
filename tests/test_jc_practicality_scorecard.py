from datetime import datetime, timezone
from pathlib import Path

import jason_checks.jc_practicality_scorecard as scorecard
from jason_checks.jc_practicality_scorecard import calculate_scorecard, render_markdown, write_reports


def _base_context(session="MARKET_CLOSED", mode="prod"):
    return {
        "themes": {
            "mode": mode,
            "alphaforge_candidates_loaded": 5,
            "alphaforge_candidates_generated_at": "2026-06-06T09:00:00+09:00",
            "alphaforge_picks": [
                {
                    "symbol": f"00000{i}",
                    "name": f"후보{i}",
                    "alert_type": "RISK_WATCH",
                    "rs": 90,
                    "vcp_status": "OK",
                    "box_upper_price": 1000,
                }
                for i in range(5)
            ],
            "quote_polling": {
                "total": 96,
                "success": 96,
                "missing": 0,
                "strength_total": 96,
                "strength_success": 96,
            },
            "supply_polling": {"target_total": 5, "ok": 5, "data_na": 0},
            "supply_data_reason": "KIS 선택수급: 전일수급 5/5 · 전체 섹터 수급: 미조회",
        },
        "decision_summary": {
            "session": session,
            "market_gate_level": "CRASH" if session == "MARKET_CLOSED" else "NORMAL",
            "market_gate_reason": "지수 급락",
            "decision_counts": {
                "BUY_NOW": 0,
                "STARTER_POSITION": 0,
                "CONDITIONAL_BUY": 0,
                "WATCH_ONLY": 5,
                "AVOID": 0,
            },
            "results": [
                {
                    "symbol": f"00000{i}",
                    "decision": "WATCH_ONLY",
                    "no_buy_reason": "MARKET_CLOSED",
                    "max_position_pct": 0,
                    "setup_score": 60,
                    "setup_label": "SETUP_WATCH",
                    "setup_reason": "장마감 관찰",
                    "reason_codes": ["MARKET_CRASH"],
                    "next_session_trigger": "박스 돌파",
                    "has_supply": True,
                    "has_strength": True,
                    "has_price": True,
                    "has_trading_value": True,
                }
                for i in range(5)
            ],
        },
        "forward_test_summary": {
            "status": "OK",
            "horizons": {
                "1d": {"count": 10},
                "3d": {"count": 0},
                "5d": {"count": 0},
                "10d": {"count": 0},
            },
            "blocked_quality": {
                "blocked_count": 10,
                "good_block_count": 4,
                "missed_opportunity_count": 0,
                "quality_grade": "우수",
            },
        },
        "guard_status": {"overall_status": "OK", "is_stale": False, "age_sec": 60},
        "telegram_status": {"enabled": True, "configured": True, "dry_run": False, "recent_events": []},
        "alphaforge_validation": {"available": True},
    }


def test_market_closed_strength_zero_is_not_evaluated_or_capped():
    ctx = _base_context()
    ctx["themes"]["quote_polling"]["strength_success"] = 0
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["score_context"] == "CLOSED_REVIEW"
    assert report["realtime_strength_status"] == "NOT_EVALUATED_SESSION_CLOSED"
    assert report["realtime_strength_evaluable"] is False
    assert report["scores"]["realtime_strength_score"]["score"] is None
    assert not any("체결강도 커버리지 0%" in reason for reason in report["cap_reasons"])


def test_live_strength_zero_caps_overall_at_75():
    ctx = _base_context(session="LIVE", mode="LIVE")
    ctx["themes"]["quote_polling"]["strength_success"] = 0
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["score_context"] == "LIVE_TRADING_REVIEW"
    assert report["realtime_strength_status"] == "FAILED_LIVE_STRENGTH_COLLECTION"
    assert report["realtime_strength_evaluable"] is True
    assert report["overall_jc_practicality_score"] <= 75
    assert any("체결강도 커버리지 0%" in reason for reason in report["cap_reasons"])


def test_market_closed_crash_buy_zero_scores_high_gate():
    report = calculate_scorecard(_base_context(), now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["scores"]["market_gate_score"]["score"] >= 90
    assert report["summary"]["decision_counts"]["BUY_NOW"] == 0


def test_guard_stale_lowers_ops_score():
    ctx = _base_context()
    ctx["guard_status"] = {"overall_status": "STALE", "is_stale": True, "age_sec": 90000}
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["scores"]["ops_reliability_score"]["score"] < 70
    assert any("JC Guard STALE" in note for note in report["scores"]["ops_reliability_score"]["notes"])


def test_session_label_mismatch_is_reported():
    ctx = _base_context(session="MARKET_CLOSED", mode="LIVE")
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert any(a["code"] == "SESSION_LABEL_MISMATCH" for a in report["anomalies"])


def test_guard_age_anomaly_and_parse_failure_are_safe():
    ctx = _base_context()
    ctx["guard_status"] = {"overall_status": "STALE", "is_stale": True, "timestamp": "2026-05-01T00:00:00+09:00"}
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["guard_timestamp_diagnostics"]["guard_age_anomaly"] is True
    ctx["guard_status"] = {"overall_status": "STALE", "is_stale": True, "timestamp": "not-a-date"}
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["guard_timestamp_diagnostics"]["guard_timestamp_parse_status"] == "PARSE_FAILED"


def test_forward_small_sample_confidence_not_high():
    report = calculate_scorecard(_base_context(), now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["confidence"] in ("LOW", "MEDIUM_LOW")
    assert any("Forward Test 표본 부족" in reason for reason in report["cap_reasons"])


def test_telegram_401_penalizes_ops():
    ctx = _base_context()
    ctx["telegram_status"] = {
        "enabled": True,
        "configured": True,
        "last_error_reason": "send_error: http_401_unauthorized",
    }
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["scores"]["ops_reliability_score"]["score"] <= 60
    rendered = render_markdown(report)
    assert "http_401_unauthorized" in rendered
    assert "TOKEN" not in rendered.upper()
    assert "CHAT_ID" not in rendered.upper()


def test_alphaforge_validation_missing_penalizes_link_score(monkeypatch):
    monkeypatch.setattr(scorecard, "ALPHAFORGE_VALIDATION_PATHS", [])
    ctx = _base_context()
    ctx["alphaforge_validation"] = {"available": False, "reason": "AlphaForge 검증 리포트 없음"}
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    link = report["scores"]["alphaforge_validation_link_score"]
    assert link["status"] == "DATA_NA"
    assert link["score"] < 70


def test_alphaforge_validation_path_mismatch(monkeypatch, tmp_path):
    found = tmp_path / "alphaforge_performance_scorecard.json"
    found.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scorecard, "ALPHAFORGE_VALIDATION_PATHS", [found, Path("/missing/nope.json")])
    ctx = _base_context()
    ctx["alphaforge_validation"] = {"available": False, "reason": "AlphaForge 검증 리포트 없음"}
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    diag = report["alphaforge_validation_diagnostics"]
    assert report["scores"]["alphaforge_validation_link_score"]["status"] == "PATH_MISMATCH"
    assert diag["alphaforge_validation_status"] == "PATH_MISMATCH"
    assert str(found) in diag["alphaforge_validation_path_found"]


def test_reports_render_without_dashboard_imports(tmp_path, monkeypatch):
    ctx = _base_context()
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    text = render_markdown(report)
    assert "JC Practicality & Accuracy Scorecard v1" in text
    monkeypatch.setattr("jason_checks.jc_practicality_scorecard.REPORT_DIR", tmp_path)
    monkeypatch.setattr("jason_checks.jc_practicality_scorecard.JSON_REPORT", tmp_path / "score.json")
    monkeypatch.setattr("jason_checks.jc_practicality_scorecard.MD_REPORT", tmp_path / "score.md")
    paths = write_reports(report)
    assert (tmp_path / "score.json").exists()
    assert (tmp_path / "score.md").exists()
    assert paths["json"].endswith("score.json")


def test_alphaforge_validation_loader_finds_jo_scorecard_json(monkeypatch, tmp_path):
    import jason_checks.web.app as web_app

    report = tmp_path / "alphaforge_performance_scorecard.json"
    report.write_text(
        '{"generated_at":"2026-06-06T17:29:49","scores":{"overall_practicality_score":61.5,"confidence":"LOW","cap_reasons":["sample insufficient"]},"input_counts":{"candidate_rows":2,"snapshot_rows":1},"horizons":["1d"],"performance":{"by_label":{}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "_AV_REPORT_CANDIDATES", [(report, "JO_SCORECARD_JSON")])
    monkeypatch.setattr(web_app, "_av_cache", {})
    monkeypatch.setattr(web_app, "_av_last_mtime", -1.0)
    monkeypatch.setattr(web_app, "_av_last_path", "")
    data = web_app._load_alphaforge_validation()
    assert data["status"] == "FOUND"
    assert data["source_type"] == "JO_SCORECARD_JSON"
    assert data["overall_practicality_score"] == 61.5
    assert data["confidence"] == "LOW"


def test_alphaforge_validation_loader_missing_is_data_na(monkeypatch, tmp_path):
    import jason_checks.web.app as web_app

    monkeypatch.setattr(web_app, "_AV_REPORT_CANDIDATES", [(tmp_path / "missing.json", "JO_SCORECARD_JSON")])
    monkeypatch.setattr(web_app, "_av_cache", {})
    monkeypatch.setattr(web_app, "_av_last_mtime", -1.0)
    monkeypatch.setattr(web_app, "_av_last_path", "")
    data = web_app._load_alphaforge_validation()
    assert data["status"] == "DATA_NA"
    assert data["source_type"] == "DATA_NA"


def test_alphaforge_validation_loader_tolerates_partial_json(monkeypatch, tmp_path):
    import jason_checks.web.app as web_app

    report = tmp_path / "partial.json"
    report.write_text('{"generated_at":"2026-06-06T17:29:49","scores":{}}', encoding="utf-8")
    monkeypatch.setattr(web_app, "_AV_REPORT_CANDIDATES", [(report, "JO_SCORECARD_JSON")])
    monkeypatch.setattr(web_app, "_av_cache", {})
    monkeypatch.setattr(web_app, "_av_last_mtime", -1.0)
    monkeypatch.setattr(web_app, "_av_last_path", "")
    data = web_app._load_alphaforge_validation()
    assert data["status"] == "FOUND"
    assert data["overall_practicality_score"] is None
    assert data["confidence"] == "DATA_NA"


# ────────────────────────────────────────────────────────────────────────────
# New tests per spec (Section 7)
# ────────────────────────────────────────────────────────────────────────────

def test_jo_scorecard_found_means_top_level_alphaforge_status_found():
    """JO scorecard json이 존재하면 CLI Scorecard top-level에 alphaforge_validation_status=FOUND."""
    ctx = _base_context()
    # Simulate the shared loader already finding the file
    ctx["alphaforge_validation"] = {
        "status": "FOUND",
        "available": True,
        "source_type": "JO_SCORECARD_JSON",
        "path_found": "/some/jo/report.json",
        "paths_checked": ["/some/jo/report.json"],
        "reason": "AlphaForge 검증 리포트 로드 성공: JO_SCORECARD_JSON (report.json)",
    }
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["alphaforge_validation_status"] == "FOUND", \
        f"Expected FOUND, got {report['alphaforge_validation_status']}"
    assert report["alphaforge_validation_link_score"] is not None
    assert report["alphaforge_validation_source_type"] == "JO_SCORECARD_JSON"
    assert "/some/jo/report.json" in report["alphaforge_validation_path_found"]


def test_found_means_no_data_na_cap_reason():
    """FOUND이면 cap_reasons에 AlphaForge DATA_NA 문구가 없다."""
    ctx = _base_context()
    ctx["alphaforge_validation"] = {
        "status": "FOUND",
        "available": True,
        "source_type": "JO_SCORECARD_JSON",
        "path_found": "/some/jo/report.json",
        "paths_checked": ["/some/jo/report.json"],
        "reason": "OK",
    }
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    for reason in report["cap_reasons"]:
        assert "AlphaForge Validation DATA_NA" not in reason, \
            f"Unexpected DATA_NA cap when FOUND: {reason}"
        assert "미연동 또는 DATA_NA" not in reason, \
            f"Unexpected '미연동 또는 DATA_NA' cap when FOUND: {reason}"


def test_closed_review_offline_no_strength_data_is_not_evaluable():
    """CLOSED_REVIEW + API fallback + 체결강도 데이터 부족이면 realtime_strength_evaluable=false."""
    ctx = _base_context(session="MARKET_CLOSED")
    # Simulate no strength data at all (total=0) — offline/closed
    ctx["themes"]["quote_polling"] = {
        "total": 0,
        "success": 0,
        "missing": 0,
        "strength_total": 0,
        "strength_success": 0,
    }
    ctx["decision_summary"]["results"] = []
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["score_context"] == "CLOSED_REVIEW"
    assert report["realtime_strength_evaluable"] is False, \
        f"Expected evaluable=False, got {report['realtime_strength_evaluable']}"
    assert report["realtime_strength_status"] in (
        "NOT_EVALUATED_SESSION_CLOSED",
        "NOT_EVALUATED_OFFLINE_CLOSED",
    ), f"Unexpected status: {report['realtime_strength_status']}"


def test_closed_review_no_strength_cap_not_applied():
    """CLOSED_REVIEW에서는 체결강도 커버리지 부족 cap이 적용되지 않는다."""
    ctx = _base_context(session="MARKET_CLOSED")
    ctx["themes"]["quote_polling"]["strength_success"] = 0
    ctx["themes"]["quote_polling"]["strength_total"] = 96
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["score_context"] == "CLOSED_REVIEW"
    for reason in report["cap_reasons"]:
        assert "체결강도 커버리지 0%" not in reason, \
            f"체결강도 cap should NOT apply during CLOSED_REVIEW: {reason}"
        assert "체결강도 커버리지 < 50%" not in reason, \
            f"체결강도 cap should NOT apply during CLOSED_REVIEW: {reason}"


def test_live_strength_zero_triggers_failed_and_cap():
    """LIVE_TRADING_REVIEW + 체결강도 0%에서만 FAILED_LIVE_STRENGTH_COLLECTION 및 max 75 cap 적용."""
    ctx = _base_context(session="LIVE", mode="LIVE")
    ctx["themes"]["quote_polling"]["strength_success"] = 0
    ctx["themes"]["quote_polling"]["strength_total"] = 96
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["score_context"] == "LIVE_TRADING_REVIEW"
    assert report["realtime_strength_status"] == "FAILED_LIVE_STRENGTH_COLLECTION"
    assert report["realtime_strength_evaluable"] is True
    assert report["overall_jc_practicality_score"] <= 75
    assert any("체결강도 커버리지 0%" in r for r in report["cap_reasons"]), \
        f"Expected 체결강도 cap, got: {report['cap_reasons']}"


def test_forward_test_api_unavailable_status_when_offline():
    """API가 없어서 Forward Test를 못 읽은 경우 forward_test_status=API_UNAVAILABLE 또는 DATA_INSUFFICIENT."""
    ctx = _base_context()
    # Simulate OFFLINE_FALLBACK mode with no forward test data
    ctx["score_data_mode"] = "OFFLINE_FALLBACK"
    ctx["forward_test_summary"] = {}  # empty = no data
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["forward_test_status"] in ("API_UNAVAILABLE", "DATA_INSUFFICIENT"), \
        f"Unexpected forward_test_status: {report['forward_test_status']}"
    assert report["forward_test_sample_n"] is not None  # must not be None


def test_top_level_fields_not_none():
    """top-level 필드가 None으로 누락되지 않는다."""
    ctx = _base_context()
    ctx["alphaforge_validation"] = {"available": False, "reason": "없음"}
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    required_fields = [
        "alphaforge_validation_status",
        "alphaforge_validation_link_score",
        "alphaforge_validation_source_type",
        "alphaforge_validation_path_found",
        "alphaforge_validation_reason",
        "score_data_mode",
        "forward_test_status",
        "forward_test_sample_n",
        "forward_test_reason",
    ]
    for field in required_fields:
        assert field in report, f"Missing field: {field}"
        assert report[field] is not None, f"Field {field!r} is None"


# ────────────────────────────────────────────────────────────────────────────
# Telegram 401 historical/current discrimination tests
# ────────────────────────────────────────────────────────────────────────────

def test_telegram_401_historical_recovered_no_cap():
    """last_ok_at > last_error_at + failed_today=0 → HISTORICAL 판단, cap 없음."""
    ctx = _base_context()
    ctx["telegram_status"] = {
        "enabled": True,
        "configured": True,
        "credentials_present": True,
        "last_error_reason": "send_error: http_401_unauthorized Unauthorized",
        "last_error_at": 1780545764980,
        "last_ok_at": 1780638322024,   # last_ok_at > last_error_at
        "failed_today_count": 0,
    }
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["telegram_auth_status"] == "HISTORICAL_UNAUTHORIZED_RECOVERED", \
        f"Expected HISTORICAL_UNAUTHORIZED_RECOVERED, got {report['telegram_auth_status']}"
    assert report["telegram_error_status"] == "HISTORICAL_ERROR_RECOVERED"
    # cap에 Telegram 401 없음
    for reason in report["cap_reasons"]:
        assert "Telegram" not in reason or "401" not in reason, \
            f"Telegram 401 cap should NOT appear for historical: {reason}"
    # ops score 보다 크게 패널티 없음 (>= 80)
    ops_score = report["scores"]["ops_reliability_score"]["score"]
    assert ops_score >= 80, f"Expected ops_score >= 80 for historical error, got {ops_score}"


def test_telegram_401_current_unauthorized_triggers_cap():
    """last_ok_at이 없으면 CURRENT_UNAUTHORIZED → cap 적용."""
    ctx = _base_context()
    ctx["telegram_status"] = {
        "enabled": True,
        "configured": True,
        "credentials_present": True,
        "last_error_reason": "send_error: http_401_unauthorized Unauthorized",
        "last_error_at": 1780638322024,
        # last_ok_at 없음
        "failed_today_count": 0,
    }
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["telegram_auth_status"] == "CURRENT_UNAUTHORIZED", \
        f"Expected CURRENT_UNAUTHORIZED, got {report['telegram_auth_status']}"
    assert any("Telegram current 401 unauthorized" in r for r in report["cap_reasons"]), \
        f"Expected Telegram cap for current 401, got: {report['cap_reasons']}"
    assert report["scores"]["ops_reliability_score"]["score"] <= 60


def test_telegram_401_last_error_at_newer_than_ok_is_current():
    """last_error_at > last_ok_at이면 CURRENT_UNAUTHORIZED."""
    ctx = _base_context()
    ctx["telegram_status"] = {
        "enabled": True,
        "configured": True,
        "credentials_present": True,
        "last_error_reason": "send_error: http_401_unauthorized",
        "last_error_at": 1780700000000,   # newer
        "last_ok_at": 1780600000000,       # older
        "failed_today_count": 0,
    }
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["telegram_auth_status"] == "CURRENT_UNAUTHORIZED"
    assert report["telegram_error_status"] == "CURRENT_ERROR_401"


def test_telegram_fields_no_token_in_report():
    """telegram 필드에 token/chat_id 실제 값이 없어야 한다."""
    ctx = _base_context()
    ctx["telegram_status"] = {
        "enabled": True,
        "configured": True,
        "token_present": True,
        "chat_id_present": True,
        "last_error_reason": "send_error: http_401_unauthorized Unauthorized",
        "last_error_at": 1780545764980,
        "last_ok_at": 1780638322024,
        "failed_today_count": 0,
        "sent_today_count": 5,
        "skipped_today_count": 1,
    }
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    rendered = render_markdown(report)
    # top-level fields populated
    assert report["telegram_auth_status"] is not None
    assert report["telegram_error_status"] is not None
    assert report["telegram_failed_today_count"] == 0
    assert report["telegram_sent_today_count"] == 5
    assert report["telegram_skipped_today_count"] == 1
    assert report["telegram_last_error_at"] == 1780545764980
    assert report["telegram_last_ok_at"] == 1780638322024
    # no sensitive values
    assert "TOKEN" not in rendered.upper()
    assert "CHAT_ID" not in rendered.upper()


# ────────────────────────────────────────────────────────────────────────────
# Quote coverage top-level fields and cap precision tests
# ────────────────────────────────────────────────────────────────────────────

def test_quote_coverage_top_level_fields_present():
    """quote coverage top-level 필드가 None 없이 존재한다."""
    ctx = _base_context()
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    for field in ["quote_coverage_score", "quote_coverage_pct", "quote_total",
                  "quote_success", "quote_missing", "quote_status", "quote_reason", "quote_source"]:
        assert field in report, f"Missing field: {field}"
    # base_context has quote_polling total=96, success=96 → pct=100%
    assert report["quote_total"] == 96
    assert report["quote_success"] == 96
    assert report["quote_coverage_pct"] == 100.0
    assert report["quote_status"] == "OK"


def test_quote_coverage_low_triggers_cap():
    """quote coverage < 90% 이고 total > 0이면 cap reason 추가."""
    ctx = _base_context()
    ctx["themes"]["quote_polling"]["success"] = 40
    ctx["themes"]["quote_polling"]["total"] = 96
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["quote_status"] == "LOW_COVERAGE"
    assert any("quote coverage < 90%" in r for r in report["cap_reasons"]), \
        f"Expected quote cap, got: {report['cap_reasons']}"


def test_quote_coverage_no_data_no_false_cap():
    """quote total=0이면 quote coverage < 90% cap이 아니라 DATA_INSUFFICIENT cap 사용."""
    ctx = _base_context()
    ctx["themes"]["quote_polling"] = {"total": 0, "success": 0, "missing": 0}
    report = calculate_scorecard(ctx, now=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc))
    assert report["quote_total"] is None
    assert report["quote_status"] == "DATA_INSUFFICIENT"
    for r in report["cap_reasons"]:
        assert "quote coverage < 90%" not in r, \
            f"Should NOT have '< 90%' cap when data is missing: {r}"
