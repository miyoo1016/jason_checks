"""AlphaForge candidate loader for optional watchlist overrides."""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from collections import Counter
import structlog


TARGET_ALERT_TYPE = "ACTION_ALERT"
TARGET_TIER = "TIER_3"
ALPHAFORGE_TRACKING_LABELS = {
    "ACTION_ALERT",
    "PRIORITY_WATCH",
    "NEAR_BUY",
    "BUY_CANDIDATE",
}
ALPHAFORGE_LABEL_FIELDS = (
    "alert_type",
    "watch_alert_type",
    "legacy_label",
    "final_label",
    "display_label",
    "display_watch_alert_type",
)
LOAD_TIERS = {"TIER_2", "TIER_3"}
EXCLUDED_LOAD_STATUSES = {"REJECTED", "EXCLUDED", "DROP", "DROPPED"}
EXPORT_PATH = Path("data") / "exports" / "alphaforge_candidates.json"
DUAL_HORIZON_PATH = Path("data") / "exports" / "alphaforge_dual_horizon.json"
EXPORT_ROW_KEYS = ("rows", "final_rows", "normalized_rows", "candidates", "data", "results")
# active 파일이 이 시간(시간) 이상 오래됐으면 STALE 표시
_STALE_HOURS_THRESHOLD = 6
logger = structlog.get_logger()


def get_candidates_path(root: Path | None = None) -> Path:
    """Return the default AlphaForge candidates path."""
    project_root = root or Path(__file__).parent.parent.parent
    return project_root / "data" / "alphaforge_candidates.json"


def get_candidate_path_priority(root: Path | None = None) -> list[Path]:
    """Return candidate paths in load priority order.

    Priority:
      1. ALPHAFORGE_CANDIDATES_PATH env override
      2. JO active file  (alphaforge_candidates_active.json)  ← 최우선
      3. JO latest file  (alphaforge_candidates.json)
      4. JC local data fallback
    """
    project_root = root or Path(__file__).parent.parent.parent
    paths: list[Path] = []
    env_path = os.getenv("ALPHAFORGE_CANDIDATES_PATH")
    if env_path:
        paths.append(Path(env_path).expanduser())
    jo_exports = project_root.parent / "jason_octopus" / "data" / "exports"
    paths.extend(
        [
            jo_exports / "alphaforge_candidates_active.json",   # active 우선
            jo_exports / "alphaforge_candidates.json",          # latest fallback
            project_root / "data" / "alphaforge_candidates.json",  # JC local fallback
        ]
    )
    return paths


def get_dual_horizon_path(root: Path | None = None) -> Path:
    project_root = root or Path(__file__).parent.parent.parent
    return project_root.parent / "jason_octopus" / DUAL_HORIZON_PATH


def get_export_path(root: Path | None = None) -> Path:
    """Return the AlphaForge candidate export path."""
    project_root = root or Path(__file__).parent.parent.parent
    return project_root / EXPORT_PATH


