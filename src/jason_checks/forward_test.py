"""Forward Test Evaluator v1 - Decision Engine 신호 사후 검증 레이어."""

import os
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import structlog

logger = structlog.get_logger()
KST = ZoneInfo("Asia/Seoul")

# ── Caching state ────────────────────────────────────────────────────────────
_signals_cache = []
_cache_mtime = 0
_cache_file_size = 0

def _decision_journal_path() -> Path:
    project_root = Path(__file__).parent.parent.parent
    return project_root / "data" / "signal_journal" / "decision_signals.jsonl"


def _normalize_symbol(code: object) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        value = value[1:]
    if value.isdigit() and len(value) < 6:
        value = value.zfill(6)
    return value


def _load_signals_cached(path: Path) -> list[dict]:
    """mtime & size 변동이 없으면 파싱 결과를 캐시에서 반환하는 고속 로더."""
    global _signals_cache, _cache_mtime, _cache_file_size
    if not path.exists():
        return []

    try:
        stat = path.stat()
        if stat.st_mtime == _cache_mtime and stat.st_size == _cache_file_size:
            return _signals_cache

        signals = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    signals.append(row)
                except Exception:
                    continue

        _signals_cache = signals
        _cache_mtime = stat.st_mtime
        _cache_file_size = stat.st_size
        logger.info("forward_test_log_loaded", count=len(signals), size_mb=round(stat.st_size / (1024*1024), 2))
    except Exception as e:
        logger.warning("forward_test_load_failed_use_cached", error=str(e))
        
    return _signals_cache


def _aggregate_returns(returns: list[float]) -> dict:
    """성과 집계 헬퍼 함수."""
    if not returns:
        return {
            "count": 0,
            "avg_return": 0.0,
            "win_rate": 0.0,
            "best_return": 0.0,
            "worst_return": 0.0,
            "data_status": "DATA_INSUFFICIENT"
        }

    count = len(returns)
    avg_return = sum(returns) / count
    wins = sum(1 for r in returns if r > 0)
    win_rate = (wins / count) * 100
    best_return = max(returns)
    worst_return = min(returns)

    return {
        "count": count,
        "avg_return": round(avg_return, 2),
        "win_rate": round(win_rate, 1),
        "best_return": round(best_return, 2),
        "worst_return": round(worst_return, 2),
        "data_status": "OK"
    }


def _analyze_blocked_quality(blocked_signals: list[dict]) -> dict:
    """WATCH_ONLY / AVOID 차단 품질 분석."""
    blocked_count = len(blocked_signals)
    good_block_count = 0
    missed_opportunity_count = 0
    missed_opportunity_symbols = []

    for sig in blocked_signals:
        ret = sig.get("return_pct", 0.0)
        symbol = sig.get("symbol", "")
        
        # WATCH/AVOID 후 -3% 이하 하락: good_block
        if ret <= -3.0:
            good_block_count += 1
        # WATCH/AVOID 후 +5% 이상 상승: missed_opportunity
        elif ret >= 5.0:
            missed_opportunity_count += 1
            if symbol and symbol not in missed_opportunity_symbols:
                missed_opportunity_symbols.append(symbol)

    # 차단 품질 평가 등급 산출
    if blocked_count == 0:
        quality_grade = "아직 데이터 부족"
    elif missed_opportunity_count == 0:
        quality_grade = "우수"
    elif (missed_opportunity_count / blocked_count) <= 0.2:
        quality_grade = "양호"
    else:
        quality_grade = "점검 필요"

    return {
        "blocked_count": blocked_count,
        "good_block_count": good_block_count,
        "missed_opportunity_count": missed_opportunity_count,
        "missed_opportunity_symbols": missed_opportunity_symbols[:15],
        "quality_grade": quality_grade
    }


