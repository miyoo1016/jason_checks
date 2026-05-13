"""KIS WebSocket client - real-time tick streaming.

KIS WebSocket protocol:
- Connect → no auth frame needed
- Subscribe message: JSON with header.approval_key + body.input.tr_id/tr_key
- Server response is either:
  * JSON (subscribe ack, errors, PINGPONG)
  * Pipe-delimited string (actual tick data)
    Format: `0|H0STCNT0|005930|field1^field2^...`
"""

import asyncio
import json
from datetime import datetime
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Callable
import websockets
import structlog

from jason_checks.config import get_urls, get_active_credentials, get_settings
from jason_checks.kis_auth import get_approval_key

logger = structlog.get_logger()


@dataclass
class ExecutionTick:
    """Real-time execution tick."""

    code: str
    price: float                # 현재가
    volume: int                 # 체결 거래량
    cumulative_volume: int      # 누적 거래량
    cumulative_trading_value: int  # 누적 거래대금
    high_price: float           # 최고가
    low_price: float            # 최저가
    strength: float             # 체결강도
    change_pct: float           # 전일 대비 등락률
    timestamp: str              # HHMMSS
    market: str = "J"           # J=KRX, NX=Nextrade, US=USA


def _parse_execution_tick(raw: str) -> Optional[ExecutionTick]:
    """Parse pipe-delimited H0STCNT0 (KR) message."""
    parts = raw.split("|")
    if len(parts) < 4:
        return None

    tr_id = parts[1]
    if tr_id not in ("H0STCNT0", "H0STCNI0", "H0UNCNT0"):
        return None

    fields = parts[3].split("^")
    if len(fields) < 19:
        return None

    try:
        code = fields[0]
        timestamp = fields[1]
        price = float(fields[2])
        change_pct = float(fields[5])
        volume = int(fields[12])
        cumulative_volume = int(fields[13])
        cumulative_trading_value = int(fields[14])
        high_price = float(fields[8])
        low_price = float(fields[9])
        strength = float(fields[18])

        return ExecutionTick(
            code=code,
            price=price,
            volume=volume,
            cumulative_volume=cumulative_volume,
            cumulative_trading_value=cumulative_trading_value,
            high_price=high_price,
            low_price=low_price,
            strength=strength,
            change_pct=change_pct,
            timestamp=timestamp,
            market="NX" if tr_id == "H0UNCNT0" else "J"
        )
    except (ValueError, IndexError) as e:
        logger.warning("parse_tick_failed", error=str(e), raw=raw[:100])
        return None


def _parse_us_execution_tick(raw: str) -> Optional[ExecutionTick]:
    """Parse pipe-delimited HDFSCNT0 (US) message."""
    parts = raw.split("|")
    if len(parts) < 4:
        return None

    fields = parts[3].split("^")
    if len(fields) < 12:
        return None

    try:
        # fields[0] is symbol with prefix like NASAAPL or NYSTSLA
        raw_code = fields[0]
        # Strip prefixes (NAS, NYS, AMS, DNAS, DNYS, DAMS)
        code = raw_code
        for p in ["NAS", "NYS", "AMS", "DNAS", "DNYS", "DAMS"]:
            if raw_code.startswith(p):
                code = raw_code[len(p):]
                break

        timestamp = fields[1]
        price = float(fields[5])
        change_pct = float(fields[4])
        volume = int(fields[10])
        cumulative_volume = int(fields[12]) if len(fields) > 12 else 0
        trading_value = float(fields[11])

        return ExecutionTick(
            code=code,
            price=price,
            volume=volume,
            cumulative_volume=cumulative_volume,
            cumulative_trading_value=int(trading_value),
            high_price=price,
            low_price=price,
            strength=100.0,
            change_pct=change_pct,
            timestamp=timestamp,
            market="US"
        )
    except (ValueError, IndexError) as e:
        logger.warning("parse_us_tick_failed", error=str(e), raw=raw[:100])
        return None


