# Discord bridge

`discord_bridge` is the C++ helper used by Ember Deck's optional Discord Rich Presence integration. It passes requests from `rich_presence.py` to the Discord Social SDK and returns results over JSON lines on standard input and output. It doesn't handle music playback or control any Deck hardware.

For account linking, configuration and hosted album artwork, use the [Discord Rich Presence guide](../../docs/ENABLING_DISCORD_RICH_PRESENCE.md).

## Before building

Download the **Discord Social SDK** from the [Discord Developer Portal](https://discord.com/developers/applications). The SDK is proprietary, is not included in this repository and is governed by Discord's terms, not the AWOO Licence.

Extract it to `~/EmberDeck/third_party/discord_social_sdk/`. The SDK and downloaded ZIP should stay out of Git.

**Raspberry Pi 5:** SDK **1.10.19337** supplies a Linux x86_64 library but no native Linux ARM64 library. The bridge therefore runs through **Box64**. The Android ARM64 library and Windows ARM64 files cannot replace it. You can check a different SDK version with:

```bash
bash ~/EmberDeck/discord_bridge/inspect_sdk.sh
```

The Pi also needs native ALSA, PulseAudio and X11 libraries (`libasound2`, `libpulse0`, `libx11-6`).

## Build on the Raspberry Pi

Install CMake, the x86_64 cross-compiler and Box64. Box64 availability and package sources vary by Raspberry Pi OS installation.

```bash
sudo apt install cmake g++-x86-64-linux-gnu box64
cd ~/EmberDeck/discord_bridge
cmake -S . -B build-x86_64 -DCMAKE_TOOLCHAIN_FILE=cmake/x86_64-linux-gnu.cmake \
  -DDISCORD_SOCIAL_SDK_DIR="$HOME/EmberDeck/third_party/discord_social_sdk" \
  -DBRIDGE_BUILD_TESTS=OFF
cmake --build build-x86_64
```

The resulting executable is `discord_bridge/build-x86_64/discord_bridge`. In `configs/rich_presence.local.yaml`, use:

```yaml
discord:
  backend: auto
  box64:
    executable: box64
    bridge: discord_bridge/build-x86_64/discord_bridge
  sdk_library_dir: third_party/discord_social_sdk/lib/release
```

The cross-build toolchain supplies `-Wl,--allow-shlib-undefined` for the SDK's external dependencies. You shouldn't need to add linker flags yourself. The Python worker automatically sets `UV_USE_IO_URING=0` for Box64; without it, SDK initialisation stalled in testing. This does not change the rest of Ember Deck.

**Browser linking:** the Box64 backend uses the included `launcher/xdg-open` wrapper so Discord can open its authorisation page without blocking the OAuth callback. Keep that script executable with LF line endings. Account linking must be started in the Pi's graphical desktop session, not an ordinary SSH terminal; see the main guide.

## Other builds

A native Linux ARM64 build will only work if a future SDK provides a compatible Linux ARM64 library:

```bash
cmake -S . -B build -DDISCORD_SOCIAL_SDK_DIR="$HOME/EmberDeck/third_party/discord_social_sdk"
cmake --build build && ctest --test-dir build
```

For a Windows development build using MSVC and the SDK's Windows x64 library:

```bat
cmake -S . -B build-win -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release -DBRIDGE_WARNINGS_AS_ERRORS=ON
cmake --build build-win && ctest --test-dir build-win
```

Run the protocol tests without the SDK:

```bash
cmake -S . -B build-tests -DDISCORD_SOCIAL_SDK_DIR=/nonexistent
cmake --build build-tests
ctest --test-dir build-tests
```

## How the bridge communicates

The bridge uses **protocol version 1**: one JSON object per line. Requests contain `id` and `op`; responses return the matching `id`, `ok` and `result`. Events are sent separately with an `event` field.

| Operation | Purpose |
|:--|:--|
| `hello` | Check protocol and SDK version |
| `init` | Initialise with an application ID and external-artwork permission |
| `begin_auth` / `cancel_auth` | Start or cancel desktop account linking |
| `set_token` / `refresh_token` | Connect with a stored token or renew it |
| `set_presence` / `clear_presence` | Update or remove the Discord activity |
| `revoke` | Revoke the linked account token |
| `shutdown` | Shut down cleanly |

The bridge emits `status`, `tokens`, `auth_failed` and `protocol_error` events. Results distinguish successful requests, missing SDK, authentication failures, temporary failures and permanent failures. Temporary network problems do not unlink the user.

`set_presence` accepts the activity type, title/details, state, image assets, image links and timestamps. Small images must use the registered Ember Deck asset keys. Large images can also use public HTTPS artwork URLs when explicitly enabled. Unsafe or private URLs are rejected and fall back to the static Ember Deck image; image links likewise require valid HTTPS addresses. See `protocol.cpp` and the worker's `presence_model.py` for the precise validation rules and field limits.

## Authentication and security

Discord linking uses a **Public Client** with PKCE and the desktop authorisation flow (`Authorize` and `GetToken`). The Discord application must allow `http://127.0.0.1/callback`. The SDK temporarily listens on that loopback address to complete linking. The failed console device-code flow is not used.

The worker stores Discord access and refresh tokens in the private, gitignored `configs/discord_secrets.yaml`, with file mode `0600`, and refreshes them when necessary. The bridge never needs a client secret, bot token or Discord password. Its own process opens no permanent network listener.

Diagnostic output goes to stderr. `rich_presence.py` retains only a limited, redacted error tail if the bridge fails; it does not continuously log SDK output or tokens. The bridge exits when stdin closes, and Linux parent-death handling prevents it from being left running after its worker exits.

## Tested compatibility

The bridge and Discord Rich Presence were successfully used on a **Raspberry Pi 5**, running the x86_64 SDK **1.10.19337** through **Box64 v0.4.5**, with live track details, hosted Plex artwork and playback timing displayed in Discord on **15 October 2026**.

At that point, full-reboot recovery and the revised detached-browser launcher had not been separately re-tested. This is a record of what was verified, not a guarantee for every Pi OS or SDK release.
