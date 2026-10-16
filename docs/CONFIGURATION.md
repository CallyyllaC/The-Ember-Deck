# Configuration reference

[← Software architecture](SOFTWARE.md) · [Installation →](INSTALLATION.md)

This is the operator reference for the files in `EmberEngine/configs`. Values are read when a worker starts; unless a row explicitly says otherwise, restart `emberdeck.service` after changing it. Foundry supplies each worker's file through `CONFIG_PATH`.

YAML is type-sensitive. Do not quote numbers or `true`/`false`. Every Foundry worker except Whisper rejects unknown options and malformed values at startup with an option-specific message (exit status 2, which restarts the worker group), so check edited files before restarting:

```bash
cd ~/EmberDeck
.venv/bin/python runtime_config.py configs
```

Add `--require-secrets` to also check Minstrel's Plex identity and login. Whisper and the Ember UI are only checked by this command; at runtime they stay lenient. Keep a backup before changing physical calibration.

## Pawprint: physical I/O and routing

`pawprint.yaml` controls the only process allowed to own GPIO, I²C, the HT16K33, and `/dev/uinput`.

### Timing, devices, and analogue inputs

| Option | Type / units | Default / current | Purpose and guidance |
|:--|:--|:--|:--|
| `proc_name` | string | `pawprint` | Heartbeat/log identity. Do not change without also changing Foundry's worker name. |
| `poll_hz` | integer Hz | `50` | GPIO and output-loop rate. Higher reduces sampling latency but increases CPU/I²C traffic. Recommended 25–100. |
| `button_debounce_s` | seconds | `0.05` | Stable time before a tape-key transition is accepted. Raise for contact bounce; lowering too far can double-trigger. |
| `selector_debounce_s` | seconds | `0.07` | Stable time before a selector position is published. |
| `i2c_retry_s` | seconds | `2.0` | Delay between ADS1115/HT16K33 reconnection attempts. |
| `ads_address` | integer I²C address | `72` (`0x48`) | ADS1115 address. Valid 0–127; normally `0x48`–`0x4b`. YAML accepts decimal or `0x48`. |
| `matrix_address` | integer I²C address | `112` (`0x70`) | HT16K33 address. Valid 0–127. |
| `ads_gain` | numeric ADS gain | `1` | ADS1115 programmable gain. Changing it changes voltage range and requires recalibration. |
| `pot_input_max_v` | volts | `3.30` | Divisor used to turn `AnalogIn.voltage` into a normalised 0–1 reading. |
| `pot_smoothing` | ratio 0–1 | `0.18` | Fraction of each new sample applied. Higher responds faster; lower is smoother. |
| `gain_enabled` | boolean | `true` | Whether ADS1115 A1 is opened/read. Set false for disconnected or unsafe A1 wiring. |

The four `*_input_min`/`*_input_max` settings below are **normalised voltage values**, calculated as measured volts divided by `pot_input_max_v`. They are not raw ADC counts and not volts. For example, `0.36` means roughly 1.19 V when `pot_input_max_v` is 3.30 V.

| Option | Type / range | Current | Purpose and guidance |
|:--|:--|:--|:--|
| `colour_input_min`, `colour_input_max` | normalised voltage 0–1 | `0.0`, `0.075` | A0 physical end stops mapped to semantic 0–1. Maximum must exceed minimum. Narrower spans increase sensitivity/noise. |
| `gain_input_min`, `gain_input_max` | normalised voltage 0–1 | `0.36`, `0.99` | A1 physical end stops mapped to semantic 0–1. Maximum must exceed minimum. |
| `colour_curve`, `gain_curve` | exponent, >0 | `1.0` | Response curve. Above 1 gives finer low-end control; below 1 raises the low end. |
| `colour_deadband`, `gain_deadband` | semantic ratio 0–1 | `0.003` | Minimum accepted post-filter movement. Raise to suppress jitter. |
| `pickup_tolerance` | semantic ratio 0–1 | `0.1` | Distance from a restored value at which soft pickup engages. Higher is easier to pick up but can make a small jump. |
| `gain_output_max`, `brightness_output_max`, `colour_output_max`, `saturation_output_max` | ratio 0–1 | `1.0` | Semantic safety ceilings after calibration. They do not change ADC scaling. |
| `default_control_value` | ratio 0–1 | `0.50` | Value used when no valid persisted control state exists. |
| `state_path` | path | `pawprint_state.yaml` | Persisted semantic values, relative to the config directory unless absolute. Written atomically. |
| `state_save_interval_s` | seconds | `30` | Minimum interval between dirty-state saves. Longer reduces writes but loses more adjustment on sudden power loss. |

