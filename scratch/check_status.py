import asyncio
import json
from jason_checks.web.ws_bridge import get_bridge

async def check_bridge():
    bridge = get_bridge()
    print(f"Bridge running: {bridge.running}")
    print(f"Clients: {len(bridge.clients)}")
    from jason_checks.kis_ws import get_ws
    ws = get_ws()
    print(f"KIS WS Connected: {ws.connected}")
    print(f"Subscribed codes: {len(ws.subscribed_codes)}")

if __name__ == "__main__":
    try:
        asyncio.run(check_bridge())
    except Exception as e:
        print(f"Error: {e}")
