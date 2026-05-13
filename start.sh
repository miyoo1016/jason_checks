#!/bin/bash
# CHECKS Terminal - Unified startup script

set -e

cd "$(dirname "$0")"

# Ensure data and logs directories exist
mkdir -p data logs

echo "🚀 Starting CHECKS Terminal (Web) via uv..."
# Kill any existing processes on port 8000
kill $(lsof -t -i:8000) 2>/dev/null || true

# Run the server using uv
uv run python3 server.py
