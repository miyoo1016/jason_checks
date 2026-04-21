"""Shared application state - single event loop, no locks needed."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Set
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
    market: str = "J"  # J=KRX, NX=Nextrade (updated from tick source)
    ask_vol_total: int = 0
    bid_vol_total: int = 0
    # Investor trends (net buy in 원, daily cumulative) — updated by REST polling
    investor_foreigner: int = 0
    investor_institution: int = 0
    investor_individual: int = 0
    investor_updated_ts: datetime = field(default_factory=datetime.now)
    # Volume surge detection (5-min rolling window)
    volume_history: deque = field(default_factory=lambda: deque(maxlen=5))
    surge_active: bool = False
    last_surge_ts: datetime = field(default_factory=datetime.now)


@dataclass
class IndexState:
    """KOSPI/KOSDAQ index with price and investor trends."""

    code: str                       # "0001"=KOSPI, "1001"=KOSDAQ
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    change_value: float = 0.0
    investor_foreigner: int = 0
    investor_institution: int = 0
    investor_individual: int = 0
    updated_ts: datetime = field(default_factory=datetime.now)


@dataclass
class AppState:
    """Global app state (single event loop, no locks)."""

    stocks: Dict[str, StockState] = field(default_factory=dict)
    indices: Dict[str, "IndexState"] = field(default_factory=dict)
    mode: str = "live"  # "live", "preview", "replay"
    ws_connected: bool = False
    rest_ok: bool = False
    current_rest_rps: float = 0.0
    
    # Track which codes are currently on the user's screen (for prioritized polling)
    visible_codes: Set[str] = field(default_factory=set)

    def set_visible_codes(self, codes: list[str]) -> None:
        """Update the set of codes currently visible on the UI."""
        self.visible_codes = set(codes)

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

    def get_or_create_index(self, code: str, name: str = "") -> "IndexState":
        """Get or create index state."""
        if code not in self.indices:
            self.indices[code] = IndexState(code=code, name=name)
        return self.indices[code]

    def update_index(self, code: str, **kwargs) -> None:
        idx = self.get_or_create_index(code)
        for k, v in kwargs.items():
            if hasattr(idx, k):
                setattr(idx, k, v)
        idx.updated_ts = datetime.now()


# Global singleton (used throughout the app)
app_state = AppState()
