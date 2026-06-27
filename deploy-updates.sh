#!/usr/bin/env bash
#
# deploy-updates.sh — push this repo to the Kuma VM and apply everything, idempotently.
#
# Run from your local host (needs gcloud + the GCP creds for the VM project):
#
#     ./deploy-updates.sh            # full deploy  (git -> live)
#     ./deploy-updates.sh --dry-run  # show monitor changes without writing them
#     ./deploy-updates.sh --export   # pull live config into git (live -> local tree)
#
# The Kuma admin password is read from GCP Secret Manager (KUMA_SECRET in deploy.config)
# at deploy time and streamed to the VM over stdin — it never lands on disk, on argv, or
# in the command string. See the README "Security model" section.
#
# Deploy steps (each safe to re-run):
#   1.  Stream the working tree to VM_REPO (excludes .git / deploy.config / VM-only secrets).
#   2.  Copy Caddyfile + docker-compose.yml into the compose dir; `up -d`; reload Caddy.
#   3.  Install the managed crontab (consensus_monitor.sh testnet|devnet).
#   3.5 Snapshot the live Kuma data dir into VM_REPO/backups (real deploys only) for rollback.
#   4.  Sync committed config into Uptime Kuma via the API (notifications, monitors,
#       status pages — upsert by name/slug), then verify push tokens.
#
# --export instead runs the reverse: it captures the live GUI config back into the
# local working tree (monitors/, notifications/ minus secrets.json, status-pages/)
# for review and commit. It does NOT modify the live instance.
#
set -euo pipefail

cd "$(dirname "$0")"
MODE="deploy"
DRY_RUN=""
case "${1:-}" in
  --dry-run) DRY_RUN="--dry-run" ;;
  --export)  MODE="export" ;;
  "")        ;;
  *) echo "ERROR: unknown argument '$1' (use --dry-run or --export)"; exit 1 ;;
esac

# --- config ---
[[ -f deploy.config ]] || { echo "ERROR: deploy.config missing — copy deploy.config.example"; exit 1; }
# shellcheck disable=SC1091
source deploy.config
: "${GCP_PROJECT:?}" "${GCP_ZONE:?}" "${VM_INSTANCE:?}" "${VM_USER:?}" "${VM_REPO:?}" "${VM_COMPOSE_DIR:?}"
: "${KUMA_URL:?}" "${KUMA_USERNAME:?}" "${KUMA_SECRET:?}"

SSH=(gcloud compute ssh "${VM_USER}@${VM_INSTANCE}" --project "$GCP_PROJECT" --zone "$GCP_ZONE")
remote() { "${SSH[@]}" --command="$1"; }

# Fetch the admin password locally from Secret Manager. It stays in this shell variable
# (never written to disk) and is delivered to the VM over stdin by run_kuma below.
echo "==> Fetching Kuma admin password from Secret Manager (secret: ${KUMA_SECRET})"
KUMA_PASSWORD="$(gcloud secrets versions access latest --secret="$KUMA_SECRET" --project="$GCP_PROJECT")" \
  || { echo "ERROR: cannot read secret '$KUMA_SECRET' in project '$GCP_PROJECT' (need roles/secretmanager.secretAccessor)"; exit 1; }
[[ -n "$KUMA_PASSWORD" ]] || { echo "ERROR: Secret Manager secret '$KUMA_SECRET' is empty"; exit 1; }

# Run kuma_config.py on the VM with the admin password delivered over stdin — never on
# argv or in the command string. Only the non-secret KUMA_URL/KUMA_USERNAME are
# interpolated; the password is read into the remote environment, where kuma_config.py
# already reads it from os.environ. Installs the pinned API lib if it is missing.
run_kuma() {  # $1 = kuma_config.py arguments
  printf '%s\n' "$KUMA_PASSWORD" | "${SSH[@]}" --command="
    set -e
    cd '$VM_REPO'
    export KUMA_URL='$KUMA_URL' KUMA_USERNAME='$KUMA_USERNAME'
    IFS= read -r KUMA_PASSWORD; export KUMA_PASSWORD
    python3 -c 'import uptime_kuma_api' 2>/dev/null || pip3 install --quiet -r '$VM_REPO/scripts/requirements.txt'
    python3 '$VM_REPO/scripts/kuma_config.py' $1
  "
}

