# Software architecture

[← Hardware and I/O](HARDWARE.md) · [Configuration →](CONFIGURATION.md)

The Ember Engine is a collection of supervised workers rather than one monolithic application. Workers communicate through Synapse shared state, which keeps hardware ownership, media discovery, rendering and UI concerns separate.

Note, HDMI-2 context refers to the touchscreen display.

## Service lifecycle

The repository contains these user-service definitions:

- `emberdeck.service` orders Plexamp first and launches Foundry. Plexamp is a soft dependency: GPIO, Bluetooth, MPRIS, and LEDs still start if it is down.
- `ember-ui.service` launches the Textual UI after the graphical session and runtime are available.
- `ember-browser.service` launches the touchscreen Firefox kiosk after both Wayland and the local Plexamp page respond.

Foundry starts each worker in the order below with its matching YAML path in `CONFIG_PATH`. It monitors process exit and monotonic heartbeat counters. A worker gets a 30-second warm-up and 30-second heartbeat freshness window; 30 consecutive stale checks cause a complete worker-group restart with bounded 5–15 second backoff. Systemd separately restarts Foundry if the supervisor itself exits. The group restart is deliberate: it returns shared ownership to a known state and prevents a second Pawprint from competing for physical I/O.

## Workers

| Worker | Responsibility |
|:--|:--|
| `whisper_daemon.py` | Logging |
| `pawprint.py` | Sole owner of GPIO, ADS1115, HT16K33, uinput, physical debounce/holds, Bluetooth AVRCP state and physical transport routing |
| `hdmi2_source_controller.py` | Owns the single graphical guest-app slot and Labwc rules; consumes REC hold cycle requests |
| `mpris_bridge.py` | Selects one allowed local MPRIS player, publishes metadata and consumes MPRIS command counters |
| `echo.py` | Captures PipeWire/PortAudio input and publishes spectrum/waveform frames |
| `minstrel.py` | Monitors and controls the Deck's named Plexamp client; publishes Plex metadata, artwork and lyrics |
| `aurora.py` | Owns artwork/manual colour policy and publishes physical/UI palettes |
| `foxfire.py` | Converts spectrum, selectors and semantic controls into an RGBA LED frame |
| `willo_wisp.py` | Sole owner of the BlinkStick USB output; reconnects when the controller disappears |

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

Synapse uses one named shared-memory segment per key. Writers own their namespaces: Pawprint owns `/io/*`, `/media/*`, and `/bt/*`; Minstrel owns `/plex/*` and `/lyrics/*`; MPRIS bridge owns `/mpris/*`; Echo owns `/audio/*`; Aurora owns `/theme/*`; and Foxfire owns `/leds/main/*`. Fixed-size array/text segments survive worker restarts. After changing a configured array size such as `event_capacity`, `waveform_samples`, `metadata_buffer_bytes`, or `led_count`, stop the services and reboot (or remove only the named stale segments) before restart.

## Source and transport routing

The physical media keys target the most relevant source:

1. Active Plex playback
2. Active Bluetooth AVRCP media
3. Active local MPRIS playback
4. The selected HDMI-2 context when every source is idle

The decision selects exactly one target; commands are never broadcast. BlueZ discovery and Bluetooth command calls run outside Pawprint's 50 Hz GPIO loop, so a slow or unavailable D-Bus service cannot stop button sampling. Plex and MPRIS commands cross worker boundaries as monotonic counters, which prevents a worker restart from replaying an old physical press.

A paused Plex session retains physical-control ownership so the Play key can resume it. A connected phone or a lingering Bluetooth claim cannot steal that paused route; Bluetooth takes over when it is genuinely playing, and a paused Bluetooth source retains ownership only if Bluetooth was already the active target.

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

Machine-specific device names and credentials should remain outside public configuration or be injected through the documented environment values. See the [configuration reference](CONFIGURATION.md) for every option, unit, default, interaction, validation rule, and restart requirement.

## Logging

Logging has two audiences: the panel shows a small personality-driven event stream, while priority-zero faults are retained as durable rotating JSONL. There is intentional overlap, but the durable files are operational history, not a transcript of ambient UI lines.

The technical answer is that workers send structured messages to Whisper. Whisper owns scheduling, the bound UI and error history. Standard output and error remain available to the user journal. The implementation contract is documented directly in `whisper_daemon.py`, `spirit_messages.py` and `spirit_ink.py`.

For physical controls, Pawprint also publishes quiet diagnostic counters under `/io/health`: accepted/rejected button sequences, last button code, chosen target, command sequence, and dispatch status. Minstrel and MPRIS bridge publish their last command and success state in their own `/control/*` keys.

## Diagnostic tools

- `ember_ui.py` displays media, system state and structured logs.
- `ember_ui_probe.py` checks the data consumed by that UI.
- `album_art_probe.py` exercises media and artwork presentation.
- `lyrics_probe.py` inspects lyrics-related Plex streams.
- `emberdeck_log_console.sh` follows the user-service journal.
