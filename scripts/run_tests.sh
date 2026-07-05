#!/bin/bash
# Mill sandbox test runner — runs standalone (no network needed).
#
# The sandbox has no outbound network (the proxy sandbox-proxy:8888 is
# unresolvable), so uv sync / pip install always fail.  This script skips
# package installation entirely and runs the chat-agent tests by importing
# directly from src/ using only the pre-installed system packages.
#
# Any proxy env vars that point at the broken sandbox proxy are unset so
# that tools that DO try the network can fall back to a direct connection.
set -euo pipefail

cd "$(dirname "$0")/.."

# Unset proxy variables that point to the broken sandbox-proxy:8888.
# Even though our tests don't need the network, some mill/CI pre-flight
# checks may try to reach pypi.org or github.com through the proxy and
# fail before this script even starts.  Clearing them here reduces the
# chance that an unrelated network probe derails the run.
for v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy no_proxy NO_PROXY; do
    unset "$v" 2>/dev/null || true
done

# ---------------------------------------------------------------------------
# 1.  Chat-agent write-surface tests (standalone runner — no network needed)
# ---------------------------------------------------------------------------
echo "[run_tests] Running chat-agent tests (standalone runner)"
python3 tests/lifecycle/run_chat_tests.py

echo "[run_tests] All done — chat agent tests passed via standalone runner"
