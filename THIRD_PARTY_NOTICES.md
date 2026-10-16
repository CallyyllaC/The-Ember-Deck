# Third-party notices

[Awoo Licence v2.0](LICENSE) applies only to material that the project's Licensor is entitled to license. It does not replace the licences or other terms that apply to third-party software, services, media, names or marks. Project assets, such as the demonstration videos, are identified in [AWOO-ASSETS-DEFINITION.md](AWOO-ASSETS-DEFINITION.md).

## Python dependencies

The repository does not contain installed copies of these packages. They are installed separately using the instructions in `docs/INSTALLATION.md`; `docs/requirements.txt` is a historical full-machine environment snapshot.

| Dependency | Snapshot version | Licence |
|:--|:--|:--|
| [NumPy](https://pypi.org/project/numpy/2.3.5/) | 2.3.5 | BSD 3-Clause |
| [python-sounddevice](https://pypi.org/project/sounddevice/0.5.3/) | 0.5.3 | MIT |
| [PyYAML](https://pypi.org/project/PyYAML/6.0.3/) | 6.0.3 | MIT |
| [PlexAPI](https://pypi.org/project/PlexAPI/4.17.2/) | 4.17.2 | BSD 3-Clause |
| [BlinkStick](https://pypi.org/project/BlinkStick/1.2.0/) | 1.2.0 | BSD 3-Clause |
| [PyUSB](https://pypi.org/project/pyusb/1.3.1/) | 1.3.1 | BSD 3-Clause |
| [Adafruit CircuitPython ADS1x15](https://pypi.org/project/adafruit-circuitpython-ads1x15/3.0.2/) | 3.0.2 | MIT |
| [Adafruit CircuitPython HT16K33](https://pypi.org/project/adafruit-circuitpython-ht16k33/4.6.15/) | 4.6.15 | MIT |
| [Adafruit Blinka](https://pypi.org/project/Adafruit-Blinka/8.69.0/) | 8.69.0 | MIT |
| [gpiozero](https://pypi.org/project/gpiozero/2.0.1/) | 2.0.1 | BSD 3-Clause |
| [Pillow](https://pypi.org/project/pillow/11.1.0/) | 11.1.0 | MIT-CMU |
| [psutil](https://pypi.org/project/psutil/7.0.0/) | 7.0.0 | BSD 3-Clause |
| [Rich](https://pypi.org/project/rich/15.0.0/) | 15.0.0 | MIT |
| [Textual](https://pypi.org/project/textual/8.2.7/) | 8.2.7 | MIT |
| [cryptography](https://pypi.org/project/cryptography/43.0.0/) | 43.0.0 | Apache-2.0 OR BSD-3-Clause |

`cryptography` provides the Ed25519 verification in `awoo_supporter.py`, the retained Awoo supporter-licence verifier. Ember Deck currently defines no Supporter Features, so no runtime service uses it; without `cryptography` the verifier fails closed and everything else is unaffected.

Those packages may carry their own bundled components and notices. A distribution that includes package files, wheels, native libraries or a full runtime environment must preserve the licence and notice files supplied with the exact distributions it ships.

## Separately installed software and services

The project integrates with or documents separately installed software and services including Raspberry Pi OS, Python, systemd, BlueZ, PipeWire, WirePlumber, PortAudio, libusb, playerctl, Firefox, Flatpak, EasyEffects, Plex/Plexamp, Shortwave and Pear. None of those programs or services is distributed in this repository. Their own licences and service terms continue to apply.

## Discord Social SDK and Box64

The optional Discord Rich Presence feature uses the **Discord Social SDK**, proprietary software from Discord Inc. that is downloaded separately from the Discord Developer Portal under Discord's developer terms and policies. No part of the SDK (headers, libraries or binaries) is included in this repository, and the SDK is not covered by the Awoo Licence. `EmberEngine/discord_bridge/` contains only Ember Deck's own source; a compiled `discord_bridge` links against the separately obtained SDK, and anyone distributing such a build is responsible for complying with Discord's terms. Use of Discord's services through the SDK is subject to Discord's Terms of Service, Developer Terms and Developer Policy.

The SDK archive includes its own `License-Notices.txt` for open-source components built into Discord's binaries (for example libuv, WebRTC, jsoncpp and uWebSockets). Those notices travel with the SDK; anyone redistributing the SDK libraries must include them.

Discord Social SDK 1.10.19337 ships its Linux library for x86_64 only, so on a Raspberry Pi the helper runs under [Box64](https://github.com/ptitSeb/box64) (MIT), a separately installed user-space x86_64 emulator. Box64 is not distributed in this repository.

## Discord art-asset icons

The four source icons in `RPC Images/sources/` (`source-bluetooth.svg`, `source-dab.svg`, `source-plex.svg`, `source-ytmusic.svg`) are unmodified [Lucide](https://lucide.dev/) icons ("bluetooth", "radio-tower", "disc-3" and "music-2"), used under the ISC License. The 1024×1024 PNGs in `RPC Images/discord-ready/` are derived from them by `New-AwooDiscordAssets.ps1`. The licence text is in `RPC Images/sources/LICENSE-lucide.txt` and must accompany copies of the icons or the derived PNGs. These generic icons are not the Plex, YouTube Music, Bluetooth or DAB logos.

## Photographs, videos and marks

The `Images/` and `Videos/` directories document the physical build. The 13 MP4s in `Videos/` are intentional audiovisual demonstrations, and their audio is a functional part of them: The Ember Deck is a music playback system. The project-owned footage and photographs are covered by the Awoo Licence as described in [AWOO-ASSETS-DEFINITION.md](AWOO-ASSETS-DEFINITION.md).

Third-party music, recordings, radio broadcasts, trademarks, product designs, user interfaces, album artwork, lyrics or other content audible or visible in that media remain the property of their respective rightsholders, keep their original copyright and licensing status, and are not licensed by the Awoo Licence. Their inclusion in a demonstration does not transfer ownership or grant separate rights to reproduce or redistribute them. The individual tracks and broadcasts are not catalogued and no permission from their rightsholders is recorded, so redistribution rights for the third-party audio are unverified; anyone distributing the videos is responsible for any rights that redistribution requires.

Names such as Hitachi, Raspberry Pi, Plex, Plexamp, Bluetooth, BlinkStick, YouTube, Firefox, Discord and the names of other products or projects are used only to identify the systems with which The Ember Deck works. Their use does not imply ownership or endorsement.
