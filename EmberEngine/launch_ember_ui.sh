#!/usr/bin/env bash
# Open the Textual panel UI in kitty. kitty draws the Scope page's phosphor
# trace through its graphics protocol, so the panel needs it (Debian Trixie:
# sudo apt install kitty). Options come from panel_terminal.py, which reuses
# the emberdeck LXTerminal profile's font and keeps existing Labwc window
# rules matching; it reads those files and changes none of them.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v kitty >/dev/null 2>&1; then
  echo "[ember-ui] kitty is not installed; the panel UI needs it: sudo apt install kitty" >&2
  exit 1
fi

ARGS=()
while IFS= read -r arg; do
  ARGS+=("$arg")
done < <(python3 "$ROOT/panel_terminal.py" --args || printf '%s\n' --class emberdeck-panel --start-as=fullscreen)

# The scope's image id travels in a 24-bit text colour, so Textual must not
# fall back to 256 colours.
export TEXTUAL_COLOR_SYSTEM=truecolor

exec kitty \
  --config "$ROOT/kitty-panel.conf" \
  "${ARGS[@]}" \
  --title "EMBER DECK" \
  bash "$ROOT/run_ember_ui.sh"
