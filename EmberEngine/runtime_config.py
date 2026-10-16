"""Actionable configuration validation for the supervised Ember Deck runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


class ConfigError(ValueError):
    """A configuration error suitable for showing directly to an operator."""


def _fail(component: str, option: str, message: str, value: Any) -> None:
    raise ConfigError(f"{component}: {option} {message}; got {value!r}")


def require_mapping(component: str, value: Any) -> Dict[str, Any]:
    """Require a YAML document to contain an option mapping."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(component, "configuration root", "must be a mapping", value)
    return dict(value)


def _number(
    component: str,
    cfg: Mapping[str, Any],
    key: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    integer: bool = False,
) -> None:
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(component, key, "must be a number" if not integer else "must be an integer", value)
    if integer and not isinstance(value, int):
        _fail(component, key, "must be an integer", value)
    if minimum is not None and value < minimum:
        _fail(component, key, f"must be at least {minimum:g}", value)
    if maximum is not None and value > maximum:
        _fail(component, key, f"must be at most {maximum:g}", value)


def _text(component: str, cfg: Mapping[str, Any], key: str, *, allow_blank: bool = False) -> None:
    value = cfg.get(key)
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        _fail(component, key, "must be a non-empty string" if not allow_blank else "must be a string", value)


def _boolean(component: str, cfg: Mapping[str, Any], key: str) -> None:
    value = cfg.get(key)
    if not isinstance(value, bool):
        _fail(component, key, "must be true or false", value)


def _list_of_text(component: str, cfg: Mapping[str, Any], key: str) -> None:
    value = cfg.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail(component, key, "must be a list of strings", value)


def _known(component: str, cfg: Mapping[str, Any], keys: Iterable[str]) -> None:
    unknown = sorted(set(cfg) - set(keys) - {"_config_path", "_config_dir", "period"})
    if unknown:
        raise ConfigError(f"{component}: unknown option(s): {', '.join(unknown)}")


PAWPRINT_KEYS = {
    "proc_name", "poll_hz", "button_debounce_s", "selector_debounce_s", "i2c_retry_s",
    "ads_address", "matrix_address", "ads_gain", "pot_input_max_v", "pot_smoothing",
    "gain_deadband", "colour_deadband", "pickup_tolerance", "gain_input_min",
    "gain_input_max", "gain_curve", "gain_enabled", "colour_input_min", "colour_input_max",
    "colour_curve", "gain_output_max", "brightness_output_max", "colour_output_max",
    "saturation_output_max", "default_control_value", "state_save_interval_s", "state_path",
    "default_indicator_brightness", "graph_mode", "graph_progress_segments",
    "graph_progress_show_first_segment", "graph_external_override_enabled", "heartbeat_period_s",
    "system_sample_interval_s", "network_activity_min_bytes", "network_pulse_s",
    "disk_activity_min_sectors", "disk_pulse_s", "storage_path", "storage_warning_min_free_gb",
    "storage_warning_min_free_percent", "memory_warning_min_available_percent",
    "media_keys_enabled", "uinput_path", "media_key_device_name", "media_reconnect_s",
    "media_source_refresh_s", "media_command_timeout_s", "minstrel_heartbeat_stale_s",
    "bluetooth_media_enabled", "bluetoothctl_path", "gdbus_path", "bluetooth_metadata_refresh_s",
    "bluetooth_claim_metadata_enabled", "bluetooth_claim_threshold_s", "bluetooth_claim_linger_s",
    "bluetooth_claim_min_duration_ms", "record_source_hold_s", "record_source_hold_confirm_s",
    "eject_shutdown_hold_s", "shutdown_helper", "shutdown_blink_hz", "shutdown_enabled",
    "event_capacity",
}


