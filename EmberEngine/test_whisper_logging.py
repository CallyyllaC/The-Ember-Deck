"""Runtime support for the Ember Deck system."""

import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile

from spirit_ink import INFO_POOLS, render_message
from spirit_messages import SpiritMessage, render_plain, render_rich
from whisper_daemon import DEFAULTS, EventFileSink, WhisperScheduler


def message(priority, process, event, *, kind="lifecycle", **kwargs):
    """Handle the message lifecycle step."""
    return SpiritMessage(priority, kind, process, event, **kwargs)


class SpiritInkTests(unittest.TestCase):
    """Validate SpiritInk behaviour."""
    def test_existing_template_wording_is_preserved(self):
        """Verify that existing template wording is preserved."""
        fact = message(
            1,
            "aurora",
            "process_starting",
            metadata={"template_event": "starting"},
        )
        with patch("spirit_ink.random.choice", side_effect=lambda pool: pool[0]):
            rendered = render_message(fact)
        self.assertEqual(
            render_plain(rendered),
            INFO_POOLS["starting"][0].format(process="Aurora"),
        )
        rich_output = render_rich(rendered)
        self.assertEqual(getattr(rich_output, "plain", rich_output), render_plain(rendered))
        self.assertEqual(rendered.fragments[0].role, "process:aurora")
        self.assertTrue(all(fragment.role != "lifecycle" for fragment in rendered.fragments))

    def test_only_service_and_metadata_fragments_receive_semantic_roles(self):
        """Verify that only service and metadata fragments receive semantic roles."""
        fact = message(
            2,
            "minstrel",
            "track_changed",
            kind="media",
            metadata={
                "template_event": "track_change",
                "track": "Ember Song",
                "artist": "The Foundry",
            },
        )
        with patch("spirit_ink.random.choice", side_effect=lambda pool: pool[0]):
            rendered = render_message(fact)
        roles = {fragment.text: fragment.role for fragment in rendered.fragments}
        self.assertEqual(roles["Minstrel"], "process:minstrel")
        self.assertEqual(roles["“Ember Song”"], "title")
        self.assertEqual(roles["The Foundry"], "artist")
        self.assertIn("body", rendered.fragments[1].role)


