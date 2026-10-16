#!/usr/bin/env bash
# Test harness for scripts/upgrade-agent.sh.
#
# The plain-run mode refuses to operate unless EUID==0 (a deliberate safety check,
# since the real script runs as root on the host) — so this harness itself must run
# as root too. On a CI runner that isn't already root, invoke it with sudo:
#   sudo bash tests/bash/test_upgrade_agent.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="$REPO_ROOT/scripts/upgrade-agent.sh"

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: this harness must run as root (the helper refuses otherwise). Try: sudo $0" >&2
    exit 1
fi

PASS=0
FAIL=0

_ok() { PASS=$((PASS + 1)); echo "  ok - $1"; }
_fail() { FAIL=$((FAIL + 1)); echo "  NOT OK - $1"; }

_assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        _ok "$desc"
    else
        _fail "$desc (expected [$expected], got [$actual])"
    fi
}

_assert_file_missing() {
    local desc="$1" path="$2"
    if [[ ! -e "$path" ]]; then
        _ok "$desc"
    else
        _fail "$desc (file unexpectedly exists: $path)"
    fi
}

_assert_file_exists() {
    local desc="$1" path="$2"
    if [[ -e "$path" ]]; then
        _ok "$desc"
    else
        _fail "$desc (file missing: $path)"
    fi
}

_assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        _ok "$desc"
    else
        _fail "$desc (did not find [$needle] in [$haystack])"
    fi
}

TESTREPO="$(mktemp -d)"
trap 'rm -rf "$TESTREPO"' EXIT

_setup_testrepo() {
    rm -rf "$TESTREPO"
    mkdir -p "$TESTREPO/scripts" "$TESTREPO/upgrade"
    cp "$HELPER" "$TESTREPO/scripts/upgrade-agent.sh"
    echo "1.0.0" > "$TESTREPO/VERSION"
    chmod 770 "$TESTREPO/upgrade"
    cat > "$TESTREPO/update.sh" <<'STUBEOF'
#!/usr/bin/env bash
echo "STUB CALLED WITH: $*"
echo "STUB CALLED WITH: $*" > "$(dirname "$0")/stub_called.txt"
exit "${STUB_EXIT_CODE:-0}"
STUBEOF
    chmod +x "$TESTREPO/update.sh"
    cat > "$TESTREPO/upgrade-agent.env" <<EOF
REPO_DIR="$TESTREPO"
UPGRADE_DIR="$TESTREPO/upgrade"
EVERY_MINUTES=15
APP_UPGRADE_ARGS=""
EOF
}

echo "=== valid request ==="
_setup_testrepo
cat > "$TESTREPO/upgrade/request.json" <<'EOF'
{"id": "3fa2c1e4b5a74d0f9a1c2b3d4e5f6a7b", "version": "1.1.0", "requested_by": "192.168.1.5", "requested_at": "2026-01-01T00:00:00Z"}
EOF
bash "$TESTREPO/scripts/upgrade-agent.sh"
_assert_file_missing "request.json removed" "$TESTREPO/upgrade/request.json"
_assert_file_exists "stub was invoked" "$TESTREPO/stub_called.txt"
status="$(cat "$TESTREPO/upgrade/status.json")"
_assert_contains "status.json reports succeeded" "$status" '"state": "succeeded"'
log="$(cat "$TESTREPO/upgrade/upgrade.log")"
_assert_contains "upgrade.log contains stub output" "$log" "STUB CALLED WITH"
_assert_file_exists "agent.json written" "$TESTREPO/upgrade/agent.json"

echo ""
echo "=== malformed request (bad id) ==="
_setup_testrepo
cat > "$TESTREPO/upgrade/request.json" <<'EOF'
{"id": "not-a-valid-id!", "version": "1.1.0", "requested_by": "x", "requested_at": "2026-01-01T00:00:00Z"}
EOF
bash "$TESTREPO/scripts/upgrade-agent.sh"
_assert_file_missing "request.json removed" "$TESTREPO/upgrade/request.json"
_assert_file_missing "stub was never invoked" "$TESTREPO/stub_called.txt"
status="$(cat "$TESTREPO/upgrade/status.json")"
_assert_contains "status.json reports failed" "$status" '"state": "failed"'
_assert_contains "status.json explains malformed request" "$status" "malformed request.json"

echo ""
echo "=== symlinked request.json ==="
_setup_testrepo
echo "secret" > "$TESTREPO/outside_file.txt"
ln -s "$TESTREPO/outside_file.txt" "$TESTREPO/upgrade/request.json"
bash "$TESTREPO/scripts/upgrade-agent.sh"
# rm -f on a symlink only unlinks the symlink itself — it never follows the link to
# touch its target — so proactively removing the stray symlink here is safe
# self-healing, not a risk to whatever it pointed at.
_assert_file_missing "symlinked request.json was unlinked (target untouched)" "$TESTREPO/upgrade/request.json"
_assert_contains "symlink target file still has its original contents" "$(cat "$TESTREPO/outside_file.txt")" "secret"
_assert_file_missing "stub was never invoked" "$TESTREPO/stub_called.txt"
_assert_file_missing "status.json not written" "$TESTREPO/upgrade/status.json"

echo ""
echo "=== concurrent run via flock ==="
_setup_testrepo
cat > "$TESTREPO/upgrade/request.json" <<'EOF'
{"id": "3fa2c1e4b5a74d0f9a1c2b3d4e5f6a7b", "version": "1.1.0", "requested_by": "x", "requested_at": "2026-01-01T00:00:00Z"}
EOF
( exec 9<"$TESTREPO/upgrade"; flock 9; sleep 3 ) &
LOCKPID=$!
sleep 0.3
set +e
bash "$TESTREPO/scripts/upgrade-agent.sh"
rc=$?
set -e
_assert_eq "locked-out run exits 0" "0" "$rc"
_assert_file_exists "pending request still present (untouched by locked-out run)" "$TESTREPO/upgrade/request.json"
_assert_file_missing "stub was never invoked by the locked-out run" "$TESTREPO/stub_called.txt"
wait "$LOCKPID"

echo ""
echo "=== --print makes no filesystem changes ==="
_setup_testrepo
rm -f "$TESTREPO/upgrade-agent.env"
before="$(find "$TESTREPO" -type f | sort)"
output="$(cd "$TESTREPO" && bash "$TESTREPO/scripts/upgrade-agent.sh" --print)"
after="$(find "$TESTREPO" -type f | sort)"
_assert_contains "prints a cron line" "$output" "upgrade-agent.sh"
_assert_eq "no files created or removed" "$before" "$after"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