The persisted `pawprint_state.yaml` contains `version: 1` and a `controls` mapping for `gain`, `brightness`, `colour`, and `saturation`. It is runtime state, not a hand-edited hardware configuration, and is ignored by Git.

### Graph, status lamps, and host thresholds

| Option | Type / units | Current | Purpose and guidance |
|:--|:--|:--|:--|
| `default_indicator_brightness` | ratio 0–1 | `0.55` | HT16K33 graph/top/front brightness. Not RGBW-strip brightness. |
| `graph_mode` | string | `progress` | `progress` enables media progress; any other value leaves normal graph blank. |
| `graph_progress_segments` | integer 1–10 | `10` | Number of physical graph segments used. |
| `graph_progress_show_first_segment` | boolean | `true` | Shows segment one as soon as position is non-zero. |
| `graph_external_override_enabled` | boolean | `true` | Allows `/io/out/led/graph_override` and `graph_mask` to temporarily own the graph. REC hold still has priority. |
| `heartbeat_period_s` | seconds | `1.60` | Double-pulse period for top green lamp 1. |
| `system_sample_interval_s` | seconds | `0.50` | Host network/disk/memory/storage sampling interval. |
| `network_activity_min_bytes` | bytes/sample | `2048` | RX+TX change needed to pulse top green lamp 2. |
| `network_pulse_s` | seconds | `0.14` | Network-lamp pulse duration. |
| `disk_activity_min_sectors` | sectors/sample | `8` | Disk-sector change needed to pulse the amber lamp. |
| `disk_pulse_s` | seconds | `0.14` | Disk-lamp pulse duration. |
| `storage_path` | path | `/` | Filesystem checked for the red storage warning. |
| `storage_warning_min_free_gb` | GiB | `2.0` | Red storage lamp lights below this absolute free space or the percentage limit. |
| `storage_warning_min_free_percent` | percent | `10.0` | Red storage lamp percentage threshold. |
| `memory_warning_min_available_percent` | percent | `10.0` | Red memory lamp threshold, using Linux `MemAvailable`. |

### Media routing, transport, and holds

