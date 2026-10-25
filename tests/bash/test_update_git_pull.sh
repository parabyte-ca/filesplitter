#!/usr/bin/env bash
# Test harness for update.sh's default (no --version) git-pull step.
#
# Reproduces the real-world failure: the in-app Upgrade button always runs
# `update.sh --version X.Y.Z`, which checks out a tag and leaves the repo in detached
# HEAD. A later plain `./update.sh` used to run a bare `git pull` there, which fails
# with "You are not currently on a branch." This harness runs the real update.sh (not a
# stub) against a real git remote+clone in that exact detached state, with docker/curl
# stubbed out so the later build/deploy/healthcheck steps no-op successfully.
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

# --- Fake docker/curl on PATH, so update.sh's build/deploy/healthcheck steps succeed
# without needing real Docker. No container ever reports as running (`compose ps`
# prints nothing), so update.sh naturally skips the DB backup and pre-flight check —
# the only things actually exercised here are the git step and the deploy/healthcheck
# steps that follow it. ---
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
    git -C "$CLONE" add -A
    git -C "$CLONE" commit --quiet -m "1.0.0"
    git -C "$CLONE" tag v1.0.0

    # A second, newer commit on main — this is what a plain `./update.sh` should pull.
    echo "1.0.1" > "$CLONE/VERSION"
    git -C "$CLONE" commit --quiet -am "1.0.1"

    git -C "$CLONE" remote add origin "$ORIGIN"
    git -C "$CLONE" push --quiet origin main --tags

    # Simulate a prior in-app upgrade — `update.sh --version 1.0.0` — which detaches
    # HEAD at the tag and never advances the local `main` ref. Force `main` itself back
    # to that same older commit too, so the "latest code" (1.0.1) genuinely only exists
    # on origin and a real `git pull` is required to fetch it.
    git -C "$CLONE" checkout --quiet v1.0.0
    git -C "$CLONE" branch -f main v1.0.0
}

echo "=== plain ./update.sh recovers from a detached-HEAD clone ==="
_setup_repo
if git -C "$CLONE" symbolic-ref -q HEAD >/dev/null 2>&1; then
    _fail "clone starts detached (sanity check)"
else
    _ok "clone starts detached (sanity check)"
fi
set +e
( cd "$CLONE" && ./update.sh >"$WORKDIR/output.log" 2>&1 )
rc=$?
set -e
output="$(cat "$WORKDIR/output.log")"
if [[ "$rc" -ne 0 ]]; then
    echo "--- update.sh output ---"
    echo "$output"
    echo "------------------------"
fi
_assert_eq "update.sh exits 0" "0" "$rc"
_assert_eq "HEAD is reattached to a branch" "0" "$(git -C "$CLONE" symbolic-ref -q HEAD >/dev/null 2>&1; echo $?)"
_assert_eq "branch is main" "refs/heads/main" "$(git -C "$CLONE" symbolic-ref -q HEAD 2>/dev/null || echo '')"
_assert_eq "VERSION pulled the new commit" "1.0.1" "$(cat "$CLONE/VERSION")"

echo ""
echo "=== summary: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
