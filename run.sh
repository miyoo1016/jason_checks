#!/bin/bash
# CHECKS Terminal - Quick run script

set -e

cd "$(dirname "$0")"

# Activate venv if not active
if [ -z "$VIRTUAL_ENV" ]; then
    source venv/bin/activate
fi

mkdir -p data logs

echo "🚀 CHECKS Terminal starting..."
python3 tima.py
