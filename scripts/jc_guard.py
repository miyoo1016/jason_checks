#!/usr/bin/env python3
"""Self-healing guard v1 for CHECKS.

The guard diagnoses local runtime health, optionally performs safe server
restart repair, writes reports, and generates a Codex prompt for minimal fixes.
It never edits application code and never commits changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, parse, request


ROOT = Path("/Users/miyoo1016/jason_checks")
BASE_URL = "http://127.0.0.1:8000"
REPORT_DIR = ROOT / "data" / "reports" / "guard"
HEALTH_PATH = REPORT_DIR / "latest_health.json"
INCIDENT_PATH = REPORT_DIR / "latest_incident.md"
CODEX_PROMPT_PATH = REPORT_DIR / "latest_codex_prompt.txt"
SERVER_LOG = ROOT / "server.log"

LOG_PATTERNS = [
    "ERROR",
    "Exception",
    "Traceback",
    "telegram_send_error",
    "Failed to load",
]

HTTP_ERROR_RE = re.compile(r"(?:HTTP/[^\" ]+\"?\s+|status(?:_code)?[=: ]+)(401|403|500)\b")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_cmd(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def read_env_flags() -> dict[str, str]:
    env_path = ROOT / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def env_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def add_issue(issues: list[dict[str, Any]], severity: str, code: str, summary: str, evidence: Any = None) -> None:
    issues.append({
        "severity": severity,
        "code": code,
        "summary": summary,
        "evidence": evidence,
    })


def http_json(path: str, timeout: float = 5.0) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    started = time.time()
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            return {
                "ok": 200 <= resp.status < 300,
                "status_code": resp.status,
                "duration_sec": round(time.time() - started, 3),
                "data": data,
                "error": "",
            }
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")[:500]
        return {
            "ok": False,
            "status_code": exc.code,
            "duration_sec": round(time.time() - started, 3),
            "data": {},
            "error": text or str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": 0,
            "duration_sec": round(time.time() - started, 3),
            "data": {},
            "error": f"{type(exc).__name__}: {exc}",
        }


def get_port_pids() -> list[int]:
    try:
        proc = run_cmd(["lsof", "-tiTCP:8000", "-sTCP:LISTEN"], timeout=3)
    except Exception:
        return []
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def kill_port_8000() -> list[int]:
    pids = get_port_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(1)
    for pid in get_port_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return pids


def start_server() -> int | None:
    log = open(SERVER_LOG, "ab")
    proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "server.py"],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc.pid


def restart_server() -> dict[str, Any]:
    killed = kill_port_8000()
    pid = start_server()
    time.sleep(5)
    indices = http_json("/api/indices", timeout=5)
    return {
        "attempted": True,
        "action": "restart_server",
        "killed_pids": killed,
        "started_pid": pid,
        "post_check_ok": indices["ok"],
        "post_check_status_code": indices["status_code"],
    }


def diagnose_indices(api_result: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    data = api_result.get("data") or {}
    indices = data.get("indices") if isinstance(data, dict) else {}
    result = {"ok": bool(api_result.get("ok")), "count": 0, "items": {}, "issues": []}
    if not api_result.get("ok"):
        add_issue(issues, "FAIL", "INDICES_API_UNAVAILABLE", "/api/indices 응답 실패", api_result)
        result["issues"].append("api_unavailable")
        return result
    if not isinstance(indices, dict):
        add_issue(issues, "FAIL", "INDICES_BAD_PAYLOAD", "/api/indices 응답 구조 이상", data)
        result["issues"].append("bad_payload")
        return result

    wanted = {"KOSPI": False, "KOSDAQ": False}
    for code, item in indices.items():
        name = str(item.get("name") or code).upper()
        logical = "KOSDAQ" if "KOSDAQ" in name or code == "1001" else "KOSPI" if "KOSPI" in name or code == "0001" else name
        price = to_float(item.get("price"))
        source = str(item.get("source") or "").lower()
        spark = item.get("sparkline")
        spark_status = spark.get("status") if isinstance(spark, dict) else None
        spark_points = spark.get("points") if isinstance(spark, dict) else []
        point_count = len(spark_points) if isinstance(spark_points, list) else 0
        result["items"][logical] = {
            "code": code,
            "price": price,
            "change_pct": item.get("change_pct"),
            "source": source or "unknown",
            "sparkline_status": spark_status,
            "sparkline_points": point_count,
        }
        if logical in wanted:
            wanted[logical] = True
        if price <= 0:
            add_issue(issues, "FAIL", "INDEX_PRICE_MISSING", f"{logical} price <= 0", result["items"][logical])
            result["issues"].append(f"{logical}_price_missing")
        if source in {"dummy", "mock"}:
            add_issue(issues, "FAIL", "INDEX_DUMMY_SOURCE", f"{logical} source가 {source}", result["items"][logical])
            result["issues"].append(f"{logical}_dummy_source")
        if isinstance(spark, dict) and spark_status == "OK" and point_count < 2:
            add_issue(issues, "WARN", "INDEX_SPARKLINE_WEAK", f"{logical} sparkline OK인데 points 부족", result["items"][logical])
            result["issues"].append(f"{logical}_sparkline_points")
    result["count"] = len(indices)
    for logical, seen in wanted.items():
        if not seen:
            add_issue(issues, "FAIL", "INDEX_MISSING", f"{logical} 지수 없음", {"available": list(indices.keys())})
            result["issues"].append(f"{logical}_missing")
    return result


def diagnose_telegram(api_result: dict[str, Any], env_values: dict[str, str], issues: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "ok": bool(api_result.get("ok")),
        "status_code": api_result.get("status_code"),
        "enabled": None,
        "dry_run": None,
        "credentials_present": None,
        "daily_reset_date": "",
        "sent_today_count": 0,
        "failed_today_count": 0,
        "skipped_today_count": 0,
        "recent_events_count": 0,
        "send_error": "",
    }
    if not api_result.get("ok"):
        add_issue(issues, "WARN", "TELEGRAM_STATUS_UNAVAILABLE", "/api/telegram/status 없음 또는 응답 실패", {
            "status_code": api_result.get("status_code"),
            "error": api_result.get("error"),
        })
        return result

    data = api_result.get("data") or {}
    for key in (
        "enabled", "dry_run", "credentials_present", "daily_reset_date",
        "sent_today_count", "failed_today_count", "skipped_today_count",
    ):
        result[key] = data.get(key)
    recent = data.get("recent_events")
    if isinstance(recent, list):
        result["recent_events_count"] = len(recent)
    else:
        add_issue(issues, "WARN", "TELEGRAM_RECENT_EVENTS_MISSING", "recent_events 필드 없음", None)
    result["send_error"] = data.get("last_error_reason") or ""
    if result["send_error"]:
        add_issue(issues, "WARN", "TELEGRAM_SEND_ERROR", "텔레그램 최근 발송 오류 존재", result["send_error"])

    env_dry_run = env_bool(env_values.get("KR_TELEGRAM_DRY_RUN"))
    if result["dry_run"] is True and env_dry_run is False:
        add_issue(
            issues,
            "WARN",
            "TELEGRAM_DRY_RUN_ENV_MISMATCH",
            "KR_TELEGRAM_DRY_RUN=false 인데 API dry_run=true",
            {"api_dry_run": True, "env_expected_dry_run": False},
        )
    return result


def diagnose_themes(api_result: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "ok": bool(api_result.get("ok")),
        "status_code": api_result.get("status_code"),
        "theme_count": 0,
        "quote_load_success": 0,
        "quote_load_total": 0,
        "quote_missing": 0,
        "sector_warning_count": 0,
        "duplicated_symbols_count": 0,
        "suspicious_symbols_count": 0,
    }
    if not api_result.get("ok"):
        add_issue(issues, "WARN", "THEMES_API_UNAVAILABLE", "/api/themes 응답 실패", {
            "status_code": api_result.get("status_code"),
            "error": api_result.get("error"),
        })
        return result
    data = api_result.get("data") or {}
    themes = data.get("themes") or {}
    result["theme_count"] = len(themes) if isinstance(themes, dict) else 0
    quote = data.get("quote_polling") or {}
    result["quote_load_success"] = int_or_zero(quote.get("success") or quote.get("theme_row_price_count"))
    result["quote_load_total"] = int_or_zero(quote.get("total") or quote.get("theme_row_total"))
    result["quote_missing"] = int_or_zero(quote.get("missing"))
    result["sector_warning_count"] = len(data.get("sector_audit_warnings") or [])
    result["duplicated_symbols_count"] = len(data.get("duplicated_symbols") or [])
    result["suspicious_symbols_count"] = len(data.get("suspicious_sector_members") or [])
    if result["theme_count"] <= 0:
        add_issue(issues, "WARN", "THEMES_EMPTY", "산업군/테마 데이터 없음", None)
    if result["quote_load_total"] and result["quote_load_success"] == 0:
        add_issue(issues, "WARN", "QUOTE_ALL_MISSING", "시세 표시 가능 종목 0개", quote)
    if result["sector_warning_count"] or result["duplicated_symbols_count"] or result["suspicious_symbols_count"]:
        add_issue(issues, "WARN", "SECTOR_AUDIT_WARNINGS", "섹터 audit 경고 존재", result)
    return result


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def int_or_zero(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def read_log_tail(lines: int = 300) -> list[str]:
    if not SERVER_LOG.exists():
        return []
    raw = SERVER_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    return raw[-lines:]


def extract_traceback_block(lines: list[str]) -> str:
    start = None
    for idx in range(len(lines) - 1, -1, -1):
        if "Traceback" in lines[idx]:
            start = idx
            break
    if start is None:
        return ""
    return "\n".join(lines[start:start + 60])


def diagnose_logs(issues: list[dict[str, Any]]) -> dict[str, Any]:
    lines = read_log_tail(300)
    hits = []
    for line in lines:
        if any(pattern in line for pattern in LOG_PATTERNS) or HTTP_ERROR_RE.search(line):
            hits.append(line[-500:])
    traceback_block = extract_traceback_block(lines)
    result = {
        "server_log_exists": SERVER_LOG.exists(),
        "scanned_lines": len(lines),
        "hit_count": len(hits),
        "hits": hits[-20:],
        "traceback": traceback_block,
    }
    if traceback_block:
        add_issue(issues, "FAIL", "SERVER_LOG_TRACEBACK", "server.log 최근 300줄에 Traceback 존재", traceback_block)
    elif hits:
        add_issue(issues, "WARN", "SERVER_LOG_WARNINGS", "server.log 최근 300줄에 오류 패턴 존재", hits[-10:])
    return result


def overall_status(issues: list[dict[str, Any]]) -> str:
    severities = {item["severity"] for item in issues}
    if "FAIL" in severities:
        return "FAIL"
    if "WARN" in severities:
        return "WARN"
    return "OK"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_incident(health: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    lines = [
        "# JC Guard Incident",
        f"- time: {health['timestamp']}",
        f"- status: {health['overall_status']}",
        f"- summary: {summarize_issues(issues)}",
        "- detected issues:",
    ]
    if issues:
        for issue in issues:
            lines.append(f"  - [{issue['severity']}] {issue['code']}: {issue['summary']}")
    else:
        lines.append("  - none")
    lines.extend(["- evidence:"])
    for issue in issues:
        evidence = issue.get("evidence")
        if evidence:
            rendered = evidence if isinstance(evidence, str) else json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
            lines.append(f"  - {issue['code']}:")
            lines.append("```")
            lines.append(rendered[:4000])
            lines.append("```")
    if not issues:
        lines.append("  - healthcheck completed without detected issues")
    auto = health.get("auto_repair") or {}
    lines.extend([
        f"- auto repair attempted: {auto.get('attempted', False)}",
        f"- result: {auto.get('result', 'not_attempted')}",
        f"- next action: {'Use latest_codex_prompt.txt' if issues else 'No code action needed'}",
    ])
    INCIDENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    INCIDENT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_codex_prompt(health: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    if not issues:
        text = (
            "작업 폴더: /Users/miyoo1016/jason_checks\n\n"
            "현재 JC Guard healthcheck가 OK입니다. 코드 수정하지 말고 상태만 확인하세요.\n"
        )
    else:
        issue_lines = "\n".join(f"- [{i['severity']}] {i['code']}: {i['summary']}" for i in issues)
        text = f"""작업 폴더: /Users/miyoo1016/jason_checks