| Option | Type / units | Current | Purpose and guidance |
|:--|:--|:--|:--|
| `media_source_refresh_s` | seconds | `0.75` | BlueZ routing-state refresh cadence. Refresh runs off the GPIO loop. Lower is fresher but creates more subprocess/D-Bus traffic. |
| `media_command_timeout_s` | seconds | `2.0` | Per `bluetoothctl`/`gdbus` call limit. A metadata refresh may make several calls, but cannot block GPIO sampling. |
| `minstrel_heartbeat_stale_s` | seconds | `5.0` | Maximum Minstrel heartbeat age before Plex is unavailable for routing. |
| `bluetooth_media_enabled` | boolean | `true` | Enables BlueZ AVRCP discovery/control and metadata publication. |
| `bluetoothctl_path`, `gdbus_path` | executable | `bluetoothctl`, `gdbus` | Commands used for BlueZ discovery and D-Bus calls. Absolute paths are useful in restricted services. |
| `bluetooth_metadata_refresh_s` | seconds | `1.0` | Slower cadence for title/artist/position/battery properties. |
| `bluetooth_claim_metadata_enabled` | boolean | `true` | Allows credible long-form metadata to claim routing immediately. |
| `bluetooth_claim_threshold_s` | seconds | `2.0` | Continuous Playing time required when metadata is insufficient; filters phone notification sounds. |
| `bluetooth_claim_linger_s` | seconds | `45.0` | Retains Bluetooth ownership across pause/brief status gaps when Bluetooth was already the active target. Active or paused Plex ownership still wins otherwise. |
| `bluetooth_claim_min_duration_ms` | milliseconds | `30000` | Minimum track duration that helps metadata count as real media. |
| `media_keys_enabled` | boolean | `true` | Enables fallback virtual Linux media-key device creation. Direct Plex/Bluetooth/MPRIS routes do not broadcast to it. |
| `uinput_path` | path | `/dev/uinput` | Kernel uinput device. Requires installed module/rule. |
| `media_key_device_name` | string | `Ember Deck Media Keys` | Name visible to Linux input consumers. |
| `media_reconnect_s` | seconds | `5.0` | Retry delay when uinput is unavailable. |
| `record_source_hold_s` | seconds | `3.0` | REC hold threshold for cycling Plex → radio → YouTube. A shorter release toggles the pot bank. |
| `record_source_hold_confirm_s` | seconds | `0.40` | All-lit graph confirmation after a successful REC hold. |
| `eject_shutdown_hold_s` | seconds | `5.0` | Eject hold threshold for orderly shutdown. |
| `shutdown_blink_hz` | Hz | `4.0` | Front red-lamp blink rate while Eject is armed. |
| `shutdown_enabled` | boolean | `true` | Enables the shutdown helper. |
| `shutdown_helper` | absolute path | `/usr/local/sbin/emberdeck-poweroff` | Narrow privileged helper invoked through `sudo -n`. |
| `event_capacity` | integer records | `64` | Selector-event ring capacity. Changing it changes a shared-memory shape; stop the service and reboot or remove the stale segment before restart. |

GPIO numbers and selector maps are intentionally fixed ownership data in `pawprint.py`, not runtime configuration: Play 6, Stop 16, Rewind 12, Fast-forward 8, Eject 21, REC 5, three-way selector 24/25, and four-way selector 22/23. They are BCM numbers. A wiring change requires a code change and full service restart.

## Minstrel: Plex/Plexamp

`minstrel.yaml` holds non-secret settings only. The Plex username, password and the refreshed server URL/token live in the gitignored `configs/minstrel_secrets.yaml` (copy `minstrel_secrets.example.yaml`, then `chmod 600`), or in `EMBERDECK_PLEX_*` environment values. Minstrel never writes `minstrel.yaml`.

```yaml
# configs/minstrel_secrets.yaml (mode 0600, never committed)
version: 1
credentials:        # yours; Minstrel never rewrites this section
  username: you@example.com
  password: ...
cache:              # Minstrel's; rewritten atomically after each account login
  url: null
  token: null
```

Precedence: `EMBERDECK_PLEX_USERNAME`/`_PASSWORD` → secrets `credentials` → legacy values still in `minstrel.yaml` (honoured but reported). The direct `url`+`token` pair: `EMBERDECK_PLEX_URL`/`_TOKEN` → secrets `cache` → legacy YAML. A token is only tried first for a fast start; if it fails and credentials exist, Minstrel logs in with the username and password, saves the fresh token to `cache`, and carries on. Environment values are never written to the file.

A valid configuration without Plex identity or credentials does not stop Minstrel: it runs **unconfigured** (Plex shown as stopped, one actionable error, rechecked every 30 s). Wrong credentials or an unreachable server are retried with backoff. Only malformed `minstrel.yaml` stops it.

