#!/bin/bash
#
# consensus_monitor.sh <network> — consensus liveness push monitor for Uptime Kuma.
#
# Reports consensus UP/DOWN based on whether consensus is ACTUALLY ADVANCING across the
# fleet, NOT on whether every individual node's RPC happens to be reachable. A single
# node's RPC being down (or returning height 0) is the per-node RPC/TCP monitors' job to
# flag — this monitor stays UP as long as at least one node reports a fresh leader header.
#
# Network config lives in targets/<network>.env (PUSH_URL, NODES, THRESHOLD), so testnet
# and devnet share ONE script and can never cross-contaminate tokens/hostnames.
#
# Signals (from tn_latestConsensusHeader -> ConsensusHeader, serde snake_case defaults):
#   .number                      -> u64 consensus height (monotonic).
#   .sub_dag.headers[-1].created_at -> leader header creation time, UNIX SECONDS.
#   (commit_timestamp is a PRIVATE field on CommittedSubDag and is NOT serialized — the
#    old scripts read it, always got KeyError, and went DOWN on every poll.)
#
# Dependencies: bash, curl, jq. (jq replaces the brittle python KeyError-swallowing parse.)
#
# Usage:
#   ./consensus_monitor.sh devnet
#   ./consensus_monitor.sh testnet
#
set -uo pipefail

NETWORK="${1:-}"
if [ -z "${NETWORK}" ]; then
    echo "usage: $0 <network>  (e.g. devnet | testnet)" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/targets/${NETWORK}.env"
if [ ! -f "${ENV_FILE}" ]; then
    echo "$(date): no target config for network '${NETWORK}' (${ENV_FILE} not found)" >&2
    exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "$(date): jq not installed — required by consensus_monitor.sh" >&2
    exit 2
fi

# shellcheck source=/dev/null
source "${ENV_FILE}"   # committed config: NODES=(...), THRESHOLD, placeholder PUSH_URL
# Optional VM-only override carrying the REAL PUSH_URL (secret push token); gitignored,
# sourced AFTER the committed env so its PUSH_URL wins. See README "Security model".
# shellcheck source=/dev/null
[ -f "${ENV_FILE}.local" ] && source "${ENV_FILE}.local"

: "${PUSH_URL:?targets/${NETWORK}.env(.local) must set PUSH_URL}"
: "${THRESHOLD:=60}"
if [ "${#NODES[@]}" -eq 0 ]; then
    echo "$(date): targets/${NETWORK}.env defines no NODES" >&2
    exit 2
fi

NOW="$(date +%s)"
REACHABLE=0
MAX_NUMBER=0
MAX_CREATED_AT=0
DETAILS=""

for entry in "${NODES[@]}"; do
    NAME="${entry%%|*}"
    URL="${entry##*|}"

    # Parse height + leader (last) header created_at in one jq pass. Emits "<number> <created_at>"
    # only when both are present; any RPC/parse failure yields empty -> treated as unreachable.
    PARSED="$(curl -s -X POST "${URL}" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","method":"tn_latestConsensusHeader","params":[],"id":1}' \
        --connect-timeout 10 --max-time 15 2>/dev/null \
        | jq -r '.result | "\(.number) \(.sub_dag.headers[-1].created_at)"' 2>/dev/null)"

    NUMBER="${PARSED%% *}"
    CREATED_AT="${PARSED##* }"

    if [ -z "${PARSED}" ] || ! [[ "${NUMBER}" =~ ^[0-9]+$ ]] || ! [[ "${CREATED_AT}" =~ ^[0-9]+$ ]]; then
        echo "$(date): ${NAME} - rpc-unreachable"
        DETAILS="${DETAILS} ${NAME}:rpc-unreachable"
        continue
    fi

    if [ "${NUMBER}" -eq 0 ]; then
        # Reachable but reporting genesis/height 0 — counts as reachable but contributes
        # nothing to liveness; flagged so a true halt (all at 0) still goes DOWN below.
        echo "$(date): ${NAME} - height 0"
        REACHABLE=$((REACHABLE + 1))
        DETAILS="${DETAILS} ${NAME}:h=0"
        continue
    fi

    AGE=$((NOW - CREATED_AT))
    REACHABLE=$((REACHABLE + 1))
    [ "${NUMBER}" -gt "${MAX_NUMBER}" ] && MAX_NUMBER="${NUMBER}"
    [ "${CREATED_AT}" -gt "${MAX_CREATED_AT}" ] && MAX_CREATED_AT="${CREATED_AT}"
    echo "$(date): ${NAME} ok h=${NUMBER} age=${AGE}s"
    DETAILS="${DETAILS} ${NAME}:ok h=${NUMBER} age=${AGE}s"
done

# Decide status from FLEET-WIDE liveness, not per-node reachability.
STATUS="up"
REASON=""
if [ "${REACHABLE}" -eq 0 ]; then
    STATUS="down"; REASON="no nodes reachable"
elif [ "${MAX_NUMBER}" -eq 0 ]; then
    STATUS="down"; REASON="consensus halted (max height 0)"
else
    FRESH_AGE=$((NOW - MAX_CREATED_AT))
    if [ "${FRESH_AGE}" -gt "${THRESHOLD}" ]; then
        STATUS="down"; REASON="freshest leader header ${FRESH_AGE}s old (> ${THRESHOLD}s)"
    fi
fi

if [ "${STATUS}" = "up" ]; then
    MSG="${NETWORK} consensus advancing (max h=${MAX_NUMBER}, freshest=$((NOW - MAX_CREATED_AT))s) —${DETAILS}"
else
    MSG="${NETWORK} consensus DOWN: ${REASON} —${DETAILS}"
fi

echo "$(date): Status: ${STATUS} — ${MSG}"
ENC_MSG="$(jq -rn --arg m "${MSG}" '$m|@uri')"
curl -s "${PUSH_URL}?status=${STATUS}&msg=${ENC_MSG}" > /dev/null
