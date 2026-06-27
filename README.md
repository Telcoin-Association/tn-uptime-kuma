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

## Config in version control (bidirectional GitOps)

The full live config — monitors, notification channels, and status pages — is
version-controlled and managed from one local script in **both** directions:

| Directory | What it holds |
|---|---|
| `monitors/<network>.json` | Monitors, grouped by name prefix (`testnet*`, `devnet*`, else `other`). Each monitor links to notification channels by **name** (`notificationNames`). |
| `notifications/<slug>.json` | One notification channel each. Secret fields (webhook URLs, tokens, passwords) are redacted to `${KUMA_NOTIF_<NAME>_<FIELD>}` placeholders. |
| `notifications/secrets.json` | The **real** secret values. Gitignored — lives only on the VM, never committed. |
| `status-pages/<slug>.json` | Status-page config; published monitors stored by **name** (`monitorNames`). |

Names — not Kuma's internal integer IDs — are the stable key, so the same JSON
applies cleanly across instances. **Don't hand-edit config only in the UI**: the
files are the source of truth. To capture GUI changes, run `--export` (below)
and commit the diff.

`scripts/kuma_config.py` is the single tool behind both directions
(`export` / `sync`); it runs on the VM against `localhost:3001` and is driven by
`deploy-updates.sh`. Push-monitor tokens are **secrets and are never committed**:
`sync` reads each existing monitor's live token straight from Kuma, and the real push
URL lives only in `targets/<network>.env.local` on the VM (see "Security model" below).
After a sync, `deploy-updates.sh` runs `kuma_config.py sync --verify-push`, which
cross-checks each live push monitor's token against its `targets/<network>.env.local`
`PUSH_URL` and prints a `✓`/`✗` per monitor.

Two caveats on "source of truth":

- **No prune.** `sync` only adds/updates; deleting a monitor from git does **not** delete
  it live. Git is the source of truth for *what should exist*, not for *what must not* —
  retire a monitor in the Kuma UI (then re-export to drop it from git).
- **Fresh instances need push tokens set by hand.** A re-created push monitor gets a
  brand-new server-assigned token, so the JSON is **not** fully portable for push
  monitors: after bootstrapping a new instance, read each push monitor's token from Kuma
  and write it into the matching `targets/<network>.env.local` `PUSH_URL` (`--verify-push`
  flags the mismatch until you do).

## Security model

No secret is committed to this repo. Each secret lives in exactly one place:

| Secret | Where it lives | How it reaches Kuma / the monitor |
|---|---|---|
| Kuma admin password | GCP Secret Manager (`KUMA_SECRET`, default `kuma-admin-password`) | `deploy-updates.sh` fetches it locally with `gcloud secrets versions access` and streams it to the VM over **stdin** — never on disk, argv, or the SSH command line |
| Notification secrets (Slack webhook, …) | `notifications/secrets.json` — **VM-only**, gitignored | re-injected into the `${KUMA_NOTIF_*}` placeholders by `kuma_config.py sync` |
| Consensus push URLs / tokens | `targets/<network>.env.local` — **VM-only**, gitignored | `consensus_monitor.sh` sources it after the committed `targets/<network>.env`, so its real `PUSH_URL` wins over the placeholder |

The committed `monitors/<network>.json` and `targets/<network>.env` carry **placeholders**,
not tokens. `deploy.config` (gitignored) holds the VM coordinates and the *name* of the
Secret Manager secret — not the password itself.

**Back up the VM-only secrets out of band.** `notifications/secrets.json` and the Kuma
database (`/app/data` — snapshotted into `backups/` before each sync) exist **only on the
VM**. A VM rebuild loses the Slack webhook and all monitor history unless you also copy
them somewhere durable (another Secret Manager secret, a private GCS bucket, etc.).

### One-time operator setup (before the first hardened deploy)

1. Create the Secret Manager secret and add the password (replace `<password>`); grant the
   operator and/or VM service account `roles/secretmanager.secretAccessor`; then delete the
   plaintext `KUMA_PASSWORD` line from your local `deploy.config`:
   ```
   gcloud secrets create kuma-admin-password --replication-policy=automatic --project=tn-devops-sandbox
   printf %s '<password>' | gcloud secrets versions add kuma-admin-password --data-file=- --project=tn-devops-sandbox
   ```
2. On the VM, create `targets/testnet.env.local` and `targets/devnet.env.local`, each with
   the real push URL: `PUSH_URL="https://kuma.telscan.xyz/api/push/<token>"`.
3. (Recommended) Rotate both push tokens in Kuma — the old ones remain in git history. Set
   the token on the push monitor (edit it via the API if your `uptime-kuma-api` build
   allows, else recreate the push monitor, which resets its heartbeat history), then update
   the two `targets/<network>.env.local` `PUSH_URL`s to match.

## Files

