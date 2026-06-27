#!/usr/bin/env python3
"""Bidirectional GitOps for an Uptime Kuma instance — one module, two directions.

    python3 kuma_config.py export [--network all]          # live  -> git (safe to commit)
    python3 kuma_config.py sync   [--network all] [--dry-run]  # git -> live (idempotent)

`export` pulls the complete live config (monitors, notifications, status pages)
into the repo, rewriting live integer IDs to stable *names* so the captured JSON
is portable across instances. Notification secrets (webhook URLs, tokens,
passwords) are redacted to `${KUMA_NOTIF_<NAME>_<FIELD>}` placeholders in the
committed JSON; the real values are written to a gitignored
`notifications/secrets.json`. Push-monitor tokens are secrets too (a token lets
anyone POST fake UP/DOWN) and are never written to the committed monitor JSON — the
real push URL lives only in `targets/<network>.env.local` on the VM.

`sync` pushes the committed config back, upserting by name/slug in dependency
order — notifications first, then monitors (linked to notifications by name),
then status pages (referencing monitors by name). Existing push monitors keep their
live token (the committed JSON carries none; use `sync --verify-push` to confirm it
matches `targets/<network>.env.local`). Secrets are re-injected from
`notifications/secrets.json` (or the environment) before notifications upsert.

Runs on the Kuma VM against http://localhost:3001. Credentials come from the
environment (KUMA_URL / KUMA_USERNAME / KUMA_PASSWORD) so they never hit argv.

Usage:
    KUMA_USERNAME=admin KUMA_PASSWORD=... \
        python3 kuma_config.py export
    KUMA_USERNAME=admin KUMA_PASSWORD=... \
        python3 kuma_config.py sync --network all [--dry-run]
"""
import argparse
import inspect
import json
import os
import re
import sys
from pathlib import Path

import requests
from uptime_kuma_api import UptimeKumaApi

# JSON "type" -> Kuma API type value, and the inverse for export.
TYPE_MAP = {"tcp-port": "port", "port": "port", "keyword": "keyword",
            "push": "push", "http": "http", "group": "group"}
TYPE_MAP_INV = {"port": "tcp-port"}  # API value -> preferred JSON value

REPO = Path(__file__).resolve().parent.parent
MONITORS_DIR = REPO / "monitors"
NOTIFICATIONS_DIR = REPO / "notifications"
STATUS_PAGES_DIR = REPO / "status-pages"
SECRETS_FILE = NOTIFICATIONS_DIR / "secrets.json"

# Monitor fields we round-trip (everything build_kwargs / Kuma understands).
MONITOR_FIELDS = (
    "interval", "retryInterval", "maxretries", "timeout", "description",
    "hostname", "port", "url", "keyword", "method", "body", "headers",
    "accepted_statuscodes",
)

# Notification base fields that are NOT provider config (dropped/handled apart).
NOTIF_BASE_DROP = ("id", "userId", "active")

# Substrings (case-insensitive) marking a notification config field as secret.
# Err toward redacting: an over-redacted field still round-trips via secrets.json,
# an under-redacted one leaks a credential into git.
SECRET_HINTS = (
    "password", "token", "secret", "apikey", "api_key", "accesskey",
    "privatekey", "webhook", "url", "userkey", "appkey", "serverkey",
    "authkey", "apptoken", "usertoken", "botid", "phonenumber", "sender",
    "credential", "auth",
)


def log(msg):
    print(msg, flush=True)


