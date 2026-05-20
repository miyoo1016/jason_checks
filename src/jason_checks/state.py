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
    cumulative_volume: int = 0
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
    # Candidate-only KIS REST supply snapshot. None means not received.
    foreign_flow: int | None = None
    institution_flow: int | None = None
    individual_flow: int | None = None
    supply_status: str = "DATA_NA"
    supply_updated_at: str = ""
    supply_source: str = ""
    supply_recency: str = "UNKNOWN"
    supply_date: str = ""
    supply_error: str = ""
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

    def get_or_create_stock(self, code: str) -> StockState:
        """Get or create stock state."""
        if not code: return None
        code = str(code).strip().upper()
        if code.startswith("A") and code[1:].isdigit(): code = code[1:]
        if code.isdigit() and len(code) < 6: code = code.zfill(6)
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