# ----------------------------------------------------------------------------- #
#  --export : live -> local working tree
# ----------------------------------------------------------------------------- #
if [[ "$MODE" == "export" ]]; then
  echo "==> [export] Capturing live Uptime Kuma config from ${VM_INSTANCE}"
  # 1. Sync the tool + its pinned deps up first so the VM runs the current kuma_config.py.
  remote "mkdir -p '$VM_REPO/scripts'"
  COPYFILE_DISABLE=1 tar czf - scripts/kuma_config.py scripts/requirements.txt \
    | remote "tar xzf - -C '$VM_REPO' && chmod +x '$VM_REPO'/scripts/*.py"
  # 2. Run the export on the VM (password via stdin; never on argv).
  run_kuma "export"
  # 3. Stream the captured config back into the LOCAL tree (secrets stay on the VM).
  echo "==> [export] Pulling captured config into the local working tree"
  remote "cd '$VM_REPO' && \
          dirs=; for d in monitors notifications status-pages; do [ -d \"\$d\" ] && dirs=\"\$dirs \$d\"; done; \
          tar czf - --exclude='notifications/secrets.json' \$dirs" | tar xzf -
  echo "==> [export] Done. Review with 'git status' / 'git diff', then commit."
  echo "    (notifications/secrets.json stays on the VM and is gitignored locally.)"
  exit 0
fi

echo "==> [1/4] Syncing working tree to ${VM_INSTANCE}:${VM_REPO}"
remote "mkdir -p '$VM_REPO'"
# Stream the working tree to the VM via tar. Excludes .git, the local deploy.config, the
# scratchpad, macOS AppleDouble (._*) sidecars, Python bytecode, pre-sync backups, and the
# VM-only secret files (secrets.json / targets/*.env.local) so a deploy can never clobber
# or leak them. COPYFILE_DISABLE stops macOS tar from injecting ._* sidecar files.
COPYFILE_DISABLE=1 tar czf - \
    --exclude='.git' \
    --exclude='deploy.config' \
    --exclude='./scratchpad' \
    --exclude='._*' \
    --exclude='*.pyc' \
    --exclude='*__pycache__*' \
    --exclude='*.env.local' \
    --exclude='notifications/secrets.json' \
    --exclude='./backups' \
    . | remote "tar xzf - -C '$VM_REPO' && chmod +x '$VM_REPO'/*.sh '$VM_REPO'/scripts/*.py"

echo "==> [2/4] Updating Caddy + compose"
remote "
  set -e
  cp '$VM_REPO/Caddyfile' '$VM_COMPOSE_DIR/Caddyfile'
  cp '$VM_REPO/docker-compose.yml' '$VM_COMPOSE_DIR/docker-compose.yml'
  cd '$VM_COMPOSE_DIR'
  docker compose up -d
  docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
"

echo "==> [3/4] Installing managed crontab"
# Replace only our managed block (between markers); leave any other cron lines intact.
remote "
  set -e
  BEGIN='# >>> tn-uptime-kuma managed >>>'
  END='# <<< tn-uptime-kuma managed <<<'
  ( crontab -l 2>/dev/null | sed \"/\$BEGIN/,/\$END/d\";
    echo \"\$BEGIN\";
    echo '* * * * * /bin/bash $VM_REPO/consensus_monitor.sh testnet >> /home/$VM_USER/consensus_testnet.log 2>&1';
    echo '* * * * * /bin/bash $VM_REPO/consensus_monitor.sh devnet  >> /home/$VM_USER/consensus_devnet.log 2>&1';
    echo \"\$END\";
  ) | crontab -
  echo 'crontab now:'; crontab -l | sed -n \"/\$BEGIN/,/\$END/p\"
"

# Pre-sync safety net: snapshot the whole Kuma data dir (SQLite kuma.db + WAL + uploaded
# icons) so a bad sync can be rolled back. Real deploys only — skipped on --dry-run. A
# failed backup aborts before any write (set -e), so we never sync without a rollback point.
if [[ -z "$DRY_RUN" ]]; then
  echo "==> [3.5/4] Snapshotting live Kuma data dir before sync"
  remote "
    set -e
    mkdir -p '$VM_REPO/backups'
    ts=\$(date +%Y%m%d-%H%M%S)
    docker cp uptime-kuma:/app/data \"$VM_REPO/backups/data-\$ts\"
    echo \"  backup written: $VM_REPO/backups/data-\$ts\"
    ls -1dt '$VM_REPO/backups'/data-* | tail -n +11 | xargs -r rm -rf   # keep last 10
  "
fi

echo "==> [4/4] Syncing config into Uptime Kuma ${DRY_RUN:+(dry run)}"
# Notifications -> monitors -> status pages (upsert by name/slug), then cross-check each
# live push monitor's token against targets/<net>.env.local. Password travels via stdin.
run_kuma "sync --network all --verify-push $DRY_RUN"

echo "==> Done."
