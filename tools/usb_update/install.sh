#!/usr/bin/env bash
# Ember Deck USB update. Run on the Deck, as the user that runs Ember Deck:
#
#     bash /media/$USER/<usb-name>/<this-folder>/install.sh
#
# 1. Installs kitty (the panel UI runs in it) if it is not there yet.
# 2. Hands over to install.py, which checks everything first, backs up
#    ~/EmberDeck, copies the new code in without changing any config file,
#    restarts the Deck and rolls itself back if the new version is unhealthy.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" = "0" ]; then
  echo "Run this as the Ember Deck user, not with sudo (it asks for sudo only to install kitty)." >&2
  exit 1
fi
if [ ! -d "$HOME/EmberDeck" ]; then
  echo "No Ember Deck install at $HOME/EmberDeck. Run this as the user that runs the Deck." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is missing." >&2
  exit 1
fi

if command -v kitty >/dev/null 2>&1; then
  echo "kitty is already installed: $(kitty --version)"
else
  echo "Installing kitty (you may be asked for your password)..."
  sudo apt-get update
  sudo apt-get install -y kitty
fi

# Image placeholders, which the scope relies on, arrived in kitty 0.28.
python3 - "$(kitty --version)" <<'PY'
import re, sys
match = re.search(r"(\d+)\.(\d+)", sys.argv[1])
if not match or (int(match.group(1)), int(match.group(2))) < (0, 28):
    sys.exit(f"kitty 0.28 or newer is needed; found: {sys.argv[1]}")
PY

exec python3 "$HERE/install.py" "$@"
