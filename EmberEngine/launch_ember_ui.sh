#!/usr/bin/env bash
# Open the diagnostic Textual UI in its dedicated terminal profile.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

exec lxterminal \
  --no-remote \
  --profile=emberdeck \
  --title="EMBER DECK" \
  --command="bash \"$ROOT/run_ember_ui.sh\""
