#!/usr/bin/env bash
# Run by agent-pipeline after a merge to main: restart the live service so it picks up the change.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
sudo -n systemctl restart mc-mcp.service