def validate_pawprint(cfg: Mapping[str, Any]) -> None:
    component = "pawprint"
    _known(component, cfg, PAWPRINT_KEYS)
    for key in ("proc_name", "state_path", "storage_path", "uinput_path", "media_key_device_name",
                "bluetoothctl_path", "gdbus_path", "shutdown_helper", "graph_mode"):
        _text(component, cfg, key)
    for key in ("poll_hz", "event_capacity", "graph_progress_segments", "network_activity_min_bytes",
                "disk_activity_min_sectors", "bluetooth_claim_min_duration_ms"):
        _number(component, cfg, key, minimum=1, integer=True)
    for key in ("ads_address", "matrix_address"):
        _number(component, cfg, key, minimum=0, maximum=127, integer=True)
    for key in ("button_debounce_s", "selector_debounce_s"):
        _number(component, cfg, key, minimum=0.0, maximum=1.0)
    for key in ("i2c_retry_s", "media_reconnect_s", "media_source_refresh_s", "media_command_timeout_s",
                "minstrel_heartbeat_stale_s", "bluetooth_metadata_refresh_s", "record_source_hold_s",
                "eject_shutdown_hold_s", "heartbeat_period_s", "system_sample_interval_s"):
        _number(component, cfg, key, minimum=0.01, maximum=300.0)
    for key in ("pot_smoothing", "gain_deadband", "colour_deadband", "pickup_tolerance",
                "gain_input_min", "gain_input_max", "colour_input_min", "colour_input_max",
                "gain_output_max", "brightness_output_max", "colour_output_max", "saturation_output_max",
                "default_control_value", "default_indicator_brightness"):
        _number(component, cfg, key, minimum=0.0, maximum=1.0)
    for key in ("gain_curve", "colour_curve", "pot_input_max_v", "ads_gain", "state_save_interval_s",
                "network_pulse_s", "disk_pulse_s", "storage_warning_min_free_gb",
                "storage_warning_min_free_percent", "memory_warning_min_available_percent",
                "bluetooth_claim_threshold_s", "bluetooth_claim_linger_s",
                "record_source_hold_confirm_s", "shutdown_blink_hz"):
        _number(component, cfg, key, minimum=0.0)
    for key in ("gain_enabled", "graph_progress_show_first_segment", "graph_external_override_enabled",
                "media_keys_enabled", "bluetooth_media_enabled", "bluetooth_claim_metadata_enabled",
                "shutdown_enabled"):
        _boolean(component, cfg, key)
    for physical in ("gain", "colour"):
        lower = float(cfg[f"{physical}_input_min"])
        upper = float(cfg[f"{physical}_input_max"])
        if upper <= lower:
            _fail(component, f"{physical}_input_max", f"must exceed {physical}_input_min", upper)


MINSTREL_KEYS = {
    "proc_name", "server_name", "username", "password", "url", "token", "plex_client_name",
    "plex_control_client_name", "plex_control_client_machine_identifier", "poll_interval_s",
    "plex_timeout_s",
    "lyrics_update_interval_s", "clock_seek_threshold_s", "clock_fresh_nudge_max_s",
    "clock_fresh_deadband_s", "clock_offset_change_epsilon_s", "album_art_enabled",
    "album_art_width", "album_art_height", "album_art_timeout_s", "public_artwork_lookup",
    "secrets_path", "plex_retry_initial_s", "plex_retry_max_s",
}


def validate_minstrel(cfg: Mapping[str, Any], *, require_runtime_values: bool = True) -> None:
    component = "minstrel"
    _known(component, cfg, MINSTREL_KEYS)
    _text(component, cfg, "proc_name")
    for key in ("poll_interval_s", "plex_timeout_s", "lyrics_update_interval_s", "clock_seek_threshold_s",
                "clock_fresh_nudge_max_s", "clock_fresh_deadband_s", "clock_offset_change_epsilon_s",
                "album_art_timeout_s"):
        _number(component, cfg, key, minimum=0.0)
    for key in ("album_art_width", "album_art_height"):
        _number(component, cfg, key, minimum=12, maximum=48, integer=True)
    _boolean(component, cfg, "album_art_enabled")
    _booleans(component, cfg, ("public_artwork_lookup",))  # optional: older minstrel.yaml files omit it
    _texts(component, cfg, ("secrets_path",))
    _numbers(component, cfg, ("plex_retry_initial_s", "plex_retry_max_s"), minimum=0.5, maximum=3600.0)
    if "plex_retry_initial_s" in cfg and "plex_retry_max_s" in cfg and cfg["plex_retry_max_s"] < cfg["plex_retry_initial_s"]:
        _fail(component, "plex_retry_max_s", "must be at least plex_retry_initial_s", cfg["plex_retry_max_s"])
    for key in ("server_name", "username", "password", "url", "token", "plex_client_name",
                "plex_control_client_name", "plex_control_client_machine_identifier"):
        value = cfg.get(key)
        if value is not None and not isinstance(value, str):
            # Do not echo the value: these options may hold credentials.
            raise ConfigError(f"{component}: {key} must be a string or null; got {type(value).__name__}")
    if not require_runtime_values:
        return
    client_name = str(cfg.get("plex_client_name") or "").strip()
    if not client_name:
        _fail(component, "plex_client_name", "must identify this Deck's Plexamp client", cfg.get("plex_client_name"))
    has_url = bool(str(cfg.get("url") or "").strip())
    has_token_value = bool(str(cfg.get("token") or "").strip())
    if has_url != has_token_value:
        raise ConfigError("minstrel: url and token must be configured together")
    has_token = has_url and has_token_value
    has_account = all(str(cfg.get(key) or "").strip() for key in ("server_name", "username", "password"))
    if not (has_token or has_account):
        raise ConfigError(
            "minstrel: configure server_name plus username+password (preferably in minstrel_secrets.yaml), "
            "url+token, or the matching EMBERDECK_PLEX_* environment values"
        )


