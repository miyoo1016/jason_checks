# CHECKS Terminal - Real-time Stock Trading TUI

A terminal-based UI clone of the CHECKS stock trading app, built with Textual and powered by KIS Open API.

## Phase 1: MVP - Real-time Price Streaming

### Current Features
- ✅ KIS Open API WebSocket connection (real-time execution ticks)
- ✅ Approval Key auto-issuance with caching (24h)
- ✅ Real-time price display (Samsung Electronics - 005930)
- ✅ Change % visualization (green/red)
- ✅ Connection status indicator
- ✅ Graceful shutdown

### Project Structure
```
jason_checks/
├── .env                    # API credentials (paper/live)
├── .gitignore
├── pyproject.toml          # Dependencies
├── README.md
├── data/                   # SQLite DB + cache files
└── src/jason_checks/
    ├── config.py           # Settings & URL routing
    ├── kis_auth.py         # Approval Key issuance
    ├── kis_ws.py           # WebSocket client
    ├── state.py            # Shared app state
    ├── tui_main.py         # Textual UI
    └── logging_config.py   # Logging setup
```

## Installation

### Prerequisites
- Python 3.11+
- KIS (Korea Investment & Securities) account with:
  - App Key & App Secret
  - 모의투자 (mock trading) account

### Setup

1. **Clone and navigate:**
   ```bash
   cd jason_checks
   ```

2. **Install dependencies:**
   ```bash
   pip install -e .
   ```

3. **Verify `.env` file:**
   ```bash
   cat .env
   # Should contain KIS_MODE, KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO
   ```

4. **Check .gitignore:**
   ```bash
   # .env should NOT be committed
   git status | grep .env
   ```

## Running Phase 1

### Test Config (dry run)
```bash
python -m jason_checks.config
# Output:
# KIS Mode: paper
# REST URL: https://openapivts.koreainvestment.com:29443
# WebSocket URL: ws://ops.koreainvestment.com:21000
# Account: 50183315-01
```

### Test Approval Key Issuance
```bash
python -m jason_checks.kis_auth
# Output:
# ✓ Approval Key (first 20): [key preview]...
```

### Run the TUI App
```bash
python -m jason_checks.tui_main
```

**Expected behavior:**
- Textual window opens
- "KIS Approval Key 발급 중..." → "🟢 WS:OK"
- Real-time price for Samsung (005930) updates every 1-2 seconds
- Press `r` to force refresh
- Press `q` to quit

## Key Bindings (Phase 1)

| Key | Action |
|---|---|
| `r` | Refresh display (force reconnect if stale) |
| `q` | Quit app |
| `m` | Memo (Phase 2) |

## Troubleshooting

### "WS:DISCONNECTED"
1. Check `.env` credentials
2. Verify 모의투자 (mock) account is active (KIS Developers portal)
3. Check internet connectivity

### No price updates
1. Verify "실시간 시세 이용약관" checkbox in KIS Developers portal is enabled
2. Check logs: `tail -f logs/app.log`

### "No approval_key in response"
1. Credentials in `.env` are incorrect
2. KIS server may be down

## Next Phases
- **Phase 2**: REST API for trading volume, theme strength
- **Phase 3**: Full 3-panel Textual layout (themes, leaders, stock list)
- **Phase 4**: Tick replay, preview mode, memo storage

## References
- [KIS Open API Docs](https://apiportal.koreainvestment.com/)
- [Textual Docs](https://textual.textualize.io/)
- [Python asyncio](https://docs.python.org/3/library/asyncio.html)
