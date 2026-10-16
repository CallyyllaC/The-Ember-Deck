"""Whisper transport contracts: how a worker's log call reaches the scheduler.

The scheduler/renderer itself is covered by EmberEngine/test_whisper_logging.py.
These tests cover the connection between workers and Foundry's shared queue,
and what happens when that queue is unavailable.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
sys.path.insert(0, str(ENGINE))

import whisper_daemon  # noqa: E402
from spirit_messages import PRIORITY_AMBIENT, PRIORITY_CRITICAL, SpiritMessage  # noqa: E402


class BrokenQueue:
    def put(self, item):
        raise BrokenPipeError("manager went away")


class RecordingQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class QueueTransportTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(whisper_daemon, "QUEUE", None),
            mock.patch.object(whisper_daemon, "QUEUE_CONNECT_ATTEMPTED", False),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("WHISPER_MANAGER_HOST", "WHISPER_MANAGER_PORT", "WHISPER_MANAGER_AUTH"):
            os.environ.pop(name, None)

    def test_standalone_worker_logs_fall_back_to_stdout(self):
        output = io.StringIO()
        with redirect_stdout(output):
            whisper_daemon.log_error("echo", "audio capture unavailable", "no device")
        self.assertTrue(output.getvalue().strip())

    def test_queue_failure_falls_back_and_allows_reconnect(self):
        whisper_daemon.QUEUE = BrokenQueue()
        whisper_daemon.QUEUE_CONNECT_ATTEMPTED = True
        output = io.StringIO()
        with redirect_stdout(output):
            whisper_daemon.log_info("echo", "started")
        self.assertTrue(output.getvalue().strip())
        self.assertIsNone(whisper_daemon.QUEUE)

    def test_failed_put_reconnects_immediately_to_a_restored_queue(self):
        restored = RecordingQueue()
        whisper_daemon.QUEUE = BrokenQueue()

        def reconnect(*, retry=False):
            whisper_daemon.QUEUE = restored

        with mock.patch.object(whisper_daemon, "_connect_queue_from_env", side_effect=reconnect):
            whisper_daemon.log_info("echo", "started")
        self.assertEqual(len(restored.items), 1)

    def test_messages_cross_the_queue_as_plain_wire_dicts(self):
        queue = RecordingQueue()
        whisper_daemon.QUEUE = queue
        whisper_daemon.log_error("minstrel", "Failed to connect to Plex", "timeout")
        wire = queue.items[0]
        self.assertIsInstance(wire, dict)
        message = SpiritMessage.from_wire(wire)
        self.assertEqual((message.priority, message.kind, message.event),
                         (PRIORITY_CRITICAL, "error", "plex_connection_failed"))
        self.assertEqual(message.dedupe_key, "minstrel:plex_connection_failed:")
        self.assertTrue(message.persistent)

    def test_heartbeat_logs_are_rate_limited_in_the_worker_before_any_ipc(self):
        queue = RecordingQueue()
        whisper_daemon.QUEUE = queue
        with mock.patch.dict(whisper_daemon._last_ambient_heartbeat, {}, clear=True):
            for second in range(120):  # two minutes of 1 Hz heartbeats
                whisper_daemon.log_heartbeat("echo", now=float(second))
                whisper_daemon.log_heartbeat("aurora", now=float(second))
        processes = [SpiritMessage.from_wire(item).process for item in queue.items]
        self.assertEqual(processes, ["echo", "aurora", "echo", "aurora"])
        self.assertEqual(SpiritMessage.from_wire(queue.items[0]).priority, PRIORITY_AMBIENT)

    def test_reconnect_attempts_are_rate_limited_unless_retry_is_requested(self):
        os.environ.update(WHISPER_MANAGER_HOST="127.0.0.1", WHISPER_MANAGER_PORT="1", WHISPER_MANAGER_AUTH="00")
        attempts = []

        class Client:
            def __init__(self, *args, **kwargs):
                attempts.append(kwargs)

            @classmethod
            def register(cls, *args, **kwargs):
                pass

            def connect(self):
                raise ConnectionRefusedError("not yet")

        clock = [100.0]
        with mock.patch.object(whisper_daemon, "BaseManager", Client),                 mock.patch.object(whisper_daemon.time, "monotonic", lambda: clock[0]),                 mock.patch.object(whisper_daemon, "_next_connect_at", 0.0):
            whisper_daemon._connect_queue_from_env()
            whisper_daemon._connect_queue_from_env()
            self.assertEqual(len(attempts), 1)
            whisper_daemon._connect_queue_from_env(retry=True)
            self.assertEqual(len(attempts), 2)
            clock[0] += whisper_daemon.QUEUE_RETRY_INTERVAL_S
            whisper_daemon._connect_queue_from_env()
            self.assertEqual(len(attempts), 3)
        self.assertIsNone(whisper_daemon.QUEUE)

    def test_unknown_queue_items_are_ignored_and_legacy_text_is_wrapped(self):
        self.assertIsNone(whisper_daemon._coerce_message(42))
        legacy = whisper_daemon._coerce_message("plain words")
        self.assertEqual((legacy.kind, legacy.metadata["text"]), ("legacy", "plain words"))


class ManagerRoundTripTests(unittest.TestCase):
    """Foundry hosts one queue; a worker reaches it purely through its environment."""

    def test_worker_publishes_through_foundry_manager_into_whisper_files(self):
        import FoundryCore

        manager = FoundryCore.WhisperQueueManager(address=("127.0.0.1", 0), authkey=os.urandom(16))
        manager.start()
        self.addCleanup(manager.shutdown)
        host, port = manager.address
        env = {
            "WHISPER_MANAGER_HOST": str(host),
            "WHISPER_MANAGER_PORT": str(port),
            "WHISPER_MANAGER_AUTH": manager._authkey.hex(),
        }
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(whisper_daemon, "QUEUE", None), \
                mock.patch.object(whisper_daemon, "QUEUE_CONNECT_ATTEMPTED", False):
            whisper_daemon.log_error("pawprint", "I2C connection failed", "OSError(121)")
            delivered = whisper_daemon.QUEUE is not None

        self.assertTrue(delivered, "worker should have connected using the environment only")
        item = manager.get_queue().get(timeout=5)
        message = whisper_daemon._coerce_message(item)
        self.assertEqual(message.event, "i2c_connection_failed")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(whisper_daemon.DEFAULTS)
            sink = whisper_daemon.EventFileSink(cfg, Path(tmp))
            scheduler = whisper_daemon.WhisperScheduler(cfg, now=0.0)
            scheduler.enqueue(message, now=1.0)
            popped = scheduler.pop_next(now=1.0)
            rendered = whisper_daemon.render_message(popped)
            sink.write(rendered, popped.priority, monotonic_now=1.0)
            ui_snapshot = Path(tmp, cfg["structured_log_path"])
            error_files = list(Path(tmp, "logs").glob("foundry-errors-*.jsonl"))
            self.assertTrue(ui_snapshot.is_file())
            self.assertEqual(len(error_files), 1)


if __name__ == "__main__":
    unittest.main()
