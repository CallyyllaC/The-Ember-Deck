"""Tests for operator-facing configuration failures."""

import ast
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "EmberEngine"
sys.path.insert(0, str(ENGINE))

from runtime_config import ConfigError, require_mapping, validate_minstrel, validate_pawprint  # noqa: E402


def pawprint_defaults():
    tree = ast.parse((ENGINE / "pawprint.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "DEFAULTS" for target in node.targets):
                values = ast.literal_eval(node.value)
                values["state_path"] = "pawprint_state.yaml"
                return values
    raise AssertionError("pawprint DEFAULTS not found")


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


if __name__ == "__main__":
    unittest.main()