| Option | Type / units | Default | Purpose and guidance |
|:--|:--|:--|:--|
| `proc_name` | string | `minstrel` | Heartbeat/log identity. |
| `server_name` | string/null | null | Plex resource name used for username/password login. Env: `EMBERDECK_PLEX_SERVER_NAME`. |
| `secrets_path` | path | `minstrel_secrets.yaml` | Credentials/cache file, relative to the directory holding `minstrel.yaml` unless absolute. |
| `plex_retry_initial_s`, `plex_retry_max_s` | seconds 0.5–3600 | `2.0`, `300.0` | Connection/login retry backoff: doubles from the initial value to the maximum, and resets after a successful session query. |
| `username`, `password`, `url`, `token` | string/null | absent | **Legacy.** Still accepted here for existing installs, but reported as a fault; move them to `minstrel_secrets.yaml`. |
| `plex_client_name` | string | null | Required for Plex to be configured: exact `player.title` for this Deck's Plexamp. Prevents another household session from claiming physical controls. Env: `EMBERDECK_PLEX_CLIENT_NAME`. |
| `plex_control_client_name` | string/null | null | Optional separate control client title; defaults to `plex_client_name`. Env: `EMBERDECK_PLEX_CONTROL_CLIENT_NAME`. |
| `plex_control_client_machine_identifier` | string/null | null | Strongest optional control-client identity. Env: `EMBERDECK_PLEX_CONTROL_CLIENT_ID`. |
| `poll_interval_s` | seconds | `1.0` | Plex session polling. Lower is more responsive but increases server traffic. |
| `plex_timeout_s` | seconds | `5.0` | Timeout applied to Plex server/account connections and later PlexAPI requests. Keep below Foundry's 30-second heartbeat grace. |
| `lyrics_update_interval_s` | seconds | `0.25` | Local timed-lyrics update cadence; it does not poll Plex each tick. |
| `clock_seek_threshold_s` | seconds | `18.0` | Observation difference treated as an explicit seek/reset. |
| `clock_fresh_nudge_max_s` | seconds | `1.25` | Maximum gentle correction from a normal fresh observation. |
| `clock_fresh_deadband_s` | seconds | `0.35` | Difference ignored to avoid clock jitter. |
| `clock_offset_change_epsilon_s` | seconds | `0.001` | Minimum correction change worth applying. |
| `album_art_enabled` | boolean | `true` | Enables 48×48 shared-memory artwork. |
| `album_art_width`, `album_art_height` | pixels 12–48 | `48` | Requested Plex transcode size. Shared bus remains a fixed 48×48 canvas. |
| `album_art_timeout_s` | seconds | `5.0` | Cover download timeout (also used for the public artwork lookup). |
| `public_artwork_lookup` | boolean | `false` | For Discord Rich Presence artwork (`artwork_mode: on`): on each new album, check whether the selected album poster is an original public https URL supplied by a metadata agent and publish it. One extra Plex request per new album; server-stored artwork, server URLs and tokens are never published. The existing cover download is unchanged. |

`/plex/connection_state` reports the outcome (0 unconfigured, 1 connecting, 2 connected, 3 unavailable, 4 credentials rejected); Pawprint routes transport keys to Plex only while it is 2.

## Echo: audio capture

| Option | Type / units | Default | Purpose and guidance |
|:--|:--|:--|:--|
| `proc_name` | string | `echo` | Heartbeat/log identity. |
| `sample_rate` | integer Hz | `48000` | Capture rate; must be supported by the PipeWire/source device. |
| `channels` | integer | `2` | Input channels; first two are mixed to mono. |
| `smoothness` | number | `0.25` | Multiplier for callback block size (minimum effective 0.25). Higher adds latency and stability. |
| `fps` | integer Hz | `50` | Spectrum/waveform publication rate. |
| `fft_size` | integer samples | `2048` | FFT window; use a power of two. Larger improves frequency resolution and increases latency/CPU. |
| `bins` | integer/null | `2048` in code, corrected to `fft_size/2+1` | Requested spectrum length. Echo logs and corrects mismatches; `1025` is native for FFT 2048. |
| `device` | sounddevice name/id | `default` | PipeWire/PortAudio capture source. Verify with `python -m sounddevice`. |
| `audio_reconnect_s` | seconds | `2.0` | Retry delay when PipeWire or the capture device is temporarily unavailable. Echo stays alive and keeps heartbeating. |
| `waveform_samples` | integer | `160` | UI scope sample count. Changing it changes shared-memory shape; reboot/clear stale segment. |
| `waveform_key` | Synapse key | `/audio/waveform` | Shared-state destination consumed by Ember UI. Change both together. |

## Aurora: colour policy

