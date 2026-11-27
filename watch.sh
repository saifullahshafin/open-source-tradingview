#!/bin/bash
# Monitor TradeX server for 10 minutes. Robust to being invoked from anywhere.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
LOG="/tmp/tradex_watch.log"
PORT=9500
ADDR="192.168.29.226"
START=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
echo "=== TradeX watch started $START from $SCRIPT_DIR ===" | tee "$LOG"

launch() {
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") LAUNCHING from $PWD" | tee -a "$LOG"
  nohup .venv/bin/python -u hub/server.py >/tmp/tradex.log 2>&1 &
  echo "$!" > /tmp/tradex.pid
  echo "  launched PID=$! (parent $$)" | tee -a "$LOG"
}

check() {
  local pid=$(cat /tmp/tradex.pid 2>/dev/null || true)
  local alive="no"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    alive="yes(pid=$pid)"
  fi
  local http_local=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:${PORT}/hub.html" 2>/dev/null || echo "000")
  local http_lan=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${ADDR}:${PORT}/hub.html" 2>/dev/null || echo "000")
  local ws=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${ADDR}:9501/api/health" 2>/dev/null || echo "000")
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") alive=$alive http_local=$http_local http_lan=$http_lan ws=$ws" | tee -a "$LOG"
  if ss -tlnp 2>/dev/null | grep -E "${PORT}|9501" >> "$LOG" 2>&1; then
    :
  else
    echo "  (no 9500/9501 sockets)" | tee -a "$LOG"
  fi
  if [ -f /tmp/tradex.log ]; then
    local tail
    tail=$(tail -5 /tmp/tradex.log 2>/dev/null | sed 's/^/  log: /') || true
    if [ -n "$tail" ]; then echo "$tail" | tee -a "$LOG"; fi
  fi
  if [ "$http_local" = "200" ] && [ "$http_lan" = "200" ]; then
    return 0
  else
    return 1
  fi
}

launch
sleep 12
if check; then
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") INITIAL OK" | tee -a "$LOG"
else
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") INITIAL FAIL" | tee -a "$LOG"
fi

END=$((SECONDS + 600))
while [ $SECONDS -lt $END ]; do
  sleep 30
  if check; then
    :
  else
    echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") DOWN -> relaunching" | tee -a "$LOG"
    if [ -f /tmp/tradex.log ]; then
      echo "  last log:" | tee -a "$LOG"
      tail -15 /tmp/tradex.log 2>/dev/null | sed 's/^/    /' | tee -a "$LOG" || true
    fi
    pkill -9 -f "hub/server.py" 2>/dev/null || true
    sleep 2
    launch
    sleep 12
    if check; then
      echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") RECOVERED after relaunch" | tee -a "$LOG"
    else
      echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") STILL DOWN after relaunch" | tee -a "$LOG"
    fi
  fi
done

echo "=== TradeX watch ended $(date -u +"%Y-%m-%dT%H:%M:%SZ") ===" | tee -a "$LOG"
echo "Final state:" | tee -a "$LOG"
check || true
echo "Full log: $LOG" | tee -a "$LOG"