# ---------------------------------------------------------------------------
# Schemas for the remaining workers. Each validator checks only the options
# that are present, so it accepts both a worker's merged DEFAULTS+YAML (the
# runtime path, where every option is present) and a bare YAML file (the
# ``python runtime_config.py`` pre-deploy check). Unknown options are always
# rejected; retired names that old deployed files may still carry are listed
# explicitly so an upgrade does not turn a working file into a restart loop.
# ---------------------------------------------------------------------------


def _present(cfg: Mapping[str, Any], keys: Iterable[str]) -> Iterable[str]:
    return [key for key in keys if key in cfg]


def _numbers(component: str, cfg: Mapping[str, Any], keys: Iterable[str], **limits: Any) -> None:
    for key in _present(cfg, keys):
        _number(component, cfg, key, **limits)


def _optional_numbers(component: str, cfg: Mapping[str, Any], keys: Iterable[str], **limits: Any) -> None:
    for key in _present(cfg, keys):
        if cfg[key] is not None:
            _number(component, cfg, key, **limits)


def _texts(component: str, cfg: Mapping[str, Any], keys: Iterable[str], *, allow_blank: bool = False) -> None:
    for key in _present(cfg, keys):
        _text(component, cfg, key, allow_blank=allow_blank)


def _booleans(component: str, cfg: Mapping[str, Any], keys: Iterable[str]) -> None:
    for key in _present(cfg, keys):
        _boolean(component, cfg, key)


def _text_lists(component: str, cfg: Mapping[str, Any], keys: Iterable[str]) -> None:
    for key in _present(cfg, keys):
        _list_of_text(component, cfg, key)


def _bus_keys(component: str, cfg: Mapping[str, Any], keys: Iterable[str]) -> None:
    for key in _present(cfg, keys):
        value = cfg[key]
        if not isinstance(value, str) or not value.startswith("/") or len(value) < 2:
            _fail(component, key, "must be a Synapse key beginning with '/'", value)


def _choice(component: str, cfg: Mapping[str, Any], key: str, choices: Iterable[str]) -> None:
    if key in cfg and cfg[key] not in set(choices):
        _fail(component, key, f"must be one of {', '.join(sorted(choices))}", cfg[key])


ECHO_KEYS = {
    "proc_name", "sample_rate", "channels", "smoothness", "fps", "fft_size", "bins", "device",
    "waveform_samples", "waveform_key", "audio_reconnect_s",
}


def validate_echo(cfg: Mapping[str, Any]) -> None:
    component = "echo"
    _known(component, cfg, ECHO_KEYS)
    _texts(component, cfg, ("proc_name",))
    _numbers(component, cfg, ("sample_rate",), minimum=1000, integer=True)
    _numbers(component, cfg, ("channels",), minimum=1, maximum=32, integer=True)
    _numbers(component, cfg, ("fft_size",), minimum=16, integer=True)
    _numbers(component, cfg, ("waveform_samples",), minimum=16, integer=True)
    _numbers(component, cfg, ("fps",), minimum=1, maximum=1000)
    _numbers(component, cfg, ("smoothness", "audio_reconnect_s"), minimum=0.0)
    # Null or 0 means "native"; any other value is corrected to fft_size//2+1.
    _optional_numbers(component, cfg, ("bins",), minimum=0, integer=True)
    _bus_keys(component, cfg, ("waveform_key",))
    if "device" in cfg:
        device = cfg["device"]
        if isinstance(device, bool) or not isinstance(device, (str, int)) or (isinstance(device, str) and not device.strip()):
            _fail(component, "device", "must be a sounddevice name or numeric id", device)


FOXFIRE_KEYS = {
    "proc_name", "led_count", "fps", "mode_key", "active_mode_key", "active_mode_seq_key",
    "tape_mode_engaged_key", "source_selector_key", "visualiser_mode_index_key",
    "tape_source_position", "default_mode", "gain_key", "frame_key", "gain_floor", "gain_mid",
    "gain_ceiling", "release", "attack", "decay", "rise", "gamma", "fmin_hz", "fmax_hz",
    "split_hz", "reducer_hi", "pctl_hi", "pivot_hz", "tilt_power", "tilt_cap", "fast_mix",
    # Retired names: once dead entries in foxfire.DEFAULTS and never read. Still
    # accepted (and ignored) so an old deployed file does not stop Foxfire.
    "reducer", "tilt_pow",
}


