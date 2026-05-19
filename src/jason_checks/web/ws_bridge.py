"""WebSocket bridge — browser <-> KIS connection."""

import asyncio
import json
from typing import Dict, Set
from fastapi import WebSocket
import structlog

from datetime import datetime
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
        self.target_codes: list[str] = []

    async def start_stream(self, codes: list[str]) -> None:
        """Start streaming from KIS."""
        self.target_codes = list(codes)
        if self.running:
            return

        self.running = True
        # Start two independent tasks: one for WS, one for REST
        self.stream_task = asyncio.create_task(self._stream_loop(codes))
        asyncio.create_task(self._rest_fallback_loop())

    async def _stream_loop(self, initial_codes: list[str]) -> None:
        """Stream KIS ticks with aggressive reconnection and zombie flushing."""
        retry_delay = 1
        while self.running:
            try:
                await self.kis_ws.connect()
                current_codes = self.target_codes or initial_codes
                
                # Proactively flush current target codes to clear zombie slots
                if current_codes:
                    await self.kis_ws.flush_all(current_codes)
                
                await self.kis_ws.subscribe(current_codes)
                app_state.ws_connected = True
                
                async for tick in self.kis_ws.stream():
                    self._update_and_broadcast(tick)
            except Exception as e:
                logger.warning("ws_stream_error", error=str(e))
                app_state.ws_connected = False
            await asyncio.sleep(5) # Constant retry interval

    async def _rest_fallback_loop(self):
        """Poll REST API independently every 10s as a hard guarantee of movement."""
        from jason_checks.kis_rest import fetch_current_price, fetch_investor_data
        while self.running:
            try:
                # SAFE ACCESS: Get active codes from the shared app_state
                active_codes = list(app_state.stocks.keys())
                
                if not active_codes:
                    active_codes = ["005930", "000660", "042700", "403870"]

                logger.info("hard_fallback_polling", count=len(active_codes))
                for code in active_codes:
                    if not self.running: break
                    try:
                        # Parallel fetch with timeout to prevent stalling
                        tasks = [
                            asyncio.wait_for(fetch_current_price(code), timeout=3.0),
                            asyncio.wait_for(fetch_investor_data(code), timeout=3.0)
                        ]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        
                        data = results[0] if not isinstance(results[0], Exception) else None
                        inv_data = results[1] if not isinstance(results[1], Exception) else None
                        
                        if data:
                            tick = ExecutionTick(
                                code=code,
                                price=data["price"],
                                change_pct=data["change_pct"],
                                volume=0,
                                cumulative_volume=data["volume"],
                                cumulative_trading_value=data.get("trading_value", 0),
                                strength=data.get("strength", 0.0),
                                timestamp=datetime.now().strftime("%H%M%S"),
                                market="J"
                            )
                            # Update with investor data if available (with non-zero protection)
                            if inv_data and isinstance(inv_data, dict):
                                inv_update = {}
                                if inv_data.get("foreign", 0) != 0 or stock.investor_foreigner == 0:
                                    inv_update["investor_foreigner"] = inv_data.get("foreign", 0)
                                if inv_data.get("institution", 0) != 0 or stock.investor_institution == 0:
                                    inv_update["investor_institution"] = inv_data.get("institution", 0)
                                if inv_data.get("individual", 0) != 0 or stock.investor_individual == 0:
                                    inv_update["investor_individual"] = inv_data.get("individual", 0)
                                
                                if inv_update:
                                    app_state.update_stock(code, **inv_update)
                            
                            self._update_and_broadcast(tick)
                    except Exception: pass
                    await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning("hard_fallback_error", error=str(e))
            await asyncio.sleep(5)

    def _update_and_broadcast(self, tick: ExecutionTick):
        """Internal helper to update state and broadcast to UI."""
        stock = app_state.get_or_create_stock(tick.code)
        
        # Update volume history and detect surge
        update_volume_window(stock, tick)
        is_surge = detect_surge(stock)
        
        # Retain previous non-zero strength if current tick has 0
        final_strength = tick.strength if tick.strength > 0 else stock.execution_strength
        if final_strength == 0: final_strength = 0.0 # Default if everything is 0
        
        app_state.update_stock(
            tick.code,
            price=tick.price,
            change_pct=tick.change_pct,
            cumulative_volume=tick.cumulative_volume,
            cumulative_trading_value=tick.cumulative_trading_value,
            execution_strength=final_strength,
            market=tick.market,
        )
        msg = {
            "type": "tick",
            "code": tick.code,
            "price": tick.price,
            "change_pct": tick.change_pct,
            "cumulative_volume": tick.cumulative_volume,
            "cumulative_trading_value": tick.cumulative_trading_value,
            "strength": final_strength,
            "timestamp": tick.timestamp,
            "surge_active": stock.surge_active,
            # ADDED: Include investor trends in the broadcast message
            "investor_foreigner": stock.investor_foreigner,
            "investor_institution": stock.investor_institution,
            "investor_individual": stock.investor_individual,
        }
        asyncio.create_task(self._broadcast(json.dumps(msg)))
        # LOG AND PRINT FOR FINAL VERIFICATION
        print(f"DEBUG_BROADCAST: {tick.code} P:{tick.price} S:{tick.strength} F:{msg['investor_foreigner']}")
        logger.info("broadcast_tick", code=tick.code, price=tick.price, strength=tick.strength, foreign=msg["investor_foreigner"])

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
