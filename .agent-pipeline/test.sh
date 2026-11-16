#!/usr/bin/env bash
# Run by the agent-pipeline QA1 stage. --offline: install only from uv's local cache, never the network.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --offline pytest -q -p no:cacheprovider "$@"
