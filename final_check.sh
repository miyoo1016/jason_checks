#!/bin/bash
echo "--- Absolute Final 3-Minute Verification ---"
for i in 0 1 2 3; do
  echo "Snapshot #$i at $(date +%H:%M:%S)"
  # Get top leaders and their prices
  curl -s http://localhost:8000/api/themes | jq '.themes."반도체".leaders[] | select(.code=="005930" or .code=="000660") | {name, price, timestamp}'
  # Save full state
  curl -s http://localhost:8000/api/themes | jq '.themes' > "/Users/miyoo1016/jason_checks/final_snap_$i.json"
  if [ $i -gt 0 ]; then
    if diff "/Users/miyoo1016/jason_checks/final_snap_$((i-1)).json" "/Users/miyoo1016/jason_checks/final_snap_$i.json" > /dev/null; then
      echo "ERROR: Snapshot #$i NO CHANGE!"
    else
      echo "SUCCESS: Snapshot #$i CHANGED!"
    fi
  fi
  sleep 60
done
