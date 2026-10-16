#!/usr/bin/env bash
# FileSplitter upgrade helper — runs as root on the HOST (never in a container), and
# is the only thing that ever runs ./update.sh on behalf of the in-app "Upgrade" button.
#
# Usage:
#   scripts/upgrade-agent.sh                 Plain run (what cron/systemd invokes)
#   scripts/upgrade-agent.sh --install [--every N]   Schedule it (default every 15 min)
#   scripts/upgrade-agent.sh --print          Print the cron/systemd config, change nothing
#   scripts/upgrade-agent.sh --uninstall      Remove the schedule
#
# Everything this script finds in the shared upgrade folder (request.json) is treated
# as fully untrusted input: fields are regex-validated, parsed with sed/read (never
# eval, never sourced), and symlinks are refused outright. Only a validated version
# string is ever passed to ./update.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_DIR_DEFAULT/upgrade-agent.env"
NAME="filesplitter-upgrade-agent"
ALLOWED_INTERVALS=(15 30 60 180 360 720 1440)

# ---- helpers ----

_now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

_write_file_atomic() {
    # _write_file_atomic <final_path> <content>
    local final="$1" content="$2" tmp
    tmp="$(dirname "$final")/.tmp-$$-$RANDOM"
    printf '%s' "$content" >"$tmp"
    mv -f "$tmp" "$final"
}

_json_escape() {
    # Minimal JSON string escaping for the fixed fields we emit ourselves.
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

_usage() {
    cat >&2 <<EOF
Usage: $0 [--install [--every N] | --print | --uninstall]
  (no args)            Run once (what cron/systemd invokes)
  --install [--every N]  Schedule this script (default N=15 minutes)
  --print              Print the cron/systemd config without installing it
  --uninstall          Remove the schedule
EOF
    exit 1
}

_load_env() {
    REPO_DIR="$REPO_DIR_DEFAULT"
    UPGRADE_DIR="$REPO_DIR_DEFAULT/upgrade"
    EVERY_MINUTES=15
    APP_UPGRADE_ARGS=""
    if [[ -f "$ENV_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$ENV_FILE"
        set +a
    fi
}

_cron_expr_for() {
    # Prints a 5-field cron expression for an allowed interval (minutes).
    local n="$1"
    case "$n" in
        15|30) echo "*/$n * * * *" ;;
        60)    echo "0 * * * *" ;;
        180)   echo "0 */3 * * *" ;;
        360)   echo "0 */6 * * *" ;;
        720)   echo "0 */12 * * *" ;;
        1440)  echo "0 0 * * *" ;;
        *) echo "ERROR: unsupported interval $n" >&2; return 1 ;;
    esac
}

_is_allowed_interval() {
    local n="$1" allowed
    for allowed in "${ALLOWED_INTERVALS[@]}"; do
        [[ "$n" == "$allowed" ]] && return 0
    done
    return 1
}

_create_env_file_if_missing() {
    local every="$1"
    if [[ -f "$ENV_FILE" ]]; then
        return 0
    fi
    cat >"$ENV_FILE" <<EOF
# upgrade-agent.env — host-side config for scripts/upgrade-agent.sh.
# This is separate from docker-compose.yml's env vars: those configure the
# containerized app; this file configures the bare-host cron/systemd helper.

# Absolute path to this FileSplitter checkout
REPO_DIR="$REPO_DIR_DEFAULT"

# Shared upgrade folder (must match the ./upgrade:/upgrade bind mount in docker-compose.yml)
UPGRADE_DIR="$REPO_DIR_DEFAULT/upgrade"

# How often this helper is scheduled to run (minutes) — written into agent.json for
# the app to compute its "helper connected" staleness window; does not itself change
# the schedule — re-run --install --every N to actually change the cadence.
EVERY_MINUTES=$every

# Extra flags forwarded to ./update.sh on every run. ONLY --pull and --skip-preflight
# are recognized; anything else is ignored with a warning. Leave empty for normal use.
APP_UPGRADE_ARGS=""
EOF
    echo "Created $ENV_FILE"
}

_print_config() {
    local every="$1" cron_expr
    cron_expr="$(_cron_expr_for "$every")"
    echo "--- cron.d (if /etc/cron.d is writable) ---"
    echo "$cron_expr root $REPO_DIR_DEFAULT/scripts/upgrade-agent.sh >>/var/log/${NAME}.log 2>&1"
    echo ""
    echo "--- systemd timer (if cron.d is unavailable) ---"
    echo "[Unit]"
    echo "Description=FileSplitter upgrade helper (oneshot)"
    echo ""
    echo "[Service]"
    echo "Type=oneshot"
    echo "ExecStart=$REPO_DIR_DEFAULT/scripts/upgrade-agent.sh"
    echo "---"
    echo "[Unit]"
    echo "Description=Run FileSplitter upgrade helper every ${every} minutes"
    echo ""
    echo "[Timer]"
    echo "OnBootSec=2min"
    echo "OnUnitActiveSec=${every}min"
    echo ""
    echo "[Install]"
    echo "WantedBy=timers.target"
}

