"""Volume surge detection - identifies rapid volume increases."""

from datetime import datetime, timedelta
import structlog
from jason_checks.kis_ws import ExecutionTick
from jason_checks.state import StockState

logger = structlog.get_logger()

# Configuration
SURGE_VOLUME_THRESHOLD = 50_000_000  # 5천만원
SURGE_MULTIPLIER = 3.0  # 3배
SURGE_COOLDOWN_SECONDS = 30  # 30초 쿨다운


def update_volume_window(stock_state: StockState, tick: ExecutionTick) -> None:
    """Update 5-minute volume history with current tick volume.

    Called once per tick (but we aggregate to 1-min buckets).
    For now, we accumulate volume per tick and store periodically.
    """
    # Calculate trading value for this tick
    trading_value = tick.price * tick.volume

    # Add to volume history (store (timestamp, price, volume_value))
    stock_state.volume_history.append({
        "ts": datetime.now(),
        "price": tick.price,
        "volume": tick.volume,
        "value": trading_value,
    })


def detect_surge(stock_state: StockState) -> bool:
    """Detect if current volume indicates a surge.

    Criteria:
    - Last entry's trading value >= 50M won
    - Last entry's volume >= 3x average of previous entries
    - NOT in cooldown (30s since last surge detection)

    Returns True if surge detected and activated.
    """
    if len(stock_state.volume_history) < 2:
        return False

    now = datetime.now()

    # Check cooldown
    if (now - stock_state.last_surge_ts).total_seconds() < SURGE_COOLDOWN_SECONDS:
        return False

    # Get current and historical volumes
    current = stock_state.volume_history[-1]
    historical = list(stock_state.volume_history)[:-1]

    # Check trading value threshold
    if current["value"] < SURGE_VOLUME_THRESHOLD:
        return False

    # Check volume multiplier
    if len(historical) > 0:
        avg_volume = sum(h["volume"] for h in historical) / len(historical)
        if current["volume"] < avg_volume * SURGE_MULTIPLIER:
            return False

    # Surge detected!
    stock_state.surge_active = True
    stock_state.last_surge_ts = now
    logger.info(
        "surge_detected",
        code=stock_state.code,
        volume=current["volume"],
        value=current["value"],
        price=current["price"],
    )
    return True


def clear_expired_surges() -> None:
    """Clear surge_active flags that have exceeded cooldown.

    Called periodically (e.g., every second) to reset expired surges.
    """
    # This would be called from somewhere to reset surge flags
    # For now, it's handled via is_surge_active check with timestamp
    pass