def validate_foxfire(cfg: Mapping[str, Any]) -> None:
    component = "foxfire"
    _known(component, cfg, FOXFIRE_KEYS)
    _texts(component, cfg, ("proc_name",))
    _numbers(component, cfg, ("led_count",), minimum=1, maximum=1024, integer=True)
    _numbers(component, cfg, ("fps",), minimum=1, maximum=1000)
    _bus_keys(component, cfg, (
        "mode_key", "active_mode_key", "active_mode_seq_key", "tape_mode_engaged_key",
        "source_selector_key", "visualiser_mode_index_key", "gain_key", "frame_key",
    ))
    _numbers(component, cfg, ("tape_source_position",), minimum=1, maximum=3, integer=True)
    _numbers(component, cfg, ("default_mode",), minimum=0, maximum=3, integer=True)
    _numbers(component, cfg, ("gain_floor", "gain_mid", "gain_ceiling", "fmin_hz", "split_hz", "tilt_cap"), minimum=0.0)
    _numbers(component, cfg, ("release", "attack", "decay", "rise", "fast_mix"), minimum=0.0, maximum=1.0)
    _numbers(component, cfg, ("gamma", "tilt_power", "tilt_pow"), minimum=0.0)
    _numbers(component, cfg, ("pctl_hi",), minimum=0.0, maximum=100.0)
    _numbers(component, cfg, ("pivot_hz",), minimum=0.001)
    if "fmax_hz" in cfg and cfg["fmax_hz"] not in (None, "null"):
        _number(component, cfg, "fmax_hz", minimum=0.001)
    _choice(component, cfg, "reducer_hi", ("pctl", "max", "rms"))
    _choice(component, cfg, "reducer", ("pctl", "max", "rms"))


WILLO_WISP_KEYS = {"proc_name", "pixel_max_ma", "usb_current_ma", "external_power", "data_channel"}


def validate_willo_wisp(cfg: Mapping[str, Any]) -> None:
    component = "willo_wisp"
    _known(component, cfg, WILLO_WISP_KEYS)
    _texts(component, cfg, ("proc_name",))
    _numbers(component, cfg, ("pixel_max_ma",), minimum=0.001)
    _numbers(component, cfg, ("usb_current_ma",), minimum=0.0)
    # Strictly boolean: bool("false") is True, which would silently disable
    # USB current limiting for a USB-powered strip.
    _booleans(component, cfg, ("external_power",))
    _numbers(component, cfg, ("data_channel",), minimum=0, maximum=7, integer=True)


AURORA_PALETTE_KEYS = (
    "palette_near_black_value", "palette_near_black_saturation", "palette_grayscale_saturation",
    "palette_pale_grayscale_value", "palette_pure_black_gray_value", "palette_dark_min_saturation",
    "palette_dark_min_value", "palette_dominant_min_saturation", "palette_dominant_min_value",
    "palette_accent_min_saturation", "palette_accent_min_value", "palette_light_min_saturation",
    "palette_light_min_value",
)
AURORA_KEYS = {
    "proc_name", "dynamicColour", "scheme", "static_base_saturation", "ui_saturation_scale",
    "fallback_hue", "update_hz", "album_timeout_s", "generated_palette_enabled", *AURORA_PALETTE_KEYS,
    # Retired names still honoured by aurora.load_cfg() (saturation, base_hue)
    # or documented as retired (value, alpha) in aurora.yaml.
    "saturation", "base_hue", "value", "alpha",
}


def validate_aurora(cfg: Mapping[str, Any]) -> None:
    component = "aurora"
    _known(component, cfg, AURORA_KEYS)
    # Unrecognised schemes deliberately fall back to analogous (documented).
    _texts(component, cfg, ("proc_name", "scheme"))
    _booleans(component, cfg, ("dynamicColour", "generated_palette_enabled"))
    _numbers(component, cfg, ("static_base_saturation", "fallback_hue", *AURORA_PALETTE_KEYS), minimum=0.0, maximum=1.0)
    _numbers(component, cfg, ("ui_saturation_scale", "album_timeout_s"), minimum=0.0)
    _numbers(component, cfg, ("update_hz",), minimum=0.1, maximum=1000.0)


