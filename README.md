# The Ember Deck

The Ember Deck turns a Hitachi TV/radio/cassette unit into a Raspberry Pi 5-powered media console.

It combines Plexamp, Bluetooth and MPRIS-compatible local applications with two displays, the original tape controls, an analogue VU meter, status LEDs and a 24-pixel RGBW audio visualiser.

A small but important note: the donor unit was already dead. Please do not destroy working vintage equipment to copy this project.

![The finished Ember Deck](Images/Top%20Right%20Final.jpg)

The Deck is both a finished machine and an ongoing experiment. These documents record the final working design, how it was put together and the lessons learned along the way.

They are not a drop-in recipe for every vintage radio. Hardware and Software requirments will vary.

> [Watch the final testing demonstration](Videos/Final%20Testing%20Demo.mp4)

## What it does

- Runs Plexamp Headless as its main music player.
- Accepts Bluetooth audio and local MPRIS players.
- Displays Plex metadata and lyrics.
- Shows whatever useful metadata is available from Bluetooth and MPRIS sources.
- Uses the original tape keys for media controls and safe shutdown.
- Uses the original selectors for visualiser modes and source context.
- Drives a ten-segment track-progress display and six status indicators.
- Renders live audio effects on a 24-pixel RGBW strip.
- Shows media, lyrics, diagnostics and system state across two displays.
- Supervises its worker processes and presents structured, personality-driven logs.
- Provides Ethernet, fast-charge USB, USB data, stereo line input and monitored headphone output.
- Displays live internal voltage and current draw on a dedicated panel meter.

## Documentation
The documentation has been split into separate guides.

| Guide | Contents |
|:--|:--|
| [Build story](docs/BUILD.md) | Teardown, restoration, speakers, packaging and lessons learned |
| [Hardware and I/O](docs/HARDWARE.md) | Power design, peripherals, GPIO, I²C, controls and indicators |
| [Software architecture](docs/SOFTWARE.md) | Runtime services, workers, shared state and source routing |
| [Installation](docs/INSTALLATION.md) | Raspberry Pi packages, Python environment, permissions and services |
| [Backup and troubleshooting](docs/TROUBLESHOOTING.md) | System images, audio routing, USB recovery and common checks |
| [Media gallery](docs/MEDIA.md) | Finished-build photographs and demonstration videos |

## Runtime at a glance

`FoundryCore.py` supervises the Ember Engine workers:

```text
Plexamp / Bluetooth / MPRIS / hardware
                  │
                  ▼
      Pawprint · Minstrel · MPRIS bridge
                  │
             Synapse state
                  │
       Echo · Aurora · Foxfire · UI
                  │
       LEDs · displays · status output
```

The code lives in [`EmberEngine`](EmberEngine). Each configurable worker has a matching YAML file under [`EmberEngine/configs`](EmberEngine/configs).

The individual workers and their responsibilities are explained properly in the [software architecture guide](docs/SOFTWARE.md).

## Important safety note

The donor unit originally contained CRT circuitry, and the finished Deck contains mains wiring.

CRT capacitors can retain lethal voltages after the unit has been disconnected. Mains wiring is equally capable of ruining your day, permanently.

If you are not competent enough to identify, discharge, isolate and safely enclose those parts, do not work on them. Get help from someone who is.

## Project status

The Ember Deck is now paused at its **V1 release**.

The hardware works, the software works, and the final photographs and demonstration videos have been recorded. After nine months of on-and-off tinkering, I am happy to call this chapter finished and move on to something new.

There are still a few physical blemishes, mostly hot glue, rough edges and small 3D-printing errors. I may tidy those up later, and there will probably be future upgrades when the urge returns.

For now, though, it is done.