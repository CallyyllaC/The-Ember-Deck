# Backup and troubleshooting

[← Installation](INSTALLATION.md) · [Project overview](../README.md)

The most useful recovery tool is a known-good system image. Make one after the hardware works and before experimenting with audio routing, desktop services or package upgrades.

## Back up the Raspberry Pi

The original build stores images on a separate server over CIFS. Create a protected mount point and supply credentials through a credentials file where possible; avoid placing passwords directly in shell history.

```bash
sudo mkdir -p /mnt/plexserver
sudo mount -t cifs //<SERVER>/<SHARE> /mnt/plexserver \
  -o credentials=/root/.smbcredentials,iocharset=utf8,vers=3.0
```

Create a compressed image of the card containing the running root filesystem:

```bash
sudo dd if=/dev/mmcblk0 bs=4M status=progress \
  | gzip -1 > /mnt/plexserver/emberdeck-$(date +%F).img.gz
```

Then flush writes and unmount it:

```bash
sync
sudo umount /mnt/plexserver
```

Confirm the actual card device with `lsblk` before running `dd`. Reversing the input and output or choosing the wrong device can destroy data.

## No sound from Plexamp

First establish which layer has failed:

```bash
lsusb
aplay -l
pactl list short sinks
wpctl status
pactl info | grep 'Default Sink'
```

- Missing from `lsusb`: investigate power, the hub, cable and USB enumeration.
- Present in USB but absent from `aplay -l`: investigate `snd_usb_audio`.
- Present in ALSA but absent from `pactl`: restart or inspect PipeWire and WirePlumber.
- Present as a sink but silent: set it as the default and confirm Plexamp is routed to it.

Test outside Plexamp:

```bash
pw-play /usr/share/sounds/alsa/Front_Center.wav
speaker-test -c2 -twav -D default
```

The repository’s `ensure_dac.sh` automates this staged diagnosis for the Burr-Brown DAC. Its privileged recovery helper is intentionally separate and should remain narrowly scoped.

## PipeWire service checks

```bash
systemctl --user status pipewire.service pipewire-pulse.service wireplumber.service
journalctl --user -u pipewire.service -u pipewire-pulse.service -u wireplumber.service -b
```

If the DAC is visible to ALSA but no sink is created, restart the user audio stack:

```bash
systemctl --user restart wireplumber.service pipewire.service pipewire-pulse.service
```

Avoid relying on a numeric `wpctl` device ID in permanent configuration; IDs can change across boots.

## Worker or runtime failure

Foundry supervises the workers, so inspect the whole runtime rather than starting a second copy of an individual hardware worker:

```bash
systemctl --user status emberdeck.service
journalctl --user -u emberdeck.service -b -n 250
```

The included compact follower is:

```bash
./emberdeck_log_console.sh
```

Do not create separate services for Pawprint, Echo, Minstrel or Whisper. Duplicate Pawprint instances would compete for GPIO and I²C ownership.

## I²C hardware missing

```bash
i2cdetect -y 1
groups
```

Expected addresses are `0x48` for ADS1115 and `0x70` for HT16K33. If neither appears, confirm I²C is enabled and inspect SDA, SCL, power and ground. If the devices appear but the service cannot open them, confirm the runtime user belongs to the appropriate groups and has logged in again since membership changed.

## BlinkStick unavailable

```bash
lsusb
blinkstick --info
```

If the controller appears over USB but access is denied, reload the udev rules documented in the installation guide and reconnect it. Older Python packages may also require:

```python
from collections.abc import Callable
```

instead of importing `Callable` from `collections`.

## Browser kiosk does not launch

The launcher waits for both the Wayland socket and the local Plexamp page. Check them independently:

```bash
echo "$XDG_RUNTIME_DIR"
echo "$WAYLAND_DISPLAY"
test -S "${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY:-wayland-0}"
curl -fsS --max-time 2 http://127.0.0.1:32500 >/dev/null
command -v firefox
```

## Bluetooth cannot connect

```bash
systemctl status bluetooth.service
bluetoothctl
```

Within `bluetoothctl`, verify the adapter is powered, then pair, trust and connect the device. PipeWire also needs the Bluetooth SPA package installed before it can expose the phone as an audio source.

