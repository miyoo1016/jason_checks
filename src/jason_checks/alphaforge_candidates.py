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
LOAD_TIERS = {"TIER_2", "TIER_3"}
EXCLUDED_LOAD_STATUSES = {"REJECTED", "EXCLUDED", "DROP", "DROPPED"}
EXPORT_PATH = Path("data") / "exports" / "alphaforge_candidates.json"
DUAL_HORIZON_PATH = Path("data") / "exports" / "alphaforge_dual_horizon.json"
EXPORT_ROW_KEYS = ("rows", "final_rows", "normalized_rows", "candidates", "data", "results")
logger = structlog.get_logger()


def get_candidates_path(root: Path | None = None) -> Path:
    """Return the default AlphaForge candidates path."""
    project_root = root or Path(__file__).parent.parent.parent
    return project_root / "data" / "alphaforge_candidates.json"


def get_candidate_path_priority(root: Path | None = None) -> list[Path]:
    """Return candidate paths in load priority order."""
    project_root = root or Path(__file__).parent.parent.parent
    paths: list[Path] = []
    env_path = os.getenv("ALPHAFORGE_CANDIDATES_PATH")
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.extend(
        [
            project_root.parent / "jason_octopus" / "data" / "exports" / "alphaforge_candidates.json",
            project_root / "data" / "alphaforge_candidates.json",
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


def _is_export_candidate(item: dict[str, Any]) -> bool:
    return (
        item.get("watch_alert_type") == TARGET_ALERT_TYPE
        or item.get("alert_type") == TARGET_ALERT_TYPE
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
        return "not_action_alert_or_tier3"
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
    """Export ACTION_ALERT or TIER_3 AlphaForge candidates as JSON."""
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


def _extract_candidates(raw: Any) -> tuple[list[Any], str]:
    if isinstance(raw, dict):
        generated_at = str(raw.get("generated_at") or "")
        raw_candidates = raw.get("candidates", [])
    elif isinstance(raw, list):
        generated_at = ""
        raw_candidates = raw
    else:
        return [], ""

    if not isinstance(raw_candidates, list):
        return [], generated_at

    if not generated_at:
        for item in raw_candidates:
            if isinstance(item, dict) and item.get("generated_at"):
                generated_at = str(item["generated_at"])
                break
    return raw_candidates, generated_at


def load_alphaforge_candidates_with_meta(
    path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load candidates plus source metadata without raising on bad files."""
    paths = [path] if path else get_candidate_path_priority()
    for candidate_path in paths:
        if not candidate_path or not candidate_path.exists():
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
            raw_candidates, generated_at = _extract_candidates(raw)
            loaded, skipped_reason_counts = _normalize_loaded_candidates(raw_candidates)
            first_symbol = ""
            if raw_candidates and isinstance(raw_candidates[0], dict):
                first_symbol = str(_pick(raw_candidates[0], "symbol", "code", "ticker"))
            logger.info(
                "alphaforge_candidates_load",
                path=str(candidate_path),
                file_exists=True,
                raw_count=len(raw_candidates),
                filtered_count=len(loaded),
                first_symbol=first_symbol,
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
            }
        except Exception as e:
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