def run_forward_test() -> dict:
    """Retrospective Forward Test Evaluator v1 진입점."""
    path = _decision_journal_path()
    
    # 1. 신호 읽기 (파일이 없으면 DATA_INSUFFICIENT 안전 반환)
    if not path.exists():
        return _make_insufficient_response("신호 기록 파일 없음")

    all_signals = _load_signals_cached(path)
    if not all_signals:
        return _make_insufficient_response("기록된 신호 없음")

    # 2. 중복 제거 (동일 symbol/date의 최신 signal만 추출)
    # KST 기준 날짜별로 그룹화
    latest_signals: dict[tuple[str, str], dict] = {}
    for sig in all_signals:
        symbol = _normalize_symbol(sig.get("symbol"))
        ts_str = sig.get("timestamp")
        if not symbol or not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            else:
                dt = dt.astimezone(KST)
            date_str = dt.strftime("%Y-%m-%d")
            latest_signals[(symbol, date_str)] = sig
        except Exception:
            continue

    # 3. 실시간 가격 정보 로드 및 성과 계산
    try:
        from jason_checks.state import app_state
    except ImportError:
        return _make_insufficient_response("애플리케이션 상태 조회 불가")

    today = datetime.now(KST).date()
    evaluated_by_horizon: dict[str, list[dict]] = {
        "1d": [], "3d": [], "5d": [], "10d": []
    }

    for (symbol, date_str), sig in latest_signals.items():
        signal_price = sig.get("price")
        # 필수 보호: signal_price <= 0 제외
        if signal_price is None or signal_price <= 0:
            continue

        # 필수 보호: current_price <= 0 및 미등록 제외
        stock_state = app_state.stocks.get(symbol)
        if not stock_state or stock_state.price <= 0:
            continue
        current_price = stock_state.price

        # 기본 성과 계산식 적용
        return_pct = (current_price - signal_price) / signal_price * 100
        sig_eval = dict(sig)
        sig_eval["return_pct"] = return_pct
        sig_eval["current_price"] = current_price

        # 날짜 차이 계산
        try:
            sig_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            diff_days = (today - sig_date).days
            
            # horizon에 매칭
            if diff_days == 1:
                evaluated_by_horizon["1d"].append(sig_eval)
            elif diff_days == 3:
                evaluated_by_horizon["3d"].append(sig_eval)
            elif diff_days == 5:
                evaluated_by_horizon["5d"].append(sig_eval)
            elif diff_days == 10:
                evaluated_by_horizon["10d"].append(sig_eval)
        except Exception:
            continue

    # 4. Horizon별 결과 집계
    horizons_summary = {}
    all_evaluated_signals = []

    for hz_key in ("1d", "3d", "5d", "10d"):
        hz_sigs = evaluated_by_horizon[hz_key]
        all_evaluated_signals.extend(hz_sigs)

        if not hz_sigs:
            horizons_summary[hz_key] = {
                "status": "DATA_INSUFFICIENT",
                "count": 0,
                "avg_return": 0.0,
                "win_rate": 0.0,
                "best_return": 0.0,
                "worst_return": 0.0,
                "blocked_quality": {
                    "blocked_count": 0,
                    "good_block_count": 0,
                    "missed_opportunity_count": 0,
                    "missed_opportunity_symbols": [],
                    "quality_grade": "아직 데이터 부족"
                }
            }
            continue

        # horizon 내 성과 리스트
        hz_returns = [s["return_pct"] for s in hz_sigs]
        hz_blocked = [s for s in hz_sigs if s.get("decision") in ("AVOID", "WATCH_ONLY")]
        
        hz_stats = _aggregate_returns(hz_returns)
        hz_stats["status"] = "OK"
        hz_stats["blocked_quality"] = _analyze_blocked_quality(hz_blocked)
        horizons_summary[hz_key] = hz_stats

    # 데이터가 아예 없는 경우 처리
    if not all_evaluated_signals:
        return _make_insufficient_response("유효한 분석 대상 신호 없음")

    # 5. 전체 집계 (by_decision, by_setup_label, blocked_quality)
    by_decision = {}
    by_setup_label = {}
    by_market_gate_level = {}
    by_reason_code = {}

    # 속성별 성과 그룹화 리스트
    dec_groups: dict[str, list[float]] = {}
    setup_groups: dict[str, list[float]] = {}
    gate_groups: dict[str, list[float]] = {}
    reason_groups: dict[str, list[float]] = {}
    overall_blocked = []

    for sig in all_evaluated_signals:
        ret = sig["return_pct"]
        dec = sig.get("decision") or "WATCH_ONLY"
        setup = sig.get("setup_label") or "RISK_ONLY"
        gate = sig.get("market_gate_level") or "NORMAL"
        reasons = sig.get("reason_codes") or []

        dec_groups.setdefault(dec, []).append(ret)
        setup_groups.setdefault(setup, []).append(ret)
        gate_groups.setdefault(gate, []).append(ret)
        for r_code in reasons:
            reason_groups.setdefault(r_code, []).append(ret)

        if dec in ("AVOID", "WATCH_ONLY"):
            overall_blocked.append(sig)

    # 집계 수행
    for dec, rets in dec_groups.items():
        by_decision[dec] = _aggregate_returns(rets)
    for setup, rets in setup_groups.items():
        by_setup_label[setup] = _aggregate_returns(rets)
    for gate, rets in gate_groups.items():
        by_market_gate_level[gate] = _aggregate_returns(rets)
    for r_code, rets in reason_groups.items():
        by_reason_code[r_code] = _aggregate_returns(rets)

    overall_blocked_quality = _analyze_blocked_quality(overall_blocked)

    # 6. 최종 요약 반환
    return {
        "status": "OK",
        "message": "성과 분석 완료",
        "horizons": horizons_summary,
        "by_decision": by_decision,
        "by_setup_label": by_setup_label,
        "by_market_gate_level": by_market_gate_level,
        "by_reason_code": by_reason_code,
        "blocked_quality": overall_blocked_quality,
        "last_updated": datetime.now(KST).isoformat()
    }


def _make_insufficient_response(msg: str) -> dict:
    """데이터 부족 시 안전한 응답 생성."""
    empty_quality = {
        "blocked_count": 0,
        "good_block_count": 0,
        "missed_opportunity_count": 0,
        "missed_opportunity_symbols": [],
        "quality_grade": "아직 데이터 부족"
    }
    empty_hz = {
        "status": "DATA_INSUFFICIENT",
        "count": 0,
        "avg_return": 0.0,
        "win_rate": 0.0,
        "best_return": 0.0,
        "worst_return": 0.0,
        "blocked_quality": empty_quality
    }
    return {
        "status": "DATA_INSUFFICIENT",
        "message": msg,
        "horizons": {
            "1d": empty_hz,
            "3d": empty_hz,
            "5d": empty_hz,
            "10d": empty_hz
        },
        "by_decision": {},
        "by_setup_label": {},
        "by_market_gate_level": {},
        "by_reason_code": {},
        "blocked_quality": empty_quality,
        "last_updated": datetime.now(KST).isoformat()
    }
