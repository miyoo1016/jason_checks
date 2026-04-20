"""CHECKS Terminal UI — Textual-based real-time trading dashboard."""

import asyncio
from datetime import datetime, time
from typing import Optional

from textual.app import ComposeResult, App
from textual.containers import Container, Vertical
from textual.widgets import Header, Footer, Static
from textual.binding import Binding

from jason_checks.kis_ws import get_ws, ExecutionTick
from jason_checks.state import app_state
from jason_checks.config import get_settings
import structlog

logger = structlog.get_logger()


def is_market_open() -> bool:
    """Rough check: KRX market is Mon-Fri 09:00-15:30 KST."""
    now = datetime.now()
    if now.weekday() >= 5:  # Sat, Sun
        return False
    t = now.time()
    return time(9, 0) <= t <= time(15, 30)


class StatusLine(Static):
    """Top status line showing boot progress / errors."""

    def update_status(self, msg: str, style: str = "cyan") -> None:
        self.update(f"[bold {style}]{msg}[/]")


class PriceDisplay(Static):
    """Real-time price display."""

    def __init__(self, stock_code: str):
        super().__init__()
        self.stock_code = stock_code

    def render_display(self) -> str:
        stock = app_state.stocks.get(self.stock_code)
        if not stock or stock.price == 0:
            market_note = "" if is_market_open() else "\n[dim yellow]⚠ 장 마감 - 체결 데이터 없음 (평일 09:00~15:30)[/]"
            return (
                f"[bold]실시간 시세[/bold]\n\n"
                f"종목코드: {self.stock_code} (삼성전자)\n"
                f"현재가: [dim]대기 중…[/]\n"
                f"{market_note}"
            )

        color = "green" if stock.change_pct >= 0 else "red"
        arrow = "▲" if stock.change_pct >= 0 else "▼"
        return (
            f"[bold]실시간 시세[/bold]\n\n"
            f"종목코드: {self.stock_code} (삼성전자)\n"
            f"현재가: [bold]{stock.price:,}[/] 원\n"
            f"[{color}]등락률: {arrow} {stock.change_pct:+.2f}%[/]\n"
            f"체결강도: {stock.execution_strength:.1f}\n"
            f"누적거래량: {stock.cum_volume_krw:,}\n"
            f"마지막 갱신: {stock.last_tick_ts.strftime('%H:%M:%S')}"
        )

    def refresh_display(self) -> None:
        self.update(self.render_display())


class ConnectionStatus(Static):
    """Footer status line."""

    def refresh_status(self) -> None:
        ws = "[green]● WS:OK[/]" if app_state.ws_connected else "[red]● WS:DISCONNECTED[/]"
        rest = "[dim]○ REST:WAITING (Phase 2)[/]"
        mode = get_settings().kis_mode.upper()
        market = "[green]장 열림[/]" if is_market_open() else "[yellow]장 마감[/]"
        self.update(f"{ws}  |  {rest}  |  MODE:{mode}  |  {market}")


class TimaApp(App):
    """CHECKS main application."""

    CSS = """
    StatusLine {
        height: 2;
        padding: 0 1;
    }
    PriceDisplay {
        height: auto;
        padding: 1 2;
        border: solid cyan;
        margin: 1 2;
    }
    ConnectionStatus {
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh (reconnect WS)", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("m", "memo", "Memo (Phase 2)", show=True),
    ]

    TITLE = "CHECKS Terminal"
    SUB_TITLE = "KIS Open API - Real-time (Phase 1 MVP)"

    def __init__(self):
        super().__init__()
        self.stock_code = "005930"
        self.ws = get_ws()
        self.price_display: Optional[PriceDisplay] = None
        self.status_line: Optional[StatusLine] = None
        self.conn_status: Optional[ConnectionStatus] = None
        self._ws_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Container():
            self.status_line = StatusLine("[bold cyan]CHECKS Terminal v0.1.0[/]\n[dim]부팅 중…[/]")
            yield self.status_line
            with Vertical():
                self.price_display = PriceDisplay(self.stock_code)
                self.price_display.update(self.price_display.render_display())
                yield self.price_display
            self.conn_status = ConnectionStatus()
            yield self.conn_status
        yield Footer()

    async def on_mount(self) -> None:
        """App startup."""
        logger.info("app_started")
        # Refresh price display every 200ms
        self.set_interval(0.2, self._tick_display)
        # Refresh connection status every 1s
        self.set_interval(1.0, self._tick_status)
        # Kick off WebSocket task
        self._ws_task = asyncio.create_task(self._run_websocket())

    async def _run_websocket(self) -> None:
        """Connect, subscribe, stream ticks."""
        try:
            self.status_line.update_status("KIS Approval Key 발급 중…", "cyan")
            await self.ws.connect()

            self.status_line.update_status(f"구독 중: {self.stock_code}…", "cyan")
            await self.ws.subscribe([self.stock_code])

            app_state.ws_connected = True
            if is_market_open():
                self.status_line.update_status("✓ 실시간 체결가 수신 중", "green")
            else:
                self.status_line.update_status("✓ 연결됨 (장 마감 - 평일 09:00 이후 체결 데이터)", "yellow")

            async for tick in self.ws.stream():
                app_state.update_stock(
                    tick.code,
                    price=tick.price,
                    change_pct=tick.change_pct,
                    cum_volume_krw=tick.cumulative_volume,
                    execution_strength=tick.strength,
                )

        except Exception as e:
            logger.error("ws_task_failed", error=str(e))
            app_state.ws_connected = False
            self.status_line.update_status(f"✗ 오류: {type(e).__name__}: {str(e)[:80]}", "red")

    def _tick_display(self) -> None:
        if self.price_display:
            self.price_display.refresh_display()

    def _tick_status(self) -> None:
        if self.conn_status:
            self.conn_status.refresh_status()

    async def action_refresh(self) -> None:
        """Force reconnect WebSocket."""
        self.status_line.update_status("재연결 중…", "cyan")
        await self.ws.close()
        app_state.ws_connected = False
        if self._ws_task:
            self._ws_task.cancel()
        self._ws_task = asyncio.create_task(self._run_websocket())

    def action_memo(self) -> None:
        self.notify("메모 기능은 Phase 2에서 구현됩니다.", severity="information")

    async def on_unmount(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
        await self.ws.close()
        logger.info("app_shutdown")


def main():
    app = TimaApp()
    app.run()


if __name__ == "__main__":
    main()
