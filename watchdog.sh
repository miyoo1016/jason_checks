#!/bin/bash
while true; do
  uv run python3 server.py
  echo "Server crashed! Restarting in 1s..."
  sleep 1
done