def slugify(name):
    """Filesystem-safe slug for a notification/page name used as a file stem."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "unnamed"


def is_secret(key):
    k = key.lower()
    return any(h in k for h in SECRET_HINTS)


# --------------------------------------------------------------------------- #
#  Connection + shared helpers
# --------------------------------------------------------------------------- #

def connect():
    """Login from KUMA_URL / KUMA_USERNAME / KUMA_PASSWORD env. Returns api."""
    url = os.environ.get("KUMA_URL", "http://localhost:3001")
    user = os.environ.get("KUMA_USERNAME")
    pw = os.environ.get("KUMA_PASSWORD")
    if not user or not pw:
        sys.exit("ERROR: set KUMA_USERNAME and KUMA_PASSWORD in the environment")
    log(f"Connecting to {url} ...")
    api = UptimeKumaApi(url)
    api.login(user, pw)
    return api


def call_stripping_unknown(fn, kwargs):
    """Call fn(**kwargs), dropping any single key the installed lib rejects, and retry.

    Makes the sync resilient to field-name differences across uptime_kuma_api
    versions instead of hard-failing on one unsupported key.
    """
    kwargs = dict(kwargs)
    while True:
        try:
            return fn(**kwargs)
        except (ValueError, TypeError) as e:
            removed = False
            for key in list(kwargs):
                if key not in ("type", "name", "slug") and key in str(e):
                    del kwargs[key]
                    removed = True
                    break
            if not removed:
                raise
            log(f"      (dropped unsupported field for this version: {e})")


def type_value(t):
    """Normalize a MonitorType enum (or str) to its lowercase string value."""
    return getattr(t, "value", None) or str(t)


def network_of(name):
    """Classify a (possibly root-group) name -> monitors/<network>.json stem."""
    low = (name or "").lower()
    if "testnet" in low:
        return "testnet"
    if "devnet" in low:
        return "devnet"
    return "other"


# --------------------------------------------------------------------------- #
#  EXPORT  (live -> git)
# --------------------------------------------------------------------------- #

def _notif_ids_to_names(nid, notif_id_to_name):
    """Normalize a monitor's notificationIDList (list[id] or {id: bool}) to names."""
    if isinstance(nid, dict):
        ids = [int(k) for k, v in nid.items() if v]
    else:
        ids = [int(x) for x in (nid or [])]
    return sorted(notif_id_to_name[i] for i in ids if i in notif_id_to_name)


def export_monitors(api, notif_id_to_name):
    """Group live monitors into monitors/<network>.json, IDs rewritten to names."""
    monitors = api.get_monitors()
    by_id = {m["id"]: m for m in monitors}

    def root_name(m):
        cur, seen = m, set()
        while cur.get("parent") in by_id and cur["id"] not in seen:
            seen.add(cur["id"])
            cur = by_id[cur["parent"]]
        return cur["name"]

    groups = {}
    for m in monitors:
        api_type = type_value(m.get("type"))
        entry = {
            "name": m["name"],
            "type": TYPE_MAP_INV.get(api_type, api_type),
        }
        parent = m.get("parent")
        if parent in by_id:
            entry["groupName"] = by_id[parent]["name"]
        for f in MONITOR_FIELDS:
            v = m.get(f)
            if v is not None and v != "":
                entry[f] = v
        # A live push monitor's pushToken is a SECRET (anyone holding it can POST fake
        # UP/DOWN and mask a real consensus halt) — deliberately NOT exported into the
        # committed JSON. sync_monitors carries the live token over for existing monitors,
        # so this omission is a no-op against the live instance; the real push URL is kept
        # only in targets/<network>.env.local on the VM.
        names = _notif_ids_to_names(m.get("notificationIDList"), notif_id_to_name)
        if names:
            entry["notificationNames"] = names
        # Network grouping follows the monitor's root group (testnet/devnet).
        groups.setdefault(network_of(root_name(m)), []).append(entry)

    MONITORS_DIR.mkdir(exist_ok=True)
    for net, entries in sorted(groups.items()):
        # Stable order: groups before their members, then by name.
        entries.sort(key=lambda e: (e["type"] != "group", e["name"]))
        path = MONITORS_DIR / f"{net}.json"
        path.write_text(json.dumps(
            {"version": "2.3.2", "notificationList": [], "monitorList": entries},
            indent=2,
        ) + "\n")
        log(f"  wrote {path.relative_to(REPO)} ({len(entries)} monitors)")


