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
Single push monitor fed by consensus_block.sh cron script.
Queries tn_latestConsensusHeader on all nodes every minute.
Checks commit_timestamp age on each node.
Alerts if any node timestamp is older than 60 seconds.
Reports per-node status in alert message.

## Files

| File | Description |
|---|---|
| `docker-compose.yml` | Uptime Kuma + Caddy containers |
| `Caddyfile` | Caddy reverse proxy config |
| `consensus_block.sh` | Cron script — consensus health monitor |

## Cron Jobs

All scripts run on the Kuma VM every minute:

    * * * * * /bin/bash /home/nick/consensus_block.sh >> /home/nick/consensus_block.log 2>&1

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
