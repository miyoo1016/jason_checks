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
    """Real-time execution tick (H0STCNT0)."""

    code: str
    price: int                  # 현재가
    volume: int                 # 체결 거래량
    cumulative_volume: int      # 누적 거래량
    cumulative_trading_value: int  # 누적 거래대금 (원)
    high_price: int             # 최고가
    low_price: int              # 최저가
    strength: float             # 체결강도
    change_pct: float           # 전일 대비 등락률
    timestamp: str              # HHMMSS
    market: str = "J"           # J=KRX, NX=Nextrade


# H0STCNT0 field indices (KIS official spec)
# https://apiportal.koreainvestment.com/ - 실시간 체결가
H0STCNT0_FIELDS = [
    "stck_cntg_hour",      # 0  주식 체결 시간
    "stck_prpr",           # 1  주식 현재가
    "prdy_vrss_sign",      # 2  전일 대비 부호
    "prdy_vrss",           # 3  전일 대비
    "prdy_ctrt",           # 4  전일 대비율
    "wghn_avrg_stck_prc",  # 5  가중 평균 주식 가격
    "stck_oprc",           # 6  주식 시가
    "stck_hgpr",           # 7  주식 최고가
    "stck_lwpr",           # 8  주식 최저가
    "askp1",               # 9  매도호가 1
    "bidp1",               # 10 매수호가 1
    "cntg_vol",            # 11 체결 거래량
    "acml_vol",            # 12 누적 거래량
    "acml_tr_pbmn",        # 13 누적 거래대금
    "seln_cntg_csnu",      # 14 매도 체결 건수
    "shnu_cntg_csnu",      # 15 매수 체결 건수
    "ntby_cntg_csnu",      # 16 순매수 체결 건수
    "cttr",                # 17 체결강도
    # ... 더 많은 필드 있지만 MVP에는 여기까지
]


def _parse_execution_tick(raw: str) -> Optional[ExecutionTick]:
    """Parse pipe-delimited H0STCNT0 message.

    Format: `0|H0STCNT0|N|encryptedPayload`  (encrypted)
            `0|H0STCNT0|1|005930^HHMMSS^price^...` (unencrypted)
    """

    parts = raw.split("|")
    if len(parts) < 4:
        return None

    tr_id = parts[1]
    if tr_id not in ("H0STCNT0", "H0UNCNT0"):
        return None

    # parts[3] = caret-separated fields
    fields = parts[3].split("^")
    if len(fields) < 19:
        return None

    try:
        # H0STCNT0/H0STCNI0/H0UNCNT0 payload structure
        # fields[0] is usually stck_shrn_iscd (stock code)
        code = fields[0]
        timestamp = fields[1]
        price = int(fields[2])
        # change_pct is index 4 in spec, so fields[5] if fields[0] is code
        change_pct = float(fields[5])
        
        # acml_vol is index 12 in spec -> fields[13]
        cumulative_volume = int(fields[13])
        # acml_tr_pbmn is index 13 in spec -> fields[14]
        cumulative_trading_value = int(fields[14])
        
        # cntg_vol is index 11 in spec -> fields[12]
        volume = int(fields[12])
        
        high_price = int(fields[8])
        low_price = int(fields[9])
        
        # strength is index 17 in spec -> fields[18]
        strength = float(fields[18] or 0)

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