def export_notifications(api):
    """Write notifications/<slug>.json each, secrets split to secrets.json."""
    notifs = api.get_notifications()
    if not notifs:
        log("  (no notifications)")
        return
    NOTIFICATIONS_DIR.mkdir(exist_ok=True)
    secrets = {}
    for n in notifs:
        name = n["name"]
        cfg = {k: v for k, v in n.items() if k not in NOTIF_BASE_DROP}
        redacted = {}
        for k, v in cfg.items():
            if k in ("name", "type", "isDefault"):
                redacted[k] = v
            elif is_secret(k) and v not in (None, "", False):
                ph = f"KUMA_NOTIF_{slugify(name).replace('-', '_').upper()}_{k.upper()}"
                redacted[k] = "${%s}" % ph
                secrets.setdefault(name, {})[k] = v
            else:
                redacted[k] = v
        path = NOTIFICATIONS_DIR / f"{slugify(name)}.json"
        path.write_text(json.dumps(redacted, indent=2) + "\n")
        log(f"  wrote {path.relative_to(REPO)}"
            + (f" ({len(secrets.get(name, {}))} secret(s) redacted)" if name in secrets else ""))
    if secrets:
        SECRETS_FILE.write_text(json.dumps(secrets, indent=2) + "\n")
        log(f"  wrote {SECRETS_FILE.relative_to(REPO)} (gitignored — real secret values)")


# Status-page config fields that are volatile / instance-specific — not committed.
STATUS_PAGE_DROP = ("id",)


def fetch_status_page(api, slug):
    """Version-resilient status-page read (the lib's get_status_page assumes an
    older API shape and KeyErrors on Kuma 2.x). Returns (config, publicGroupList)."""
    config = api._call("getStatusPage", slug)["config"]
    http = requests.get(f"{api.url}/api/status-page/{slug}", timeout=api.timeout).json()
    return config, (http.get("publicGroupList") or [])


def save_status_page(api, slug, config_fields, public_group_list):
    """Version-resilient status-page write.

    Bypasses the lib's save_status_page / _build_status_page_data, which assume an
    older schema and drop fields the Kuma 2.x server requires (e.g. analyticsType).
    We merge our managed fields over the *live* raw config — which already carries
    every key the server validates — and post the saveStatusPage event directly:
    (slug, config, imgDataUrl, publicGroupList).
    """
    raw = api._call("getStatusPage", slug)["config"]
    config = {**raw, **config_fields}
    config.pop("publicGroupList", None)  # travels as its own positional arg
    icon = config.get("icon", "/icon.svg")
    return api._call("saveStatusPage", (slug, config, icon, public_group_list))


def export_status_pages(api):
    """Write status-pages/<slug>.json; published monitors stored by name."""
    pages = api.get_status_pages()
    if not pages:
        log("  (no status pages)")
        return
    STATUS_PAGES_DIR.mkdir(exist_ok=True)
    for summary in pages:
        slug = summary["slug"]
        config, pgl = fetch_status_page(api, slug)
        # Keep config-level fields; drop volatile/id-bearing ones.
        page = {k: v for k, v in config.items() if k not in STATUS_PAGE_DROP}
        page["slug"] = slug  # ensure slug is present for sync regardless of API shape
        groups = []
        for g in pgl:
            groups.append({
                "name": g.get("name"),
                "weight": g.get("weight", 0),
                "monitorNames": [
                    mon["name"] for mon in g.get("monitorList") or [] if mon.get("name")
                ],
            })
        page["groups"] = groups
        path = STATUS_PAGES_DIR / f"{slug}.json"
        path.write_text(json.dumps(page, indent=2) + "\n")
        log(f"  wrote {path.relative_to(REPO)} ({sum(len(g['monitorNames']) for g in groups)} monitors)")


def do_export(api):
    log("\n== Exporting notifications ==")
    # Need id->name map for monitor links; pull notifications first.
    notif_id_to_name = {n["id"]: n["name"] for n in api.get_notifications()}
    export_notifications(api)
    log("\n== Exporting monitors ==")
    export_monitors(api, notif_id_to_name)
    log("\n== Exporting status pages ==")
    export_status_pages(api)


