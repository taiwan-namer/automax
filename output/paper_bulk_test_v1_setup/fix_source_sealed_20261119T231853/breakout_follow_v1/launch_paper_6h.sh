#!/bin/zsh
# Detached paper launcher for breakout_follow_v1 (6h observation window)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p output/breakout_follow_v1
LOG=output/breakout_follow_v1/paper_stdout.log
echo "" >> "$LOG"
echo "--- launch $(date '+%Y-%m-%d %H:%M:%S %z') ---" >> "$LOG"
nohup python3 -u -m breakout_follow_v1.paper_main --paper --hours 6 >> "$LOG" 2>&1 &
PID=$!
echo "$PID" > output/breakout_follow_v1/paper.pid
echo "started PID=$PID"
sleep 3
if kill -0 "$PID" 2>/dev/null; then
  echo "alive"
  tail -8 "$LOG"
else
  echo "FAILED to stay up"
  tail -20 "$LOG"
  exit 1
fi