| Option | Type / range | Default/current | Purpose |
|:--|:--|:--|:--|
| `proc_name` | string | `aurora` | Heartbeat/log identity. |
| `dynamicColour` | boolean | `true` | Uses active artwork palettes where available. |
| `scheme` | `analogous`, `shades`, `split_comp`, `triadic`, `tetradic` | `analogous` | Static fallback palette relationship. Unrecognised values currently fall back to analogous. |
| `static_base_saturation` | 0–1 | `0.85` | Base saturation for the physical colour control. |
| `ui_saturation_scale` | non-negative multiplier | `0.45` | Calms artwork colour for the text display without dimming the LED strip. |
| `fallback_hue` | 0–1 turns | `0.0` | Static hue when no physical value is available. |
| `update_hz` | Hz | `10` | Colour publication cadence. |
| `album_timeout_s` | seconds | `2.0` | How long old dynamic art remains usable after its source disappears. |
| `generated_palette_enabled` | boolean | `true` | Allows semantic extraction from the 48×48 artwork bus. |
| `palette_near_black_value` | 0–1 | `0.05` | Value threshold treated as near-black. |
| `palette_near_black_saturation` | 0–1 | `0.78` | Saturation borrowed for near-black chromatic output. |
| `palette_grayscale_saturation` | 0–1 | `0.12` | Maximum saturation still treated as grayscale. |
| `palette_pale_grayscale_value` | 0–1 | `0.60` | Light grayscale threshold retained as a useful white peak. |
| `palette_pure_black_gray_value` | 0–1 | `0.035` | Safe neutral output for exact black. |
| `palette_{dominant,accent,dark,light}_min_saturation` | 0–1 | `0.55`, `0.65`, `0.70`, `0.30` | Per-role minimum saturation. Raising makes output more vivid. |
| `palette_{dominant,accent,dark,light}_min_value` | 0–1 | `0.28`, `0.48`, `0.10`, `0.75` | Per-role brightness floors. |

## Foxfire: spectrum transformation and animation

| Option | Type / units | Current | Purpose and guidance |
|:--|:--|:--|:--|
| `proc_name` | string | `foxfire` | Heartbeat/log identity. |
| `led_count` | integer pixels | `24` | Strip length; must match hardware. Changes shared-memory frame shape. |
| `fps` | integer Hz | `50` | Animation and LED-frame rate. |
| `mode_key`, `active_mode_key`, `active_mode_seq_key`, `tape_mode_engaged_key` | Synapse keys | see YAML | Published mode/diagnostic keys. Change consumers together. |
| `source_selector_key`, `visualiser_mode_index_key` | Synapse keys | see YAML | Pawprint selector inputs. |
| `tape_source_position` | selector position | `3` | Source-selector position that allows the four-way switch to change animations. |
| `default_mode` | integer 0–3 | `0` | Boot mode: Bounce, Aurora/stars, Tails, Illusion. |
| `gain_key`, `frame_key` | Synapse keys | see YAML | Semantic gain input and output RGBA frame. |
| `gain_floor`, `gain_mid`, `gain_ceiling` | multipliers | `0.08`, `1.0`, `2.0` | Piecewise mapping for physical gain 0, 0.5, and 1.0. |
| `release`, `attack`, `decay`, `rise` | DSP ratios | `0.99`, `0.90`, `0.999`, `0.5` | Envelope/peak response. Higher release/decay retain energy longer; higher rise follows peaks faster. |
| `gamma` | exponent | `2.2` | Output response curve. Higher suppresses lower levels. |
| `fmin_hz`, `fmax_hz` | Hz/null | `10`, null | Analysed frequency range; null uses Nyquist. |
| `split_hz` | Hz | `1500` | Boundary between mean low-band and configured high-band reduction. |
| `reducer_hi` | `pctl`, `max`, `rms` | `pctl` | High-frequency bin reducer. |
| `pctl_hi` | percentile 0–100 | `90` | Percentile when `reducer_hi` is `pctl`. |
| `pivot_hz`, `tilt_power`, `tilt_cap` | Hz/exponent/multiplier | `1000`, `0.25`, `3.0` | Gentle high-frequency compensation. |
| `fast_mix` | ratio 0–1 | `0.25` | Mix between slow envelope and faster response. |

