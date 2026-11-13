#!/usr/bin/env bash
# Put back the Ember Deck install that was there before the last update.
# Restores the newest backup in ~/EmberDeck-backups (program files only;
# config files are never changed by the update) and restarts the Deck.
# kitty stays installed. Run as the Ember Deck user:
#
#     bash /media/$USER/<usb-name>/<this-folder>/rollback.sh
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/install.py" --rollback "$@"
