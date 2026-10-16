# Backup and troubleshooting

[← Enabling Discord Rich Presence](ENABLING_DISCORD_RICH_PRESENCE.md) · [Media gallery →](MEDIA.md)

This page covers backing up your Deck and fixing problems with **Ember Deck software**. For hardware faults, wiring, DAC setup, USB devices or Raspberry Pi OS issues, use the documentation for those components. The [installation guide](INSTALLATION.md) covers the hardware and system setup needed to run Ember Deck.

## Back up the Raspberry Pi

A working system image is worth keeping before upgrades or configuration changes. The original build saves backups to a separate server over CIFS; change the server and share names to suit your setup.

First mount the destination. Keep SMB credentials in a protected file rather than typing passwords into the command:

```bash
sudo mkdir -p /mnt/plexserver
sudo mount -t cifs //<SERVER>/<SHARE> /mnt/plexserver \
  -o credentials=/root/.smbcredentials,iocharset=utf8,vers=3.0
```

Confirm the SD card device with `lsblk`, then create the compressed image:

```bash
sudo dd if=/dev/mmcblk0 bs=4M status=progress \
  | gzip -1 > /mnt/plexserver/emberdeck-$(date +%F).img.gz
sync
sudo umount /mnt/plexserver
```

**Important:** `/dev/mmcblk0` is an example, not a guarantee. Imaging a running filesystem may produce an inconsistent backup; for a reliably restorable full image, shut down and image the card from another system. Never reverse the `dd` input and output devices.

For a quick backup before editing software configuration, you can also save `~/EmberDeck/configs/` privately. It may contain Plex and Discord tokens:

```bash
umask 077
tar -czf "$HOME/emberdeck-config-backup-$(date +%Y%m%d-%H%M%S).tar.gz" \
  -C "$HOME/EmberDeck" configs
```

## Start with the service logs

Most Ember Deck software problems can be narrowed down with these commands:

```bash
systemctl --user status emberdeck.service --no-pager
journalctl --user -u emberdeck.service -b -n 100 --no-pager
```

You can also follow the log as events happen:

```bash
cd ~/EmberDeck
./emberdeck_log_console.sh
```

Foundry starts and supervises Ember Deck's workers. **Do not start a second copy of Pawprint or another hardware worker** while the service is running.

To restart the Engine:

```bash
systemctl --user restart emberdeck.service
```

If only the display interface or browser has stopped responding, restart it separately:

```bash
systemctl --user restart ember-ui.service
systemctl --user restart ember-browser.service
```

## Configuration problems

Run the configuration check after changing YAML files or upgrading:

```bash
cd ~/EmberDeck
.venv/bin/python runtime_config.py configs
```

To also check the Plex identity and credentials:

```bash
.venv/bin/python runtime_config.py configs --require-secrets
```

The validator identifies unknown settings, invalid values and missing Plex settings without printing credentials. Remember that YAML distinguishes numbers, strings and booleans. The [configuration reference](CONFIGURATION.md) explains the available settings and their defaults.

Personal settings belong in the applicable `*.local.yaml` files; credentials belong in the separate `*_secrets.yaml` files. Keep these when updating the installed Deck.

## Plex won't connect

The Deck should continue running when Plex is unavailable. Minstrel retries connections without taking down the other workers.

Check the configuration first:

```bash
cd ~/EmberDeck
.venv/bin/python runtime_config.py configs --require-secrets
```

Then inspect the connection state:

```bash
.venv/bin/python -c "import synapse as s; print(s.get_int('/plex/connection_state', -1)); s.close_all()"
```

| State | Meaning | Action |
|:--|:--|:--|
| `0` | Not configured | Check `server_name`, `plex_client_name` and the credentials in `minstrel_secrets.yaml`. |
| `1` | Connecting | Minstrel is attempting a connection. |
| `2` | Connected | Plex is connected; check playback and control routing instead. |
| `3` | Unavailable | Check Plex server availability and the configured server name. Minstrel retries automatically. |
| `4` | Authentication failed | Correct the saved credentials; Minstrel will retry. |
| `-1` | No state published | Check the service journal for Minstrel startup errors. |

Minstrel can replace an expired or rejected token using your saved username and password. If the secrets file is rejected, compare it with `configs/minstrel_secrets.example.yaml` and ensure it is private:

```bash
chmod 600 ~/EmberDeck/configs/minstrel_secrets.yaml
```

## Media buttons control the wrong source

Ember Deck deliberately sends each transport command to **one** source, not every connected player. Active Plex playback takes priority, followed by active Bluetooth playback and local players. Paused sources retain control in some circumstances so Play can resume them.

If the touchscreen works but a physical key appears to do nothing, check the service log for `media target`, `media command routed`, `completed`, `rejected` or `failed`. These show whether Pawprint accepted the action and which software player received it.

Plex must report a connected state before Pawprint routes commands to it. If Plex is playing but control goes elsewhere, check [Plex won't connect](#plex-wont-connect) and the configured `plex_client_name`, which must identify this Deck's Plexamp client rather than another household session.

This guide covers **software detection and routing**, not troubleshooting the electrical button, GPIO wiring or controller hardware.

## UI or browser doesn't start

Check the two display services independently:

```bash
systemctl --user status ember-ui.service ember-browser.service --no-pager
journalctl --user -u ember-ui.service -u ember-browser.service -b -n 100 --no-pager
```

The browser waits for the desktop session and Plexamp's local page. You can test the latter with:

```bash
curl -fsS --max-time 2 http://127.0.0.1:32500 >/dev/null
```

If it is unavailable, check that Plexamp itself is running. For screen, compositor or connector faults, use the Raspberry Pi OS and display documentation.

## Discord Rich Presence isn't working

Rich Presence is optional. A Discord failure should not interrupt playback, controls or LEDs.

Check its configuration and recent messages:

```bash
cd ~/EmberDeck
.venv/bin/python runtime_config.py configs
journalctl --user -u emberdeck.service -b | grep -iE 'rich_presence|discord|artwork_service|artwork_selection'
```

If the panel shows **Disabled** or **No Discord app ID**, check `configs/rich_presence.local.yaml`. If it shows **Not linked**, complete the one-time account link from the Pi's desktop session. If it reports an unavailable SDK or bridge, follow the build and setup steps in [Enabling Discord Rich Presence](ENABLING_DISCORD_RICH_PRESENCE.md).

For missing album covers, hosted artwork and Cloudflare caching, use the [Discord guide's troubleshooting section](ENABLING_DISCORD_RICH_PRESENCE.md#troubleshooting). Keeping those details in one place avoids maintaining two potentially conflicting sets of instructions.

## Shared-memory error after changing settings

Some Synapse buffers have a fixed size. Changing settings such as `led_count`, `waveform_samples`, `metadata_buffer_bytes` or `event_capacity` can make existing buffers incompatible with the new configuration.

Stop the Deck and reboot to clear the old shared-memory layout:

```bash
systemctl --user stop emberdeck.service
sudo reboot
```

A reboot is safer than manually deleting unrelated objects from `/dev/shm`.

## Reporting a software bug

If the problem persists, include the affected feature, what you expected, what happened, and the relevant **redacted** service-log lines. Never post Plex passwords, access tokens, Discord tokens or personal configuration files publicly.