HDMI2_KEYS = {
    "proc_name", "poll_hz", "command_timeout_s", "startup_settle_s", "flatpak_path",
    "shortwave_app_id", "pear_appimage", "pear_args", "pear_process_match", "labwc_rules_enabled",
    "labwc_rc_path", "labwc_hdmi2_output", "labwc_path", "heartbeat_s",
}


def validate_hdmi2_source_controller(cfg: Mapping[str, Any]) -> None:
    component = "hdmi2_source_controller"
    _known(component, cfg, HDMI2_KEYS)
    _texts(component, cfg, ("proc_name", "flatpak_path", "shortwave_app_id", "pear_appimage",
                            "labwc_rc_path", "labwc_path"))
    # A blank match disables Pear termination; a blank output is reported at runtime.
    _texts(component, cfg, ("pear_process_match", "labwc_hdmi2_output"), allow_blank=True)
    _text_lists(component, cfg, ("pear_args",))
    _booleans(component, cfg, ("labwc_rules_enabled",))
    _numbers(component, cfg, ("poll_hz",), minimum=0.1, maximum=1000.0)
    _numbers(component, cfg, ("command_timeout_s", "startup_settle_s", "heartbeat_s"), minimum=0.0, maximum=300.0)


MPRIS_BRIDGE_KEYS = {
    "proc_name", "poll_hz", "refresh_s", "playerctl_path", "gdbus_path", "command_timeout_s",
    "metadata_buffer_bytes", "art_enabled", "art_width", "art_height", "art_timeout_s",
    "art_max_bytes", "unavailable_grace_s", "shortwave_players", "youtube_player_hints",
    "youtube_process_hints", "heartbeat_s",
}


def validate_mpris_bridge(cfg: Mapping[str, Any]) -> None:
    component = "mpris_bridge"
    _known(component, cfg, MPRIS_BRIDGE_KEYS)
    _texts(component, cfg, ("proc_name", "playerctl_path", "gdbus_path"))
    _numbers(component, cfg, ("poll_hz",), minimum=0.1, maximum=1000.0)
    _numbers(component, cfg, ("refresh_s", "command_timeout_s", "art_timeout_s", "unavailable_grace_s",
                              "heartbeat_s"), minimum=0.0, maximum=300.0)
    # Fixed shared-memory text size; mpris_bridge raises anything smaller to 64.
    _numbers(component, cfg, ("metadata_buffer_bytes",), minimum=64, maximum=65536, integer=True)
    _numbers(component, cfg, ("art_width", "art_height"), minimum=12, maximum=48, integer=True)
    _numbers(component, cfg, ("art_max_bytes",), minimum=1, integer=True)
    _booleans(component, cfg, ("art_enabled",))
    _text_lists(component, cfg, ("shortwave_players", "youtube_player_hints", "youtube_process_hints"))


WHISPER_KEYS = {
    "proc_name", "fps", "ambient_min_delay", "ambient_max_delay", "ambient_after_real_event_delay",
    "ambient_after_startup_delay", "ambient_queue_limit", "error_dedupe_cooldown_s",
    "reaction_min_delay_s", "reaction_max_delay_s", "reaction_max_per_event",
    "reaction_global_cooldown_s", "reaction_process_cooldown_s", "structured_log_path",
    "ui_event_limit", "ui_snapshot_interval_s", "important_log_path", "important_log_max_priority",
    "important_log_max_bytes_mb", "log_retention_days", "log_cleanup_interval_s",
    "legacy_log_paths", "critical_visible_s", "lifecycle_visible_s", "interaction_visible_s",
    "reaction_visible_s", "ambient_visible_s",
}


def validate_whisper_daemon(cfg: Mapping[str, Any]) -> None:
    """Whisper schema. Used by the pre-deploy check, not enforced at Whisper start.

    Whisper is the logging path for every other worker, and a Whisper exit
    restarts the whole group, so whisper_daemon.load_cfg() stays lenient.
    """
    component = "whisper_daemon"
    _known(component, cfg, WHISPER_KEYS)
    _texts(component, cfg, ("proc_name",))
    _texts(component, cfg, ("structured_log_path", "important_log_path"), allow_blank=True)
    _text_lists(component, cfg, ("legacy_log_paths",))
    _numbers(component, cfg, ("fps",), minimum=1.0, maximum=1000.0)
    _numbers(component, cfg, (
        "ambient_min_delay", "ambient_max_delay", "ambient_after_real_event_delay",
        "ambient_after_startup_delay", "error_dedupe_cooldown_s", "reaction_min_delay_s",
        "reaction_max_delay_s", "reaction_global_cooldown_s", "reaction_process_cooldown_s",
        "log_cleanup_interval_s", "ui_snapshot_interval_s", "critical_visible_s", "lifecycle_visible_s",
        "interaction_visible_s", "reaction_visible_s", "ambient_visible_s",
    ), minimum=0.0)
    _numbers(component, cfg, ("important_log_max_bytes_mb",), minimum=0.001)
    _numbers(component, cfg, ("ambient_queue_limit", "ui_event_limit", "log_retention_days"), minimum=1, integer=True)
    _numbers(component, cfg, ("reaction_max_per_event",), minimum=0, integer=True)
    _numbers(component, cfg, ("important_log_max_priority",), minimum=0, maximum=4, integer=True)
    for lower, upper in (("ambient_min_delay", "ambient_max_delay"), ("reaction_min_delay_s", "reaction_max_delay_s")):
        if lower in cfg and upper in cfg and float(cfg[upper]) < float(cfg[lower]):
            _fail(component, upper, f"must be at least {lower}", cfg[upper])


