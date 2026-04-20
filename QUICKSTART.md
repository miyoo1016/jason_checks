# CHECKS Terminal — Phase 1 Quick Start

## Ready to Run ✅

All files created. Dependencies installed. Credentials configured.

## 3-Step Launch

### Step 1: Activate Virtual Environment
```bash
cd /Users/miyoo1016/jason_checks
source venv/bin/activate
```

### Step 2: Run the App
```bash
python3 src/jason_checks/tui_main.py
```

Or one-command:
```bash
./run.sh
```

### Step 3: Watch Real-Time Updates
```
╔════════════════════════════════════════╗
║      CHECKS Terminal v0.1.0              ║
║   KIS Open API — Samsung (005930)      ║
╠════════════════════════════════════════╣
║                                        ║
║  실시간 시세                           ║
║                                        ║
║  종목코드: 005930                      ║
║  현재가: 72,500 원                     ║
║  등락률: +2.15%                        ║
║  체결강도: 142.3                       ║
║  누적거래량: 45,123,456                ║
║  수급비: 0.78                          ║
║  갱신: 14:23:47                        ║
║                                        ║
╠════════════════════════════════════════╣
║ 🟢 WS:OK | ⚪ REST:WAITING | MODE:LIVE ║
╚════════════════════════════════════════╝

[r] Refresh  [m] Memo  [q] Quit
```

## What to Expect

1. **Launch (0-5s)**: Window opens
2. **"KIS Approval Key 발급 중..."**: System getting auth token
3. **"🟢 WS:OK"** (10-30s): Connected to KIS
4. **Real-time updates**: Price changes every 1-2 seconds

## Controls

| Key | Action |
|---|---|
| `r` | Force refresh |
| `m` | Add memo (Phase 2) |
| `q` | **Quit** |

## If It Doesn't Work

**Most common issue**: Mock account not active in KIS Developers

Fix:
1. Open [KIS Developers](https://apiportal.koreainvestment.com/)
2. Login with your KIS ID
3. Check "신청/변경" → "실시간 시세" is enabled
4. Restart the app

**All other issues**: See `README.md` Troubleshooting section

## What's Running

- ✅ KIS WebSocket (real-time price)
- ✅ Approval Key caching (24h)
- ✅ Textual TUI (responsive, 200ms refresh)
- ✅ Async event loop (non-blocking)
- ⏳ REST API (Phase 2)
- ⏳ Themes & leaders (Phase 2)
- ⏳ Memo storage (Phase 2)

---

**Questions?** Check `PHASE_1_SUMMARY.md` or `README.md`
