#!/usr/bin/env bash
# Pull the latest code, rebuild, restart, and confirm it came back up.
# Run this from the Pi: ./update.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Pulling latest code..."
git pull

echo "==> Rebuilding and restarting..."
docker compose -f docker-compose.pi.yml up -d --build

echo "==> Checking it came back up..."
MCP_PUBLIC_URL="$(grep -E '^MCP_PUBLIC_URL=' .env | cut -d= -f2-)"

if [ -z "$MCP_PUBLIC_URL" ]; then
    echo "MCP_PUBLIC_URL not set in .env - skipping health check."
    exit 0
fi

for _ in $(seq 1 10); do
    if curl -sf "$MCP_PUBLIC_URL/health" > /dev/null; then
        echo "==> Update complete - $MCP_PUBLIC_URL/health is responding."
        exit 0
    fi
    sleep 2
done

echo "==> WARNING: $MCP_PUBLIC_URL/health didn't respond after rebuild."
echo "    Check: docker compose -f docker-compose.pi.yml logs -f"
exit 1