| File | Description |
|---|---|
| `docker-compose.yml` | Uptime Kuma + Caddy containers |
| `Caddyfile` | Caddy reverse proxy config |
| `consensus_monitor.sh` | Parametrized consensus health monitor (`<network>` arg) |
| `targets/testnet.env`, `targets/devnet.env` | Per-network consensus-monitor config (nodes, threshold, **placeholder** push URL) |
| `targets/<network>.env.local` | VM-only override with the real `PUSH_URL` (secret push token); gitignored |
| `scripts/requirements.txt` | Pinned `uptime-kuma-api` version (known-good against Kuma 2.3.2) |
| `monitors/<network>.json` | Kuma monitor definitions per network (notification links by name) |
| `notifications/<slug>.json` | Notification-channel definitions (secrets redacted to placeholders) |
| `status-pages/<slug>.json` | Status-page definitions (published monitors by name) |
| `scripts/kuma_config.py` | Bidirectional Kuma config tool (`export` / `sync`); runs on the VM |
| `consensus_block.sh`, `consensus_block_devnet.sh` | Deprecated shims → `consensus_monitor.sh testnet\|devnet` |

Dependency: `consensus_monitor.sh` requires `jq` (and `curl`) on the Kuma VM.

## Cron Jobs

Each network calls the one script with its arg; both run on the Kuma VM every minute:

    * * * * * /bin/bash /home/nick/tn-uptime-kuma/consensus_monitor.sh testnet >> /home/nick/consensus_testnet.log 2>&1
    * * * * * /bin/bash /home/nick/tn-uptime-kuma/consensus_monitor.sh devnet  >> /home/nick/consensus_devnet.log 2>&1

## Deployment

### One-command deploy (recommended)

From your **local** host (needs `gcloud` + creds for the VM project), `deploy-updates.sh`
pushes the whole repo to the VM and applies everything idempotently. Complete the one-time
operator setup under "Security model" first (Secret Manager secret + VM-only overrides):

    cp deploy.config.example deploy.config   # fill in VM coordinates + KUMA_SECRET (no password)
    ./deploy-updates.sh --dry-run            # preview config changes (no writes)
    ./deploy-updates.sh                      # deploy for real (git -> live)

It runs these safe-to-repeat steps:

1. **Sync files** — streams the working tree to `VM_REPO` (so the cron scripts update),
   excluding `.git`, `deploy.config`, Python bytecode, `backups/`, and the VM-only secret
   files (`notifications/secrets.json`, `targets/*.env.local`).
2. **Caddy** — copies `Caddyfile` + `docker-compose.yml` into the compose dir, runs
   `docker compose up -d`, and reloads Caddy.
3. **Crontab** — installs the managed `consensus_monitor.sh testnet|devnet` cron block.
   - **3.5 Backup** — on a real deploy, snapshots the live Kuma data dir (`/app/data` —
     the SQLite DB + uploaded icons) into `VM_REPO/backups/data-<timestamp>` (last 10 kept)
     so a bad sync can be rolled back.
4. **Config** — upserts the committed config into Uptime Kuma via the API
   (`scripts/kuma_config.py sync`) in dependency order: notifications → monitors
   (linked to notifications by name) → status pages. Matching is by name/slug; existing
   push-monitor tokens are read live and preserved. Then `--verify-push` cross-checks each
   live push token against `targets/<network>.env.local`.

`--dry-run` skips the backup (3.5) and makes step 4 read-only (it shows the changes and runs
`--verify-push` but writes nothing); steps 1–3 are idempotent and always run. The admin
password is fetched from Secret Manager at deploy time and streamed to the VM over stdin —
it is never written to disk or passed on the command line.

### Capturing live GUI changes back into git (`--export`)

Config built or tweaked by hand in the Kuma UI can be pulled back into the repo:

    ./deploy-updates.sh --export   # live -> local working tree

This runs `kuma_config.py export` on the VM and streams the captured
`monitors/`, `notifications/` (minus `secrets.json`), and `status-pages/` back
into your local tree. It does **not** touch the live instance. The real
notification secrets are written to `notifications/secrets.json` **on the VM
only** (gitignored, reused by `sync`) — they never reach git or your local disk.

The intended loop is **export → review → commit → deploy**:

    ./deploy-updates.sh --export   # capture live state
    git status && git diff         # review what changed
    git add -A && git commit ...   # commit the captured config
    ./deploy-updates.sh            # (later) push committed edits back

Right after an export, `./deploy-updates.sh --dry-run` should report only
`~ EDIT`/no-change — a spurious `+ ADD` means a name/slug didn't round-trip.

### Manual first-time setup

Prerequisites: GCP VM e2-small Ubuntu 22.04, Docker installed, DNS A records pointing to VM IP.

    mkdir ~/kuma && cd ~/kuma
    # Copy docker-compose.yml and Caddyfile
    docker compose up -d

## GCP Firewall Rules

| Rule | Project | Ports | Source | Target |
|---|---|---|---|---|
| allow-kuma-web | tn-devops-sandbox | tcp:80,443 | 0.0.0.0/0 | uptime-kuma tag |
| allow-validator-healthcheck-from-kuma | telcoin-network | tcp:43174 | Kuma VM IP/32 | validator tag |
