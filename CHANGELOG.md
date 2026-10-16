# Changelog

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