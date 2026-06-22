#!/bin/bash
# DEPRECATED shim — kept so existing cron entries keep working.
# Real logic now lives in consensus_monitor.sh + targets/devnet.env.
exec "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/consensus_monitor.sh" devnet