EMBER_UI_KEYS = {
    "proc_name", "logical_width", "logical_height", "update_hz", "metadata_refresh_hz",
    "global_refresh_hz", "now_refresh_hz", "scope_refresh_hz", "info_refresh_hz",
    "console_refresh_hz", "palette_refresh_hz", "ui_brightness_floor", "ui_brightness_ceiling",
    "system_refresh_s", "log_refresh_s", "log_path", "structured_log_path",
    "structured_log_required", "metadata_dead_grace_s", "art_dead_grace_s", "waveform_key",
    "waveform_samples", "safe_margin_cells", "diagnostics_scroll_step_s",
}


def validate_ember_ui(cfg: Mapping[str, Any]) -> None:
    """Ember UI schema. Used by the pre-deploy check; the UI is not a Foundry worker."""
    component = "ember_ui"
    _known(component, cfg, EMBER_UI_KEYS)
    _texts(component, cfg, ("proc_name", "log_path", "structured_log_path"))
    _bus_keys(component, cfg, ("waveform_key",))
    _booleans(component, cfg, ("structured_log_required",))
    _numbers(component, cfg, ("logical_width", "logical_height", "waveform_samples"), minimum=16, integer=True)
    _numbers(component, cfg, ("safe_margin_cells",), minimum=0, integer=True)
    _numbers(component, cfg, (
        "update_hz", "metadata_refresh_hz", "global_refresh_hz", "now_refresh_hz", "scope_refresh_hz",
        "info_refresh_hz", "console_refresh_hz", "palette_refresh_hz",
    ), minimum=0.1, maximum=1000.0)
    _numbers(component, cfg, ("ui_brightness_floor", "ui_brightness_ceiling"), minimum=0.0, maximum=1.0)
    _numbers(component, cfg, ("system_refresh_s", "log_refresh_s", "metadata_dead_grace_s", "art_dead_grace_s",
                              "diagnostics_scroll_step_s"), minimum=0.0)


RICH_PRESENCE_KEYS = {"proc_name", "enabled", "discord", "presence"}
RICH_PRESENCE_DISCORD_KEYS = {
    "application_id", "backend", "native_bridge", "sdk_library_dir", "box64", "auth_state_path",
    "request_timeout_s", "retry_initial_s", "retry_max_s", "auth_timeout_s", "refresh_margin_s",
}
RICH_PRESENCE_BOX64_KEYS = {"executable", "bridge"}
RICH_PRESENCE_PRESENCE_KEYS = {
    "show_source", "show_media_metadata", "show_idle", "activity_type", "artwork_mode", "update_debounce_s",
    "min_update_interval_s", "project_url",
}


def _section(component: str, cfg: Mapping[str, Any], key: str, known: Iterable[str]) -> Mapping[str, Any]:
    value = cfg.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail(component, key, "must be a mapping", value)
    unknown = sorted(set(value) - set(known))
    if unknown:
        raise ConfigError(f"{component}: unknown option(s) in {key}: {', '.join(map(str, unknown))}")
    return value


def _optional_texts(component: str, cfg: Mapping[str, Any], keys: Iterable[str]) -> None:
    for key in _present(cfg, keys):
        if cfg[key] is not None:
            _text(component, cfg, key)


