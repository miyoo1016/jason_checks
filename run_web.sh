#!/bin/bash
# CHECKS Terminal Web Server (Phase 2)

set -e

cd "$(dirname "$0")"

# Activate venv if not active
if [ -z "$VIRTUAL_ENV" ]; then
    source venv/bin/activate
fi

mkdir -p data logs

echo "🚀 CHECKS Terminal — Phase 2 (Web Server)"
echo "📍 http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop"
echo ""

python3 server.py