cmd_print() {
    local every="${1:-15}"
    _load_env
    _print_config "$every"
}

cmd_install() {
    local every=15
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --every) every="$2"; shift 2 ;;
            *) echo "ERROR: unknown option to --install: $1" >&2; exit 1 ;;
        esac
    done
    if [[ "$EUID" -ne 0 ]]; then
        echo "ERROR: --install must be run as root." >&2
        exit 1
    fi
    if ! _is_allowed_interval "$every"; then
        echo "ERROR: --every must be one of: ${ALLOWED_INTERVALS[*]}" >&2
        exit 1
    fi

    _create_env_file_if_missing "$every"
    _load_env

    mkdir -p "$UPGRADE_DIR"
    chmod 770 "$UPGRADE_DIR"

    local cron_expr
    cron_expr="$(_cron_expr_for "$every")"

    if [[ -d /etc/cron.d ]] && [[ -w /etc/cron.d ]]; then
        _write_file_atomic "/etc/cron.d/$NAME" \
            "$cron_expr root $REPO_DIR/scripts/upgrade-agent.sh >>/var/log/${NAME}.log 2>&1
"
        echo "Installed /etc/cron.d/$NAME (every ${every} min)."
    elif command -v systemctl &>/dev/null && [[ -d /etc/systemd/system ]] && [[ -w /etc/systemd/system ]]; then
        _write_file_atomic "/etc/systemd/system/${NAME}.service" \
            "[Unit]
Description=FileSplitter upgrade helper (oneshot)

[Service]
Type=oneshot
ExecStart=$REPO_DIR/scripts/upgrade-agent.sh
"
        _write_file_atomic "/etc/systemd/system/${NAME}.timer" \
            "[Unit]
Description=Run FileSplitter upgrade helper every ${every} minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=${every}min

[Install]
WantedBy=timers.target
"
        systemctl daemon-reload
        systemctl enable --now "${NAME}.timer"
        echo "Installed and started ${NAME}.timer (every ${every} min)."
    else
        echo "Could not write to /etc/cron.d or /etc/systemd/system — nothing installed." >&2
        echo "Run with --print and add this to your own scheduler:" >&2
        _print_config "$every" >&2
        exit 1
    fi

    echo "Running once now so the dashboard shows 'helper connected'..."
    "$SCRIPT_DIR/upgrade-agent.sh" || true
    echo "Done."
}

cmd_uninstall() {
    if [[ "$EUID" -ne 0 ]]; then
        echo "ERROR: --uninstall must be run as root." >&2
        exit 1
    fi
    local removed=0
    if [[ -f "/etc/cron.d/$NAME" ]]; then
        rm -f "/etc/cron.d/$NAME"
        echo "Removed /etc/cron.d/$NAME"
        removed=1
    fi
    if command -v systemctl &>/dev/null && systemctl list-unit-files "${NAME}.timer" &>/dev/null; then
        systemctl disable --now "${NAME}.timer" 2>/dev/null || true
        rm -f "/etc/systemd/system/${NAME}.timer" "/etc/systemd/system/${NAME}.service"
        systemctl daemon-reload
        echo "Removed ${NAME}.timer / ${NAME}.service"
        removed=1
    fi
    if [[ "$removed" -eq 0 ]]; then
        echo "Nothing to uninstall (no cron.d entry or systemd timer found)."
    fi
    echo "upgrade-agent.env and ./upgrade (including upgrade.log history) were left in place."
}

# ---- plain run ----

