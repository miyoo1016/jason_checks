"""Shared application state - single event loop, no locks needed."""

from dataclasses import dataclass, field
from datetime import datetime
import os
import json
import time
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
    sparkline_points: Dict[int, float] = field(default_factory=dict)


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
    source: str = "live"            # "live" or "dummy" / "mock"
    updated_ts: datetime = field(default_factory=datetime.now)
    sparkline_points: Dict[int, float] = field(default_factory=dict)


@dataclass
class AppState:
    """Global app state (single event loop, no locks)."""

    stocks: Dict[str, StockState] = field(default_factory=dict)
    indices: Dict[str, "IndexState"] = field(default_factory=dict)
    mode: str = "live"  # "live", "preview", "replay"
    ws_connected: bool = False
    rest_ok: bool = False
    current_rest_rps: float = 0.0
    _sparkline_dirty: bool = False
    _sparkline_last_save: float = 0.0

    def load_sparkline_history(self):
        try:
            path = "data/runtime/intraday_sparkline.json"
            if not os.path.exists(path): return
            with open(path, "r") as f:
                data = json.load(f)
            now = datetime.now()
            if data.get("date") != now.strftime("%Y-%m-%d"): return
            today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
            cutoff_ms = int(today_9am.timestamp() * 1000)
            if now < today_9am: return

            for code, points in data.get("stocks", {}).items():
                stock = self.get_or_create_stock(code)
                for t, p in points:
                    if t >= cutoff_ms: stock.sparkline_points[t] = p

            for code, points in data.get("indices", {}).items():
                idx = self.get_or_create_index(code)
                for t, p in points:
                    if t >= cutoff_ms: idx.sparkline_points[t] = p
        except Exception as e:
            print(f"Failed to load sparkline: {e}")

    def save_sparkline_history(self, force: bool = False):
        if not self._sparkline_dirty and not force: return
        now_ts = time.time()
        if not force and now_ts - self._sparkline_last_save < 30: return

        try:
            path = "data/runtime/intraday_sparkline.json"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "stocks": {code: [[t, p] for t, p in sorted(st.sparkline_points.items())] for code, st in self.stocks.items() if st.sparkline_points},
                "indices": {code: [[t, p] for t, p in sorted(idx.sparkline_points.items())] for code, idx in self.indices.items() if idx.sparkline_points}
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
            self._sparkline_dirty = False
            self._sparkline_last_save = now_ts
        except Exception as e:
            print(f"Failed to save sparkline: {e}")

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

        if "price" in kwargs:
            p = float(kwargs["price"])
            if p > 0:
                now = datetime.now()
                today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
                if now >= today_9am:
                    # 9시 이전 데이터 삭제
                    keys_to_del = [k for k in stock.sparkline_points.keys() if k < int(today_9am.timestamp() * 1000)]
                    for k in keys_to_del:
                        del stock.sparkline_points[k]

                    ts = int(now.timestamp() * 1000)
                    bucket = (ts // 300000) * 300000
                    if stock.sparkline_points.get(bucket) != p:
                        stock.sparkline_points[bucket] = p
                        self._sparkline_dirty = True
                        self.save_sparkline_history()

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

        if "price" in kwargs:
            p = float(kwargs["price"])
            is_dummy = getattr(idx, "source", "") in ("dummy", "mock")
            if is_dummy or p <= 0:
                if idx.sparkline_points:
                    idx.sparkline_points.clear()
                    self._sparkline_dirty = True
                    self.save_sparkline_history()
            else:
                now = datetime.now()
                today_9am = now.replace(hour=9, minute=0, second=0, microsecond=0)
                if now >= today_9am:
                    keys_to_del = [k for k in idx.sparkline_points.keys() if k < int(today_9am.timestamp() * 1000)]
                    for k in keys_to_del:
                        del idx.sparkline_points[k]

                    ts = int(now.timestamp() * 1000)
                    bucket = (ts // 300000) * 300000
                    if idx.sparkline_points.get(bucket) != p:
                        idx.sparkline_points[bucket] = p
                        self._sparkline_dirty = True
                        self.save_sparkline_history()


# Global singleton (used throughout the app)
app_state = AppState()
