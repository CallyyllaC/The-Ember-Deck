"""Portable configuration for the Discord / Cloudflare / Plex integrations.

The shipped ``configs/<name>.yaml`` files are templates: they must carry no
personal deployment values and keep optional integrations off. A Deck's own
public values go in the gitignored ``<name>.local.yaml`` overlay, credentials
in ``*_secrets.yaml``. These tests read the shipped files directly, never a
developer's local overlay.
"""

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, has_module  # noqa: E402

import runtime_config  # noqa: E402

HAS_YAML = has_module("yaml")
ROOT = ENGINE.parent
CONFIGS = ENGINE / "configs"


def shipped(name):
    import yaml
    return yaml.safe_load((CONFIGS / f"{name}.yaml").read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_YAML, "PyYAML is required")
class ShippedTemplateTests(unittest.TestCase):
    def test_discord_integrations_are_off_and_unconfigured(self):
        rp = shipped("rich_presence")
        self.assertIs(rp["enabled"], False)
        self.assertIsNone(rp["discord"]["application_id"])
        self.assertEqual(rp["discord"]["box64"]["executable"], "box64", "no machine-specific absolute path")
        self.assertIs(rp["presence"]["show_media_metadata"], False)
        self.assertEqual(rp["presence"]["artwork_mode"], "audit")
        self.assertEqual(rp["presence"]["project_url"], "https://awoo.ltd/projects/ember-deck",
                         "the image links point at the project's canonical AWOO page")
        art = shipped("artwork_service")
        self.assertIs(art["enabled"], False)
        self.assertIsNone(art["public_base_url"])

    def test_plex_identity_is_not_shipped(self):
        minstrel = shipped("minstrel")
        for key in ("server_name", "plex_client_name", "username", "password", "url", "token"):
            self.assertIsNone(minstrel.get(key), key)
        self.assertIs(minstrel["public_artwork_lookup"], False)

    def test_templates_contain_no_deployment_values(self):
        for name in ("rich_presence", "artwork_service", "minstrel"):
            text = (CONFIGS / f"{name}.yaml").read_text(encoding="utf-8")
            code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
            # The public project page (Rich Presence image links) is not a deployment value.
            code = code.replace("project_url: https://awoo.ltd/projects/ember-deck", "")
            with self.subTest(config=name):
                self.assertNotRegex(code, r"\b\d{15,22}\b", "no Discord application ID")
                self.assertNotRegex(code, r"https?://", "no hostnames or URLs")
                self.assertNotRegex(code, r"(?m):\s*/(home|usr|opt|etc)/", "no absolute local paths")

    def test_shipped_templates_validate(self):
        results = runtime_config.check_config_dir(CONFIGS)
        for name in ("rich_presence", "artwork_service", "minstrel"):
            self.assertEqual(results[name], "ok", name)


class SourceCodeTests(unittest.TestCase):
    def test_no_deployment_hostnames_or_ids_in_code(self):
        sources = list(ENGINE.glob("*.py")) + list((ENGINE / "discord_bridge" / "src").glob("*.[ch]pp"))
        for path in sources:
            if path.name == "awoo_supporter.py":
                continue  # the licence issuer's identity, not a deployment value
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("awoo.ltd", text)
                self.assertNotRegex(text, r"\b\d{17,20}\b", "no hardcoded Discord snowflake IDs")

    def test_artwork_service_has_no_built_in_hostname(self):
        import artwork_service
        self.assertIsNone(artwork_service.DEFAULTS["public_base_url"])


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "git checkout required")
class IgnoreRulesTests(unittest.TestCase):
    def test_private_files_are_ignored(self):
        for path in ("EmberEngine/configs/rich_presence.local.yaml", "EmberEngine/configs/artwork_service.local.yaml",
                     "EmberEngine/configs/minstrel.local.yaml", "EmberEngine/configs/discord_secrets.yaml",
                     "EmberEngine/configs/minstrel_secrets.yaml", "EmberEngine/third_party/discord_social_sdk/x.so",
                     "EmberEngine/DiscordSocialSdk-1.10.19337.zip", "EmberEngine/discord_bridge/build-x86_64/x",
                     "supporter.licence"):
            with self.subTest(path=path):
                self.assertEqual(subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode, 0)

    def test_templates_are_not_ignored(self):
        for path in ("EmberEngine/configs/rich_presence.yaml", "EmberEngine/configs/artwork_service.yaml",
                     "EmberEngine/configs/minstrel_secrets.example.yaml"):
            with self.subTest(path=path):
                self.assertEqual(subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode, 1)


