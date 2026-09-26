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

Foundry supervises the workers, so inspect the whole runtime rather than starting a second copy of an individual hardware worker. Plexamp is a soft dependency; its failure should leave Pawprint, Bluetooth, MPRIS, and the LEDs running:

```bash
systemctl --user status emberdeck.service
journalctl --user -u emberdeck.service -b -n 250
```

The included compact follower is:

```bash
./emberdeck_log_console.sh
```

Do not create separate services for Pawprint, Echo, Minstrel or Whisper. Duplicate Pawprint instances would compete for GPIO and I²C ownership.

Foundry's workers are one recovery unit. Restart them with:

```bash
systemctl --user restart emberdeck.service
```

The observer services can be recovered independently without touching GPIO:

```bash
systemctl --user restart ember-ui.service
systemctl --user restart ember-browser.service
```

## Physical media key does nothing

Touchscreen control bypasses Pawprint, so first distinguish input, routing and target failure. The quiet `/io/health` counters are more useful than repeatedly pressing buttons while watching a noisy poll log:

```bash
cd ~/EmberDeck
.venv/bin/python - <<'PY'
import synapse as s
for key in (
    "/io/health/button_event_seq",
    "/io/health/button_rejected_seq",
    "/io/health/button_last_code",
    "/io/health/media_target",
    "/io/health/media_command_seq",
    "/io/health/media_command_last_target",
    "/io/health/media_command_last_code",
    "/io/health/media_command_last_status",
    "/plex/control/last_command",
    "/plex/control/last_ok",
    "/mpris/control/last_command",
    "/mpris/control/last_ok",
):
    print(f"{key}: {s.get_int(key, -999)}")
s.close_all()
PY
```

Button codes are Play 1, Stop 2, Fast-forward 3, Rewind 4 (the separate raw button diagnostic uses Rewind 3, Fast-forward 4, and Eject 5). Target codes are none 0, Plex 1, Bluetooth 2, and MPRIS 3. Dispatch status is 0 no command yet, 1 handed to the selected target, 2 confirmed by the direct Bluetooth call, and -1 rejected/failed. Plex and MPRIS final success is in their own `last_ok` key.

- `button_event_seq` does not change: inspect the BCM pin, pull-up, common ground and contact; Pawprint did not accept an edge.
- only `button_rejected_seq` changes: the contact did not remain stable for `button_debounce_s`.
- `media_command_seq` changes: the event was received and routed; inspect the target code and its `last_ok`/journal entry.
- target is Plex while Plex is playing: this is required even if Bluetooth or MPRIS is also active.
- Bluetooth status is stale/unavailable: check `bluetoothctl devices Connected` and the system Bluetooth journal. BlueZ calls are timeout-bounded and run off the GPIO polling loop, so BlueZ failure should not stop later GPIO edges.

Relevant journal messages contain `media target`, `media command routed`, `dispatched`, `completed`, `rejected`, or `failed` without logging every poll.

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

## Shared-memory layout error after configuration change

Synapse arrays have fixed shapes and survive worker restarts. If a log says a key is smaller than the current process requires after changing `led_count`, `waveform_samples`, `metadata_buffer_bytes`, or `event_capacity`, stop the user services and reboot. This clears `/dev/shm` without guessing at segment names. Do not delete unrelated shared-memory objects on a live system.
