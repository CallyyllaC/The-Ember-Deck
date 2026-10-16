# The Ember Deck

The Ember Deck turns a Hitachi TV/radio/cassette unit into a Raspberry Pi 5-powered media console.

It combines Plexamp, Bluetooth and MPRIS-compatible local applications with two displays, the original tape controls, an analogue VU meter, status LEDs and a 24-pixel RGBW audio visualiser.

A small but important note: the donor unit was already dead. Please do not destroy working vintage equipment to copy this project.

![The finished Ember Deck](Images/Top%20Right%20Final.jpg)

The Deck is both a finished machine and an ongoing experiment. These documents record the final working design, how it was put together and the lessons learned along the way.

They are not a drop-in recipe for every vintage radio. Hardware and software requirements will vary.

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
| [Architecture reference](docs/ARCHITECTURE.md) | Process model, supervision, Synapse key registry, failure behaviour, risks and adding a worker |
| [Configuration reference](docs/CONFIGURATION.md) | Every runtime option, units, defaults, interactions and restart requirements |
| [Installation](docs/INSTALLATION.md) | Raspberry Pi packages, Python environment, permissions and services |
| [Enabling Discord Rich Presence](docs/ENABLING_DISCORD_RICH_PRESENCE.md) | Optional Discord presence and hosted Plex artwork: setup, configuration, privacy and removal |
| [Backup and troubleshooting](docs/TROUBLESHOOTING.md) | System images, audio routing, USB recovery and common checks |
| [Media gallery](docs/MEDIA.md) | Finished-build photographs and demonstration videos |
| [Changelog](CHANGELOG.md) | v2 changes, upgrade steps from V1 and known limitations |

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

Workers that own hardware are restarted together as one group, so GPIO, I²C, USB and audio always return to a known owner. Optional services degrade instead: if Plex, Bluetooth or a local player is unavailable, the rest of the Deck keeps running.

The individual workers and their responsibilities are explained properly in the [software architecture guide](docs/SOFTWARE.md).

## Plex sign-in and secrets

The Deck signs in to Plex unattended with your Plex account username and password, so nobody needs to log into the Pi to paste in a fresh token.

- Non-secret settings, this Deck's Plexamp client name (`plex_client_name`) and your Plex server name (`server_name`), go in `configs/minstrel.local.yaml`, which is merged over the shipped `minstrel.yaml` template and ignored by Git (setting them in `minstrel.yaml` itself also works).
- The username and password go in `configs/minstrel_secrets.yaml`. Create it from [`minstrel_secrets.example.yaml`](EmberEngine/configs/minstrel_secrets.example.yaml) and run `chmod 600` on it. Git ignores the file, so it is never committed.
- Minstrel stores the server URL and token it receives in the `cache` section of that same file and uses them for a quick start next time. You never edit the cache by hand.
- If the cached token is rejected or revoked, Minstrel signs in again with the username and password, saves the new token, and carries on.
- `EMBERDECK_PLEX_*` environment variables override the file and are never written into it.
- Without credentials, with a wrong password, or while Plex is down, the Deck keeps running. Plex shows as unavailable, the panel log reports what is wrong, and Minstrel keeps retrying at growing intervals. Fixing the file takes effect without a restart.

Upgrading from an older setup? If `minstrel.yaml` still contains `username`, `password`, `url` or `token`, those values keep working, but Minstrel reports them until you move them into `minstrel_secrets.yaml`. Minstrel no longer writes tokens into `minstrel.yaml`.

Setup steps are in the [installation guide](docs/INSTALLATION.md). The full precedence and refresh rules are in the [architecture reference](docs/ARCHITECTURE.md#secrets-and-plex-authentication).

## Discord Rich Presence (optional)

Ember Deck can show what it is doing on your Discord profile, for example "Listening to Ember Deck · Playing music · via Plex", with the source's icon and a progress or listening timer (track titles only if you turn them on). It is an ordinary, optional integration available to everyone: it needs only a Discord application ID and a one-time Discord account link, and no supporter licence or payment.

- It is entirely optional. Without Discord the Deck behaves exactly as it always has, and the Discord Social SDK and Box64 are needed only for this integration.
- Presence is private by default. It shows playing/idle and the source, but not track titles, artists, device names or anything about your network. Track and artist can be turned on in `configs/rich_presence.local.yaml`.
- The Deck links to Discord without a Discord app on the Pi, using the Discord Social SDK through a small native helper. The current SDK (1.10.19337) ships its Linux library for x86_64 only, so on the Pi the helper runs under Box64; a native ARM64 helper is used automatically if Discord ships one later. On a Raspberry Pi 5, Rich Presence is confirmed end to end on hardware, including hosted Plex album artwork. Linking uses Discord's desktop sign-in page on the Deck itself. Reboot recovery and a fresh link with the current browser launcher have not yet been re-tested (see the [verification record](docs/ENABLING_DISCORD_RICH_PRESENCE.md#hardware-verification-record-15-october-2026)).
- Discord trouble never restarts the hardware workers.

Setup, configuration and troubleshooting are in [Enabling Discord Rich Presence](docs/ENABLING_DISCORD_RICH_PRESENCE.md).

## Important safety note

The donor unit originally contained CRT circuitry, and the finished Deck contains mains wiring.

CRT capacitors can retain lethal voltages after the unit has been disconnected. Mains wiring is equally capable of ruining your day, permanently.

If you are not competent enough to identify, discharge, isolate and safely enclose those parts, do not work on them. Get help from someone who is.

## Project status

The Ember Deck is at its **v2 release**.

v2 is a software release on the finished V1 hardware. It adds optional Discord Rich Presence with hosted Plex artwork, unattended Plex sign-in with credentials kept in a separate private file, per-Deck local configuration overlays, strict configuration validation and more resilient worker supervision. The full list, upgrade steps and known limitations are in the [changelog](CHANGELOG.md).

The hardware works, the software works, and the final photographs and demonstration videos have been recorded. After nine months of on-and-off tinkering, I am happy to call this chapter finished and move on to something new.

There are still a few physical blemishes, mostly hot glue, rough edges and small 3D-printing errors. I may tidy those up later, and there will probably be future upgrades when the urge returns.

For now, though, it is done.

## Licence

The current version of The Ember Deck is available under the
[Awoo Licence v2.0](https://awoo.ltd/licence/); the complete terms are in
[LICENSE](LICENSE). Third-party components and material remain subject to their
own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The
demonstration videos, photographs and other project assets are described in
[AWOO-ASSETS-DEFINITION.md](AWOO-ASSETS-DEFINITION.md): the project's own
footage is covered by the licence, but the music and other third-party content
in it is not.