def validate_rich_presence(cfg: Mapping[str, Any]) -> None:
    """Discord Rich Presence. Nested sections are checked by presence."""
    component = "rich_presence"
    _known(component, cfg, RICH_PRESENCE_KEYS)
    _texts(component, cfg, ("proc_name",))
    _booleans(component, cfg, ("enabled",))

    discord = _section(component, cfg, "discord", RICH_PRESENCE_DISCORD_KEYS)
    prefix = f"{component}.discord: "
    if "application_id" in discord and discord["application_id"] is not None:
        value = discord["application_id"]
        text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
        if not isinstance(text, str) or not text.isdigit() or not 15 <= len(text) <= 22:
            raise ConfigError(f"{prefix}application_id must be the numeric Discord application ID or null")
    _choice(f"{component}.discord", discord, "backend", ("auto", "native", "box64"))
    _optional_texts(f"{component}.discord", discord, ("native_bridge", "sdk_library_dir"))
    _texts(f"{component}.discord", discord, ("auth_state_path",))
    _numbers(f"{component}.discord", discord, ("request_timeout_s",), minimum=0.5, maximum=60.0)
    _numbers(f"{component}.discord", discord, ("retry_initial_s", "retry_max_s"), minimum=0.5, maximum=86400.0)
    _numbers(f"{component}.discord", discord, ("auth_timeout_s",), minimum=60.0, maximum=3600.0)
    _numbers(f"{component}.discord", discord, ("refresh_margin_s",), minimum=60.0, maximum=7 * 86400.0)
    if "retry_initial_s" in discord and "retry_max_s" in discord and discord["retry_max_s"] < discord["retry_initial_s"]:
        raise ConfigError(f"{prefix}retry_max_s must be at least retry_initial_s")
    box64 = _section(f"{component}.discord", discord, "box64", RICH_PRESENCE_BOX64_KEYS)
    _texts(f"{component}.discord.box64", box64, ("executable",))
    _optional_texts(f"{component}.discord.box64", box64, ("bridge",))

    presence = _section(component, cfg, "presence", RICH_PRESENCE_PRESENCE_KEYS)
    _booleans(f"{component}.presence", presence, ("show_source", "show_media_metadata", "show_idle"))
    _choice(f"{component}.presence", presence, "activity_type", ("listening", "playing"))
    _choice(f"{component}.presence", presence, "artwork_mode", ("off", "audit", "on"))
    _numbers(f"{component}.presence", presence, ("update_debounce_s",), minimum=0.0, maximum=60.0)
    _numbers(f"{component}.presence", presence, ("min_update_interval_s",), minimum=1.0, maximum=300.0)
    # project_url is deliberately not checked here: an unusable value only turns
    # the image links off (presence_model.link_url), it never stops the worker.


ARTWORK_SERVICE_KEYS = {"proc_name", "enabled", "port", "public_base_url", "cache_dir", "retention_days",
                        "max_cache_mb"}


def validate_artwork_service(cfg: Mapping[str, Any]) -> None:
    """Local artwork origin for Discord (artwork_service.py). Disabled unless enabled: true."""
    from urllib.parse import urlsplit

    component = "artwork_service"
    _known(component, cfg, ARTWORK_SERVICE_KEYS)
    _texts(component, cfg, ("proc_name", "cache_dir"))
    _booleans(component, cfg, ("enabled",))
    _numbers(component, cfg, ("port",), minimum=1024, maximum=65535, integer=True)
    # retention_days: retired (artwork no longer expires by age); accepted and ignored.
    _numbers(component, cfg, ("retention_days",), minimum=0.0)
    _numbers(component, cfg, ("max_cache_mb",), minimum=16, maximum=65536, integer=True)
    if cfg.get("enabled") and not cfg.get("public_base_url"):
        _fail(component, "public_base_url", "is required when enabled (your Cloudflare Tunnel hostname, "
              "e.g. https://artwork.example.org)", cfg.get("public_base_url"))
    if cfg.get("public_base_url") is not None:
        value = cfg["public_base_url"]
        try:
            parts = urlsplit(value) if isinstance(value, str) else None
            port = parts.port if parts is not None else None
        except ValueError:
            parts, port = None, None
        host = (parts.hostname or "") if parts is not None else ""
        if (parts is None or parts.scheme != "https" or "." not in host or parts.username or parts.password
                or port is not None or parts.query or parts.fragment or parts.path not in ("", "/")):
            # Not echoed: a malformed URL could carry user:password@.
            raise ConfigError(f"{component}: public_base_url must be an https origin such as "
                              "https://artwork.example.org (no path, port, query or credentials)")


VALIDATORS = {
    "artwork_service": validate_artwork_service,
    "echo": validate_echo,
    "foxfire": validate_foxfire,
    "willo_wisp": validate_willo_wisp,
    "aurora": validate_aurora,
    "hdmi2_source_controller": validate_hdmi2_source_controller,
    "mpris_bridge": validate_mpris_bridge,
    "whisper_daemon": validate_whisper_daemon,
    "ember_ui": validate_ember_ui,
    "rich_presence": validate_rich_presence,
}


