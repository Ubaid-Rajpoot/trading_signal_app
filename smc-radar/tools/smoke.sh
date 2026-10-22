#!/usr/bin/env bash
# End-to-end smoke test of the running app, no extra dependencies.
#
#   tools/smoke.sh                 # replay the recorded candles on port 8891
#   PORT=9000 tools/smoke.sh
#   DATA=/path/to/csv/folder tools/smoke.sh
#
# It starts the server itself, then checks that
#   1. the page is served,
#   2. the state advances on its own (no restart needed),
#   3. the candle endpoint serves a deep history,
#   4. the incremental refresh works,
#   5. server-sent events actually push,
#   6. the websocket actually pushes,
# and shuts everything down again.
set -u
cd "$(dirname "$0")/.." || exit 2

PORT="${PORT:-8891}"
DATA="${DATA:-data/btc}"
PY="${PY:-../.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
URL="http://127.0.0.1:$PORT"
FAILED=0

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAILED=$((FAILED + 1)); }
json() { "$PY" -c "import json,sys;d=json.load(sys.stdin);print($1)"; }

[ -d "$DATA" ] || { echo "no candle folder $DATA - run the download in docs/data.md first"; exit 2; }

"$PY" -u run.py replay "$DATA" 25 --port "$PORT" >/tmp/smc-smoke.log 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT

for _ in $(seq 1 40); do
  curl -sf "$URL/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done

curl -sf "$URL/" | grep -q "SMC radar" && pass "page served" || fail "page served"

A=$(curl -sf "$URL/api/state" | json "d['seq']")
sleep 6
B=$(curl -sf "$URL/api/state" | json "d['seq']")
[ "${A:-0}" -lt "${B:-0}" ] && pass "state advances by itself (seq $A -> $B)" || fail "state advances by itself (seq $A -> $B)"

CANDLES=$(curl -sf "$URL/api/candles?symbol=BTCUSDT&timeframe=5m&limit=1000" | json "len(d['bars'])")
[ "${CANDLES:-0}" -ge 200 ] && pass "candles served: $CANDLES" || fail "candles served: $CANDLES"

SINCE=$(curl -sf "$URL/api/candles?symbol=BTCUSDT&timeframe=5m&limit=1000" | json "d['bars'][-2]['open_time']")
INC=$(curl -sf "$URL/api/candles?symbol=BTCUSDT&timeframe=5m&limit=1000&since=$SINCE" | json "len(d['bars'])")
[ "${INC:-0}" -ge 1 ] && [ "${INC:-0}" -lt "${CANDLES:-0}" ] && pass "incremental refresh returns only the new bars ($INC)" || fail "incremental refresh ($INC of $CANDLES)"

SSE=$(timeout 8 curl -sN "$URL/api/stream" | grep -c "data: ")
[ "${SSE:-0}" -ge 1 ] && pass "server-sent events push ($SSE frames)" || fail "server-sent events push"

if "$PY" - <<'EOF' "$URL"
import asyncio, json, sys
import websockets

async def main(url: str) -> int:
    async with websockets.connect(url.replace("http", "ws") + "/ws") as ws:
        for _ in range(2):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") == "state" and msg.get("symbols"):
                return 0
    return 1

sys.exit(asyncio.run(main(sys.argv[1])))
EOF
then pass "websocket pushes"; else fail "websocket pushes"; fi

echo
if [ "$FAILED" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILED FAILED"; fi
exit "$FAILED"
