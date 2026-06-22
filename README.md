# tn-uptime-kuma

Uptime Kuma monitoring setup for the Telcoin Adiri Testnet validator infrastructure, including Docker deployment, Caddy reverse proxy config, and consensus health monitoring scripts.

## Infrastructure

- **Uptime Kuma:** v2.3.2
- **Reverse Proxy:** Caddy v2 (auto SSL via Let's Encrypt)
- **VM:** GCP e2-small Ubuntu 22.04

## Monitor Groups

### 🟢 Validators
TCP port checks on port 43174 (node healthcheck listener) for all 5 validators.
Poll interval: 60 seconds.
Success: TCP connection accepted — node process is running.

### 🔵 RPC Nodes
HTTP and keyword monitors for each node RPC endpoint.
POSTs eth_blockNumber JSON-RPC call every 60 seconds.
RPC Up/Down: success = HTTP 200.
Block Height: success = response contains "result" keyword.

### ⛓ Consensus
Single push monitor per network, fed by the `consensus_monitor.sh <network>` cron script.
Queries `tn_latestConsensusHeader` on all nodes every minute and reports UP/DOWN based on
whether **consensus is advancing**, not on whether every node's RPC is reachable:

- Parses `.number` (consensus height) and `.sub_dag.headers[-1].created_at` (leader header
  creation time, unix seconds) via `jq`. (The old scripts read `sub_dag.commit_timestamp`,
  a **private, never-serialized** field — they hit a swallowed `KeyError` and went DOWN on
  every poll. That bug is fixed here.)
- **DOWN** only if zero nodes are reachable, the max height is 0, or the freshest leader
  header is older than `THRESHOLD` (60s) — i.e. consensus has genuinely halted.
- **UP** otherwise. A single node's RPC being down does **not** flip the monitor — that is
  the per-node RPC / TCP monitors' job. The message always lists per-node detail
  (`nodeN: ok h=<height> age=<s>s` / `nodeN:rpc-unreachable`).

## Monitor definitions (version-controlled)

The TCP-43174, HTTP-RPC, and consensus-push monitors are defined declaratively under
`monitors/<network>.json` in Uptime Kuma's backup/import format. **Each network has its
own file; never edit monitors only in the UI** — the file is the source of truth and is
what keeps testnet and devnet targets from cross-contaminating.

Import via **Settings → Backup → Import** (choose the network's file; "Keep" existing
monitors so you only add this network's set), or via the Kuma API. After import, confirm
the consensus push monitor's token matches the corresponding `targets/<network>.env`
`PUSH_URL`.

## Files

| File | Description |
|---|---|
| `docker-compose.yml` | Uptime Kuma + Caddy containers |
| `Caddyfile` | Caddy reverse proxy config |
| `consensus_monitor.sh` | Parametrized consensus health monitor (`<network>` arg) |
| `targets/testnet.env`, `targets/devnet.env` | Per-network config (push token, nodes, threshold) |
| `monitors/testnet.json`, `monitors/devnet.json` | Importable Kuma monitor definitions per network |
| `consensus_block.sh`, `consensus_block_devnet.sh` | Deprecated shims → `consensus_monitor.sh testnet\|devnet` |

Dependency: `consensus_monitor.sh` requires `jq` (and `curl`) on the Kuma VM.

## Cron Jobs

Each network calls the one script with its arg; both run on the Kuma VM every minute:

    * * * * * /bin/bash /home/nick/tn-uptime-kuma/consensus_monitor.sh testnet >> /home/nick/consensus_testnet.log 2>&1
    * * * * * /bin/bash /home/nick/tn-uptime-kuma/consensus_monitor.sh devnet  >> /home/nick/consensus_devnet.log 2>&1

## Deployment

Prerequisites: GCP VM e2-small Ubuntu 22.04, Docker installed, DNS A records pointing to VM IP.

    mkdir ~/kuma && cd ~/kuma
    # Copy docker-compose.yml and Caddyfile
    docker compose up -d

## GCP Firewall Rules

| Rule | Project | Ports | Source | Target |
|---|---|---|---|---|
| allow-kuma-web | tn-devops-sandbox | tcp:80,443 | 0.0.0.0/0 | uptime-kuma tag |
| allow-validator-healthcheck-from-kuma | telcoin-network | tcp:43174 | Kuma VM IP/32 | validator tag |