class SchedulerTests(unittest.TestCase):
    """Validate Scheduler behaviour."""
    def setUp(self):
        """Create isolated state for the test case."""
        self.cfg = dict(DEFAULTS)
        self.cfg.update({
            "ambient_min_delay": 60.0,
            "ambient_max_delay": 60.0,
            "ambient_after_startup_delay": 45.0,
            "error_dedupe_cooldown_s": 10.0,
        })

    def test_critical_preempts_lower_priorities(self):
        """Verify that critical preempts lower priorities."""
        scheduler = WhisperScheduler(self.cfg, now=0.0)
        scheduler.enqueue(message(3, "aurora", "reaction", kind="reaction"), now=1.0)
        scheduler.enqueue(message(0, "pawprint", "i2c_connection_failed", kind="error"), now=1.0)
        self.assertEqual(scheduler.pop_next(now=1.0).priority, 0)

    def test_ambient_uses_one_real_deadline(self):
        """Verify that ambient uses one real deadline."""
        scheduler = WhisperScheduler(self.cfg, now=0.0)
        scheduler.enqueue(message(4, "echo", "heartbeat", kind="ambient"), now=1.0)
        self.assertIsNone(scheduler.pop_next(now=44.9))
        self.assertEqual(scheduler.pop_next(now=45.0).event, "heartbeat")
        self.assertEqual(scheduler.next_ambient_at, 105.0)

    def test_duplicate_errors_are_suppressed_and_summarised(self):
        """Verify that duplicate errors are suppressed and summarised."""
        scheduler = WhisperScheduler(self.cfg, now=0.0)
        first = message(0, "pawprint", "i2c_connection_failed", kind="error", dedupe_key="pawprint:i2c:ads")
        repeat = message(0, "pawprint", "i2c_connection_failed", kind="error", dedupe_key="pawprint:i2c:ads")
        self.assertTrue(scheduler.enqueue(first, now=1.0))
        self.assertFalse(scheduler.enqueue(repeat, now=2.0))
        self.assertEqual(scheduler.pop_next(now=2.0).event, "i2c_connection_failed")
        summary = scheduler.pop_next(now=12.0)
        self.assertEqual(summary.event, "duplicate_summary")
        self.assertEqual(summary.metadata["count"], 1)

    def test_recovery_receives_fault_count_and_duration(self):
        """Verify that recovery receives fault count and duration."""
        scheduler = WhisperScheduler(self.cfg, now=0.0)
        key = "pawprint:i2c:ads"
        scheduler.enqueue(message(0, "pawprint", "i2c_connection_failed", kind="error", dedupe_key=key), now=1.0)
        scheduler.enqueue(message(0, "pawprint", "i2c_connection_failed", kind="error", dedupe_key=key), now=2.0)
        recovery = message(
            1,
            "pawprint",
            "i2c_connection_recovered",
            kind="recovery",
            metadata={"recovers_key": key},
        )
        scheduler.enqueue(recovery, now=6.0)
        self.assertEqual(recovery.metadata["repeat_count"], 1)
        self.assertEqual(recovery.metadata["fault_duration_s"], 5.0)

    def test_track_change_cancels_old_track_reaction(self):
        """Verify that track change cancels old track reaction."""
        scheduler = WhisperScheduler(self.cfg, now=0.0)
        old = message(
            3,
            "aurora",
            "reaction",
            kind="reaction",
            metadata={"template_event": "track_change", "reaction_scope": "track", "delay_s": 8.0},
        )
        scheduler.enqueue(old, now=1.0)
        current = message(
            2,
            "minstrel",
            "track_changed",
            kind="media",
            metadata={"template_event": "track_change", "track": "Current"},
        )
        with patch("whisper_daemon.random.randint", return_value=0):
            scheduler.enqueue(current, now=2.0)
        self.assertFalse(any(entry[2].metadata.get("track") != "Current" for entry in scheduler.delayed))

    def test_equal_priority_fairness_avoids_three_in_a_row(self):
        """Verify that equal priority fairness avoids three in a row."""
        scheduler = WhisperScheduler(self.cfg, now=0.0)
        scheduler.enqueue(message(2, "minstrel", "one", kind="interaction"), now=1.0)
        scheduler.enqueue(message(2, "minstrel", "two", kind="interaction"), now=1.0)
        scheduler.enqueue(message(2, "pawprint", "three", kind="interaction"), now=1.0)
        self.assertEqual(scheduler.pop_next(now=1.0).process, "minstrel")
        self.assertEqual(scheduler.pop_next(now=1.0).process, "pawprint")


class EventFileSinkTests(unittest.TestCase):
    """Validate EventFileSink behaviour."""
    def test_ui_snapshot_is_bounded_and_durable_log_is_error_only(self):
        """Verify that ui snapshot is bounded and durable log is error only."""
        with tempfile.TemporaryDirectory() as directory:
            cfg = dict(DEFAULTS)
            cfg.update({
                "structured_log_path": "ui.jsonl",
                "important_log_path": "errors.jsonl",
                "ui_event_limit": 2,
                "log_cleanup_interval_s": 86400,
            })
            sink = EventFileSink(cfg, Path(directory))
            ambient = message(4, "echo", "heartbeat", kind="ambient")
            lifecycle = message(1, "aurora", "startup_complete")
            critical = message(0, "pawprint", "i2c_connection_failed", kind="error")
            sink.write(render_message(ambient), ambient.priority, monotonic_now=1.0)
            sink.write(render_message(lifecycle), lifecycle.priority, monotonic_now=2.0)
            sink.write(render_message(critical), critical.priority, monotonic_now=3.0)

            ui_lines = (Path(directory) / "ui.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(ui_lines), 2)
            error_files = list(Path(directory).glob("errors-*.jsonl"))
            self.assertEqual(len(error_files), 1)
            self.assertEqual(len(error_files[0].read_text(encoding="utf-8").splitlines()), 1)

    def test_cleanup_removes_error_files_older_than_retention(self):
        """Verify that cleanup removes error files older than retention."""
        with tempfile.TemporaryDirectory() as directory:
            cfg = dict(DEFAULTS)
            cfg.update({
                "structured_log_path": "ui.jsonl",
                "important_log_path": "errors.jsonl",
                "log_retention_days": 183,
            })
            sink = EventFileSink(cfg, Path(directory))
            old = Path(directory) / "errors-2020-01.jsonl"
            old.write_text("{}\n", encoding="utf-8")
            old_time = datetime.now(timezone.utc) - timedelta(days=200)
            os.utime(old, (old_time.timestamp(), old_time.timestamp()))
            sink.cleanup(now=datetime.now(timezone.utc))
            self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
