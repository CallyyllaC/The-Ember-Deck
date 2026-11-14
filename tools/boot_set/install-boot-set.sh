#!/usr/bin/env bash
# Ember Deck boot set: the awoo boot logo and the boot running light.
#
#   sudo bash install-boot-set.sh             install (asks before changing anything)
#   sudo bash install-boot-set.sh --yes       install without asking
#   sudo bash install-boot-set.sh --force     also replace theme files changed on this Deck
#   sudo bash install-boot-set.sh --uninstall put everything back as it was
#
# Unlike the Ember Deck update, this changes system settings, so it lists
# every change first:
#   * copies the "emberdeck" Plymouth theme to /usr/share/plymouth/themes and
#     makes it the boot theme (rebuilds the boot image; about a minute);
#   * turns the boot splash on if it is off (raspi-config's own setting);
#   * installs and enables emberdeck-boot-lights.service.
# What it replaced is recorded in /var/lib/emberdeck-boot-set for --uninstall.
# Needs the Ember Deck update installed first (it reads from ~/EmberDeck).
set -euo pipefail

if [ "$(id -u)" != "0" ]; then
  exec sudo bash "$0" "$@"
fi

USER_NAME="${SUDO_USER:-}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
  echo "Run this with sudo from the Ember Deck user's account, not as root directly." >&2
  exit 1
fi
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
DECK="$HOME_DIR/EmberDeck"
THEME_SRC="$DECK/boot/plymouth/emberdeck"
UNIT_SRC="$DECK/system/emberdeck-boot-lights.service"
THEME_DIR="/usr/share/plymouth/themes/emberdeck"
UNIT_DEST="/etc/systemd/system/emberdeck-boot-lights.service"
STATE="/var/lib/emberdeck-boot-set"
CMDLINE="/boot/firmware/cmdline.txt"
[ -f "$CMDLINE" ] || CMDLINE="/boot/cmdline.txt"

uninstall() {
  echo "Removing the Ember Deck boot set..."
  if systemctl list-unit-files emberdeck-boot-lights.service >/dev/null 2>&1; then
    systemctl disable --now emberdeck-boot-lights.service 2>/dev/null || true
  fi
  rm -f "$UNIT_DEST"
  systemctl daemon-reload
  if [ -f "$STATE/splash-enabled" ]; then
    if command -v raspi-config >/dev/null 2>&1; then
      raspi-config nonint do_boot_splash 1
    elif [ -f "$STATE/cmdline.txt" ]; then
      cp "$STATE/cmdline.txt" "$CMDLINE"
    fi
  fi
  PREVIOUS="$(cat "$STATE/previous-theme" 2>/dev/null || echo pix)"
  if command -v plymouth-set-default-theme >/dev/null 2>&1; then
    echo "Restoring the boot theme \"$PREVIOUS\" (rebuilding the boot image)..."
    plymouth-set-default-theme -R "$PREVIOUS"
  fi
  rm -rf "$THEME_DIR" "$STATE"
  echo "Done. Reboot to see the previous boot screen."
}

YES=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --uninstall) uninstall; exit 0 ;;
    --yes) YES=1 ;;
    --force) FORCE=1 ;;
    *) echo "Unknown option: $arg (use --yes, --force or --uninstall)" >&2; exit 2 ;;
  esac
done

for path in "$THEME_SRC/emberdeck.script" "$UNIT_SRC" "$DECK/boot_lights.py" "$DECK/.venv/bin/python"; do
  if [ ! -e "$path" ]; then
    echo "Missing $path. Install the Ember Deck update first." >&2
    exit 1
  fi
done

SPLASH_ON=1
grep -qw splash "$CMDLINE" || SPLASH_ON=0

echo "This will change these system settings:"
echo "  1. Copy the boot theme to $THEME_DIR and make it the boot theme"
echo "     (rebuilds the boot image; about a minute)."
if [ -d "$THEME_DIR" ] && [ "$FORCE" = "0" ]; then
  echo "     Theme files you have changed there are kept (--force replaces them)."
fi
if [ "$SPLASH_ON" = "0" ]; then
  echo "  2. Turn the boot splash on (it is off in $CMDLINE)."
fi
echo "  3. Install and enable $UNIT_DEST for user $USER_NAME."
echo "Undo all of it later with: sudo bash $0 --uninstall"
if [ "$YES" = "0" ]; then
  read -r -p "Go ahead? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Nothing changed."; exit 0 ;; esac
fi

mkdir -p "$STATE"
if ! command -v plymouth-set-default-theme >/dev/null 2>&1; then
  echo "Installing Plymouth..."
  apt-get update
  apt-get install -y plymouth plymouth-themes
fi
if [ ! -f "$STATE/previous-theme" ]; then
  plymouth-set-default-theme > "$STATE/previous-theme" 2>/dev/null || echo pix > "$STATE/previous-theme"
fi

if [ "$SPLASH_ON" = "0" ]; then
  cp "$CMDLINE" "$STATE/cmdline.txt"
  if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_boot_splash 0
  else
    sed -i '1 s/$/ quiet splash plymouth.ignore-serial-consoles/' "$CMDLINE"
  fi
  touch "$STATE/splash-enabled"
fi

echo "Installing the boot theme..."
# A file already installed that differs from this version was changed on this
# Deck (for example images flipped for an upside-down screen, or the script
# adjusted for its displays). Keep it unless --force; save the new version
# beside the install state so it can be compared or copied in by hand.
install -d -m 0755 "$THEME_DIR"
KEPT=()
for src in "$THEME_SRC"/*; do
  name="$(basename "$src")"
  dest="$THEME_DIR/$name"
  if [ -f "$dest" ] && ! cmp -s "$src" "$dest" && [ "$FORCE" = "0" ]; then
    KEPT+=("$name")
    install -d -m 0755 "$STATE/new-theme"
    install -m 0644 "$src" "$STATE/new-theme/$name"
  else
    install -m 0644 "$src" "$dest"
  fi
done
if [ "${#KEPT[@]}" -gt 0 ]; then
  echo "Kept your changed theme files: ${KEPT[*]}"
  echo "  (this version's copies are in $STATE/new-theme; --force replaces yours)"
fi
plymouth-set-default-theme -R emberdeck

echo "Installing the boot running light..."
sed -e "s#\[USERNAME\]#$USER_NAME#g" -e "s#\[HOME\]#$HOME_DIR#g" "$UNIT_SRC" > "$UNIT_DEST"
chmod 0644 "$UNIT_DEST"
systemctl daemon-reload
systemctl enable emberdeck-boot-lights.service

echo
echo "Boot set installed. Reboot to see it: sudo reboot"
echo "Remove it with: sudo bash $0 --uninstall"