cmd_run() {
    if [[ "$EUID" -ne 0 ]]; then
        echo "ERROR: must be run as root." >&2
        exit 1
    fi

    _load_env

    if [[ ! -d "$UPGRADE_DIR" ]] || [[ -L "$UPGRADE_DIR" ]]; then
        echo "ERROR: UPGRADE_DIR ($UPGRADE_DIR) does not exist or is a symlink." >&2
        exit 1
    fi

    # Lock on the folder itself — a second concurrent invocation exits silently.
    exec 9<"$UPGRADE_DIR"
    if ! flock -n 9; then
        exit 0
    fi

    local agent_json="$UPGRADE_DIR/agent.json"
    local request_json="$UPGRADE_DIR/request.json"
    local status_json="$UPGRADE_DIR/status.json"
    local log_file="$UPGRADE_DIR/upgrade.log"

    _write_file_atomic "$agent_json" \
        "{\"last_seen\": \"$(_now_iso)\", \"every_minutes\": $EVERY_MINUTES}"

    if [[ ! -f "$request_json" ]]; then
        exit 0
    fi
    if [[ -L "$request_json" ]]; then
        echo "WARNING: request.json is a symlink — refusing to process it." >&2
        # `rm -f` on a symlink only unlinks the symlink itself, never follows it to
        # touch whatever it points at — safe to clean up so a stray/malicious link
        # doesn't sit here forever.
        rm -f "$request_json"
        exit 0
    fi

    local size
    size="$(wc -c <"$request_json" | tr -d ' ')"
    if [[ "$size" -gt 4096 ]]; then
        echo "WARNING: request.json is too large ($size bytes) — treating as malformed." >&2
        rm -f "$request_json"
        _write_file_atomic "$status_json" \
            "{\"id\": null, \"state\": \"failed\", \"from_version\": null, \"to_version\": null, \"requested_by\": null, \"started_at\": \"$(_now_iso)\", \"finished_at\": \"$(_now_iso)\", \"message\": \"malformed request.json (too large)\"}"
        exit 0
    fi

    local raw_id raw_version raw_requested_by
    raw_id="$(grep -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' "$request_json" | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
    raw_version="$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$request_json" | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
    raw_requested_by="$(grep -o '"requested_by"[[:space:]]*:[[:space:]]*"[^"]*"' "$request_json" | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"

    local from_version
    from_version="$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo "unknown")"

    if ! [[ "$raw_id" =~ ^[0-9a-fA-F-]{8,36}$ ]] || ! [[ "$raw_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "WARNING: request.json failed validation (id=${raw_id:-<empty>} version=${raw_version:-<empty>}) — rejecting." >&2
        rm -f "$request_json"
        _write_file_atomic "$status_json" \
            "{\"id\": null, \"state\": \"failed\", \"from_version\": \"$(_json_escape "$from_version")\", \"to_version\": null, \"requested_by\": null, \"started_at\": \"$(_now_iso)\", \"finished_at\": \"$(_now_iso)\", \"message\": \"malformed request.json\"}"
        exit 0
    fi

    local requested_by="unknown"
    if [[ "$raw_requested_by" =~ ^[A-Za-z0-9._:-]{1,64}$ ]]; then
        requested_by="$raw_requested_by"
    fi

    # Delete the request FIRST so a mid-run crash can't reprocess it forever.
    rm -f "$request_json"

    _write_file_atomic "$status_json" \
        "{\"id\": \"$(_json_escape "$raw_id")\", \"state\": \"running\", \"from_version\": \"$(_json_escape "$from_version")\", \"to_version\": \"$(_json_escape "$raw_version")\", \"requested_by\": \"$(_json_escape "$requested_by")\", \"started_at\": \"$(_now_iso)\", \"finished_at\": null, \"message\": \"\"}"

    rm -f "$log_file"
    : >"$log_file"

    # Whitelist-parse extra args — no eval, no glob expansion.
    local -a extra_args=()
    local -a raw_args=()
    read -ra raw_args <<<"${APP_UPGRADE_ARGS:-}"
    local a
    for a in "${raw_args[@]}"; do
        case "$a" in
            --pull|--skip-preflight) extra_args+=("$a") ;;
            "") ;;
            *) echo "WARNING: ignoring unrecognized APP_UPGRADE_ARGS token: $a" >&2 ;;
        esac
    done

    local rc=0
    ( cd "$REPO_DIR" && ./update.sh --version "$raw_version" "${extra_args[@]}" ) >>"$log_file" 2>&1 || rc=$?

    if [[ "$rc" -eq 0 ]]; then
        _write_file_atomic "$status_json" \
            "{\"id\": \"$(_json_escape "$raw_id")\", \"state\": \"succeeded\", \"from_version\": \"$(_json_escape "$from_version")\", \"to_version\": \"$(_json_escape "$raw_version")\", \"requested_by\": \"$(_json_escape "$requested_by")\", \"started_at\": \"$(_now_iso)\", \"finished_at\": \"$(_now_iso)\", \"message\": \"Upgraded to $(_json_escape "$raw_version")\"}"
    else
        _write_file_atomic "$status_json" \
            "{\"id\": \"$(_json_escape "$raw_id")\", \"state\": \"failed\", \"from_version\": \"$(_json_escape "$from_version")\", \"to_version\": \"$(_json_escape "$raw_version")\", \"requested_by\": \"$(_json_escape "$requested_by")\", \"started_at\": \"$(_now_iso)\", \"finished_at\": \"$(_now_iso)\", \"message\": \"update.sh exited with code $rc -- see upgrade.log\"}"
    fi
}

# ---- entry point ----

case "${1:-}" in
    --install)
        shift
        cmd_install "$@"
        ;;
    --print)
        shift
        every=15
        if [[ "${1:-}" == "--every" ]]; then
            every="$2"
        fi
        cmd_print "$every"
        ;;
    --uninstall)
        cmd_uninstall
        ;;
    --help|-h)
        _usage
        ;;
    "")
        cmd_run
        ;;
    *)
        echo "ERROR: unknown argument: $1" >&2
        _usage
        ;;
esac