# --------------------------------------------------------------------------- #
#  SYNC  (git -> live)
# --------------------------------------------------------------------------- #

SECRET_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def load_secrets():
    if SECRETS_FILE.exists():
        return json.loads(SECRETS_FILE.read_text())
    return {}


def resolve_secret(name, key, value, secrets):
    """Replace a ${KUMA_NOTIF_*} placeholder with its real value (secrets.json/env)."""
    if not isinstance(value, str):
        return value
    m = SECRET_RE.match(value)
    if not m:
        return value
    env_key = m.group(1)
    if name in secrets and key in secrets[name]:
        return secrets[name][key]
    if env_key in os.environ:
        return os.environ[env_key]
    raise SystemExit(
        f"ERROR: secret {env_key} for notification '{name}' not found in "
        f"{SECRETS_FILE.name} or environment")


def sync_notifications(api, secrets, dry_run):
    """Upsert notifications by name; return {name: id} for monitor linking."""
    files = sorted(NOTIFICATIONS_DIR.glob("*.json")) if NOTIFICATIONS_DIR.exists() else []
    files = [f for f in files if f.name != "secrets.json" and not f.name.startswith(".")]
    existing = {n["name"]: n for n in api.get_notifications()}
    name_to_id = {n["name"]: n["id"] for n in existing.values()}
    if not files:
        log("  (no notifications/ files)")
        return name_to_id
    log(f"\n== notifications: {len(files)} files ==")
    for path in files:
        data = json.loads(path.read_text())
        name = data["name"]
        kw = {k: resolve_secret(name, k, v, secrets) for k, v in data.items()}
        cur = existing.get(name)
        if cur is None:
            log(f"  + ADD  notification {name}")
            if not dry_run:
                res = call_stripping_unknown(api.add_notification, kw)
                # Refresh id map from the live list after the add.
                name_to_id = {n["name"]: n["id"] for n in api.get_notifications()}
        else:
            log(f"  ~ EDIT notification {name} (id={cur['id']})")
            if not dry_run:
                call_stripping_unknown(
                    lambda **k: api.edit_notification(cur["id"], **k), kw)
                name_to_id[name] = cur["id"]
    return name_to_id


def build_monitor_kwargs(m, notif_name_to_id, mon_name_to_id):
    """Translate one JSON monitor entry into add/edit kwargs.

    Only managed fields are emitted; edit_monitor merges these over the live
    monitor, so unmanaged fields (weight, ordering, etc.) are preserved.
    """
    mtype = TYPE_MAP.get(m["type"], m["type"])
    kw = {
        "type": mtype,
        "name": m["name"],
        "interval": m.get("interval", 60),
        "retryInterval": m.get("retryInterval", 60),
        "maxretries": m.get("maxretries", 1),
    }
    for opt in ("description", "timeout"):
        if opt in m:
            kw[opt] = m[opt]
    if mtype == "port":
        kw["hostname"] = m["hostname"]
        kw["port"] = m["port"]
    elif mtype in ("keyword", "http"):
        kw.update(
            url=m["url"],
            method=m.get("method", "GET"),
            body=m.get("body"),
            headers=m.get("headers"),
            accepted_statuscodes=m.get("accepted_statuscodes", ["200-299"]),
        )
        if mtype == "keyword":
            kw["keyword"] = m["keyword"]
    elif mtype == "push" and m.get("pushToken"):
        kw["pushToken"] = m["pushToken"]
    # group: no extra connection fields.

    # Link to a parent group by name.
    gname = m.get("groupName")
    if gname:
        pid = mon_name_to_id.get(gname)
        if pid is None:
            log(f"      ! parent group '{gname}' for monitor '{m['name']}' "
                f"not found live yet — leaving ungrouped")
        else:
            kw["parent"] = pid
    # Resolve notification names -> live ids -> list[id] (Kuma's wire format).
    names = m.get("notificationNames") or []
    link = []
    for n in names:
        nid = notif_name_to_id.get(n)
        if nid is None:
            log(f"      ! notification '{n}' for monitor '{m['name']}' "
                f"not found live — skipping link")
        else:
            link.append(nid)
    if names:
        kw["notificationIDList"] = link
    return {k: v for k, v in kw.items() if v is not None}


