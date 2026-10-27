#!/usr/bin/env bash
# FileSplitter update script — pulls latest code and restarts the service, with a
# database backup, a pre-flight check of the new image against a copy of the
# database, and automatic rollback if the new release doesn't come up healthy.
#
# Usage: ./update.sh [--version X.Y.Z] [--pull] [--no-git] [--skip-preflight]
#        ./update.sh --rollback-rev <git-ref>
#
# This is the one and only upgrade path — whether run by hand over SSH, or by
# scripts/upgrade-agent.sh on behalf of the in-app "Upgrade" button.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET_VERSION=""
NO_GIT=0
SKIP_PREFLIGHT=0
ROLLBACK_REV=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) TARGET_VERSION="$2"; shift 2 ;;
        --pull) shift ;;  # default behaviour already; accepted for explicitness
        --no-git) NO_GIT=1; shift ;;
        --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
        --rollback-rev) ROLLBACK_REV="$2"; shift 2 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
done

# Prefer `docker compose` (v2 plugin) over `docker-compose` (v1 standalone)
if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: neither 'docker compose' nor 'docker-compose' found." >&2
    exit 1
fi

# Existing installs that git-pull this feature in get the folder automatically,
# without needing to run the helper's --install first just to create it.
[ -d upgrade ] || { mkdir -p upgrade; chmod 770 upgrade; }

_wait_healthy() {
    # _wait_healthy <url> <tries> <delay_secs>
    local url="$1" tries="$2" delay="$3" i
    for ((i = 1; i <= tries; i++)); do
        if curl -sf "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep "$delay"
    done
    return 1
}

_image_name() {
    # Best-effort: ask compose what image name it resolved for this service; fall
    # back to compose v2's default <project>-<service> naming if that's unavailable.
    local img
    img="$($DC config --images filesplitter 2>/dev/null | head -1)"
    if [[ -n "$img" ]]; then
        echo "$img"
    else
        echo "$(basename "$SCRIPT_DIR" | tr '[:upper:]' '[:lower:]')-filesplitter"
    fi
}