@unittest.skipUnless(HAS_YAML, "PyYAML is required")
class LocalOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text):
        (self.tmp / name).write_text(text, encoding="utf-8")
        return self.tmp / name

    def test_overlay_merges_nested_sections_key_by_key(self):
        base = self.write("rich_presence.yaml", "enabled: false\ndiscord:\n  application_id: null\n"
                                                "  backend: auto\npresence:\n  show_source: true\n")
        self.write("rich_presence.local.yaml", "enabled: true\ndiscord:\n  application_id: 123456789012345678\n")
        merged = runtime_config.load_yaml_mapping(base, "rich_presence")
        self.assertEqual(merged, {"enabled": True, "discord": {"application_id": 123456789012345678,
                                                               "backend": "auto"},
                                  "presence": {"show_source": True}})

    def test_absent_or_empty_overlay_changes_nothing(self):
        base = self.write("minstrel.yaml", "proc_name: minstrel\n")
        self.assertEqual(runtime_config.load_yaml_mapping(base, "minstrel"), {"proc_name": "minstrel"})
        self.write("minstrel.local.yaml", "# nothing yet\n")
        self.assertEqual(runtime_config.load_yaml_mapping(base, "minstrel"), {"proc_name": "minstrel"})

    def test_bad_overlay_is_a_config_error(self):
        base = self.write("artwork_service.yaml", "enabled: false\n")
        self.write("artwork_service.local.yaml", "- a list\n")
        with self.assertRaisesRegex(runtime_config.ConfigError, "artwork_service.local.yaml must contain a YAML mapping"):
            runtime_config.load_yaml_mapping(base, "artwork_service")

    def test_parse_errors_never_quote_file_content(self):
        secret = "hunter2-SECRET-value"
        base = self.write("minstrel.yaml", f"proc_name: minstrel\npassword: [{secret}\n")
        with self.assertRaises(runtime_config.ConfigError) as raised:
            runtime_config.load_yaml_mapping(base, "minstrel")
        self.assertNotIn(secret, str(raised.exception))
        self.assertRegex(str(raised.exception), r"cannot read .*invalid YAML .*line \d+")
        good = self.write("rich_presence.yaml", "enabled: false\n")
        self.write("rich_presence.local.yaml", f"discord:\n  application_id: {secret}: x\n")
        with self.assertRaises(runtime_config.ConfigError) as raised:
            runtime_config.load_yaml_mapping(good, "rich_presence")
        self.assertNotIn(secret, str(raised.exception))
        out = io.StringIO()
        with redirect_stdout(out):
            runtime_config.main([str(self.tmp)])
        self.assertNotIn(secret, out.getvalue(), "the pre-deploy check never prints file content")

    def test_the_validator_sees_the_overlay(self):
        self.write("artwork_service.yaml", "proc_name: artwork_service\nenabled: false\npublic_base_url: null\n")
        self.assertEqual(runtime_config.check_config_dir(self.tmp)["artwork_service"], "ok")
        self.write("artwork_service.local.yaml", "enabled: true\n")
        self.assertIn("public_base_url is required", runtime_config.check_config_dir(self.tmp)["artwork_service"])
        self.write("artwork_service.local.yaml", "enabled: true\npublic_base_url: https://artwork.example.org\n")
        self.assertEqual(runtime_config.check_config_dir(self.tmp)["artwork_service"], "ok")


@unittest.skipUnless(HAS_YAML, "PyYAML is required")
class ArtworkServiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        import artwork_service
        self.svc = artwork_service
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_enabled_requires_a_public_base_url(self):
        with self.assertRaisesRegex(runtime_config.ConfigError, "public_base_url is required when enabled"):
            runtime_config.validate_artwork_service(dict(self.svc.DEFAULTS, enabled=True))
        runtime_config.validate_artwork_service(dict(self.svc.DEFAULTS))  # disabled: nothing required

    def test_rejected_base_url_is_never_echoed(self):
        for bad in ("https://user:hunter2pass@artwork.example.org", "https://artwork.example.org/?token=hunter2pass",
                    "http://artwork.example.org", "https://artwork.example.org:8443", "https://localhost"):
            with self.subTest(url=bad), self.assertRaises(runtime_config.ConfigError) as raised:
                runtime_config.validate_artwork_service(dict(self.svc.DEFAULTS, enabled=True, public_base_url=bad))
            self.assertNotIn("hunter2pass", str(raised.exception))
            self.assertIn("public_base_url must be an https origin", str(raised.exception))

    def test_public_urls_come_from_configuration(self):
        (self.tmp / "artwork_service.yaml").write_text(
            "proc_name: artwork_service\nenabled: false\n", encoding="utf-8")
        (self.tmp / "artwork_service.local.yaml").write_text(
            f"enabled: true\npublic_base_url: https://covers.example.net\ncache_dir: {self.tmp.as_posix()}/c\n",
            encoding="utf-8")
        cache = self.svc.cache_for_config_dir(self.tmp)
        self.assertEqual(cache.url_for("album-artist", "abcdef012345"),
                         "https://covers.example.net/album-artist.png?v=abcdef012345")

    def test_disabled_or_unconfigured_service_has_no_cache(self):
        self.assertIsNone(self.svc.cache_from_cfg(dict(self.svc.DEFAULTS)))
        self.assertIsNone(self.svc.cache_from_cfg(dict(self.svc.DEFAULTS, enabled=True)))
        self.assertIsNone(self.svc.cache_for_config_dir(self.tmp), "no file: disabled")


@unittest.skipUnless(HAS_YAML, "PyYAML is required")
class RichPresenceCredentialTests(unittest.TestCase):
    """Disabled or unconfigured Discord never touches credentials or authentication."""

    def test_disabled_or_unconfigured_never_reads_tokens_or_starts_the_bridge(self):
        import test_rich_presence as trp
        import rich_presence as rp
        for overrides in ({"enabled": False}, {"discord": {"application_id": None}}):
            with self.subTest(overrides=overrides):
                h = trp.Harness(self, tokens=trp.tokens(), overrides=overrides)
                with mock.patch.object(rp.TokenStore, "load", side_effect=AssertionError("tokens read")), \
                        mock.patch.object(rp.TokenStore, "signature", side_effect=AssertionError("tokens read")):
                    h.tick(n=5)
                self.assertEqual(h.world.bridges, [])

    def test_link_requires_an_application_id(self):
        import rich_presence as rp
        cfg = rp._merge(rp.DEFAULTS, {"discord": {"application_id": None}})
        factory = mock.Mock(side_effect=AssertionError("no bridge"))
        self.assertEqual(rp.link(cfg, bridge_factory=factory, environ={"DISPLAY": ":0"}),
                         "no discord.application_id configured")


if __name__ == "__main__":
    unittest.main()
