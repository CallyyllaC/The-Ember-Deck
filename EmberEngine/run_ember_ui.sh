#!/usr/bin/env bash
# Run the diagnostic Textual UI with its explicit deployment configuration.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export CONFIG_PATH="$ROOT/configs/ember_ui.yaml"
exec "$ROOT/.venv/bin/python" -u "$ROOT/ember_ui.py"
