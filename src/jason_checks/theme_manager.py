"""Theme management - load themes, rank stocks, calculate strength."""

import yaml
from pathlib import Path
from typing import Dict, List, Optional
from jason_checks.kis_ws import ExecutionTick


def _normalize_symbol(code: str) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        value = value[1:]
    if value.isdigit() and len(value) < 6:
        value = value.zfill(6)
    return value


def load_themes() -> Dict:
    """Load theme configuration from themes.yaml at project root."""
    yaml_path = Path(__file__).parent.parent.parent / "themes.yaml"

    if not yaml_path.exists():
        raise FileNotFoundError(f"themes.yaml not found at {yaml_path}")

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data.get("themes", {})


def compute_leader_score(
    tick: Optional[ExecutionTick],
    sort_mode: str = "strength"
) -> float:
    """Compute a leader score for ranking within a theme.

    Higher score = more likely to be in top 4.

    Modes:
    - "strength": execution strength (체결강도) primary
    - "change_pct": absolute change percentage primary
    - "trading_value": trading value primary
    """
    if not tick:
        return 0.0

    # Handle both ExecutionTick objects and dicts
    strength = tick.strength if hasattr(tick, 'strength') else tick.get('strength', 0)
    change_pct = tick.change_pct if hasattr(tick, 'change_pct') else tick.get('change_pct', 0)
    cum_trading_value = (tick.cumulative_trading_value if hasattr(tick, 'cumulative_trading_value')
                         else tick.get('cumulative_trading_value', 0))

    if sort_mode == "strength":
        # Primary: execution strength
        strength_score = strength * 100
        change_score = abs(change_pct) * 10
        return strength_score + change_score

    elif sort_mode == "change_pct":
        # Primary: absolute change percentage
        change_score = abs(change_pct) * 100
        strength_score = strength * 10
        return change_score + strength_score

    elif sort_mode == "trading_value":
        # Primary: trading value (누적 거래대금)
        value_score = (cum_trading_value / 1_000_000_000) * 100
        change_score = abs(change_pct) * 10
        return value_score + change_score

    else:
        # Default to strength
        return compute_leader_score(tick, "strength")


def select_leaders(
    theme_code: str,
    stock_ticks: Dict[str, ExecutionTick],
    theme_data: Dict,
    sort_mode: str = "strength"
) -> List[Dict]:
    """Select top 4 stocks from a theme pool based on leader score.

    Returns list of dicts with stock code, name, tick data, and score.

    Args:
        theme_code: Theme identifier
        stock_ticks: Current ticks dict
        theme_data: Theme configuration
        sort_mode: "strength" | "change_pct" | "trading_value"
    """
    theme_config = theme_data.get(theme_code, {})
    stocks = theme_config.get("stocks", [])

    # Score each stock in the pool
    scored = []
    for stock_info in stocks:
        raw_code = stock_info["code"]
        code = _normalize_symbol(raw_code)
        tick = stock_ticks.get(code)
        score = compute_leader_score(tick, sort_mode=sort_mode)

        scored.append({
            "code": code,
            "name": stock_info["name"],
            "score": score,
            "tick": tick,
            "core": bool(stock_info.get("core", False)),
            "tier": stock_info.get("tier", ""),
            "alert_type": stock_info.get("alert_type", ""),
            "rs": stock_info.get("rs", ""),
            "vcp_status": stock_info.get("vcp_status", ""),
            "box_upper_price": stock_info.get("box_upper_price", ""),
            "short_swing_score": stock_info.get("short_swing_score", "-"),
            "position_swing_score": stock_info.get("position_swing_score", "-"),
            "horizon_label": stock_info.get("horizon_label", "-"),
            "short_reasons": stock_info.get("short_reasons", "-"),
            "position_reasons": stock_info.get("position_reasons", "-"),
        })

    # Sort by score descending, take top 4. AlphaForge candidates are already
    # pre-filtered upstream, so keep the whole candidate set visible.
    scored.sort(key=lambda x: x["score"], reverse=True)
    if theme_code == "AlphaForge":
        return scored

    core_leaders = [stock for stock in scored if stock.get("core")]
    selected = core_leaders[:4]
    selected_codes = {stock["code"] for stock in selected}
    for stock in scored:
        if len(selected) >= 4:
            break
        if stock["code"] not in selected_codes:
            selected.append(stock)
            selected_codes.add(stock["code"])
    return selected


def compute_theme_strength(leaders: List[Dict], sort_mode: str = "strength") -> float:
    """Compute overall theme strength from top 4 stocks.

    Returns average leader score (0-100+).
    """
    if not leaders:
        return 0.0

    total_score = sum(leader["score"] for leader in leaders)
    avg = total_score / len(leaders)
    return avg


def compute_theme_avg_change_pct(leaders: List[Dict]) -> float:
    """Compute simple arithmetic mean of change_pct across leaders.

    Used for display of theme aggregate change % (like "6.36" in reference UI).
    Handles both flattened leader dicts (change_pct at top level) and
    select_leaders() output where tick data is nested under "tick".
    """
    if not leaders:
        return 0.0

    change_pcts = []
    for leader in leaders:
        cp = leader.get("change_pct")
        if cp is None:
            tick = leader.get("tick")
            if tick is not None:
                cp = tick.change_pct if hasattr(tick, "change_pct") else (
                    tick.get("change_pct") if isinstance(tick, dict) else None
                )
        if cp is None:
            continue
        change_pcts.append(cp)

    if not change_pcts:
        return 0.0
    return sum(change_pcts) / len(change_pcts)


def get_all_stock_codes(theme_data: Dict) -> List[str]:
    """Get all unique stock codes from all themes."""
    codes = set()
    for theme_config in theme_data.values():
        for stock_info in theme_config.get("stocks", []):
            codes.add(stock_info["code"])
    return sorted(list(codes))
