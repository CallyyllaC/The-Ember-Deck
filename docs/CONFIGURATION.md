# Configuration reference

[← Software architecture](SOFTWARE.md) · [Installation →](INSTALLATION.md)

This is the operator reference for the files in `EmberEngine/configs`. Values are read when a worker starts; unless a row explicitly says otherwise, restart `emberdeck.service` after changing it. Foundry supplies each worker's file through `CONFIG_PATH`.

YAML is type-sensitive. Do not quote numbers or `true`/`false`. Pawprint and Minstrel reject malformed values at startup with an option-specific message. Keep a backup before changing physical calibration.

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
| `bluetooth_claim_linger_s` | seconds | `45.0` | Retains a recently credible Bluetooth route across pause/brief status gaps. Active Plex still wins. |
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

`minstrel.yaml` contains no real credentials in the repository. Values may be placed in the deployed file (protect it with mode `0600`) or injected through the environment names shown below.

| Option | Type / units | Default | Purpose and guidance |
|:--|:--|:--|:--|
| `proc_name` | string | `minstrel` | Heartbeat/log identity. |
| `server_name` | string/null | null | Plex resource name for account-login recovery. Env: `EMBERDECK_PLEX_SERVER_NAME`. |
| `username`, `password` | string/null | null | Plex account fallback credentials. Env: `EMBERDECK_PLEX_USERNAME`, `EMBERDECK_PLEX_PASSWORD`. |
| `url`, `token` | string/null | null | Cached direct server connection. Env: `EMBERDECK_PLEX_URL`, `EMBERDECK_PLEX_TOKEN`. Supply both or neither. |
| `plex_client_name` | string, required | null | Exact `player.title` for this Deck's Plexamp. Prevents another household session from claiming physical controls. Env: `EMBERDECK_PLEX_CLIENT_NAME`. |
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
| `album_art_timeout_s` | seconds | `5.0` | Cover download timeout. |

Minstrel may persist a refreshed `url` and `token` into its YAML. Keep the deployed file private and outside source control.

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
| `important_log_path` | path | `logs/foundry-errors.jsonl` | Base name for durable monthly error files. |
| `important_log_max_priority` | integer | `0` | Highest numeric priority retained durably; 0 means critical only. |
| `important_log_max_bytes_mb` | MiB | `10` | Per-file rollover ceiling. |
| `log_retention_days` | days | `183` | Approximate durable error retention. |
| `log_cleanup_interval_s` | seconds | `86400` | Old-log cleanup cadence. |
| `legacy_log_paths` | list of paths | foundry/browser logs | Old files removed/ignored during migration. |
| `critical_visible_s`, `lifecycle_visible_s`, `interaction_visible_s`, `reaction_visible_s`, `ambient_visible_s` | seconds | `12`, `6`, `4`, `3`, `0` | UI persistence hints by message class. |

Whisper also honours `WHISPER_COLOUR=1` for forced Rich colour output. Foundry sets its private queue-connection environment automatically.

## Foundry supervision

Foundry supervision is intentionally code-owned rather than YAML-configured:

| Constant | Current | Effect |
|:--|:--|:--|
| `HEARTBEAT_GRACE_S` | 30 s | Heartbeat freshness window. |
| `WARMUP_GRACE_S` | 30 s | Health checks ignored after each spawn. |
| `FAIL_CONSEC_N` | 30 checks | Consecutive stale checks before restart. |
| `INTER_START_DELAY_S` | 5 s | Delay between ordered worker starts. |
| restart backoff | 5 s → 15 s cap | Delay before group recovery. |
| `CHILD_TERM_GRACE_S` | 5 s | Graceful stop before force-kill. |

Foundry restarts the complete worker group when one member exits or remains unhealthy. This preserves the appliance's shared ownership boundary; do not run a second standalone Pawprint while the group is active.