목표:
JC Guard가 감지한 아래 문제를 최소 수정한다.

감지 문제:
{issue_lines}

수정 허용 파일:
- src/jason_checks/web/app.py
- src/jason_checks/telegram_notifier.py
- src/jason_checks/kis_rest.py
- src/jason_checks/state.py
- 필요 시 관련 최소 파일 1개

금지:
- 대규모 리팩터링 금지
- 새 dependency 추가 금지
- 자동 커밋 금지
- .env 값/토큰/chat_id 출력 금지
- 96개 전체 수급 조회 금지
- BUY_NOW hard gate 변경 금지
- dashboard UI 대공사 금지
- data/reports, data/runtime, cache 결과파일 커밋 금지

참고 리포트:
- {INCIDENT_PATH}
- {HEALTH_PATH}

검증:
.venv/bin/python -m compileall src server.py checks.py
git diff --check
scripts/run_jc_guard.sh --no-telegram
"""
    CODEX_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_PROMPT_PATH.write_text(text, encoding="utf-8")


def summarize_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "OK"
    return "; ".join(f"{i['code']}" for i in issues[:5])


def send_telegram_summary(health: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    env_values = read_env_flags()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env_values.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or env_values.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"attempted": False, "ok": False, "reason": "credentials_missing"}

    message = "\n".join([
        "[JC Guard]",
        f"status: {health['overall_status']}",
        f"issue: {summarize_issues(issues)}",
        f"auto_repair: {(health.get('auto_repair') or {}).get('result', 'not_attempted')}",
        f"report: {INCIDENT_PATH}",
        f"codex_prompt: {CODEX_PROMPT_PATH}",
    ])
    payload = parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST")
        with request.urlopen(req, timeout=5) as resp:
            return {"attempted": True, "ok": 200 <= resp.status < 300, "status_code": resp.status}
    except Exception as exc:
        return {"attempted": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    env_values = read_env_flags()
    pids = get_port_pids()
    server = {
        "base_url": BASE_URL,
        "port": 8000,
        "pids": pids,
        "responding": False,
        "apis": {},
    }
    auto_repair = {"attempted": False, "result": "not_attempted"}

    indices_api = http_json("/api/indices", timeout=5)
    server["responding"] = bool(indices_api["ok"])
    if not indices_api["ok"]:
        add_issue(issues, "FAIL", "SERVER_UNRESPONSIVE", "8000 서버 또는 /api/indices 미응답", indices_api)
        if args.repair:
            auto_repair = restart_server()
            auto_repair["result"] = "repaired" if auto_repair.get("post_check_ok") else "failed"
            indices_api = http_json("/api/indices", timeout=5)
            server["pids"] = get_port_pids()
            server["responding"] = bool(indices_api["ok"])
            if indices_api["ok"]:
                issues = [i for i in issues if i["code"] != "SERVER_UNRESPONSIVE"]

    themes_api = http_json("/api/themes?sort=default", timeout=8) if server["responding"] else {"ok": False, "status_code": 0, "error": "server_not_responding", "data": {}}
    telegram_api = http_json("/api/telegram/status", timeout=5) if server["responding"] else {"ok": False, "status_code": 0, "error": "server_not_responding", "data": {}}
    server["apis"] = {
        "/api/indices": strip_data(indices_api),
        "/api/themes": strip_data(themes_api),
        "/api/telegram/status": strip_data(telegram_api),
    }

    indices = diagnose_indices(indices_api, issues)
    telegram = diagnose_telegram(telegram_api, env_values, issues)
    themes = diagnose_themes(themes_api, issues)
    logs = diagnose_logs(issues)
    status = overall_status(issues)

    health = {
        "timestamp": now_iso(),
        "overall_status": status,
        "server": server,
        "indices": indices,
        "telegram": telegram,
        "themes": themes,
        "logs": logs,
        "auto_repair": auto_repair,
        "incident_path": str(INCIDENT_PATH),
        "codex_prompt_path": str(CODEX_PROMPT_PATH),
    }
    write_json(HEALTH_PATH, health)
    write_incident(health, issues)
    write_codex_prompt(health, issues)
    if not args.no_telegram:
        telegram_report = send_telegram_summary(health, issues)
        health["telegram_report"] = telegram_report
        write_json(HEALTH_PATH, health)
    print(json.dumps({
        "overall_status": health["overall_status"],
        "issues": [i["code"] for i in issues],
        "auto_repair": health["auto_repair"],
        "incident_path": str(INCIDENT_PATH),
        "codex_prompt_path": str(CODEX_PROMPT_PATH),
    }, ensure_ascii=False, indent=2))
    return health


def strip_data(api: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": api.get("ok"),
        "status_code": api.get("status_code"),
        "duration_sec": api.get("duration_sec"),
        "error": api.get("error", "")[:300] if api.get("error") else "",
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CHECKS self-healing guard v1")
    parser.add_argument("--watch", type=int, default=0, help="Run repeatedly every N seconds")
    parser.add_argument("--no-telegram", action="store_true", help="Skip guard summary Telegram report")
    parser.add_argument("--repair", action="store_true", help="Allow safe server restart repair")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.watch and args.watch < 5:
        args.watch = 5
    while True:
        run_once(args)
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
