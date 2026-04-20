"""WebSocket bridge — browser <-> KIS connection."""

import asyncio
import json
from typing import Dict, Set
from fastapi import WebSocket
import structlog

from jason_checks.kis_ws import get_ws, ExecutionTick
from jason_checks.state import app_state
from jason_checks.surge_detector import update_volume_window, detect_surge

logger = structlog.get_logger()

# Track all connected browser clients
_clients: Set[WebSocket] = set()
_background_task: asyncio.Task = None


class WSBridge:
    """Bridge between browser WebSocket clients and KIS WebSocket."""

    def __init__(self):
        self.kis_ws = get_ws()
        self.stream_task: asyncio.Task = None
        self.running = False

    async def start_stream(self, codes: list[str]) -> None:
        """Start streaming from KIS."""
        if self.running:
            return

        self.running = True
        # Start background stream task with codes
        self.stream_task = asyncio.create_task(self._stream_loop(codes))

    async def _stream_loop(self, codes: list[str]) -> None:
        """Stream KIS ticks and broadcast with proper retry backoff."""
        retry_delay = 1
        max_delay = 30

        while self.running:
            stream_ok = False
            try:
                logger.info("kis_connect_start")
                await self.kis_ws.connect()
                await self.kis_ws.subscribe(codes)
                app_state.ws_connected = True
                logger.info("kis_connected")
                retry_delay = 1

                async for tick in self.kis_ws.stream():
                    stock = app_state.get_or_create_stock(tick.code)
                    app_state.update_stock(
                        tick.code,
                        price=tick.price,
                        change_pct=tick.change_pct,
                        cum_volume_krw=tick.cumulative_volume,
                        cumulative_trading_value=tick.cumulative_trading_value,
                        execution_strength=tick.strength,
                        market=tick.market,
                    )

                    update_volume_window(stock, tick)
                    surge_detected = detect_surge(stock)

                    msg = {
                        "type": "tick",
                        "code": tick.code,
                        "price": tick.price,
                        "change_pct": tick.change_pct,
                        "cumulative_volume": tick.cumulative_volume,
                        "strength": tick.strength,
                        "timestamp": tick.timestamp,
                        "market": tick.market,
                    }
                    await self._broadcast(json.dumps(msg))

                    if surge_detected:
                        surge_msg = {
                            "type": "surge",
                            "code": tick.code,
                            "price": tick.price,
                            "volume": tick.volume,
                            "timestamp": tick.timestamp,
                        }
                        await self._broadcast(json.dumps(surge_msg))

                # async for exited without exception (stream broke cleanly)
                stream_ok = True
                logger.warning("kis_stream_ended_normally")

            except asyncio.CancelledError:
                logger.info("stream_loop_cancelled")
                app_state.ws_connected = False
                break
            except Exception as e:
                logger.error("stream_loop_error", error=str(e))

            # Disconnected state
            app_state.ws_connected = False
            try:
                await self.kis_ws.close()
            except Exception:
                pass

            if not self.running:
                break

            # Backoff (even on normal end, to prevent server hammering)
            wait = 3 if stream_ok else retry_delay
            if not stream_ok:
                retry_delay = min(retry_delay * 2, max_delay)
            logger.info("kis_reconnect_waiting", seconds=wait)
            await asyncio.sleep(wait)

    async def _broadcast(self, message: str) -> None:
        """Broadcast message to all connected clients."""
        disconnected = set()
        for client in _clients:
            try:
                await client.send_text(message)
            except Exception as e:
                logger.warning("client_send_failed", error=str(e))
                disconnected.add(client)

        for client in disconnected:
            _clients.discard(client)

    async def add_client(self, websocket: WebSocket) -> None:
        """Add a new browser client."""
        _clients.add(websocket)
        logger.info("client_connected", total=len(_clients))

    async def remove_client(self, websocket: WebSocket) -> None:
        """Remove a browser client."""
        _clients.discard(websocket)
        logger.info("client_disconnected", total=len(_clients))

    async def stop(self) -> None:
        """Stop streaming."""
        if self.stream_task:
            self.stream_task.cancel()
            try:
                await self.stream_task
            except asyncio.CancelledError:
                pass
        await self.kis_ws.close()
        self.running = False


# Global bridge instance
_bridge = WSBridge()


def get_bridge() -> WSBridge:
    """Get global bridge instance."""
    return _bridge


async def start_bridge(codes: list[str]) -> None:
    """Start the bridge."""
    await get_bridge().start_stream(codes)
