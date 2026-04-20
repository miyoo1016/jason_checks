"""Shared application state - single event loop, no locks needed."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict
from collections import deque


@dataclass
class StockState:
    """Real-time state of a single stock."""

    code: str
    price: float = 0.0
    change_pct: float = 0.0
    cum_volume_krw: int = 0
    cumulative_trading_value: int = 0
    execution_strength: float = 0.0
    bid_ask_ratio: float = 0.5
    last_tick_ts: datetime = field(default_factory=datetime.now)
    ask_vol_total: int = 0
    bid_vol_total: int = 0
    # Volume surge detection (5-min rolling window)
    volume_history: deque = field(default_factory=lambda: deque(maxlen=5))
    surge_active: bool = False
    last_surge_ts: datetime = field(default_factory=datetime.now)


@dataclass
class AppState:
    """Global app state (single event loop, no locks)."""

    stocks: Dict[str, StockState] = field(default_factory=dict)
    mode: str = "live"  # "live", "preview", "replay"
    ws_connected: bool = False
    rest_ok: bool = False
    current_rest_rps: float = 0.0

    def get_or_create_stock(self, code: str) -> StockState:
        """Get or create stock state."""
        if code not in self.stocks:
            self.stocks[code] = StockState(code=code)
        return self.stocks[code]

    def update_stock(self, code: str, **kwargs) -> None:
        """Update stock state."""
        stock = self.get_or_create_stock(code)
        for key, value in kwargs.items():
            if hasattr(stock, key):
                setattr(stock, key, value)
        stock.last_tick_ts = datetime.now()


# Global singleton (used throughout the app)
app_state = AppState()