class KisWebSocket:
    """KIS WebSocket client with dedicated listener and 40-stock limit manager."""

    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.approval_key: Optional[str] = None
        self.subscribed_codes: set[str] = set()
        self.subscribed_tr_map: dict[str, str] = {}
        self.on_tick: Optional[Callable[[ExecutionTick], None]] = None
        self.connected: bool = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._listener_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.max_subscriptions = 40
        self._ignored_unsub_not_found_count = 0
        self._ignored_unsub_not_found_logged = False

    async def connect(self) -> None:
        """Connect to KIS WebSocket and start listener task."""
        async with self._lock:
            # Clean up previous connection and task
            if self._listener_task:
                self._listener_task.cancel()
                try: await self._listener_task
                except asyncio.CancelledError: pass
            
            if self.ws:
                await self.ws.close()
            
            settings = get_settings()
            _, ws_url = get_urls(settings.kis_mode)
            self.approval_key = await get_approval_key()
            
            # Reset state but keep codes for resubscription if needed
            # (or clear if we want fresh start)
            self.subscribed_codes.clear()
            self.subscribed_tr_map.clear()
            
            self.ws = await websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                open_timeout=15,
            )
            self.connected = True
            logger.info("ws_connected", url=ws_url)
            
            # Start dedicated listener
            self._listener_task = asyncio.create_task(self._listener_loop())

    async def _listener_loop(self):
        """Dedicated loop to read from websocket and put into queue."""
        assert self.ws is not None
        while self.connected:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=125.0)
                
                # Handle PINGPONG immediately
                if isinstance(msg, str) and "PINGPONG" in msg:
                    await self.ws.send(msg)
                    continue
                
                # Push to queue for stream() to consume
                await self._queue.put(msg)
                
            except asyncio.TimeoutError:
                logger.warning("ws_recv_timeout")
                self.connected = False
                break
            except Exception as e:
                if self.connected:
                    logger.error("ws_listener_error", error=str(e))
                self.connected = False
                break
        logger.info("ws_listener_stopped")

    def _get_tr_id(self, code: str) -> str:
        """Determine TR_ID based on code (KR vs US)."""
        if any(c.isalpha() for c in code):
            return "HDFSCNT0"
        return "H0STCNT0"

    def _format_tr_key(self, code: str) -> str:
        """Format symbol for US market if needed."""
        if any(c.isalpha() for c in code):
            # Market mapping for US
            nasdaq_tickers = {"NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "NFLX", "AMD", "ASML", "PLTR", "SNOW", "NOW", "CRM", "RIVN", "LCID", "QQQ"}
            nyse_tickers = {"TSM", "BABA", "V", "MA", "JNJ", "WMT", "DIS", "KO", "ORCL"}
            amex_tickers = {"SPY", "SOXL", "DIA", "IVV", "VOO"}
            
            if not any(code.startswith(p) for p in ["NAS", "NYS", "AMS", "DNAS", "DNYS", "DAMS"]):
                if code in nyse_tickers: return f"NYS{code}"
                if code in amex_tickers: return f"AMS{code}"
                return f"NAS{code}"
        return code

    async def subscribe(self, codes: list[str]) -> None:
        """Subscribe to codes, enforcing the 40-stock limit."""
        if not self.ws or not self.connected: return
        
        async with self._lock:
            for code in codes:
                if code in self.subscribed_codes: continue
                
                # Limit Management: If full, unsubscribe the oldest one
                if len(self.subscribed_codes) >= self.max_subscriptions:
                    oldest = next(iter(self.subscribed_codes))
                    logger.info("ws_auto_unsub_overflow", code=oldest)
                    await self._send_sub_msg(oldest, "2")
                    self.subscribed_codes.discard(oldest)
                
                await self._send_sub_msg(code, "1")
                self.subscribed_codes.add(code)
                logger.info("ws_subscribed", code=code, total=len(self.subscribed_codes))
                await asyncio.sleep(0.1)

    async def unsubscribe(self, codes: list[str]) -> None:
        """Unsubscribe from codes."""
        if not self.ws or not self.connected: return
        async with self._lock:
            for code in codes:
                if code not in self.subscribed_codes: continue
                await self._send_sub_msg(code, "2")
                self.subscribed_codes.discard(code)
                await asyncio.sleep(0.05)

    async def _send_sub_msg(self, code: str, tr_type: str):
        """Send 1 (Sub) or 2 (Unsub) message."""
        tr_id = self._get_tr_id(code)
        tr_key = self._format_tr_key(code)
        msg = {
            "header": {
                "approval_key": self.approval_key,
                "custtype": "P",
                "tr_type": tr_type,
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
        await self.ws.send(json.dumps(msg))

    async def flush_all(self, codes: list[str]) -> None:
        """Clear zombie slots on KIS server. Use with caution."""
        if not self.ws or not self.connected: return
        logger.info("ws_flushing_zombies", count=len(codes))
        # No lock here to avoid blocking during cleanup if needed, 
        # but _send_sub_msg is fine.
        for code in codes:
            try:
                await self._send_sub_msg(code, "2")
                await asyncio.sleep(0.05)
            except: pass

    async def resubscribe(self, new_codes: list[str]) -> None:
        """Unsubscribe removed codes, subscribe new ones."""
        async with self._lock:
            new_set = set(new_codes)
            to_remove = self.subscribed_codes - new_set
            to_add = new_set - self.subscribed_codes
            
            for code in to_remove:
                await self._send_sub_msg(code, "2")
                self.subscribed_codes.discard(code)
                await asyncio.sleep(0.05)
                
            for code in to_add:
                if len(self.subscribed_codes) >= self.max_subscriptions:
                    oldest = next(iter(self.subscribed_codes))
                    await self._send_sub_msg(oldest, "2")
                    self.subscribed_codes.discard(oldest)
                
                await self._send_sub_msg(code, "1")
                self.subscribed_codes.add(code)
                await asyncio.sleep(0.1)

    async def stream(self) -> AsyncIterator[ExecutionTick]:
        """Consume ticks from the listener queue."""
        while True:
            msg = await self._queue.get()
            if isinstance(msg, str) and msg.startswith("{"):
                try:
                    data = json.loads(msg)
                    body = data.get("body", {})
                    rt_cd = body.get("rt_cd")
                    if rt_cd != "0" and rt_cd is not None:
                        header = data.get("header", {})
                        code = header.get("tr_key", "")
                        msg1 = body.get("msg1", "")
                        if self._is_benign_unsubscribe_not_found(header, msg1):
                            self._ignored_unsub_not_found_count += 1
                            if not self._ignored_unsub_not_found_logged:
                                logger.info(
                                    "ws_unsubscribe_not_found_ignored",
                                    count=self._ignored_unsub_not_found_count,
                                    code=code,
                                    reason="server_slot_already_empty",
                                )
                                self._ignored_unsub_not_found_logged = True
                            else:
                                logger.debug(
                                    "ws_unsubscribe_not_found_ignored",
                                    count=self._ignored_unsub_not_found_count,
                                    code=code,
                                )
                        else:
                            logger.warning("ws_ack_error", code=code, msg=msg1)
                except: pass
                continue

            if isinstance(msg, str) and "|" in msg:
                parts = msg.split("|")
                tr_id = parts[1]
                tick = _parse_us_execution_tick(msg) if tr_id == "HDFSCNT0" else _parse_execution_tick(msg)
                if tick: yield tick

    @staticmethod
    def _is_benign_unsubscribe_not_found(header: dict, msg: str) -> bool:
        """KIS returns this when a cleanup unsubscribe targets an empty slot."""
        msg_l = (msg or "").lower()
        tr_type = str(header.get("tr_type", ""))
        return (
            ("unsubscribe" in msg_l or tr_type == "2")
            and "not found" in msg_l
        )

    async def close(self) -> None:
        """Clean up connection and tasks."""
        self.connected = False
        async with self._lock:
            if self._listener_task:
                self._listener_task.cancel()
            if self.ws:
                await self.ws.close()
            logger.info("ws_closed")

_instance: Optional[KisWebSocket] = None

def get_ws() -> KisWebSocket:
    """Singleton WebSocket client."""
    global _instance
    if _instance is None:
        _instance = KisWebSocket()
    return _instance