def _pick(item: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return default


def get_alphaforge_labels(candidate: dict[str, Any]) -> set[Any]:
    return {candidate.get(key) for key in ALPHAFORGE_LABEL_FIELDS}


def is_alphaforge_tracking_candidate(candidate: dict[str, Any]) -> bool:
    labels = {str(label) for label in get_alphaforge_labels(candidate) if label}
    return bool(labels & ALPHAFORGE_TRACKING_LABELS)


def _is_export_candidate(item: dict[str, Any]) -> bool:
    return (
        is_alphaforge_tracking_candidate(item)
        or item.get("primary_bucket") == TARGET_TIER
        or item.get("final_class") == TARGET_TIER
        or item.get("tier") == TARGET_TIER
    )


def flatten_alphaforge_rows(results: Any) -> list[dict[str, Any]]:
    """Return final normalized row dicts from common result containers."""
    rows: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        nested_found = False
        for key in EXPORT_ROW_KEYS:
            nested = value.get(key)
            if isinstance(nested, list):
                nested_found = True
                visit(nested)
            elif isinstance(nested, dict):
                nested_found = True
                visit(nested)
        if not nested_found:
            rows.append(value)

    visit(results)
    return rows


def _skip_reason(item: Any) -> str:
    if not isinstance(item, dict):
        return "not_dict"
    if not str(_pick(item, "symbol", "code", "ticker")).strip():
        return "missing_symbol"
    if not _is_export_candidate(item):
        return "not_tracking_label_or_tier3"
    return ""


def _is_explicitly_excluded(item: dict[str, Any]) -> bool:
    """Return True only for explicit reject/exclude markers in candidate files."""
    for key in ("status", "decision", "final_class", "primary_bucket", "tier", "alert_type"):
        value = str(item.get(key) or "").strip().upper()
        if value in EXCLUDED_LOAD_STATUSES:
            return True
    if item.get("excluded") is True or item.get("is_excluded") is True:
        return True
    return False


def export_alphaforge_candidates(
    results: Any,
    path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Export tracked AlphaForge labels or TIER_3 candidates as JSON."""
    export_path = path or get_export_path()
    export_path.parent.mkdir(parents=True, exist_ok=True)
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    rows = flatten_alphaforge_rows(results)

    candidates = []
    skipped_reason_counts: Counter[str] = Counter()
    for item in rows:
        reason = _skip_reason(item)
        if reason:
            skipped_reason_counts[reason] += 1
            continue
        symbol = str(_pick(item, "symbol", "code", "ticker")).strip()

        candidates.append(
            {
                "symbol": symbol,
                "name": _pick(item, "name", "stock_name", default=symbol),
                "tier": _pick(item, "tier", "primary_bucket", "final_class"),
                "alert_type": _pick(item, "alert_type", "watch_alert_type"),
                "watch_alert_type": _pick(item, "watch_alert_type", "alert_type"),
                "legacy_label": _pick(item, "legacy_label", "alert_type", "watch_alert_type"),
                "final_label": _pick(item, "final_label", "display_label", "display_watch_alert_type"),
                "display_label": _pick(item, "display_label", "final_label", "display_watch_alert_type"),
                "display_watch_alert_type": _pick(item, "display_watch_alert_type", "display_label", "final_label"),
                "rs": _pick(item, "rs", "rs_percentile"),
                "vcp_status": _pick(item, "vcp_status"),
                "box_upper_price": _pick(item, "box_upper_price", "box_high", "pivot_price", default=None),
                "total_score": _pick(item, "total_score"),
                "generated_at": generated,
            }
        )

    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

    stats = {
        "total_rows": len(rows),
        "exported_count": len(candidates),
        "skipped_count": len(rows) - len(candidates),
        "skipped_reason_counts": dict(skipped_reason_counts),
        "path": str(export_path),
    }
    logger.info("alphaforge_candidates_export", **stats)
    return stats


def _extract_candidates(raw: Any) -> tuple[list[Any], str, dict[str, Any]]:
    """Return (raw_candidates, generated_at, file_metadata).

    Supports both the new {metadata:{...}, candidates:[...]} format
    and the legacy flat list / dict format.
    """
    file_meta: dict[str, Any] = {}
    if isinstance(raw, dict):
        # New format: {metadata: {...}, candidates: [...]}
        if "metadata" in raw and isinstance(raw["metadata"], dict):
            file_meta = raw["metadata"]
            generated_at = str(file_meta.get("generated_at") or "")
            raw_candidates = raw.get("candidates", [])
        else:
            # Legacy dict format
            generated_at = str(raw.get("generated_at") or "")
            raw_candidates = raw.get("candidates", [])
    elif isinstance(raw, list):
        generated_at = ""
        raw_candidates = raw
    else:
        return [], "", {}

    if not isinstance(raw_candidates, list):
        return [], generated_at, file_meta

    if not generated_at:
        for item in raw_candidates:
            if isinstance(item, dict) and item.get("generated_at"):
                generated_at = str(item["generated_at"])
                break
    return raw_candidates, generated_at, file_meta


def _is_stale(
    generated_at: str,
    threshold_hours: float = _STALE_HOURS_THRESHOLD,
    published_at: str = "",
) -> tuple[bool, float]:
    """Return (is_stale, age_hours). age_hours=0 if timestamp is unparseable.

    timestamp 해석 규칙:
      - timezone 정보가 있으면 그대로 사용
      - timezone 없는 naive string은 시스템 로컬 시간(KST)으로 해석
        → datetime.now() (naive, local) 과 비교
      - 계산 결과가 음수이면 0으로 clamp
      - published_at이 있으면 generated_at보다 우선 사용
    """
    # published_at 우선, 없으면 generated_at
    ts = (published_at or generated_at or "").strip()
    if not ts:
        return False, 0.0
    try:
        dt_str = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            # naive → 시스템 로컬 시간으로 해석 (naive now와 비교)
            age_hours = (datetime.now() - dt).total_seconds() / 3600
        else:
            # aware → UTC now와 비교
            age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        age_hours = max(0.0, age_hours)  # 음수 clamp
        return age_hours >= threshold_hours, round(age_hours, 2)
    except Exception:
        return False, 0.0



def _source_tag_for_path(candidate_path: Path, root: Path | None = None) -> str:
    """Return 'active' / 'latest' / 'local' / 'env' based on where the path sits."""
    project_root = root or Path(__file__).parent.parent.parent
    name = candidate_path.name
    if "_active" in name:
        return "active"
    jo_exports = project_root.parent / "jason_octopus" / "data" / "exports"
    if candidate_path.parent == jo_exports:
        return "latest"
    if candidate_path.parent == project_root / "data":
        return "local"
    return "env"


def load_alphaforge_candidates_with_meta(
    path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load candidates plus source metadata without raising on bad files.

    Returned metadata keys:
      path, generated_at, count, raw_count, filtered_count, first_symbol,
      skipped_reason_counts, source (active/latest/local/env),
      mode, max_symbols, source_count, candidate_count,
      published_at, intended_for_session, is_stale, stale_age_hours,
      fallback_warning
    """
    paths = [path] if path else get_candidate_path_priority()
    active_path = (path is None) and None  # track whether active was tried
    active_missing = False

    for idx, candidate_path in enumerate(paths):
        # Track if this is the active slot (index 0 after env override)
        is_active_slot = "_active" in (candidate_path.name if candidate_path else "")

        if not candidate_path or not candidate_path.exists():
            if is_active_slot:
                active_missing = True
            logger.info(
                "alphaforge_candidates_load",
                path=str(candidate_path) if candidate_path else "",
                file_exists=False,
                raw_count=0,
                filtered_count=0,
                first_symbol="",
                skipped_reason_counts={"missing_file": 1},
            )
            continue
        try:
            with open(candidate_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            raw_candidates, generated_at, file_meta = _extract_candidates(raw)
            loaded, skipped_reason_counts = _normalize_loaded_candidates(raw_candidates)
            first_symbol = ""
            if raw_candidates and isinstance(raw_candidates[0], dict):
                first_symbol = str(_pick(raw_candidates[0], "symbol", "code", "ticker"))

            source_tag = _source_tag_for_path(candidate_path)
            published_at_ts = str(file_meta.get("published_at") or "")
            is_stale, stale_age_hours = _is_stale(generated_at, published_at=published_at_ts)

            # fallback_warning: active 슬롯이 없거나 비어있을 때 latest/local을 쓰면 경고
            fallback_warning = ""
            if source_tag in ("latest", "local", "env") and (
                active_missing or not any("_active" in str(p) for p in paths)
            ):
                fallback_warning = "ACTIVE 없음, latest fallback 사용"

            logger.info(
                "alphaforge_candidates_load",
                path=str(candidate_path),
                file_exists=True,
                raw_count=len(raw_candidates),
                filtered_count=len(loaded),
                first_symbol=first_symbol,
                source=source_tag,
                mode=file_meta.get("mode", ""),
                is_stale=is_stale,
                stale_age_hours=stale_age_hours,
                fallback_warning=fallback_warning,
                skipped_reason_counts=dict(skipped_reason_counts),
            )
            return loaded, {
                "path": str(candidate_path),
                "generated_at": generated_at,
                "count": len(loaded),
                "raw_count": len(raw_candidates),
                "filtered_count": len(loaded),
                "first_symbol": first_symbol,
                "skipped_reason_counts": dict(skipped_reason_counts),
                # ── 새 metadata 필드 ─────────────────────────────────
                "source": source_tag,
                "mode": str(file_meta.get("mode") or ""),
                "max_symbols": file_meta.get("max_symbols", ""),
                "source_count": file_meta.get("source_count", ""),
                "candidate_count": file_meta.get("candidate_count", ""),
                "published_at": str(file_meta.get("published_at") or ""),
                "intended_for_session": str(file_meta.get("intended_for_session") or ""),
                "is_stale": is_stale,
                "stale_age_hours": stale_age_hours,
                "fallback_warning": fallback_warning,
            }
        except Exception as e:
            if is_active_slot:
                active_missing = True
            logger.warning(
                "alphaforge_candidates_load",
                path=str(candidate_path),
                file_exists=True,
                raw_count=0,
                filtered_count=0,
                first_symbol="",
                skipped_reason_counts={"load_error": 1},
                error=str(e),
            )
            continue

    return [], {
        "path": "",
        "generated_at": "",
        "count": 0,
        "raw_count": 0,
        "filtered_count": 0,
        "first_symbol": "",
        "skipped_reason_counts": {"no_usable_file": 1},
        "source": "",
        "mode": "",
        "max_symbols": "",
        "source_count": "",
        "candidate_count": "",
        "published_at": "",
        "intended_for_session": "",
        "is_stale": False,
        "stale_age_hours": -1.0,
        "fallback_warning": "후보 파일 없음",
    }


def load_dual_horizon_by_symbol(path: Path | None = None) -> dict[str, dict[str, Any]]:
    dual_path = path or get_dual_horizon_path()
    if not dual_path.exists():
        logger.info("alphaforge_dual_horizon_load", path=str(dual_path), file_exists=False, raw_count=0, matched_count=0)
        return {}
    try:
        with open(dual_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning("alphaforge_dual_horizon_load", path=str(dual_path), file_exists=True, raw_count=0, matched_count=0, error=str(e))
        return {}

    rows = raw if isinstance(raw, list) else raw.get("rows", []) if isinstance(raw, dict) else []
    dual_by_symbol: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or item.get("code") or "").strip()
        if not symbol:
            continue
        dual_by_symbol[symbol] = {
            "short_swing_score": item.get("short_swing_score", "-"),
            "position_swing_score": item.get("position_swing_score", "-"),
            "horizon_label": item.get("horizon_label", "-"),
            "short_reasons": item.get("short_reasons", "-"),
            "position_reasons": item.get("position_reasons", "-"),
        }
    logger.info(
        "alphaforge_dual_horizon_load",
        path=str(dual_path),
        file_exists=True,
        raw_count=len(rows),
        matched_count=len(dual_by_symbol),
    )
    return dual_by_symbol


def _normalize_loaded_candidates(raw_candidates: list[Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    loaded: list[dict[str, Any]] = []
    skipped_reason_counts: Counter[str] = Counter()
    dual_by_symbol = load_dual_horizon_by_symbol()
    for item in raw_candidates:
        if not isinstance(item, dict):
            skipped_reason_counts["not_dict"] += 1
            continue
        if _is_explicitly_excluded(item):
            skipped_reason_counts["explicitly_excluded"] += 1
            continue

        symbol = str(_pick(item, "symbol", "code", "ticker")).strip()
        if not symbol:
            skipped_reason_counts["missing_symbol"] += 1
            continue

        dual = dual_by_symbol.get(symbol, {})
        loaded.append({
            "symbol": symbol,
            "code": symbol,
            "name": str(_pick(item, "name", "stock_name", default=symbol)),
            "tier": _pick(item, "tier", "primary_bucket", "final_class"),
            "alert_type": _pick(item, "alert_type", "watch_alert_type"),
            "watch_alert_type": _pick(item, "watch_alert_type", "alert_type"),
            "legacy_label": _pick(item, "legacy_label", "alert_type", "watch_alert_type"),
            "final_label": _pick(item, "final_label", "display_label", "display_watch_alert_type"),
            "display_label": _pick(item, "display_label", "final_label", "display_watch_alert_type"),
            "display_watch_alert_type": _pick(item, "display_watch_alert_type", "display_label", "final_label"),
            "rs": item.get("rs", ""),
            "vcp_status": item.get("vcp_status", ""),
            "box_upper_price": item.get("box_upper_price", ""),
            "total_score": item.get("total_score", ""),
            "generated_at": item.get("generated_at", ""),
            "short_swing_score": dual.get("short_swing_score", "-"),
            "position_swing_score": dual.get("position_swing_score", "-"),
            "horizon_label": dual.get("horizon_label", "-"),
            "short_reasons": dual.get("short_reasons", "-"),
            "position_reasons": dual.get("position_reasons", "-"),
        })
        if len(loaded) >= 5:
            break
    return loaded, skipped_reason_counts


def load_alphaforge_candidates(path: Path | None = None) -> list[dict[str, Any]]:
    """Load prioritized AlphaForge candidates from JSON if the file exists.

    Missing files intentionally return an empty list so callers can keep their
    existing default watchlist behavior.
    """
    loaded, _ = load_alphaforge_candidates_with_meta(path)
    return loaded


def build_alphaforge_theme(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert candidates to the existing theme_data shape."""
    return {
        "AlphaForge": {
            "display_name": "AlphaForge",
            "description": "AlphaForge candidates",
            "stocks": candidates,
        }
    }
