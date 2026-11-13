# Changelog

## Unreleased

### Fixed
- A Bluetooth phone or local player reporting an out-of-range position, length or track number no longer stops Pawprint or the MPRIS bridge (and with them the whole worker group); such values are shown as unknown.
- The REC-hold source cycle no longer sticks when a guest app (such as Pear) cannot be launched: HDMI-2 returns to Plex and the next hold moves on.
- Removed README and installation links to an architecture document that is not part of the repository.

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