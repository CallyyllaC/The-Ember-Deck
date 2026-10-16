"""Tests for operator-facing configuration failures."""

import ast
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "EmberEngine"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_config  # noqa: E402
from runtime_config import ConfigError, require_mapping, validate_minstrel, validate_pawprint  # noqa: E402
from _support import has_module  # noqa: E402


def module_defaults(module: str) -> dict:
    """Read a worker's DEFAULTS without importing its hardware dependencies."""
    tree = ast.parse((ENGINE / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == "DEFAULTS" for target in targets):
            value = node.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "dict":
                return {kw.arg: ast.literal_eval(kw.value) for kw in value.keywords}
            return ast.literal_eval(value)
    raise AssertionError(f"{module} DEFAULTS not found")


def pawprint_defaults():
    values = module_defaults("pawprint")
    values["state_path"] = "pawprint_state.yaml"
    return values


def valid_minstrel():
    return {
        "proc_name": "minstrel",
        "server_name": "Media",
        "username": "operator",
        "password": "secret",
        "url": None,
        "token": None,
        "plex_client_name": "Ember Deck",
        "plex_control_client_name": None,
        "plex_control_client_machine_identifier": None,
        "poll_interval_s": 1.0,
        "plex_timeout_s": 5.0,
        "lyrics_update_interval_s": 0.25,
        "clock_seek_threshold_s": 18.0,
        "clock_fresh_nudge_max_s": 1.25,
        "clock_fresh_deadband_s": 0.35,
        "clock_offset_change_epsilon_s": 0.001,
        "album_art_enabled": True,
        "album_art_width": 48,
        "album_art_height": 48,
        "album_art_timeout_s": 5.0,
    }


class ConfigurationValidationTests(unittest.TestCase):
    def test_yaml_root_must_be_mapping(self):
        with self.assertRaisesRegex(ConfigError, "pawprint: configuration root must be a mapping"):
            require_mapping("pawprint", ["not", "options"])

    def test_minstrel_rejects_old_list_placeholder(self):
        cfg = valid_minstrel()
        cfg["server_name"] = ["YOUR_PLEX_SERVER"]
        with self.assertRaisesRegex(ConfigError, "minstrel: server_name must be a string or null"):
            validate_minstrel(cfg)

    def test_minstrel_requires_deck_client_identity(self):
        cfg = valid_minstrel()
        cfg["plex_client_name"] = None
        with self.assertRaisesRegex(ConfigError, "must identify this Deck's Plexamp client"):
            validate_minstrel(cfg)

    def test_minstrel_accepts_token_login(self):
        cfg = valid_minstrel()
        cfg.update({"server_name": None, "username": None, "password": None, "url": "https://plex", "token": "token"})
        validate_minstrel(cfg)

    def test_minstrel_rejects_half_configured_token_login(self):
        cfg = valid_minstrel()
        cfg.update({"server_name": None, "username": None, "password": None, "url": "https://plex", "token": None})
        with self.assertRaisesRegex(ConfigError, "url and token must be configured together"):
            validate_minstrel(cfg)

    def test_pawprint_rejects_malformed_poll_rate(self):
        cfg = pawprint_defaults()
        cfg["poll_hz"] = "fast"
        with self.assertRaisesRegex(ConfigError, "pawprint: poll_hz must be an integer"):
            validate_pawprint(cfg)

    def test_pawprint_rejects_inverted_normalised_gain_limits(self):
        cfg = pawprint_defaults()
        cfg["gain_input_min"] = 0.9
        cfg["gain_input_max"] = 0.2
        with self.assertRaisesRegex(ConfigError, "gain_input_max must exceed gain_input_min"):
            validate_pawprint(cfg)

    def test_pawprint_rejects_unknown_option(self):
        cfg = pawprint_defaults()
        cfg["button_debouce_s"] = 0.1
        with self.assertRaisesRegex(ConfigError, "unknown option.*button_debouce_s"):
            validate_pawprint(cfg)


# Components whose worker load_cfg() now enforces a strict schema.
ENFORCED = ("echo", "foxfire", "willo_wisp", "aurora", "hdmi2_source_controller", "mpris_bridge", "rich_presence",
            "artwork_service")
# Schemas used by the pre-deploy check only.
CHECK_ONLY = ("whisper_daemon", "ember_ui")


class WorkerSchemaTests(unittest.TestCase):
    """New strict schemas must accept everything the shipped workers already accept."""

    def merged(self, component, **overrides):
        cfg = module_defaults(component)
        cfg.update(overrides)
        return cfg

    def test_every_worker_default_set_is_valid(self):
        for component in ENFORCED + CHECK_ONLY:
            with self.subTest(component=component):
                runtime_config.validate_component(component, module_defaults(component))

    def test_unknown_options_are_rejected_for_every_schema(self):
        for component in ENFORCED + CHECK_ONLY:
            with self.subTest(component=component):
                with self.assertRaisesRegex(ConfigError, f"{component}: unknown option.*not_an_option"):
                    runtime_config.validate_component(component, {"not_an_option": 1})

    def test_bare_yaml_subset_is_validated_by_presence(self):
        runtime_config.validate_echo({"fps": 50})
        with self.assertRaisesRegex(ConfigError, "echo: fps must be at least 1"):
            runtime_config.validate_echo({"fps": 0})

    def test_echo_shape_options_must_be_integers(self):
        with self.assertRaisesRegex(ConfigError, "echo: fft_size must be an integer"):
            runtime_config.validate_echo(self.merged("echo", fft_size="2048"))
        with self.assertRaisesRegex(ConfigError, "echo: waveform_samples must be at least 16"):
            runtime_config.validate_echo(self.merged("echo", waveform_samples=8))
        runtime_config.validate_echo(self.merged("echo", bins=None, device=3))
        with self.assertRaisesRegex(ConfigError, "echo: waveform_key must be a Synapse key"):
            runtime_config.validate_echo(self.merged("echo", waveform_key="audio/waveform"))

    def test_foxfire_rejects_impossible_strip_and_modes(self):
        with self.assertRaisesRegex(ConfigError, "foxfire: led_count must be at least 1"):
            runtime_config.validate_foxfire(self.merged("foxfire", led_count=0))
        with self.assertRaisesRegex(ConfigError, "foxfire: default_mode must be at most 3"):
            runtime_config.validate_foxfire(self.merged("foxfire", default_mode=4))
        with self.assertRaisesRegex(ConfigError, "foxfire: reducer_hi must be one of max, pctl, rms"):
            runtime_config.validate_foxfire(self.merged("foxfire", reducer_hi="median"))
        runtime_config.validate_foxfire(self.merged("foxfire", fmax_hz=None))
        runtime_config.validate_foxfire(self.merged("foxfire", fmax_hz="null"))

    def test_willo_wisp_external_power_must_be_a_real_boolean(self):
        # bool("false") is True: a quoted value would silently disable the USB current cap.
        with self.assertRaisesRegex(ConfigError, "willo_wisp: external_power must be true or false"):
            runtime_config.validate_willo_wisp(self.merged("willo_wisp", external_power="false"))

    def test_aurora_accepts_retired_aliases_but_bounds_palette_policy(self):
        runtime_config.validate_aurora(self.merged("aurora", saturation=0.7, base_hue=200, value=1.0, alpha=1.0))
        with self.assertRaisesRegex(ConfigError, "aurora: palette_accent_min_value must be at most 1"):
            runtime_config.validate_aurora(self.merged("aurora", palette_accent_min_value=1.5))
        # Unknown schemes are documented to fall back to analogous, so stay accepted.
        runtime_config.validate_aurora(self.merged("aurora", scheme="something-new"))

    def test_hdmi2_and_mpris_list_options_must_be_lists_of_strings(self):
        with self.assertRaisesRegex(ConfigError, "pear_args must be a list of strings"):
            runtime_config.validate_hdmi2_source_controller(
                self.merged("hdmi2_source_controller", pear_args="--ozone-platform=wayland"))
        with self.assertRaisesRegex(ConfigError, "shortwave_players must be a list of strings"):
            runtime_config.validate_mpris_bridge(self.merged("mpris_bridge", shortwave_players="Shortwave"))
        with self.assertRaisesRegex(ConfigError, "metadata_buffer_bytes must be at least 64"):
            runtime_config.validate_mpris_bridge(self.merged("mpris_bridge", metadata_buffer_bytes=32))

    def test_whisper_delay_bounds_must_be_ordered(self):
        with self.assertRaisesRegex(ConfigError, "ambient_max_delay must be at least ambient_min_delay"):
            runtime_config.validate_whisper_daemon(self.merged("whisper_daemon", ambient_min_delay=90, ambient_max_delay=60))


@unittest.skipUnless(has_module("yaml"), "PyYAML is required to parse the shipped YAML")
class ShippedConfigurationTests(unittest.TestCase):
    def test_every_shipped_yaml_file_passes_its_schema(self):
        results = runtime_config.check_config_dir(ENGINE / "configs")
        self.assertEqual({name: outcome for name, outcome in results.items() if outcome != "ok"}, {})

    def test_shipped_yaml_merged_with_defaults_is_valid(self):
        for component in ENFORCED:
            with self.subTest(component=component):
                cfg = module_defaults(component)
                cfg.update(runtime_config.load_yaml_mapping(ENGINE / "configs" / f"{component}.yaml", component))
                runtime_config.validate_component(component, cfg)

    def test_shipped_minstrel_template_has_no_credentials(self):
        cfg = runtime_config.load_yaml_mapping(ENGINE / "configs" / "minstrel.yaml", "minstrel")
        for key in ("username", "password", "url", "token"):
            self.assertIsNone(cfg.get(key), key)

    def test_pre_deploy_cli_reports_failures_with_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "foxfire.yaml").write_text("led_count: 0\n", encoding="utf-8")
            Path(tmp, "echo.yaml").write_text("fps: 50\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                status = runtime_config.main([tmp])
        self.assertEqual(status, 1)
        self.assertIn("foxfire: led_count must be at least 1", output.getvalue())
        self.assertRegex(output.getvalue(), r"echo\s+ok")
        self.assertRegex(output.getvalue(), r"pawprint\s+missing")

    def test_unreadable_yaml_is_an_actionable_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "echo.yaml")
            path.write_text("fps: [unclosed\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "echo: cannot read"):
                runtime_config.load_yaml_mapping(path, "echo")


if __name__ == "__main__":
    unittest.main()