def validate_component(component: str, cfg: Mapping[str, Any], *, require_runtime_values: bool = True) -> None:
    """Validate merged runtime configuration for components with strict schemas."""

    if component == "pawprint":
        validate_pawprint(cfg)
    elif component == "minstrel":
        validate_minstrel(cfg, require_runtime_values=require_runtime_values)
    elif component in VALIDATORS:
        VALIDATORS[component](cfg)


def _check_plex_auth(cfg: Mapping[str, Any], path: Path) -> None:
    """Resolve Plex credentials exactly as Minstrel does, reporting gaps without values."""
    import os

    from plex_auth import resolve_plex_auth

    merged = dict(cfg)
    merged["_config_dir"] = str(path.parent)
    for env_name, option in (("EMBERDECK_PLEX_SERVER_NAME", "server_name"),
                             ("EMBERDECK_PLEX_CLIENT_NAME", "plex_client_name")):
        if env_name in os.environ:
            merged[option] = os.environ[env_name]
    auth = resolve_plex_auth(merged, os.environ)
    problems = [message for _, _, message in auth.problems]
    if not auth.configured:
        problems.insert(0, f"Plex is not configured: {auth.missing_description()}")
    if problems:
        raise ConfigError("minstrel: " + " | ".join(problems))


def check_config_dir(config_dir: Path, *, require_runtime_values: bool = False) -> Dict[str, str]:
    """Validate every known ``<component>.yaml`` in a directory.

    Returns ``{component: "ok" | error message | "missing"}``. Pawprint is
    validated against its option names and types only for the keys present,
    because its strict validator expects the merged defaults.
    """
    results: Dict[str, str] = {}
    for component in ("pawprint", "minstrel", *VALIDATORS):
        path = Path(config_dir) / f"{component}.yaml"
        if not path.is_file():
            results[component] = "missing"
            continue
        try:
            cfg = load_yaml_mapping(path, component)
            if component == "pawprint":
                _known(component, cfg, PAWPRINT_KEYS)
            elif component == "minstrel":
                validate_minstrel(cfg, require_runtime_values=False)
                if require_runtime_values:
                    _check_plex_auth(cfg, path)
            else:
                validate_component(component, cfg, require_runtime_values=require_runtime_values)
            results[component] = "ok"
        except ConfigError as exc:
            results[component] = str(exc)
    return results


LOCAL_SUFFIX = ".local.yaml"


def local_overlay_path(path: Path) -> Path:
    """``configs/<name>.local.yaml`` beside ``configs/<name>.yaml``."""
    path = Path(path)
    return path.with_name(path.name[: -len(".yaml")] + LOCAL_SUFFIX) if path.name.endswith(".yaml") else path


def _read_yaml_file(path: Path, component: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise ConfigError(f"{component}: cannot read {path}: {reason}") from None
    try:
        import yaml
    except ImportError:
        raise ConfigError(f"{component}: cannot read {path}: PyYAML is not installed") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # The parser's own message quotes the offending line, which could be a
        # credential; report only what and where.
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigError(f"{component}: cannot read {path}: invalid YAML ({problem}{where})") from None


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml_mapping(path: Path, component: str) -> Dict[str, Any]:
    """Load one YAML mapping, preserving an actionable component prefix.

    If ``<name>.local.yaml`` exists beside ``<name>.yaml`` it is merged on
    top (nested sections key by key). The shipped file stays a portable,
    tracked template; the gitignored local file holds this Deck's public
    deployment values (application IDs, hostnames, paths, preferences).
    Credentials never belong in either: they live in ``*_secrets.yaml``.
    Parse errors never quote file content.
    """
    loaded = require_mapping(component, _read_yaml_file(path, component))
    overlay_path = local_overlay_path(path)
    if overlay_path != Path(path) and overlay_path.is_file():
        overlay = _read_yaml_file(overlay_path, component)
        if overlay is not None:
            if not isinstance(overlay, dict):
                raise ConfigError(f"{component}: {overlay_path} must contain a YAML mapping")
            loaded = _deep_merge(loaded, overlay)
    return loaded


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Pre-deploy check: ``python runtime_config.py [configs-dir] [--require-secrets]``."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    require = "--require-secrets" in args
    paths = [arg for arg in args if not arg.startswith("--")]
    config_dir = Path(paths[0]) if paths else Path(__file__).resolve().parent / "configs"
    failed = False
    for component, outcome in check_config_dir(config_dir, require_runtime_values=require).items():
        print(f"{component:<26} {outcome}")
        failed = failed or outcome not in ("ok", "missing")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
