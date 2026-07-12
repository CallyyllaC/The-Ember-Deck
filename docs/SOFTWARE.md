# Software architecture

[← Hardware and I/O](HARDWARE.md) · [Installation →](INSTALLATION.md)

The Ember Engine is a collection of supervised workers rather than one monolithic application. Workers communicate through Synapse shared state, which keeps hardware ownership, media discovery, rendering and UI concerns separate.

Note, HDMI-2 context refers to the touchscreen display.

## Service lifecycle

The repository contains these user-service definitions:

- `emberdeck.service` waits for Plexamp and launches Foundry.
- `ember-ui.service` launches the Textual UI after the graphical session and runtime are available.
- `run_ember_browser.sh` launches the touchscreen firefox window for Plex, this was originally launched via python but there were timing issues during boot.

Foundry starts each worker with its matching YAML path in `CONFIG_PATH`, monitors heartbeat counters, and restarts unhealthy processes with bounded backoff.

## Workers

| Worker | Responsibility |
|:--|:--|
| `whisper_daemon.py` | Logging |
| `pawprint.py` | GPIO |
| `hdmi2_source_controller.py` | Source Control |
| `mpris_bridge.py` | MPRIS Manager |
| `echo.py` | Captures audio spectrum |
| `minstrel.py` | Plex Manager |
| `aurora.py` | Colour Manager |
| `foxfire.py` | Transforms audio data into a visualiser |
| `willo_wisp.py` | Drives LED Strip |

## Shared-state flow

```text
Physical controls ──► Pawprint ───────────────┐
Plexamp ────────────► Minstrel ───────────────┤
Local players ──────► MPRIS bridge ───────────┤
                                                ▼
                                         Synapse state
                                                │
                    ┌───────────────────────────┼──────────────┐
                    ▼                           ▼              ▼
                 Ember UI                    Aurora         Echo
                                                │              │
                                                └────► Foxfire ┘
                                                        │
                                                        ▼
                                                    Willo Wisp
```

Pawprint remains the single hardware owner. Other workers request state changes instead of opening GPIO or I²C devices themselves.

## Source and transport routing

The physical media keys target the most relevant source:

1. Active Plex playback
2. Active Bluetooth AVRCP media
3. Active local MPRIS playback
4. The selected HDMI-2 context when every source is idle

Holding REC cycles the HDMI-2 context through Plex, radio and YouTube. The source controller launches or closes the corresponding application, while the MPRIS bridge publishes generic metadata and accepts transport counters.

## Configuration

Configuration files live in `EmberEngine/configs`:

| File | Owner | Secrets expected? |
|:--|:--|:--:|
| `aurora.yaml` | Aurora | No |
| `echo.yaml` | Echo | No |
| `ember_ui.yaml` | Ember UI | No |
| `foxfire.yaml` | Foxfire | No |
| `hdmi2_source_controller.yaml` | HDMI-2 controller | No |
| `minstrel.yaml` | Minstrel/Plex | Potentially; never commit a Plex token |
| `mpris_bridge.yaml` | MPRIS bridge | No |
| `pawprint.yaml` | Pawprint | No |
| `pawprint_state.yaml` | Persisted control state | No |
| `whisper_daemon.yaml` | Whisper | No |
| `willo_wisp.yaml` | Will-o'-the-wisp | No |

Machine-specific device names and credentials should remain outside public configuration or be injected through the environment.

## Logging

The non technical answer is that logging is split into, UI shown fancy logs which give the device ambience, and real warning signs are wrote to disk logs, there is an overlap but they are still seperate enough to be concidered different.

The technical answer is that workers send structured messages to Whisper. Whisper owns scheduling, the bound UI and error history. Standard output and error remain available to the user journal. The implementation contract is documented directly in `whisper_daemon.py`, `spirit_messages.py` and `spirit_ink.py`.

## Diagnostic tools

- `ember_ui.py` displays media, system state and structured logs.
- `ember_ui_probe.py` checks the data consumed by that UI.
- `album_art_probe.py` exercises media and artwork presentation.
- `lyrics_probe.py` inspects lyrics-related Plex streams.
- `emberdeck_log_console.sh` follows the user-service journal.
