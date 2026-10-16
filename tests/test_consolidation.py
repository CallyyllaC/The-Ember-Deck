"""Shared contracts, the heartbeat helper and smaller consolidation fixes.

Covers: contracts.py values and their consumers, synapse.Heartbeat and
publish_heartbeat, Whisper's throttled UI snapshot, atomic Labwc rule writes,
the MPRIS text width used by the UI, Foxfire's default names, and the UI's
installation hint.
"""

import ast
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, FakeBus, capture_whisper, import_engine_module  # noqa: E402

import contracts  # noqa: E402
import synapse  # noqa: E402
import whisper_daemon  # noqa: E402
from spirit_messages import SpiritMessage  # noqa: E402

ROOT = ENGINE.parent


def engine_sources():
    """Tracked-style engine modules (skips stray local copies such as 'x (2).py')."""
    return [path for path in sorted(ENGINE.glob("*.py"))
            if " " not in path.name and not path.name.startswith("test_")]


class ContractTests(unittest.TestCase):
    def test_wire_values_are_stable(self):
        self.assertEqual(
            (contracts.MEDIA_SOURCE_NONE, contracts.MEDIA_SOURCE_PLEX,
             contracts.MEDIA_SOURCE_BLUETOOTH, contracts.MEDIA_SOURCE_MPRIS),
            (0, 1, 2, 3))
        self.assertEqual((contracts.HDMI2_SOURCE_PLEX, contracts.HDMI2_SOURCE_RADIO, contracts.HDMI2_SOURCE_YOUTUBE),
                         (1, 2, 3))
        self.assertEqual((contracts.PLAY_STOPPED, contracts.PLAY_PLAYING, contracts.PLAY_PAUSED), (0, 1, 2))
        self.assertEqual(
            (contracts.PLEX_UNCONFIGURED, contracts.PLEX_CONNECTING, contracts.PLEX_CONNECTED,
             contracts.PLEX_UNAVAILABLE, contracts.PLEX_AUTH_FAILED),
            (0, 1, 2, 3, 4))

    def test_no_consumer_redefines_a_shared_value_as_a_literal(self):
        watched = re.compile(
            r"^(MEDIA_SOURCE_\w+|MEDIA_TARGET_(NONE|PLEX|BLUETOOTH|MPRIS|LOCAL)|SOURCE_(NONE|PLEX|BLUETOOTH|LOCAL)"
            r"|HDMI2_SOURCE_(PLEX|RADIO|YOUTUBE)|PLAY_(STOPPED|PLAYING|PAUSED)|BT_PLAY_\w+|PLEX_(PLAYING|PAUSED))$")
        offenders = []
        for path in engine_sources():
            if path.name == "contracts.py":
                continue
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, int):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and watched.match(target.id):
                            offenders.append(f"{path.name}:{node.lineno} {target.id}")
        self.assertEqual(offenders, [])

    def test_consumer_aliases_resolve_to_the_contract(self):
        pawprint_logic = import_engine_module("pawprint_logic")
        aurora = import_engine_module("aurora")
        mpris_bridge = import_engine_module("mpris_bridge")
        hdmi2 = import_engine_module("hdmi2_source_controller")
        minstrel = import_engine_module("minstrel")
        ember_ui = import_engine_module("ember_ui")
        self.assertEqual(pawprint_logic.MEDIA_TARGET_MPRIS, contracts.MEDIA_SOURCE_MPRIS)
        self.assertEqual(aurora.MEDIA_SOURCE_LOCAL, contracts.MEDIA_SOURCE_MPRIS)
        self.assertEqual(ember_ui.SOURCE_LOCAL, contracts.MEDIA_SOURCE_MPRIS)
        self.assertEqual(ember_ui.PLAY_PAUSED, contracts.PLAY_PAUSED)
        self.assertEqual(hdmi2.SOURCE_ORDER, (1, 2, 3))
        self.assertEqual(mpris_bridge.HDMI2_SOURCE_YOUTUBE, contracts.HDMI2_SOURCE_YOUTUBE)
        self.assertEqual(minstrel.PLAY_PLAYING, contracts.PLAY_PLAYING)


