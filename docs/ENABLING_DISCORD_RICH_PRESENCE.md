# Discord Rich Presence

[← Installation](INSTALLATION.md) · [Troubleshooting →](TROUBLESHOOTING.md)

Discord Rich Presence lets the Ember Deck show its current playback source on your Discord profile. You can also choose to show the track, artist, album and artwork. It's optional, free for everyone and off by default.

This guide assumes you've [installed Ember Deck](INSTALLATION.md) on a Raspberry Pi and copied `EmberEngine/` to `~/EmberDeck`. Commands below run on the Pi unless stated otherwise.

## Before you start

You'll need:

- A Discord account and access to the [Discord Developer Portal](https://discord.com/developers/applications).
- The **Discord Social SDK** for C++, downloaded separately from Discord. It isn't included in the repository.
- `cmake`, the x86_64 cross-compiler and **Box64**. The tested Social SDK (1.10.19337) provides an x86_64 Linux library, so the Pi 5 uses Box64 to run the bridge.
- A desktop session on the Pi (locally or through VNC) for the one-time Discord sign-in. SSH alone won't work for that step.

For hosted **Plex** album artwork, you'll also need a domain managed through Cloudflare and `cloudflared` on the Pi. You can enable Rich Presence without any of that.

## 1. Create a Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application. Its name is what Discord displays as the activity name.
2. Enable the **Social SDK** for that application, following Discord's instructions.
3. Under **OAuth2**, enable **Public Client** and add `http://127.0.0.1/callback` as a redirect URL. You do **not** need a client secret or bot token.
4. Under **Rich Presence → Art Assets**, upload the optional images with these exact keys: `ember-deck`, `source-plex`, `source-dab`, `source-ytmusic`, `source-bluetooth`. The repository's `RPC Images/` folder contains icons and a preparation script.
5. Copy your **Application ID** from General Information.

Rich Presence still works without the uploaded art assets; Discord simply won't have those images to display.

## 2. Install the SDK and build the bridge

Unpack the Social SDK so that its directory is:

`~/EmberDeck/third_party/discord_social_sdk/`

Check the SDK package:

```bash
bash ~/EmberDeck/discord_bridge/inspect_sdk.sh
```

Install the build tools:

```bash
sudo apt install -y cmake g++-x86-64-linux-gnu
```

Install **Box64** for your Raspberry Pi OS using the appropriate package or build instructions, then confirm that `box64` is available. A distribution's default APT repositories might not contain a `box64` package. Box64 v0.4.5 has been tested on the Pi 5 with the 16 KiB page kernel.

Build the x86_64 bridge:

```bash
cd ~/EmberDeck/discord_bridge
cmake -S . -B build-x86_64 \
  -DCMAKE_TOOLCHAIN_FILE=cmake/x86_64-linux-gnu.cmake \
  -DDISCORD_SOCIAL_SDK_DIR="$HOME/EmberDeck/third_party/discord_social_sdk" \
  -DBRIDGE_BUILD_TESTS=OFF
cmake --build build-x86_64
```

This creates `~/EmberDeck/discord_bridge/build-x86_64/discord_bridge`. **Rebuild it after an update that changes the bridge**, including the v2 image-link changes.

## 3. Enable Rich Presence

Create `~/EmberDeck/configs/rich_presence.local.yaml`:

```yaml
enabled: true
discord:
  application_id: 123456789012345678  # replace with your Application ID
  sdk_library_dir: third_party/discord_social_sdk/lib/release
presence:
  show_media_metadata: true
  artwork_mode: "on"
```

The `.local.yaml` file overrides the shipped template without changing it. Keep your own settings in this file when updating the Deck. If Box64 isn't on the service's `PATH`, add `discord.box64.executable` with its absolute path; see [Configuration](CONFIGURATION.md).

Validate the settings and restart:

```bash
cd ~/EmberDeck
.venv/bin/python runtime_config.py configs
systemctl --user restart emberdeck.service
```

With no Discord account linked yet, the panel should show `RP : Discord not linked`.

## 4. Link your Discord account

Open a terminal **inside the Pi's desktop session**, not over plain SSH:

```bash
cd ~/EmberDeck
.venv/bin/python rich_presence.py --link
```

Discord opens in a browser on the Pi. Approve the application. Once the command reports `Discord: linked.`, the running service should pick up the link and show **Connected**.

You only need to link once unless you unlink the Deck or Discord rejects its stored authorisation. Access tokens are refreshed automatically.

## 5. Choose what Discord shows

Add or adjust these settings under `presence:` in `rich_presence.local.yaml`:

| Setting | What it does |
|:--|:--|
| `show_source: true` | Shows the playback-source icon and source name. |
| `show_media_metadata: false` | Keeps track, artist, album and station names private. Set `true` to show them. |
| `show_idle: true` | Shows Idle when nothing is playing. Set `false` to clear the activity instead. |
| `artwork_mode: "off"` | Uses the Ember Deck image. `"on"` allows suitable public artwork; `audit` checks artwork availability without displaying it. |
| `activity_type: listening` | Uses Discord's Listening activity. `playing` is also available. |
| `project_url: https://awoo.ltd/projects/ember-deck` | Opens the project page when someone clicks the large image or source icon. Use `null` to disable the links. |

Artwork requires both `show_media_metadata: true` and `show_source: true`. Bluetooth always uses the static image. For Plex, you can optionally set `public_artwork_lookup: true` in `configs/minstrel.local.yaml` to try the publicly hosted poster supplied by a metadata agent. This won't expose private Plex artwork stored on your server.

When metadata is enabled, Discord shows the **title**, **artist • album**, and **Explore The Ember Deck**. That last line is also the large image's tooltip; Discord uses the same text for both. The small image's tooltip still names the source. DAB uses the station name rather than treating programme information as an album.

After changing settings, run the validator and restart `emberdeck.service` as in step 3.

## 6. Hosted Plex album artwork (optional)

This option serves a copy of the Plex cover to Discord without exposing your Plex server or its token. The Deck serves artwork on `127.0.0.1`, while a **dedicated Cloudflare Tunnel** makes the images reachable from Discord.

**Privacy note:** The resulting album/artist image URLs are public and can be guessed. Anyone with a URL can request that cover, and copies may remain in Cloudflare's or Discord's cache. Do not enable this if that disclosure bothers you.

### Enable the artwork service

Create `~/EmberDeck/configs/artwork_service.local.yaml`, replacing the example hostname with your own:

```yaml
enabled: true
public_base_url: https://artwork.example.org
```

Validate and restart:

```bash
cd ~/EmberDeck
.venv/bin/python runtime_config.py configs
systemctl --user restart emberdeck.service
```

Play a Plex album with artwork to populate the local cache.

### Create the tunnel

Install `cloudflared` using [Cloudflare's instructions](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/). Then run:

```bash
cloudflared tunnel login
cloudflared tunnel create emberdeck-artwork
cloudflared tunnel route dns emberdeck-artwork artwork.example.org
```

Save the tunnel UUID printed by `tunnel create`. Put the following in `~/.cloudflared/config.yml`, replacing `<TUNNEL-ID>` and `<user>`:

```yaml
tunnel: <TUNNEL-ID>
credentials-file: /home/<user>/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: artwork.example.org
    service: http://127.0.0.1:8765
  - service: http_status:404
```

The final rule rejects requests for any other hostname or path not matched by the artwork ingress. Check it:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://artwork.example.org/test.png
```

Enable the tunnel at boot:

```bash
sudo cloudflared --config /home/<user>/.cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
systemctl cat cloudflared
```

**Check the last command.** On the tested Pi, the installed service used `/etc/cloudflared/config.yml` even though the original file was in the home directory. Future edits must go to the path the service actually loads.

Do **not** put Cloudflare Access in front of the hostname: Discord can't sign in to fetch artwork.

### Set Cloudflare caching

In your Cloudflare dashboard, open **Caching → Cache Rules** and create a rule for **only** your artwork hostname:

| Setting | Value |
|:--|:--|
| Filter | Hostname equals `artwork.example.org` |
| Cache eligibility | **Eligible for cache** |
| Edge TTL | **Use cache-control header if present, bypass cache if not** |
| Status code TTL | **400–526 → No store** |
| Browser TTL | **Respect origin** |
| Cache key | Default; **don't ignore the query string** |

Leave the other controls at their defaults. The successful image response uses a long-lived, versioned URL; missing images and other errors must not be cached. Cloudflare's tested dashboard capped the status-code range at 526, which covers the artwork service's expected errors.

Finally, update `rich_presence.local.yaml`:

```yaml
presence:
  show_source: true
  show_media_metadata: true
  artwork_mode: "on"
```

These settings merge with the other keys already in your overlay. Validate, restart, and play a Plex album. The Deck should choose its hosted cover when available, falling back to a suitable public poster or the Ember Deck image.

## Check that it's working

Start with the panel's `RP` status. For logs:

```bash
journalctl --user -u emberdeck.service -b | grep -E 'rich_presence|discord|artwork_service|artwork_selection'
```

If you've enabled hosted artwork, check that the service is listening locally and that the tunnel is running:

```bash
curl -sI http://127.0.0.1:8765/
systemctl status cloudflared --no-pager
```

A `404` for `/` is **expected**: the artwork server serves versioned image paths, not a homepage. To verify an actual cached cover, generate a URL from an existing cache entry:

```bash
n=$(ls ~/.cache/emberdeck/artwork/names | head -1)
url="/${n%.*}.png?v=${n##*.}"
curl -sI "http://127.0.0.1:8765$url"
curl -sI "https://artwork.example.org$url" | grep -iE '^(HTTP|cache-control|cf-cache-status|age)'
```

Replace the example hostname with yours. A real cover should return `200`; a repeated public request will usually show `cf-cache-status: HIT`. A nonexistent cover should return `404`, `cache-control: no-store`, and should **not** be cached:

```bash
curl -sI 'https://artwork.example.org/not-a-real-album.png?v=000000000000' \
  | grep -iE '^(HTTP|cache-control|cf-cache-status)'
```

In Discord, check your profile while music plays. The activity should follow the source and, when enabled, show metadata and artwork. Exact presentation and image-click behaviour can vary between Discord clients.

## Privacy and removing the integration

Rich Presence uses Discord's presence authorisation, not a bot or message access. The Deck stores Discord tokens privately in `configs/discord_secrets.yaml`; Plex credentials stay in `configs/minstrel_secrets.yaml`. Both files are excluded from Git. Never share or commit them.

The artwork service accepts image requests only, listens on localhost and uses an outbound tunnel, so it doesn't require opening a router port. Only the published image URLs are public.

- **Pause Rich Presence:** Set `enabled: false` in `rich_presence.local.yaml` and restart. This keeps the Discord link for later.
- **Hide metadata or artwork:** Set `show_media_metadata: false` or `artwork_mode: "off"`.
- **Unlink Discord:** Run `cd ~/EmberDeck && .venv/bin/python rich_presence.py --unlink`; you can also remove the application under Discord **User Settings → Authorized Apps**.
- **Stop new hosted covers:** Set `enabled: false` in `artwork_service.local.yaml` and restart.
- **Take existing public covers offline:** Stop the tunnel with `sudo systemctl disable --now cloudflared` and purge the artwork hostname in Cloudflare **Caching → Configuration → Custom Purge**. Discord may retain its own cached copy.

For a complete removal, you can also delete the dedicated Cloudflare tunnel, its DNS record and Cache Rule, the local artwork cache and your local Rich Presence overlays. Keep the shipped YAML templates.

## Troubleshooting

| Problem | What to check |
|:--|:--|
| **RP: Disabled** | Set `enabled: true` in `rich_presence.local.yaml` and restart. |
| **RP: No Discord app ID** | Check `discord.application_id` in the local overlay. |
| **Discord SDK unavailable** | Check SDK location, bridge build, Box64 installation and `sdk_library_dir`. Run `discord_bridge/inspect_sdk.sh`. |
| **Discord not linked** | Run `rich_presence.py --link` from the Pi desktop, not plain SSH. |
| **Discord OAuth error** | Enable Public Client and check the exact redirect `http://127.0.0.1/callback`. |
| **Browser doesn't open / link hangs** | Use a Pi desktop or VNC session. Check that `discord_bridge/launcher/xdg-open` exists and is executable. |
| **Connected but no Discord activity** | Check Discord's **Activity Privacy** settings and `show_idle`. |
| **Images are missing** | Upload the Rich Presence Art Assets under the expected keys. |
| **Images aren't clickable** | Check `presence.project_url` and rebuild an older bridge. Discord client behaviour may vary. |
| **Cover stays as Ember Deck image** | Enable metadata, source and `artwork_mode: "on"`; check the `artwork_selection` journal entries. Bluetooth always stays static. |
| **Hosted cover doesn't appear** | Check `artwork_service`, the tunnel and its public URL. The artwork hostname must not require Cloudflare Access or present a bot challenge. |
| **Missing images are cached** | Set the Cloudflare rule's **400–526 → No store**, check the origin's `no-store` response and purge previously cached errors. |
| **Configuration error (exit 2)** | Run `.venv/bin/python runtime_config.py configs`. YAML numbers and booleans shouldn't be quoted; `"on"` and `"off"` should. |

For general service failures, see [Troubleshooting](TROUBLESHOOTING.md). For the full list of options and defaults, see [Configuration](CONFIGURATION.md).
