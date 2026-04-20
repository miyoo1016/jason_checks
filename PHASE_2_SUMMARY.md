# Phase 2 — HTML Web Conversion (Complete)

**Status**: ✅ **READY FOR TESTING**

## What Changed

### Before (Phase 1)
- Textual TUI in terminal
- Direct asyncio event loop
- Terminal-only UI rendering
- Single process

### After (Phase 2)
- FastAPI web server on `localhost:8000`
- Browser connects via WebSocket
- HTML/CSS/JS frontend
- Server bridges browser ↔ KIS WebSocket

---

## Files Created

### Core Web Module
- `src/jason_checks/web/__init__.py`
- `src/jason_checks/web/app.py` — FastAPI application
- `src/jason_checks/web/ws_bridge.py` — Browser ↔ KIS bridge
- `src/jason_checks/web/templates/index.html` — Main page (TailwindCSS + Alpine.js)

### Static Assets
- `src/jason_checks/static/js/app.js` — WebSocket client + Alpine data
- `src/jason_checks/static/css/checks.css` — Custom styles

### Entry Points
- `server.py` — FastAPI server launcher (replaces tui_main.py)
- `run_web.sh` — One-command startup script for web server

### Dependencies Updated
- Added: `fastapi`, `uvicorn`, `jinja2`
- All Phase 1 modules unchanged (config, kis_auth, kis_ws, state)

---

## Architecture

```
Browser (Chrome)
    ↓ WebSocket (200ms updates)
FastAPI (localhost:8000)
    ↓ WebSocket (relay)
KIS API (real-time)
    ↓ REST (3-5s polls)
```

### Data Flow
1. User opens `http://localhost:8000` in browser
2. JavaScript opens WebSocket to `/ws`
3. FastAPI `ws_bridge.py` connects to KIS
4. KIS execution ticks → FastAPI → browser WebSocket → JS → DOM update
5. HTML refreshes in real-time (200ms cycle)

---

## How to Run

### Quick Start
```bash
cd /Users/miyoo1016/jason_checks
./run_web.sh
```

### Manual Start
```bash
source venv/bin/activate
python3 server.py
```

### Expected Output
```
🚀 CHECKS Terminal Web Server
📍 http://localhost:8000
Press Ctrl+C to stop

INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Application startup complete
```

### Open Browser
```
http://localhost:8000
```

---

## What You'll See

### Display
- **Header**: CHECKS Terminal title + current time + mode (PAPER)
- **Status Bar**: 🟢 WS:OK (if connected), REST:WAITING, MODE, Market status
- **Stock Card**: Samsung (005930)
  - Current Price (real-time, updates every 1-2 seconds during market hours)
  - Change % (color: red for up, blue for down — Korean convention)
  - Execution Strength, Volume, Last Tick Time
  - Market status: "장중" (if 09:00-15:30) or "장마감"
- **Footer**: Phase 2 notice

### Real-time Updates
- Price changes every 1-2 seconds (when market is open)
- Change % colored (red/blue)
- Last tick time updated
- All via WebSocket (no page refresh)

---

## Testing Checklist

- [ ] Run `./run_web.sh`
- [ ] Browser opens `http://localhost:8000` successfully
- [ ] Page shows "CHECKS Terminal" header
- [ ] WebSocket connects: `🟢 WS:OK` appears
- [ ] Samsung price displays (or "대기중" if market closed)
- [ ] **During market hours (09:00-15:30, Mon-Fri)**:
  - [ ] Price updates every 1-2 seconds
  - [ ] Change % shows with correct color (red/blue)
  - [ ] Last tick time updates
- [ ] **After market hours**:
  - [ ] Yellow message: "⚠ 장 마감 중..."
  - [ ] WS:OK still shows (connected but no data)
- [ ] Press Ctrl+C in terminal to stop server
- [ ] Server shuts down cleanly

---

## Phase 2 vs Phase 1

| Aspect | Phase 1 | Phase 2 |
|--------|---------|---------|
| Interface | Terminal (Textual) | Browser (HTML/CSS/JS) |
| Server | None (direct asyncio) | FastAPI + Uvicorn |
| Frontend | TUI widgets | DOM + Alpine.js |
| Styling | Terminal colors | Tailwind + custom CSS |
| Multi-tab | No | Yes (all tabs share 1 KIS connection) |
| Stocks shown | 1 (Samsung only) | 1 (Samsung only) |
| Real-time | 200ms refresh | 200ms WebSocket push |

---

## Known Limitations (Phase 2)

- **Only Samsung (005930)** displayed (will expand to 41 stocks in Phase 3)
- **No theme grid yet** (Phase 3)
- **No memo, search, or settings** (Phase 4)
- **No replay mode** (Phase 4)
- **No limit-up detection** (Phase 4)

---

## Next Steps → Phase 3

Phase 3 will add:
- Multiple stock subscription (up to 41)
- 2×2 theme grid (조선, 개별이슈, etc.)
- Top 4 stocks per theme
- Theme strength calculation
- Better styling + animations

---

## Troubleshooting

### "Connection refused"
- Ensure `./run_web.sh` is running in terminal
- Check `http://localhost:8000` (not another port)

### "🔴 WS:DISCONNECTED"
- Same causes as Phase 1:
  - KIS Developers: enable "실시간 시세"
  - Verify `.env` credentials
  - Check mock account active

### No price updates (but WS:OK)
- Market may be closed (outside 09:00-15:30, weekdays)
- Logs will show subscription success
- Data arrives when market opens

### Server won't start
```bash
source venv/bin/activate
pip install -r requirements.txt
python3 server.py
```

---

## Architecture Highlights

✅ **Phase 1 modules are unchanged** — Zero risk of breaking existing code
✅ **FastAPI is lightweight** — minimal overhead vs Textual
✅ **WebSocket is efficient** — 200ms pushes are cheap
✅ **Browser advantages**:
- Multi-tab support (share 1 KIS connection)
- Easier styling (CSS vs terminal)
- Keyboard shortcuts work
- Better mobile responsiveness (Phase 3)

---

**Phase 2 Complete. Ready for web testing.** 🚀
