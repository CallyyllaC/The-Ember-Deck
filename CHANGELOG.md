# Changelog

## Unreleased

### Changed
- Every worker and the UI now honour `configs/<name>.local.yaml`, not only Minstrel, Rich Presence and the artwork service. Before, `runtime_config.py configs` validated overlays such as `pawprint.local.yaml` or `echo.local.yaml` that the worker then ignored. An existing overlay of this kind now takes effect, so check it with `runtime_config.py configs` before restarting; a malformed one stops the worker with a configuration error (Whisper falls back to its defaults).

### Fixed
- A Bluetooth phone or local player reporting an out-of-range position, length or track number no longer stops Pawprint or the MPRIS bridge (and with them the whole worker group); such values are shown as unknown.
- The REC-hold source cycle no longer sticks when a guest app (such as Pear) cannot be launched: HDMI-2 returns to Plex and the next hold moves on.
- Removed README and installation links to an architecture document that is not part of the repository.
- A network blip or Plex server error no longer stops Minstrel using its cached Plex token until the next account login; only a token the server rejects (HTTP 401/403) is set aside. A connection error through a `*.plex.direct` address is no longer mistaken for a rejected login because its host name contains "401" or "403".

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