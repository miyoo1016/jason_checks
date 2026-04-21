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
                # ★ CHANGED: 재연결 시 app_state.visible_codes 우선 사용 (현재 화면 종목)
                current_codes = list(app_state.visible_codes) if app_state.visible_codes else codes
                
                logger.info("kis_connect_start", codes_count=len(current_codes))
                await self.kis_ws.connect()
                await self.kis_ws.subscribe(current_codes)
                app_state.ws_connected = True
                logger.info("kis_connected")
                retry_delay = 1

                async for tick in self.kis_ws.stream():
                    stock = app_state.get_or_create_stock(tick.code)

                    # strength=0인 틱(주로 H0UNCNT0 잘못된 파싱)이 기존 KRX 값을 덮어쓰지 않도록
                    update_kwargs = {
                        "price": tick.price,
                        "change_pct": tick.change_pct,
                        "cum_volume_krw": tick.cumulative_volume,
                        "cumulative_trading_value": tick.cumulative_trading_value,
                        "market": tick.market,
                    }
                    if tick.strength > 0:
                        update_kwargs["execution_strength"] = tick.strength
                    app_state.update_stock(tick.code, **update_kwargs)

                    update_volume_window(stock, tick)
                    surge_detected = detect_surge(stock)

                    # broadcast에는 실제 strength 사용 (0이면 기존 app_state 값)
                    display_strength = tick.strength if tick.strength > 0 else stock.execution_strength

                    msg = {
                        "type": "tick",
                        "code": tick.code,
                        "price": tick.price,
                        "change_pct": tick.change_pct,
                        "cumulative_volume": tick.cumulative_volume,
                        "strength": display_strength,
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

    async def broadcast_investor_update(self, code: str, data: dict) -> None:
        """Broadcast investor trend update (polled from REST) to all clients instantly."""
        msg = {
            "type": "investor",
            "code": code,
            "foreigner": data.get("foreigner", 0),
            "institution": data.get("institution", 0),
            "individual": data.get("individual", 0),
        }
        await self._broadcast(json.dumps(msg))

    async def broadcast_index_update(self, code: str, data: dict) -> None:
        """Broadcast market index update instantly."""
        msg = {
            "type": "index",
            "code": code,
            "name": data.get("name", ""),
            "price": data.get("price", 0),
            "change_pct": data.get("change_pct", 0),
            "change_value": data.get("change_value", 0),
            "foreigner": data.get("investor_foreigner", 0),
            "institution": data.get("investor_institution", 0),
            "individual": data.get("investor_individual", 0),
        }
        await self._broadcast(json.dumps(msg))

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