## Will-o'-the-wisp: BlinkStick output

`led_count` and `fps` come from Foxfire through Synapse.

| Option | Type / units | Current | Purpose |
|:--|:--|:--|:--|
| `proc_name` | string | `willo_wisp` | Heartbeat/log identity. |
| `pixel_max_ma` | mA/pixel | `50` | Full-white current estimate used for USB power limiting. |
| `usb_current_ma` | mA | `400` | Safe total USB budget when the strip is USB-powered. |
| `external_power` | boolean | `true` | Disables USB brightness limiting because the strip has its own PSU. Do not set true for a USB-powered strip. |
| `data_channel` | integer ≥0 | `0` | BlinkStick Pro output channel. |

## HDMI-2 source controller

| Option | Type / units | Current | Purpose |
|:--|:--|:--|:--|
| `proc_name` | string | `hdmi2_source_controller` | Heartbeat/log identity. |
| `poll_hz` | Hz | `10` | REC-cycle counter polling rate. |
| `command_timeout_s` | seconds | `4` | Limit for Flatpak, pkill, and Labwc helper commands. |
| `startup_settle_s` | seconds | `1` | Delay before enforcing cold-boot Plex context. |
| `shortwave_app_id` | Flatpak app ID | `de.haeckerfelix.Shortwave` | Radio application identity. |
| `flatpak_path` | executable | `flatpak` | Flatpak command. |
| `pear_appimage` | path | `~/apps/pear/Pear.AppImage` | YouTube Music AppImage. `~` expands for the service user. |
| `pear_process_match` | pgrep pattern | `Pear.AppImage` | Used to detect/terminate Pear. Keep specific enough not to match unrelated processes. |
| `pear_args` | list of strings | `--ozone-platform=wayland` | AppImage launch arguments. |
| `labwc_rules_enabled` | boolean | `true` | Installs/updates only Ember's two window rules. |
| `labwc_rc_path` | path | `~/.config/labwc/rc.xml` | Labwc configuration edited in place. Back it up before first run. |
| `labwc_hdmi2_output` | connector | `HDMI-A-2` | Output receiving guest applications. Verify with `wlr-randr`. |
| `labwc_path` | executable | `labwc` | Used for live reconfiguration. |
| `heartbeat_s` | seconds | `1.0` (code default) | Heartbeat interval, floored at 0.25 s. Not in the shipped YAML; normally left unset. |

## MPRIS bridge

| Option | Type / units | Current | Purpose |
|:--|:--|:--|:--|
| `proc_name` | string | `mpris_bridge` | Heartbeat/log identity. |
| `poll_hz` | Hz | `10` | Main loop and control-counter rate. |
| `refresh_s` | seconds | `0.60` | Full player/metadata snapshot interval. |
| `playerctl_path`, `gdbus_path` | executable | `playerctl`, `gdbus` | Session-bus tools. |
| `command_timeout_s` | seconds | `2.0` | Per playerctl/gdbus call limit. |
| `metadata_buffer_bytes` | bytes ≥64 | `512` | Fixed shared-memory size per MPRIS text field. Changing it requires clearing stale shared memory/reboot. |
| `unavailable_grace_s` | seconds | `2.0` | Retains the last good snapshot across brief player gaps. |
| `shortwave_players` | list of strings | Shortwave ID | Exact MPRIS names accepted in radio context. |
| `youtube_player_hints` | list of strings | YouTube/Pear hints | Case-insensitive bus-name hints in YouTube context. |
| `youtube_process_hints` | list of strings | Pear/YouTube hints | Process-name fallback when the bus suffix is dynamic. |
| `art_enabled` | boolean | `true` | Enables MPRIS artwork retrieval. |
| `art_width`, `art_height` | pixels 12–48 | `48` | Rendered image inside the fixed 48×48 canvas. |
| `art_timeout_s` | seconds | `4.0` | HTTP artwork timeout. |
| `art_max_bytes` | bytes | `2,000,000` | Maximum local/HTTP artwork payload before rejection. |
| `heartbeat_s` | seconds | `1.0` (code default) | Heartbeat interval, floored at 0.25 s. Not in the shipped YAML; normally left unset. |