def sync_monitors(api, files, notif_name_to_id, dry_run):
    existing = {m["name"]: m for m in api.get_monitors()}
    mon_name_to_id = {name: m["id"] for name, m in existing.items()}
    log(f"Found {len(existing)} existing monitors."
        + (" [DRY RUN]" if dry_run else ""))

    # Gather all entries across files, group monitors first so children can
    # resolve their parent group's id on the same run.
    entries = []
    for path in files:
        data = json.loads(path.read_text())
        for m in data.get("monitorList", []):
            entries.append((path.name, m))
    entries.sort(key=lambda pm: pm[1].get("type") != "group")

    for fname, m in entries:
        name = m["name"]
        kw = build_monitor_kwargs(m, notif_name_to_id, mon_name_to_id)
        cur = existing.get(name)
        if cur is None:
            log(f"  + ADD  [{fname}] {name}")
            if not dry_run:
                res = call_stripping_unknown(api.add_monitor, kw)
                # Make the new id resolvable as a parent for later children.
                new_id = (res or {}).get("monitorID")
                if new_id:
                    mon_name_to_id[name] = new_id
            continue
        # Push tokens are secrets and are intentionally absent from committed JSON (see
        # export_monitors). Always carry the LIVE token over so the monitor keeps its
        # endpoint; never log the token itself. Use `--verify-push` to cross-check it
        # against targets/<network>.env.local.
        if kw.get("type") == "push":
            live = cur.get("pushToken")
            if live:
                kw["pushToken"] = live
        log(f"  ~ EDIT [{fname}] {name} (id={cur['id']})")
        if not dry_run:
            call_stripping_unknown(
                lambda **k: api.edit_monitor(cur["id"], **k), kw)


def sync_status_pages(api, dry_run):
    files = sorted(STATUS_PAGES_DIR.glob("*.json")) if STATUS_PAGES_DIR.exists() else []
    files = [f for f in files if not f.name.startswith(".")]
    if not files:
        log("  (no status-pages/ files)")
        return
    existing_slugs = {p["slug"] for p in api.get_status_pages()}
    mon_name_to_id = {m["name"]: m["id"] for m in api.get_monitors()}
    log(f"\n== status pages: {len(files)} files ==")
    for path in files:
        page = json.loads(path.read_text())
        slug = page["slug"]
        groups = page.pop("groups", [])
        public_group_list = []
        for g in groups:
            mlist = []
            for n in g.get("monitorNames", []):
                mid = mon_name_to_id.get(n)
                if mid is None:
                    log(f"      ! monitor '{n}' for page '{slug}' not found live — skipping")
                else:
                    mlist.append({"id": mid})
            public_group_list.append({
                "name": g.get("name", "Services"),
                "weight": g.get("weight", 0),
                "monitorList": mlist,
            })
        if slug not in existing_slugs:
            log(f"  + ADD  status page {slug}")
            if not dry_run:
                api.add_status_page(slug, page.get("title", slug))
        else:
            log(f"  ~ EDIT status page {slug}")
        if not dry_run:
            config_fields = {k: v for k, v in page.items() if k != "slug"}
            try:
                save_status_page(api, slug, config_fields, public_group_list)
            except Exception as e:  # noqa: BLE001 — one bad page shouldn't abort the rest
                log(f"  ! FAILED saving status page {slug}: {e}")


def do_sync(api, network, dry_run):
    secrets = load_secrets()
    notif_name_to_id = sync_notifications(api, secrets, dry_run)

    if network == "all":
        files = sorted(MONITORS_DIR.glob("*.json"))
    else:
        files = [MONITORS_DIR / f"{network}.json"]
    files = [f for f in files if f.exists() and not f.name.startswith(".")]
    if not files:
        sys.exit(f"ERROR: no monitor JSON found for network={network}")
    sync_monitors(api, files, notif_name_to_id, dry_run)

    sync_status_pages(api, dry_run)


