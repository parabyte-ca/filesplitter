#!/usr/bin/env bash
# Test harness proving update.sh's own git step (which rewrites update.sh itself on
# disk, mid-run) doesn't leave the rest of that same run executing stale, pre-upgrade
# logic. Reproduces the real-world failure: a fix shipped in a new version's
# update.sh (a longer health-check budget) silently had no effect on the very
# upgrade that installed it, because the already-running process kept using
# whatever health-check logic was on disk *before* the checkout.
#
# Scenario: v1.0.0's update.sh uses a 1-try, 0-delay health check — effectively
# "pass only if the container is healthy on the very first curl". v1.0.1's
# update.sh uses a 5-try budget. The fake container takes 3 attempts to report
# healthy. Upgrading v1.0.0 -> v1.0.1 must succeed (not roll back): that's only
# possible if the *new* (v1.0.1) health-check logic is what actually runs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

ORIGIN="$WORKDIR/origin.git"
CLONE="$WORKDIR/clone"
FAKEBIN="$WORKDIR/fakebin"
CURL_COUNTER="$WORKDIR/curl_calls"

# --- Fake docker: same no-op stub as the other update.sh harnesses. ---
mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "compose" ]]; then
    shift
    case "$1" in
        config) echo "testrepo-filesplitter" ;;
        *) : ;;
    esac
fi
exit 0
EOF
chmod +x "$FAKEBIN/docker"

# --- Fake curl: fails the first 2 calls, succeeds from the 3rd call on. A single
# health-check attempt is never enough; several are. Tracked via a counter file
# since each curl invocation is a separate process. ---
cat > "$FAKEBIN/curl" <<EOF
#!/usr/bin/env bash
n=0
[[ -f "$CURL_COUNTER" ]] && n="\$(cat "$CURL_COUNTER")"
n=\$((n + 1))
echo "\$n" > "$CURL_COUNTER"
[[ "\$n" -ge 3 ]]
EOF
chmod +x "$FAKEBIN/curl"
export PATH="$FAKEBIN:$PATH"

_make_version() {
    # _make_version <version> <healthcheck_tries> <healthcheck_delay>
    local version="$1" tries="$2" delay="$3"
    sed -E "s/^HEALTHCHECK_TRIES=.*/HEALTHCHECK_TRIES=$tries/; s/^HEALTHCHECK_DELAY=.*/HEALTHCHECK_DELAY=$delay/" \
        "$REPO_ROOT/update.sh" > "$CLONE/update.sh"
    chmod +x "$CLONE/update.sh"
    echo "$version" > "$CLONE/VERSION"
    git -C "$CLONE" add -A
    git -C "$CLONE" commit --quiet -m "$version"
    git -C "$CLONE" tag "v$version"
}

rm -rf "$WORKDIR/origin.git" "$CLONE"
git init --quiet --bare -b main "$ORIGIN"
git init --quiet -b main "$CLONE"
git -C "$CLONE" config user.email "test@example.com"
git -C "$CLONE" config user.name "Test"
mkdir -p "$CLONE/data"

# v1.0.0: a health check so tight (1 try) it can never see the fake container
# become healthy (which takes 3 curl calls).
_make_version "1.0.0" 1 0
# v1.0.1: patient enough (5 tries) to see it succeed.
_make_version "1.0.1" 5 0

git -C "$CLONE" remote add origin "$ORIGIN"
git -C "$CLONE" push --quiet origin main --tags

git -C "$CLONE" checkout --quiet v1.0.0

echo "=== upgrading v1.0.0 -> v1.0.1 must use v1.0.1's (not v1.0.0's) health-check logic ==="
set +e
( cd "$CLONE" && ./update.sh --version 1.0.1 >"$WORKDIR/output.log" 2>&1 )
rc=$?
set -e
output="$(cat "$WORKDIR/output.log")"
if [[ "$rc" -ne 0 ]]; then
    echo "--- update.sh output ---"
    echo "$output"
    echo "------------------------"
fi
_assert_eq "update.sh exits 0 (not rolled back)" "0" "$rc"
_assert_eq "VERSION ended up on the target release" "1.0.1" "$(cat "$CLONE/VERSION")"
if [[ "$output" == *"ROLLED BACK"* ]]; then
    _fail "no rollback occurred (v1.0.0's stale 1-try health check did not govern this run)"
else
    _ok "no rollback occurred (v1.0.0's stale 1-try health check did not govern this run)"
fi

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