## Ember UI

| Option | Type / units | Current | Purpose |
|:--|:--|:--|:--|
| `proc_name` | string | `ember_ui` | UI identity. The UI is a separate systemd service, not a Foundry worker. |
| `logical_width`, `logical_height` | terminal cells | `80`, `60` | Design canvas used by the adaptive layout. |
| `safe_margin_cells` | cells | `1` | Bezel-safe inset. |
| `update_hz` | Hz | `50` | Scheduler rate; hidden pages remain throttled. |
| `metadata_refresh_hz` | Hz | `5` | Active media/lyrics sampling. |
| `scope_refresh_hz` | Hz | `50` | Scope-page refresh while visible. |
| `system_refresh_s`, `log_refresh_s` | seconds | `2`, `1` | Host diagnostics and log refresh intervals. |
| `metadata_dead_grace_s`, `art_dead_grace_s` | seconds | `2`, `2` | Retain last metadata/art across brief source gaps. |
| `ui_brightness_floor`, `ui_brightness_ceiling` | ratio 0–1 | `0.52`, `1.0` | Colour scaling from Pawprint brightness; monitor backlight/art are unaffected. |
| `diagnostics_scroll_step_s` | seconds | `0.35` | Marquee step for long Wi-Fi/IP text. |
| `log_path` | path | `logs/foundry.log` | Legacy plain log fallback, relative to EmberEngine. |
| `structured_log_path` | path | `logs/foundry.structured.jsonl` | Whisper's bounded UI snapshot. |
| `structured_log_required` | boolean | `true` | If true, do not silently replace missing structured logs with legacy text. |
| `waveform_key`, `waveform_samples` | Synapse key/count | `/audio/waveform`, `160` | Must match Echo; changing count requires shared-memory cleanup/reboot. |

Several lower-frequency page rates (`global_refresh_hz`, `now_refresh_hz`, `info_refresh_hz`, `console_refresh_hz`, `palette_refresh_hz`) are supported by the UI with defaults `2`, `2`, `1`, `2`, and `2` Hz. Add them only when tuning is needed.

## Discord Rich Presence and hosted artwork (optional)

