#!/usr/bin/env bash
# Systemd entry point for the supervised Ember Deck worker process tree.
set -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

echo
echo "============================================================"
echo " EmberDeck Foundry start: $(date)"
echo "============================================================"

# stdout/stderr go to systemd-journald, which has its own bounded rotation.
# Whisper owns the small UI snapshot and the rotated error-only history.
exec "$ROOT/.venv/bin/python" -u "$ROOT/FoundryCore.py"