class HeartbeatHelperTests(unittest.TestCase):
    def test_tick_publishes_at_most_once_per_interval(self):
        published, beats = [], []
        heartbeat = synapse.Heartbeat("echo", 1.0, on_beat=beats.append, publish=published.append)
        self.assertEqual(heartbeat.next_due, float("-inf"))
        self.assertTrue(heartbeat.tick(10.0))
        self.assertFalse(heartbeat.tick(10.5))
        self.assertEqual(heartbeat.next_due, 11.0)
        self.assertTrue(heartbeat.tick(11.0))
        self.assertEqual(published, ["echo", "echo"])
        self.assertEqual(beats, ["echo", "echo"])

    def test_publish_writes_wrap_safe_stamp_and_progress_counter(self):
        name = f"test_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        try:
            with mock.patch.object(synapse.time, "monotonic", return_value=30 * 86400.0):
                stamp = synapse.publish_heartbeat(name)
            self.assertEqual(stamp, int(30 * 86400.0 * 1000) & synapse.MS_CLOCK_MASK)
            self.assertEqual(synapse.get_int(f"/proc/{name}/heartbeat_ms"), stamp)
            synapse.publish_heartbeat(name)
            self.assertEqual(synapse.get_int(f"/proc/{name}/hb_seq"), 2)
            synapse.set_int(f"/proc/{name}/hb_seq", synapse.MS_CLOCK_MASK)
            synapse.publish_heartbeat(name)
            self.assertEqual(synapse.get_int(f"/proc/{name}/hb_seq"), 0, "counter wraps instead of overflowing")
        finally:
            for handle in list(synapse._HANDLE_CACHE.values()):
                try:
                    handle.unlink()
                except Exception:
                    pass
            synapse.close_all()

    def test_only_synapse_writes_heartbeat_keys(self):
        writers = [path.name for path in engine_sources()
                   if re.search(r"set_int\(\s*f?[\"']/proc/", path.read_text(encoding="utf-8"))]
        self.assertEqual(writers, ["synapse.py"])

    def test_every_supervised_worker_uses_the_shared_helper(self):
        foundry = import_engine_module("FoundryCore")
        for entry in foundry.WORKERS:
            with self.subTest(worker=entry["name"]):
                text = (ENGINE / entry["script"]).read_text(encoding="utf-8")
                self.assertRegex(text, r"bus\.Heartbeat\(|bus\.publish_heartbeat\(")
                self.assertNotIn("monotonic() * 1000", text)


class SnapshotThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = dict(whisper_daemon.DEFAULTS, structured_log_path="ui.jsonl", important_log_path="errors.jsonl",
                   ui_snapshot_interval_s=1.0)
        self.sink = whisper_daemon.EventFileSink(cfg, self.tmp)

    def rendered(self, priority, event="startup_complete"):
        message = SpiritMessage(priority=priority, kind="error" if priority == 0 else "lifecycle",
                                process="echo", event=event)
        return whisper_daemon.render_message(message), priority

    def lines(self):
        path = self.tmp / "ui.jsonl"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_bursts_are_coalesced_to_one_write_per_interval(self):
        for step in range(10):  # ten messages inside 0.9 s
            self.sink.write(*self.rendered(1), monotonic_now=10.0 + step * 0.1)
        self.assertEqual(self.sink.snapshot_writes, 1)
        self.assertEqual(len(self.lines()), 1)
        self.sink.flush(monotonic_now=10.95)
        self.assertEqual(self.sink.snapshot_writes, 1)
        self.sink.flush(monotonic_now=11.0)
        self.assertEqual((self.sink.snapshot_writes, len(self.lines())), (2, 10))
        self.sink.flush(monotonic_now=20.0)
        self.assertEqual(self.sink.snapshot_writes, 2, "a clean snapshot is never rewritten")

    def test_critical_events_reach_the_ui_and_durable_history_at_once(self):
        self.sink.write(*self.rendered(1), monotonic_now=10.0)
        self.sink.write(*self.rendered(0, "i2c_connection_failed"), monotonic_now=10.1)
        self.assertEqual(self.sink.snapshot_writes, 2)
        self.assertEqual(len(self.lines()), 2)
        errors = list(self.tmp.glob("errors-*.jsonl"))
        self.assertEqual(len(errors), 1)
        self.assertEqual(json.loads(errors[0].read_text(encoding="utf-8").splitlines()[0])["event"],
                         "i2c_connection_failed")

    def test_final_forced_flush_keeps_the_pending_snapshot(self):
        self.sink.write(*self.rendered(1), monotonic_now=10.0)
        self.sink.write(*self.rendered(2), monotonic_now=10.2)
        self.assertEqual(len(self.lines()), 1)
        self.sink.flush(monotonic_now=10.3, force=True)
        self.assertEqual(len(self.lines()), 2)

    def test_snapshot_stays_bounded(self):
        cfg = dict(whisper_daemon.DEFAULTS, structured_log_path="ui.jsonl", important_log_path="",
                   ui_event_limit=3)
        sink = whisper_daemon.EventFileSink(cfg, self.tmp)
        for step in range(10):
            sink.write(*self.rendered(1), monotonic_now=float(step * 2))
        self.assertEqual(len(self.lines()), 3)


