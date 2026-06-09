#!/bin/bash
# Devnet Consensus Block Progress Monitor
# Queries all 5 devnet nodes individually
# Alerts if any node's commit_timestamp is older than 60 seconds

PUSH_URL="https://kuma.telscan.xyz/api/push/7BS3yBebzN"
THRESHOLD=60

NODES=(
    "node1|https://node1.devnet.telcoin.network"
    "node2|https://node2.devnet.telcoin.network"
    "node3|https://node3.devnet.telcoin.network"
    "node4|https://node4.devnet.telcoin.network"
    "node5|https://node5.devnet.telcoin.network"
)

NOW=$(date +%s)
STATUS="up"
DETAILS=""
STALE=""

for entry in "${NODES[@]}"; do
    NAME="${entry%%|*}"
    URL="${entry##*|}"

    RESULT=$(curl -s -X POST "$URL" \
      -H "Content-Type: application/json" \
      -d '{"jsonrpc":"2.0","method":"tn_latestConsensusHeader","params":[],"id":1}' \
      --connect-timeout 10 \
      | python3 -c "
import sys,json
r=json.load(sys.stdin)
result=r['result']
print(result['number'], result['sub_dag']['commit_timestamp'])
" 2>/dev/null)

    if [ -z "$RESULT" ]; then
        echo "$(date): $NAME - no response"
        STATUS="down"
        STALE="$STALE $NAME(no response)"
        continue
    fi

    BLOCK=$(echo $RESULT | cut -d' ' -f1)
    COMMIT_TS=$(echo $RESULT | cut -d' ' -f2)

    if [ "$BLOCK" -eq 0 ]; then
        echo "$(date): $NAME - stalled (block=0)"
        STATUS="down"
        STALE="$STALE $NAME(stalled)"
        continue
    fi

    ELAPSED=$(( NOW - COMMIT_TS ))
    echo "$(date): $NAME block=$BLOCK age=${ELAPSED}s"

    if [ $ELAPSED -gt $THRESHOLD ]; then
        STATUS="down"
        STALE="$STALE $NAME(${ELAPSED}s)"
        DETAILS="$DETAILS $NAME:STALE(${ELAPSED}s)"
    else
        DETAILS="$DETAILS $NAME:OK(${ELAPSED}s)"
    fi
done

if [ "$STATUS" == "down" ]; then
    MSG="Devnet consensus issue:$STALE |$DETAILS"
else
    MSG="Devnet all nodes OK —$DETAILS"
fi

echo "$(date): Status: $STATUS — $MSG"
curl -s "${PUSH_URL}?status=${STATUS}&msg=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$MSG'))")" > /dev/null
