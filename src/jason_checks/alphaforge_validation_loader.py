"""Common AlphaForge validation loader.

Read-only loader that finds and parses the JO AlphaForge validation report.
Used by both app.py (web API) and jc_practicality_scorecard (CLI).
No circular imports: this module only uses stdlib.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ── Candidate paths ordered by preference ─────────────────────────────────────
# Each entry: (path, source_type_label)
_ROOT = Path(__file__).resolve().parents[2]

AV_REPORT_CANDIDATES: list[tuple[Path, str]] = [
    (
        Path("/Users/miyoo1016/jason_octopus/reports/alphaforge_performance_scorecard.json"),
        "JO_SCORECARD_JSON",
    ),
    (
        Path("/Users/miyoo1016/jason_octopus/reports/alphaforge_validation/summary.json"),
        "JO_SUMMARY_JSON",
    ),
    (
        Path("/Users/miyoo1016/jason_octopus/reports/alphaforge_performance_scorecard.md"),
        "JO_SCORECARD_MD",
    ),
    (_ROOT / "reports" / "alphaforge_performance_scorecard.json", "LOCAL_SCORECARD_JSON"),
    (_ROOT / "data" / "exports" / "alphaforge_validation.json", "LOCAL_EXPORTS_JSON"),
    (_ROOT / "data" / "reports" / "alphaforge_performance_scorecard.json", "LOCAL_DATA_REPORTS_JSON"),
]


# ── Module-level cache (shared within a process) ────────────────────────────
_cache: dict = {}
_last_mtime: float = -1.0
_last_path: str = ""


def load_alphaforge_validation(
    candidates: list[tuple[Path, str]] | None = None,
) -> dict[str, Any]:
    """Find and parse the first available JO AlphaForge validation report.

    Returns a dict with:
        status          : FOUND | DATA_NA | READ_ERROR | PATH_MISMATCH
        source_type     : JO_SCORECARD_JSON | JO_SUMMARY_JSON | ... | DATA_NA
        path_found      : str path of the file that was loaded, or ""
        paths_checked   : list[str] of all candidate paths checked
        available       : bool (True only when status == FOUND)
        reason          : human-readable reason string
        generated_at    : str | None
        overall_practicality_score : float | None
        confidence      : str | None
        cap_reasons     : list[str]
        (+ any other top-level keys from the JSON)
    """
    global _cache, _last_mtime, _last_path

    cands = candidates if candidates is not None else AV_REPORT_CANDIDATES
    paths_checked = [str(p) for p, _ in cands]

    # Find first existing file
    found_path: Path | None = None
    found_type: str = "DATA_NA"
    for p, src_type in cands:
        if p.exists():
            found_path = p
            found_type = src_type
            break

    if found_path is None:
        return {
            "status": "DATA_NA",
            "source_type": "DATA_NA",
            "path_found": "",
            "paths_checked": paths_checked,
            "available": False,
            "reason": "AlphaForge 검증 리포트 없음 — 탐색한 경로 중 존재하는 파일 없음",
            "generated_at": None,
            "overall_practicality_score": None,
            "confidence": None,
            "cap_reasons": [],
        }

    # mtime-based cache
    try:
        mtime = found_path.stat().st_mtime
    except OSError:
        mtime = -1.0

    if (
        _cache
        and _last_path == str(found_path)
        and _last_mtime == mtime
    ):
        return _cache

    # Parse
    try:
        raw = json.loads(found_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "READ_ERROR",
            "source_type": found_type,
            "path_found": str(found_path),
            "paths_checked": paths_checked,
            "available": False,
            "reason": f"파일 읽기/파싱 오류: {exc}",
            "generated_at": None,
            "overall_practicality_score": None,
            "confidence": None,
            "cap_reasons": [],
        }

    # Extract well-known fields defensively
    scores_block = raw.get("scores") or {}
    overall_score = (
        scores_block.get("overall_practicality_score")
        or raw.get("overall_practicality_score")
        or raw.get("overall_jc_practicality_score")
    )
    confidence = (
        scores_block.get("confidence")
        or raw.get("confidence")
        or "DATA_NA"
    )
    cap_reasons_raw = (
        scores_block.get("cap_reasons")
        or raw.get("cap_reasons")
        or []
    )

    result: dict[str, Any] = {
        **raw,  # include all original keys
        "status": "FOUND",
        "source_type": found_type,
        "path_found": str(found_path),
        "paths_checked": paths_checked,
        "available": True,
        "reason": f"AlphaForge 검증 리포트 로드 성공: {found_type} ({found_path.name})",
        "generated_at": raw.get("generated_at"),
        "overall_practicality_score": float(overall_score) if overall_score is not None else None,
        "confidence": str(confidence) if confidence else "DATA_NA",
        "cap_reasons": cap_reasons_raw if isinstance(cap_reasons_raw, list) else [],
    }

    _cache = result
    _last_mtime = mtime
    _last_path = str(found_path)
    return result


def find_alphaforge_scorecard_paths(
    candidates: list[tuple[Path, str]] | None = None,
) -> dict[str, Any]:
    """Return metadata about which candidate paths exist (no parsing)."""
    cands = candidates if candidates is not None else AV_REPORT_CANDIDATES
    existing = [(str(p), src) for p, src in cands if p.exists()]
    all_paths = [str(p) for p, _ in cands]
    return {
        "paths_checked": all_paths,
        "paths_found": existing,
        "any_found": bool(existing),
    }