class LabwcRuleTests(unittest.TestCase):
    hdmi2 = import_engine_module("hdmi2_source_controller")

    RC = """<?xml version="1.0"?>
<labwc_config>
  <!-- keep this comment -->
  <keyboard><keybind key="W-Return"><action name="Execute" command="foot"/></keybind></keyboard>
  <windowRules>
    <windowRule identifier="org.other.App"><action name="Maximize"/></windowRule>
  </windowRules>
</labwc_config>
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rc = self.tmp / "rc.xml"
        self.rc.write_text(self.RC, encoding="utf-8")
        if os.name == "posix":
            self.rc.chmod(0o640)
        self.ids = ["de.haeckerfelix.Shortwave", self.hdmi2.PEAR_WINDOW_IDENTIFIER]

    def test_rules_are_added_once_and_unrelated_config_is_kept(self):
        tree = self.hdmi2.parse_labwc_config(self.rc)
        self.assertTrue(self.hdmi2.apply_ember_window_rules(tree.getroot(), self.ids, "HDMI-A-2"))
        self.hdmi2.write_xml_atomically(tree, self.rc)
        text = self.rc.read_text(encoding="utf-8")
        self.assertIn("keep this comment", text)
        self.assertIn('command="foot"', text)
        self.assertIn('identifier="org.other.App"', text)
        again = self.hdmi2.parse_labwc_config(self.rc)
        self.assertFalse(self.hdmi2.apply_ember_window_rules(again.getroot(), self.ids, "HDMI-A-2"))
        self.assertEqual(len(again.getroot().find("windowRules").findall("windowRule")), 3)

    def test_output_change_retargets_only_ember_rules(self):
        tree = self.hdmi2.parse_labwc_config(self.rc)
        self.hdmi2.apply_ember_window_rules(tree.getroot(), self.ids, "HDMI-A-2")
        self.assertTrue(self.hdmi2.apply_ember_window_rules(tree.getroot(), self.ids, "HDMI-A-1"))
        outputs = {action.attrib.get("output") for action in tree.getroot().iter("action")
                   if action.attrib.get("name") == "MoveToOutput"}
        self.assertEqual(outputs, {"HDMI-A-1"})

    def test_write_is_atomic_keeps_mode_and_one_backup(self):
        tree = self.hdmi2.parse_labwc_config(self.rc)
        self.hdmi2.apply_ember_window_rules(tree.getroot(), self.ids, "HDMI-A-2")
        self.hdmi2.write_xml_atomically(tree, self.rc)
        backup = self.rc.with_name("rc.xml" + self.hdmi2.LABWC_BACKUP_SUFFIX)
        self.assertEqual(backup.read_text(encoding="utf-8"), self.RC)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["rc.xml", backup.name])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o640)
        ET.parse(self.rc)  # still well-formed

    def test_failed_replace_leaves_the_original_untouched(self):
        tree = self.hdmi2.parse_labwc_config(self.rc)
        self.hdmi2.apply_ember_window_rules(tree.getroot(), self.ids, "HDMI-A-2")
        with mock.patch.object(self.hdmi2.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.hdmi2.write_xml_atomically(tree, self.rc)
        self.assertEqual(self.rc.read_text(encoding="utf-8"), self.RC)
        self.assertFalse([p for p in self.tmp.iterdir() if p.suffix == ".tmp"])

    def test_missing_rc_file_is_created(self):
        missing = self.tmp / "new" / "rc.xml"
        tree = self.hdmi2.parse_labwc_config(missing)
        self.hdmi2.apply_ember_window_rules(tree.getroot(), self.ids, "HDMI-A-2")
        self.hdmi2.write_xml_atomically(tree, missing)
        self.assertEqual(ET.parse(missing).getroot().tag, "labwc_config")


class MprisTextWidthTests(unittest.TestCase):
    mpris_bridge = import_engine_module("mpris_bridge")
    ember_ui = import_engine_module("ember_ui")

    def setUp(self):
        self.bus = FakeBus()
        for module in (self.mpris_bridge, self.ember_ui):
            patcher = mock.patch.object(module, "bus", self.bus)
            patcher.start()
            self.addCleanup(patcher.stop)
        whisper = capture_whisper()
        whisper.__enter__()
        self.addCleanup(whisper.__exit__, None, None, None)

    def test_ui_reads_the_full_configured_width_without_splitting_utf8(self):
        bridge = self.mpris_bridge.MprisBridge(dict(self.mpris_bridge.DEFAULTS, metadata_buffer_bytes=512))
        self.assertEqual(self.bus.get_int("/mpris/text_bytes"), 512)
        title = "ラジオ " * 60  # ~600 bytes of 3-byte characters
        self.mpris_bridge.publish_text("/mpris/title_utf8", title, bridge.text_size)
        shown = self.ember_ui.read_mpris_text("/mpris/title_utf8")
        self.assertGreater(len(shown.encode("utf-8")), 192)
        self.assertNotIn("�", shown)
        self.assertTrue(title.startswith(shown))

    def test_missing_width_key_keeps_the_historical_192_byte_read(self):
        self.mpris_bridge.publish_text("/mpris/title_utf8", "x" * 400, 512)
        self.assertEqual(len(self.ember_ui.read_mpris_text("/mpris/title_utf8")), 192)

    def test_stale_narrow_segment_falls_back_instead_of_blanking(self):
        self.bus.values["/mpris/text_bytes"] = 512
        self.mpris_bridge.publish_text("/mpris/title_utf8", "Song", 192)
        self.assertEqual(self.ember_ui.read_mpris_text("/mpris/title_utf8"), "Song")


class SmallCleanupTests(unittest.TestCase):
    def test_foxfire_defaults_use_the_names_main_reads(self):
        foxfire = import_engine_module("foxfire")
        runtime_config = import_engine_module("runtime_config")
        self.assertIn("reducer_hi", foxfire.DEFAULTS)
        self.assertIn("tilt_power", foxfire.DEFAULTS)
        self.assertNotIn("reducer", foxfire.DEFAULTS)
        self.assertNotIn("tilt_pow", foxfire.DEFAULTS)
        runtime_config.validate_foxfire(foxfire.DEFAULTS)
        self.assertIn("heartbeat_ms", foxfire.__doc__)
        self.assertNotIn("/proc/foxfire/heartbeat\n", foxfire.__doc__)

    def test_no_reference_to_a_nonexistent_requirements_file(self):
        hits = []
        for path in list(ENGINE.glob("*.py")) + list(ENGINE.glob("*.sh")) + list((ROOT / "docs").glob("*.md")):
            if " " in path.name:
                continue
            for match in re.finditer(r"requirements[-\w]*\.txt", path.read_text(encoding="utf-8")):
                if not (ROOT / "docs" / match.group(0)).exists() and not (ENGINE / match.group(0)).exists():
                    hits.append(f"{path.name}: {match.group(0)}")
        self.assertEqual(hits, [])

    def test_ui_install_hint_points_at_real_documentation(self):
        for name in ("ember_ui.py", "album_art_probe.py"):
            with self.subTest(module=name):
                self.assertIn("docs/INSTALLATION.md", (ENGINE / name).read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "docs" / "INSTALLATION.md").is_file())


if __name__ == "__main__":
    unittest.main()