`rich_presence.yaml` (Discord Rich Presence) and `artwork_service.yaml` (the local origin for Plex album covers in Discord) are both disabled in the shipped files. Every option, with defaults, valid values and safe examples, is documented in one place: **[Enabling Discord Rich Presence](ENABLING_DISCORD_RICH_PRESENCE.md#configuration)**. Minstrel's related `public_artwork_lookup` option is in the Minstrel table above.

Both workers use `restart_scope="worker"`: their failures restart only themselves. Discord tokens live only in `discord_secrets.yaml`, written by the Deck; `EMBERDECK_*` environment overrides do not apply.

## Local overrides (`<name>.local.yaml`)

`minstrel.yaml`, `rich_presence.yaml` and `artwork_service.yaml` are shipped as portable templates without personal values. Put this Deck's own public values (Plex names, the Discord application ID, the artwork hostname, local paths, preferences) in `configs/<name>.local.yaml` beside the template, for example `configs/rich_presence.local.yaml`. It is merged on top of the template when the worker starts and when `runtime_config.py configs` runs: nested sections key by key, so it needs only the options you change.

- Overlays are gitignored, so updates never overwrite them; copy them when moving to a new checkout or Deck.
- They are personal, not secret. Credentials still belong only in `*_secrets.yaml`.
- An overlay must be a YAML mapping. Like the template, it is validated strictly, and errors name the file without quoting its contents.

## Whisper logging

| Option | Type / units | Current | Purpose |
|:--|:--|:--|:--|
| `proc_name`, `fps` | string / Hz | `whisper_daemon`, `10` | Identity and scheduler/output cadence. |
| `ambient_min_delay`, `ambient_max_delay` | seconds | `60`, `150` | Random interval bounds for ambient personality lines. Minimum should not exceed maximum. |
| `ambient_after_real_event_delay` | seconds | `30` | Quiet period after real activity. |
| `ambient_after_startup_delay` | seconds | `45` | Quiet period after startup. |
| `ambient_queue_limit` | records | `3` | Bound on pending ambient messages. |
| `error_dedupe_cooldown_s` | seconds | `60` | Repeated identical fault suppression interval. |
| `reaction_min_delay_s`, `reaction_max_delay_s` | seconds | `2`, `8` | Delayed reaction interval bounds. |
| `reaction_max_per_event` | count | `2` | Maximum generated reactions per event. |
| `reaction_global_cooldown_s`, `reaction_process_cooldown_s` | seconds | `5`, `15` | Reaction rate limits. |
| `structured_log_path`, `ui_event_limit` | path / records | see YAML / `100` | Rewritten bounded UI event snapshot. |
| `ui_snapshot_interval_s` | seconds | `1.0` | Minimum interval between snapshot rewrites while new records are pending. Critical events and shutdown flush immediately; durable error history is unaffected. |
| `important_log_path` | path | `logs/foundry-errors.jsonl` | Base name for durable monthly error files. |
| `important_log_max_priority` | integer | `0` | Highest numeric priority retained durably; 0 means critical only. |
| `important_log_max_bytes_mb` | MiB | `10` | Per-file rollover ceiling. |
| `log_retention_days` | days | `183` | Approximate durable error retention. |
| `log_cleanup_interval_s` | seconds | `86400` | Old-log cleanup cadence. |
| `legacy_log_paths` | list of paths | foundry/browser logs | Old files removed/ignored during migration. |
| `critical_visible_s`, `lifecycle_visible_s`, `interaction_visible_s`, `reaction_visible_s`, `ambient_visible_s` | seconds | `12`, `6`, `4`, `3`, `0` | UI persistence hints by message class. |

Whisper also honours `WHISPER_COLOUR=1` for forced Rich colour output. Foundry sets its private queue-connection environment automatically.

## Retired options

These names are still accepted so an older deployed file does not stop its worker. They can be deleted:

| File | Retired option | Behaviour |
|:--|:--|:--|
| `foxfire.yaml` | `reducer`, `tilt_pow` | Ignored. Use `reducer_hi` and `tilt_power`. |
| `aurora.yaml` | `value`, `alpha` | Ignored. |
| `aurora.yaml` | `saturation`, `base_hue` (degrees) | Used only when `static_base_saturation` or `fallback_hue` is absent. Prefer the new names. |
| `artwork_service.yaml` | `retention_days` | Ignored; covers no longer expire by age (see `max_cache_mb`). |
| `minstrel.yaml` | `username`, `password`, `url`, `token` | Still honoured but reported as a fault; move them to `minstrel_secrets.yaml`. |

## Foundry supervision

Foundry supervision is intentionally code-owned rather than YAML-configured:

| Constant | Current | Effect |
|:--|:--|:--|
| `HEARTBEAT_GRACE_S` | 30 s | Heartbeat freshness window. |
| `WARMUP_GRACE_S` | 30 s | Health checks ignored after each spawn. |
| `FAIL_CONSEC_N` | 30 checks | Consecutive stale checks before restart. |
| `INTER_START_DELAY_S` | 5 s | Delay between ordered worker starts. |
| `RESTART_BACKOFF_INITIAL_S`, `RESTART_BACKOFF_CAP_S` | 5 s → 60 s | Group restart delay, doubling per restart. |
| `WORKER_BACKOFF_INITIAL_S`, `WORKER_BACKOFF_CAP_S` | 5 s → 300 s | Delay before respawning a worker-scoped worker. |
| `BACKOFF_RESET_HEALTHY_S` | 300 s | Continuous post-warm-up health needed before a backoff returns to its initial delay. |
| `CHILD_TERM_GRACE_S` | 5 s | Graceful stop before force-kill. |

Each `WORKERS` entry has a `restart_scope`. The default, `"group"`, used by every hardware and media worker, restarts the complete worker group when that member exits or remains unhealthy, preserving the appliance's shared ownership boundary; do not run a second standalone Pawprint while the group is active. `"worker"` restarts only that worker; it is used by the optional `artwork_service` and `rich_presence` workers, which own no hardware.
