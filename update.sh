#!/usr/bin/env bash
# FileSplitter update script — pulls latest code and restarts the service.
# Usage: ./update.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer `docker compose` (v2 plugin) over `docker-compose` (v1 standalone)
if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: neither 'docker compose' nor 'docker-compose' found." >&2
    exit 1
fi

PREV_VERSION="$(cat VERSION 2>/dev/null || echo 'unknown')"
echo "=== FileSplitter Updater ==="
echo "Current version : $PREV_VERSION"
echo ""

echo "[1/4] Pulling latest code..."
git pull

NEW_VERSION="$(cat VERSION 2>/dev/null || echo 'unknown')"

echo ""
echo "[2/4] Rebuilding Docker image (version $NEW_VERSION)..."
VERSION="$NEW_VERSION" $DC build

echo ""
echo "[3/4] Restarting service..."
VERSION="$NEW_VERSION" $DC up -d

echo ""
echo "[4/4] Waiting for health check..."
sleep 5
if $DC ps | grep -q "healthy\|running"; then
    echo "      Service is up."
else
    echo "      Service started (health check pending — give it ~20s)."
fi

echo ""
echo "=== Update complete ==="
echo "Previous version : $PREV_VERSION"
echo "New version      : $NEW_VERSION"
echo "Dashboard        : http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):4250"
