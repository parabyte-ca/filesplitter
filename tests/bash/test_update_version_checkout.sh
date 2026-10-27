#!/usr/bin/env bash
# Test harness for update.sh's `--version X.Y.Z` git-checkout step (the only thing
# the in-app Upgrade button ever runs, via scripts/upgrade-agent.sh).
#
# Reproduces the real-world failure: a user with local docker-compose.yml edits (e.g.
# the GPU deploy block, exactly what the README tells GPU users to uncomment) clicks
# "Upgrade now" in the app. `git checkout <tag>` refuses to run with those local edits
# in the way ("Your local changes ... would be overwritten by checkout"), so every
# future in-app upgrade fails identically until a human fixes it by hand. This harness
# runs the real update.sh (not a stub) against a real git remote+clone with that exact
# local edit present, with docker/curl stubbed out so the later build/deploy/healthcheck
# steps no-op successfully.
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

# --- Fake docker/curl on PATH, same as test_update_git_pull.sh. ---
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
cat > "$FAKEBIN/curl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$FAKEBIN/docker" "$FAKEBIN/curl"
export PATH="$FAKEBIN:$PATH"

_setup_repo() {
    rm -rf "$WORKDIR/origin.git" "$CLONE"

    git init --quiet --bare -b main "$ORIGIN"

    git init --quiet -b main "$CLONE"
    git -C "$CLONE" config user.email "test@example.com"
    git -C "$CLONE" config user.name "Test"
    cp "$REPO_ROOT/update.sh" "$CLONE/update.sh"
    chmod +x "$CLONE/update.sh"
    mkdir -p "$CLONE/data"

    echo "1.0.0" > "$CLONE/VERSION"
    echo "image: original" > "$CLONE/docker-compose.yml"
    git -C "$CLONE" add -A
    git -C "$CLONE" commit --quiet -m "1.0.0"
    git -C "$CLONE" tag v1.0.0

    # A newer release that also changes docker-compose.yml upstream (e.g. a
    # healthcheck tweak) — the checkout genuinely needs to touch that file, which is
    # what conflicts with the user's own uncommitted edit to it below. If this commit
    # left docker-compose.yml untouched, `git checkout` would have nothing to conflict
    # with and this test would pass even against the old, unfixed update.sh.
    echo "1.0.1" > "$CLONE/VERSION"
    echo "image: v1.0.1" > "$CLONE/docker-compose.yml"
    git -C "$CLONE" commit --quiet -am "1.0.1"
    git -C "$CLONE" tag v1.0.1

    git -C "$CLONE" remote add origin "$ORIGIN"
    git -C "$CLONE" push --quiet origin main --tags

    git -C "$CLONE" checkout --quiet v1.0.0

    # The local customisation that blocks a plain `git checkout` — e.g. the GPU
    # deploy block a user uncommented per the README, never committed.
    echo "image: original
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia" > "$CLONE/docker-compose.yml"
}

echo "=== 'update.sh --version' succeeds with local docker-compose.yml edits, and restores them ==="
_setup_repo
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
_assert_eq "update.sh exits 0" "0" "$rc"
_assert_eq "VERSION checked out the target release" "1.0.1" "$(cat "$CLONE/VERSION")"
_assert_eq "local docker-compose.yml edit was restored (not overwritten)" \
    "yes" "$(grep -q "nvidia" "$CLONE/docker-compose.yml" && echo yes || echo no)"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
