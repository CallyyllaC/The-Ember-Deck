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
    "album_art_width", "album_art_height", "album_art_timeout_s",
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
    for key in ("server_name", "username", "password", "url", "token", "plex_client_name",
                "plex_control_client_name", "plex_control_client_machine_identifier"):
        value = cfg.get(key)
        if value is not None and not isinstance(value, str):
            _fail(component, key, "must be a string or null", value)
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
            "minstrel: configure url+token, server_name+username+password, or the matching EMBERDECK_PLEX_* environment values"
        )


def validate_component(component: str, cfg: Mapping[str, Any], *, require_runtime_values: bool = True) -> None:
    """Validate merged runtime configuration for components with strict schemas."""

    if component == "pawprint":
        validate_pawprint(cfg)
    elif component == "minstrel":
        validate_minstrel(cfg, require_runtime_values=require_runtime_values)


def load_yaml_mapping(path: Path, component: str) -> Dict[str, Any]:
    """Load one YAML mapping, preserving an actionable component prefix."""

    try:
        import yaml
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"{component}: cannot read {path}: {exc}") from exc
    return require_mapping(component, loaded)
