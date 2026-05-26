#!/usr/bin/env bash
# FileSplitter first-time installer.
# Usage: ./install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== FileSplitter Installer ==="

# --- Dependency checks ---
fail=0
for cmd in docker git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found — please install it first." >&2
        fail=1
    fi
done

if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo "ERROR: neither 'docker compose' nor 'docker-compose' found." >&2
    fail=1
fi

[ "$fail" -eq 1 ] && exit 1

VERSION="$(cat VERSION 2>/dev/null || echo 'dev')"
echo "Installing version: $VERSION"
echo ""

# --- Preflight ---
echo "[1/3] Creating persistent data directory..."
mkdir -p data
echo "      ./data/ created (SQLite database will be stored here)"

echo ""
echo "[2/3] Building Docker image..."
VERSION="$VERSION" $DC build

echo ""
echo "[3/3] Starting service..."
VERSION="$VERSION" $DC up -d

echo ""
echo "=== Installation complete ==="
echo "Version   : $VERSION"
echo "Dashboard : http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):4250"
echo ""
echo "Next steps:"
echo "  1. Open the dashboard and click 'Scan Now' to index your library."
echo "  2. Use 'Queue All Anthologies (Split)' or 'Queue All (Encode)' to process files."
echo "  3. Run ./update.sh whenever a new version is available."
echo ""
echo "To add more media paths, edit docker-compose.yml and run ./update.sh."
