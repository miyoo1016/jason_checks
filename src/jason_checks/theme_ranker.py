"""Dynamic theme ranker — rank themes by aggregate strength in real time."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from jason_checks.state import app_state


@dataclass
class ThemeScoreHistory:
    """Rolling score history for a theme (60-second window)."""
    scores: deque = field(default_factory=lambda: deque(maxlen=60))
    last_update: datetime = field(default_factory=datetime.now)


_theme_score_history: Dict[str, ThemeScoreHistory] = {}


def compute_theme_raw_score(theme_code: str, theme_data: Dict) -> float:
    """Compute raw theme score from current stock states."""
    theme_config = theme_data.get(theme_code, {})
    stocks_in_theme = theme_config.get("stocks", [])

    if not stocks_in_theme:
        return 0.0

    scores = []
    down_count = 0
    for stock_info in stocks_in_theme:
        code = stock_info["code"]
        stock = app_state.stocks.get(code)
        if not stock or stock.price == 0:
            continue

        # 등락률이 주 지표 (가중치 대폭 상향)
        change_score = stock.change_pct * 20
        strength_score = (stock.execution_strength or 0) * 0.3
        value_score = (stock.cumulative_trading_value / 1_000_000_000) * 0.05

        # 하락 종목은 추가 페널티
        if stock.change_pct < 0:
            down_count += 1
            change_score *= 1.5

        scores.append(change_score + strength_score + value_score)

    if not scores:
        return -999.0  # Massive penalty for themes with no data

    avg_score = sum(scores) / len(scores)

    # 커버리지(데이터 있는 종목 비율)에 따른 페널티
    # coverage 1.0 → *1.0, 0.2 → *0.45, 0.5 → *0.71
    # inactive 테마(커버리지 낮음)는 점수가 감소하므로 상승 종목이 하락 active 테마보다 우위
    coverage = len(scores) / len(stocks_in_theme)
    avg_score = avg_score * (coverage ** 0.5)

    # 절반 이상 하락 또는 활성 데이터가 부족하면 추가 페널티 (강화: -10 → -15)
    if down_count >= len(stocks_in_theme) / 2 or len(scores) < (len(stocks_in_theme) / 2):
        avg_score -= 15.0

    return avg_score


def update_theme_scores(theme_data: Dict) -> None:
    """Compute raw scores for all themes and append to rolling history."""
    now = datetime.now()
    for theme_code in theme_data.keys():
        raw = compute_theme_raw_score(theme_code, theme_data)
        if theme_code not in _theme_score_history:
            _theme_score_history[theme_code] = ThemeScoreHistory()
        history = _theme_score_history[theme_code]
        history.scores.append(raw)
        history.last_update = now


def get_smoothed_score(theme_code: str) -> float:
    """Get 60-second smoothed score for a theme."""
    history = _theme_score_history.get(theme_code)
    if not history or not history.scores:
        return 0.0
    return sum(history.scores) / len(history.scores)


def rank_themes(
    theme_data: Dict,
    top_n: int = 4,
    pinned: Optional[List[str]] = None,
) -> List[str]:
    """Return ordered list of theme_codes: pinned first, then top_n by smoothed score."""
    update_theme_scores(theme_data)
    pinned = pinned or []

    scored = [
        (code, get_smoothed_score(code))
        for code in theme_data.keys()
        if code not in pinned
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    top = [code for code, _ in scored[:top_n]]
    return pinned + top


def get_active_stock_codes(
    theme_data: Dict,
    active_themes: List[str],
) -> List[str]:
    """Return unique stock codes needed for subscription."""
    codes = set()
    for theme_code in active_themes:
        for stock in theme_data.get(theme_code, {}).get("stocks", []):
            codes.add(stock["code"])
    return sorted(codes)


def get_expanded_subscription_codes(
    theme_data: dict,
    active_themes: list,
    max_codes: int = 40,
) -> list:
    """Return subscription codes: all active theme stocks + 1 representative from each inactive theme.

    This expands the WebSocket subscription beyond just active themes so we can
    detect individual surge stocks across the full theme universe.
    """
    codes = []
    seen = set()

    # Priority 1: all stocks from active themes
    for theme in active_themes:
        for stock in theme_data.get(theme, {}).get("stocks", []):
            c = stock["code"]
            if c not in seen:
                codes.append(c)
                seen.add(c)

    # Priority 2: first stock from each inactive theme (until max_codes)
    for theme, config in theme_data.items():
        if theme in active_themes:
            continue
        stocks = config.get("stocks", [])
        if stocks:
            c = stocks[0]["code"]
            if c not in seen:
                codes.append(c)
                seen.add(c)
                if len(codes) >= max_codes:
                    break

    return codes[:max_codes]


def build_code_to_theme_map(theme_data: dict) -> dict:
    """Build mapping from stock code → (theme_code, stock_name, display_name).

    Used by /api/surges to annotate surge stocks with their theme.
    """
    mapping = {}
    for theme_code, config in theme_data.items():
        display_name = config.get("display_name", theme_code)
        for stock in config.get("stocks", []):
            mapping[stock["code"]] = {
                "theme_code": theme_code,
                "theme_display": display_name,
                "name": stock["name"],
            }
    return mapping