class KisWebSocket:
    """KIS WebSocket client."""

    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.approval_key: Optional[str] = None
        self.subscribed_codes: set[str] = set()
        self.on_tick: Optional[Callable[[ExecutionTick], None]] = None
        self.connected: bool = False
        self.cmd_queue = asyncio.Queue()
        self.last_tick_at = datetime.now()
        self._worker_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        """Connect to KIS WebSocket."""
        settings = get_settings()
        _, ws_url = get_urls(settings.kis_mode)

        self.approval_key = await get_approval_key()
        self.subscribed_codes.clear()

        self.ws = await websockets.connect(
            ws_url,
            ping_interval=20,
            ping_timeout=10,
        )
        self.connected = True
        self.last_tick_at = datetime.now()
        
        # Start command worker
        if self._worker_task:
            self._worker_task.cancel()
        self._worker_task = asyncio.create_task(self._command_worker())
        
        logger.info("ws_connected", url=ws_url)

    async def _command_worker(self):
        """Processes subscription commands one by one with safe delay."""
        while self.connected:
            try:
                cmd = await self.cmd_queue.get()
                if self.ws:
                    await self.ws.send(json.dumps(cmd))
                    await asyncio.sleep(0.2) # Strict 200ms delay between commands
                self.cmd_queue.task_done()
            except Exception as e:
                logger.error("ws_worker_error", error=str(e))
                await asyncio.sleep(1)

    async def subscribe(self, codes: list[str]) -> None:
        """Queue subscription requests."""
        from datetime import datetime as _dt
        now = _dt.now()
        ct = now.hour * 100 + now.minute
        is_nxt_time = now.weekday() < 5 and ((800 <= ct < 850) or (1530 <= ct < 2000))

        for code in codes:
            if code in self.subscribed_codes:
                continue

            # KRX 정규장 구독 (항상)
            msg = {
                "header": {
                    "approval_key": self.approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
            }
            await self.cmd_queue.put(msg)

            # NXT 구독 (NXT 시간에만)
            if is_nxt_time:
                msg_nxt = {
                    "header": {
                        "approval_key": self.approval_key,
                        "custtype": "P",
                        "tr_type": "1",
                        "content-type": "utf-8",
                    },
                    "body": {"input": {"tr_id": "H0UNCNT0", "tr_key": code}},
                }
                await self.cmd_queue.put(msg_nxt)

            self.subscribed_codes.add(code)

    async def unsubscribe(self, codes: list[str]) -> None:
        """Queue unsubscription requests."""
        for code in codes:
            if code not in self.subscribed_codes:
                continue
            for tr_id in ("H0STCNT0", "H0UNCNT0"):
                msg = {
                    "header": {
                        "approval_key": self.approval_key,
                        "custtype": "P",
                        "tr_type": "2",
                        "content-type": "utf-8",
                    },
                    "body": {"input": {"tr_id": tr_id, "tr_key": code}},
                }
                await self.cmd_queue.put(msg)
            self.subscribed_codes.discard(code)

    async def resubscribe(self, new_codes: list[str]) -> None:
        """Unsubscribe removed codes, subscribe new ones."""
        new_set = set(new_codes)
        to_remove = self.subscribed_codes - new_set
        to_add = new_set - self.subscribed_codes

        if to_remove:
            await self.unsubscribe(list(to_remove))
        if to_add:
            await self.subscribe(list(to_add))

    async def stream(self) -> AsyncIterator[ExecutionTick]:
        """Stream real-time ticks. Handles PINGPONG and subscribe acks transparently."""
        assert self.ws is not None

        while True:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=120.0)
            except websockets.exceptions.ConnectionClosed:
                logger.warning("ws_closed_by_remote")
                self.connected = False
                break
            except Exception as e:
                logger.error("ws_recv_error", error=str(e))
                self.connected = False
                break

            # JSON messages = subscribe ack, error, or PINGPONG
            if isinstance(msg, str) and msg.startswith("{"):
                try:
                    data = json.loads(msg)
                    header = data.get("header", {})
                    tr_id = header.get("tr_id", "")
                    
                    if tr_id == "PINGPONG":
                        await self.ws.send(msg) # Immediate echo
                        self.last_tick_at = datetime.now() # Reset watchdog on ping too
                        continue
                    
                    rt_cd = data.get("body", {}).get("rt_cd")
                    msg1 = data.get("body", {}).get("msg1", "")
                    logger.info("ws_ack", tr_id=tr_id, rt_cd=rt_cd, msg=msg1)
                    continue
                except json.JSONDecodeError:
                    pass

            # Watchdog check
            if (datetime.now() - self.last_tick_at).total_seconds() > 30:
                logger.warning("ws_watchdog_timeout_reconnecting")
                self.connected = False
                break
                
            # Pipe-delimited = actual tick data
            if isinstance(msg, str) and "|" in msg:
                self.last_tick_at = datetime.now() # Reset watchdog
                tick = _parse_execution_tick(msg)
                if tick:
                    if self.on_tick:
                        self.on_tick(tick)
                    yield tick

    async def close(self) -> None:
        """Close WebSocket."""
        if self.ws:
            await self.ws.close()
            self.connected = False
            logger.info("ws_closed")


_instance: Optional[KisWebSocket] = None


def get_ws() -> KisWebSocket:
    """Singleton WebSocket client."""
    global _instance
    if _instance is None:
        _instance = KisWebSocket()
    return _instance