# --------------------------------------------------------------------------- #
#  VERIFY  (push-token round-trip: live monitor vs targets/<net>.env.local)
# --------------------------------------------------------------------------- #

TARGETS_DIR = REPO / "targets"
_PUSH_URL_RE = re.compile(r"""^\s*PUSH_URL=["']?([^"'\s#]+)""")


def _mask(tok):
    """Mask a secret token for logs: keep the first 2 and last 2 chars."""
    if not tok:
        return "(empty)"
    if len(tok) <= 4:
        return "*" * len(tok)
    return f"{tok[:2]}{'*' * (len(tok) - 4)}{tok[-2:]}"


def _target_push_token(network):
    """Push token (last path segment of PUSH_URL) for a network.

    Prefers targets/<network>.env.local (the real, VM-only override) and falls back to
    the committed targets/<network>.env (a placeholder). Returns (token, source_filename),
    or (None, None) if no PUSH_URL line is found.
    """
    for fname in (f"{network}.env.local", f"{network}.env"):
        path = TARGETS_DIR / fname
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            m = _PUSH_URL_RE.match(line)
            if m:
                token = m.group(1).rstrip("/").rsplit("/", 1)[-1]
                return token, fname
    return None, None


def verify_push_tokens(api):
    """Cross-check each live push monitor's token against its targets/<net>.env(.local).

    Catches fresh-instance / rotation drift: a re-created push monitor gets a NEW
    server-assigned token, so consensus_monitor.sh's PUSH_URL would silently point at a
    dead endpoint. Read-only; logs MATCH / MISMATCH / unset per monitor and returns True
    only if every live push monitor matched its override.
    """
    monitors = api.get_monitors()
    by_id = {m["id"]: m for m in monitors}

    def root_name(m):
        cur, seen = m, set()
        while cur.get("parent") in by_id and cur["id"] not in seen:
            seen.add(cur["id"])
            cur = by_id[cur["parent"]]
        return cur["name"]

    push = [m for m in monitors if type_value(m.get("type")) == "push"]
    if not push:
        log("  (no live push monitors to verify)")
        return True
    all_ok = True
    for m in push:
        net = network_of(root_name(m))
        live = m.get("pushToken") or ""
        token, src = _target_push_token(net)
        name = m["name"]
        if token is None:
            all_ok = False
            log(f"  ? {name} [{net}]: live token {_mask(live)} but no PUSH_URL found in "
                f"targets/{net}.env.local — create that override on the VM")
        elif token == live:
            log(f"  ✓ {name} [{net}]: live token matches {src}")
        else:
            all_ok = False
            note = " placeholder" if src.endswith(".env") else ""
            log(f"  ✗ {name} [{net}]: MISMATCH — live={_mask(live)} "
                f"{src}{note}={_mask(token)}; update targets/{net}.env.local PUSH_URL on the VM")
    return all_ok


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="pull live config into git")
    p_exp.add_argument("--network", default="all", help="(monitors are always grouped by name)")

    p_syn = sub.add_parser("sync", help="push committed config to the live instance")
    p_syn.add_argument("--network", default="all", help="testnet | devnet | all (default: all)")
    p_syn.add_argument("--dry-run", action="store_true",
                       help="show what would change without writing")
    p_syn.add_argument("--verify-push", action="store_true",
                       help="after sync, cross-check each live push monitor's token "
                            "against targets/<network>.env(.local) PUSH_URL")

    args = ap.parse_args()

    api = connect()
    try:
        if args.cmd == "export":
            do_export(api)
            log("\nExport done. Review the working tree, then commit.")
        else:
            do_sync(api, args.network, args.dry_run)
            if args.verify_push:
                log("\n== verifying push tokens (live monitor vs targets/<net>.env.local) ==")
                verify_push_tokens(api)
            log("\nSync done." + (" (no changes written — dry run)" if args.dry_run else ""))
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
