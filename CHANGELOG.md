# Changelog

## Unreleased

### Changed
- Pawprint's virtual uinput media-key device is off by default (`media_keys_enabled: false`). The tape transport keys always go directly to Plex, Bluetooth or the local MPRIS player, so nothing ever reached the device; with it on and the uinput rule missing, Pawprint logged a setup error every `media_reconnect_s`. The uinput module and udev rule are now an optional install step. An installed rule does no harm and can be left in place.
- Every worker and the UI now honour `configs/<name>.local.yaml`, not only Minstrel, Rich Presence and the artwork service. Before, `runtime_config.py configs` validated overlays such as `pawprint.local.yaml` or `echo.local.yaml` that the worker then ignored. An existing overlay of this kind now takes effect, so check it with `runtime_config.py configs` before restarting; a malformed one stops the worker with a configuration error (Whisper falls back to its defaults).

### Fixed
- A Bluetooth phone or local player reporting an out-of-range position, length or track number no longer stops Pawprint or the MPRIS bridge (and with them the whole worker group); such values are shown as unknown.
- The REC-hold source cycle no longer sticks when a guest app (such as Pear) cannot be launched: HDMI-2 returns to Plex and the next hold moves on.
- Removed README and installation links to an architecture document that is not part of the repository.
- A network blip or Plex server error no longer stops Minstrel using its cached Plex token until the next account login; only a token the server rejects (HTTP 401/403) is set aside. A connection error through a `*.plex.direct` address is no longer mistaken for a rejected login because its host name contains "401" or "403".
- Echo opens a numeric `device` (for example `device: 3`) by its index instead of searching for a device whose name contains "3". A negative id is now rejected by validation.
- A worker that reads a shared-memory value at the instant another worker is first creating it now sees the value as not there yet and reads it again on its next pass, instead of exiting with `ValueError: cannot mmap an empty file` (which restarted the whole worker group).
- Whisper no longer stays "wounded" for good after an outage that has ended: Echo (audio capture), Will-o'-Wisp (BlinkStick), Rich Presence (token file permissions, now noticed after a `chmod` alone) and Foundry (a worker-scoped restart, once the worker has been healthy for five minutes) report their recovery. One-off command failures still hold the mood until Whisper restarts; see Known limitations.

### Known limitations
- Errors that report a single failed action rather than an ongoing condition (a rejected Plex, Bluetooth or MPRIS command, a guest app that would not start, an I2C indicator write) have no recovery event, so after one of them Whisper's messages stay in the "wounded" mood until Whisper restarts. Whether such errors should expire is an open design decision.

---

## v2.0 — 16 October 2026

### Added
- Discord Rich Presence with track information, album artwork, and project links.
- Optional hosted album artwork service.
- Automatic Plex authentication and reconnection.
- Local configuration overrides and validation.
- Automated tests and expanded documentation.

### Improved
- More reliable service supervision, recovery, and restart handling.
- Better Plex connection handling and media control.
- Improved logging, display handling, and hardware resilience.
- More secure credential storage and configuration management.

### Fixed
- Long-uptime heartbeat overflow.
- Plex connection failures unnecessarily restarting hardware services.
- Worker crashes and reconnection issues.
- Various logging, display, and configuration issues.

### Notes
- v2.0 runs on the existing V1 hardware without hardware modifications.
- Discord Rich Presence is optional and disabled by default.
- See [Installation](docs/INSTALLATION.md) for setup and upgrade instructions.
- Licensed under the [AWOO Licence v2.0](LICENSE).

---

## v1.1
Various bug fixes and stability improvments.

## v1.0
Initial release of The Ember Deck.