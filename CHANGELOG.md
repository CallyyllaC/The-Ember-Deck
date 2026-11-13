# Changelog

## Unreleased

### Added
- Phosphor oscilloscope: the Scope page's trace is drawn as pixels (an anti-aliased trace with a halo in the album palette's accent colour and a short afterglow), from the same Echo waveform as before. It replaces the character-cell scope.
- USB update (`tools/usb_update/`): installs kitty, backs up `~/EmberDeck`, copies the new code in without changing any config file, and restores the backup by itself if the Deck does not come up healthy. `rollback.sh` undoes it later.
- Large lyrics: on Now Playing and Scope, the current lyric line is drawn as dot-matrix capitals about two and a half times the height of normal text, with the previous and next lines around it. Lines too long for the lyric band, and text the dot font cannot draw (CJK, emoji), stay normal text. Set `large_lyrics: false` in `ember_ui.yaml` to turn it off.

### Changed
- The panel UI now runs in the kitty terminal instead of LXTerminal, which the phosphor oscilloscope needs: install it with `sudo apt install kitty` (the USB update does this). It reuses the `emberdeck` LXTerminal profile's font and keeps existing Labwc window rules matching, by reading them; no configuration file changes. New optional `ember_ui.yaml` settings: `scope_pixel_fps` and `scope_image_update`.
- Pawprint's virtual uinput media-key device is off by default (`media_keys_enabled: false`). The tape transport keys always go directly to Plex, Bluetooth or the local MPRIS player, so nothing ever reached the device; with it on and the uinput rule missing, Pawprint logged a setup error every `media_reconnect_s`. The uinput module and udev rule are now an optional install step. An installed rule does no harm and can be left in place.
- The panel UI's layout now works on any terminal grid from about 56 × 20 cells up to 80 × 60, so the `emberdeck` LXTerminal profile can use a bigger font for readability. Header text is cut short instead of wrapping, panel headings sit in the panel borders, and the Now Playing header shows track number, year, genre and audio format instead of repeating the title, artist, album and progress shown in the panel below it. The console puts heartbeats, host diagnostics and control inputs in one band above a full-width log. Track Info leaves out fields the current source does not report instead of listing N/A.
- Every worker and the UI now honour `configs/<name>.local.yaml`, not only Minstrel, Rich Presence and the artwork service. Before, `runtime_config.py configs` validated overlays such as `pawprint.local.yaml` or `echo.local.yaml` that the worker then ignored. An existing overlay of this kind now takes effect, so check it with `runtime_config.py configs` before restarting; a malformed one stops the worker with a configuration error (Whisper falls back to its defaults).

### Fixed
- The console's log panel no longer hides the newest entries. It clipped from the bottom, so once lines wrapped, the most recent third of the log was cut off. The panel now always ends on the newest line.
- The console's heartbeat list no longer drops services on a 30-row grid; all seven now show on one line, with an age only for a service that is late.
- A page now draws its current content as soon as the selector switches to it, instead of briefly showing what it held when it was last visible.
- A Bluetooth phone or local player reporting an out-of-range position, length or track number no longer stops Pawprint or the MPRIS bridge (and with them the whole worker group); such values are shown as unknown.
- The REC-hold source cycle no longer sticks when a guest app (such as Pear) cannot be launched: HDMI-2 returns to Plex and the next hold moves on.
- Removed README and installation links to an architecture document that is not part of the repository.
- A network blip or Plex server error no longer stops Minstrel using its cached Plex token until the next account login; only a token the server rejects (HTTP 401/403) is set aside. A connection error through a `*.plex.direct` address is no longer mistaken for a rejected login because its host name contains "401" or "403".
- Echo opens a numeric `device` (for example `device: 3`) by its index instead of searching for a device whose name contains "3". A negative id is now rejected by validation.
- A worker that reads a shared-memory value at the instant another worker is first creating it now sees the value as not there yet and reads it again on its next pass, instead of exiting with `ValueError: cannot mmap an empty file` (which restarted the whole worker group).
- Whisper no longer stays "wounded" for good after an outage that has ended: Echo (audio capture), Will-o'-Wisp (BlinkStick), Rich Presence (token file permissions, now noticed after a `chmod` alone) and Foundry (a worker-scoped restart, once the worker has been healthy for five minutes) report their recovery, and so does the artwork service once it can listen again. Errors that report a single failed action (a rejected Plex, Bluetooth or MPRIS command, a guest app that would not start, a failed control-state save) now count only until `error_dedupe_cooldown_s` passes without a repeat; one that keeps happening keeps the mood.
- Bluetooth titles, artists, albums and player names that contain an apostrophe are shown instead of N/A, and the phone battery (`/bt/phone_battery_pct`) is read correctly instead of always 0. gdbus prints such strings in double quotes and bytes in hexadecimal; Pawprint and the MPRIS bridge now read its output with a small parser that follows GLib's printing rules (`gvariant_text.py`). Not yet checked against a real phone on the Deck.

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