# --- Manual rollback escape hatch (separate from the automatic on-failure rollback
# below) — checks out an arbitrary ref, rebuilds, deploys, health-checks. Never
# touches the database backup; going backwards against a possibly-forward-migrated
# DB is a judgment call left to the operator. ---
if [[ -n "$ROLLBACK_REV" ]]; then
    echo "=== FileSplitter Manual Rollback ==="
    echo "Target revision : $ROLLBACK_REV"
    if [[ -t 0 ]]; then
        read -r -p "This checks out '$ROLLBACK_REV', rebuilds, and redeploys. Continue? [y/N] " confirm
        [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
    fi
    git checkout --quiet "$ROLLBACK_REV"
    RB_VERSION="$(cat VERSION 2>/dev/null || echo 'unknown')"
    echo "Rebuilding (version $RB_VERSION)..."
    VERSION="$RB_VERSION" $DC build
    VERSION="$RB_VERSION" $DC up -d
    if _wait_healthy "http://localhost:4250/api/version" 12 5; then
        echo "=== Rolled back to $ROLLBACK_REV (version $RB_VERSION) ==="
    else
        echo "ERROR: service did not become healthy after rollback — manual intervention required." >&2
        exit 1
    fi
    exit 0
fi

PREV_VERSION="$(cat VERSION 2>/dev/null || echo 'unknown')"
PREV_GIT_REV="$(git rev-parse HEAD 2>/dev/null || echo '')"
echo "=== FileSplitter Updater ==="
echo "Current version : $PREV_VERSION"
echo ""

IMAGE="$(_image_name)"

# [1/8] Snapshot: retag the running image (best-effort, for a fast rollback) and
# back up the database through the still-running old container, so it's guaranteed
# to have Python's sqlite3 available even on hosts without one installed.
echo "[1/8] Snapshotting current state..."
CURRENT_CID="$($DC ps -q filesplitter 2>/dev/null || true)"
if [[ -n "$CURRENT_CID" ]]; then
    CURRENT_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$CURRENT_CID" 2>/dev/null || true)"
    if [[ -n "$CURRENT_IMAGE_ID" ]]; then
        docker tag "$CURRENT_IMAGE_ID" "filesplitter:rollback-${PREV_VERSION}" 2>/dev/null \
            || echo "      WARNING: could not retag current image for fast rollback."
    fi
fi

BACKUP_FILE="filesplitter.db.bak-$(date +%Y%m%d%H%M%S)"
if [[ -n "$CURRENT_CID" ]]; then
    $DC exec -T filesplitter python3 - <<PYEOF || echo "      WARNING: database backup failed."
import sqlite3
sqlite3.connect("/app/data/filesplitter.db").execute("VACUUM INTO '/app/data/$BACKUP_FILE'")
PYEOF
    echo "      Backed up to ./data/$BACKUP_FILE"
else
    echo "      WARNING: service not running — skipping database backup."
    BACKUP_FILE=""
fi
# Keep the 5 most recent backups.
find ./data -maxdepth 1 -name 'filesplitter.db.bak-*' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | awk 'NR>5{print $2}' | xargs -r rm -f

# [2/8] Git step.
if [[ "$NO_GIT" -eq 1 ]]; then
    echo ""
    echo "[2/8] --no-git passed, skipping."
elif [[ -n "$TARGET_VERSION" ]]; then
    echo ""
    echo "[2/8] Fetching tags and checking out v$TARGET_VERSION..."
    # Stash any local edits (e.g. docker-compose.yml customisations, such as the GPU
    # deploy block) before the checkout, so they don't block it — this is the only
    # thing the in-app Upgrade button ever runs, so without this every upgrade would
    # fail for any install with local docker-compose.yml changes. Restore them after.
    git fetch --tags --quiet
    git stash --include-untracked --quiet || true
    git checkout --quiet "v$TARGET_VERSION" 2>/dev/null || git checkout --quiet "$TARGET_VERSION"
    git stash pop --quiet || true  # merge conflicts (rare) are left for the user
    CHECKED_OUT_VERSION="$(cat VERSION 2>/dev/null || echo 'unknown')"
    if [[ "$CHECKED_OUT_VERSION" != "$TARGET_VERSION" ]]; then
        echo "ERROR: checked out ref does not contain a matching VERSION file ($CHECKED_OUT_VERSION != $TARGET_VERSION)." >&2
        exit 1
    fi
else
    echo ""
    echo "[2/8] Pulling latest code..."
    # A prior `--version X.Y.Z` run (the only thing the in-app Upgrade button ever does,
    # via scripts/upgrade-agent.sh) checks out a tag, leaving the repo in detached HEAD —
    # a bare `git pull` has no tracking branch to use there, so re-attach to the default
    # branch first. Stash any local edits (e.g. docker-compose.yml customisations) before
    # the checkout, so they don't block it, then restore them afterwards.
    DEFAULT_BRANCH="$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)" || true
    DEFAULT_BRANCH="${DEFAULT_BRANCH#origin/}"
    DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
    git stash --include-untracked --quiet || true
    git checkout --quiet "$DEFAULT_BRANCH"
    git pull --quiet origin "$DEFAULT_BRANCH"
    git stash pop --quiet || true  # restore local edits (merge conflicts are left for the user)
fi

NEW_VERSION="$(cat VERSION 2>/dev/null || echo 'unknown')"

# [3/8] Pre-flight: build the new image and test it against a copy of the database
# on a scratch port, before touching the running production container.
DB_VALIDATED_BY_PREFLIGHT=0
echo ""
echo "[3/8] Building Docker image (version $NEW_VERSION)..."
VERSION="$NEW_VERSION" $DC build

echo ""
if [[ "$SKIP_PREFLIGHT" -eq 1 ]]; then
    echo "[4/8] --skip-preflight passed, skipping pre-flight check."
elif [[ -z "$BACKUP_FILE" ]]; then
    echo "[4/8] No database backup to test against — skipping pre-flight check."
else
    echo "[4/8] Pre-flight: testing the new image against a copy of the database..."
    SCRATCH_DB="$(mktemp /tmp/filesplitter-preflight.XXXXXX)"
    cp "./data/$BACKUP_FILE" "$SCRATCH_DB"
    docker rm -f filesplitter-preflight &>/dev/null || true
    docker run --rm -d --name filesplitter-preflight \
        -p 127.0.0.1:4299:4250 \
        -e DATABASE_PATH=/app/data/filesplitter.db \
        -v "$SCRATCH_DB:/app/data/filesplitter.db" \
        "$IMAGE" >/dev/null
    if _wait_healthy "http://127.0.0.1:4299/api/version" 10 3; then
        echo "      Pre-flight passed."
        DB_VALIDATED_BY_PREFLIGHT=1
    else
        echo "ERROR: pre-flight failed — the new image did not become healthy against a database copy." >&2
        docker logs filesplitter-preflight 2>&1 | tail -n 50 >&2 || true
        docker rm -f filesplitter-preflight &>/dev/null || true
        rm -f "$SCRATCH_DB"
        echo "The previous version is still running; nothing has been changed." >&2
        exit 1
    fi
    docker rm -f filesplitter-preflight &>/dev/null || true
    rm -f "$SCRATCH_DB"
fi

# [5/8] Deploy (image already built above).
echo ""
echo "[5/8] Restarting service..."
VERSION="$NEW_VERSION" $DC up -d

# [6/8] Real health check.
echo ""
echo "[6/8] Waiting for health check..."
if _wait_healthy "http://localhost:4250/api/version" 12 5; then
    echo "      Service is up."
else
    # [7/8] Automatic rollback.
    echo "ERROR: new version did not become healthy — rolling back." >&2
    if docker image inspect "filesplitter:rollback-${PREV_VERSION}" &>/dev/null; then
        echo "      Using the retagged previous image for a fast rollback (no rebuild)."
        docker tag "filesplitter:rollback-${PREV_VERSION}" "$IMAGE"
        VERSION="$PREV_VERSION" $DC up -d
    else
        echo "      No retagged image available — rebuilding from git history instead."
        if [[ "$NO_GIT" -eq 0 && -n "$PREV_GIT_REV" ]]; then
            git checkout --quiet "$PREV_GIT_REV"
        fi
        VERSION="$PREV_VERSION" $DC build
        VERSION="$PREV_VERSION" $DC up -d
    fi

    if [[ "$DB_VALIDATED_BY_PREFLIGHT" -eq 0 && -n "$BACKUP_FILE" ]]; then
        echo "      Restoring database backup (pre-flight was skipped, so migrations were never proven safe against a copy)..."
        $DC stop filesplitter
        cp "./data/$BACKUP_FILE" ./data/filesplitter.db
        $DC start filesplitter
    fi

    if ! _wait_healthy "http://localhost:4250/api/version" 12 5; then
        echo "ERROR: rollback also failed to become healthy — manual intervention required." >&2
    fi

    echo ""
    echo "=== ROLLED BACK to v${PREV_VERSION} ==="
    [[ -n "$BACKUP_FILE" ]] && echo "Backup used : ./data/$BACKUP_FILE"
    exit 1
fi

# [8/8] Summary.
echo ""
echo "=== Update complete ==="
echo "Previous version : $PREV_VERSION"
echo "New version      : $NEW_VERSION"
[[ -n "$BACKUP_FILE" ]] && echo "Database backup  : ./data/$BACKUP_FILE"
echo "Dashboard        : http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):4250"
