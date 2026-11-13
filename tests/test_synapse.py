"""Shared-state contract tests.

Most tests use the handle cache only. ``RealSegmentTests`` creates a few
uniquely named OS segments and unlinks them again, so the contract is checked
against the real ``multiprocessing.shared_memory`` implementation.
"""

import os
import struct
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
sys.path.insert(0, str(ENGINE))

import synapse  # noqa: E402

try:
    import _posixshmem  # POSIX only
except ImportError:
    _posixshmem = None


class SynapseTests(unittest.TestCase):
    def tearDown(self):
        synapse._HANDLE_CACHE.clear()

    def test_stale_small_segment_has_actionable_error(self):
        key = "/test/configurable_array"
        synapse._HANDLE_CACHE[synapse._norm(key)] = SimpleNamespace(size=8)
        with self.assertRaisesRegex(synapse.SharedMemoryLayoutError, "requires 16.*reboot"):
            synapse._get_handle(key, 16, create=False)

    def test_key_normalisation_is_stable_and_bounded(self):
        self.assertEqual(synapse._norm("/proc/echo/heartbeat_ms"), "smem____proc__echo__heartbeat_ms")
        self.assertLessEqual(len(synapse._norm("/x" * 400)), 254)


EMPTY_MMAP = ValueError("cannot mmap an empty file")  # what mmap raises for a 0-byte object


def fake_segment(value: int):
    buf = bytearray(4)
    struct.pack_into("<i", buf, 0, value)
    return SimpleNamespace(size=4, buf=buf, close=lambda: None)


class CreationRaceTests(unittest.TestCase):
    """A reader that opens a segment between its creation and its sizing (E-12)."""

    def tearDown(self):
        synapse._HANDLE_CACHE.clear()

    def test_unsized_segment_reads_as_missing_and_is_retried_next_time(self):
        opens = [EMPTY_MMAP, EMPTY_MMAP, EMPTY_MMAP, fake_segment(42)]

        def open_untracked(norm, *, create, size):
            self.assertFalse(create, "readers never create")
            result = opens.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(synapse, "_open_untracked", open_untracked), \
                mock.patch.object(synapse, "_posix_segment_size", return_value=0, create=True):
            self.assertEqual(synapse.get_int("/race/int", 7), 7)
            self.assertEqual(synapse.get_float("/race/float", 1.5), 1.5)
            self.assertIsNone(synapse.try_get_array("/race/array", 1, "i32"))
            self.assertEqual(synapse._HANDLE_CACHE, {}, "an unsized segment is never cached")
            self.assertEqual(synapse.get_int("/race/int", 7), 42, "the next read sees the sized segment")

    def test_not_ready_is_a_kind_of_missing_segment(self):
        self.assertTrue(issubclass(synapse.SegmentNotReady, FileNotFoundError))

    def test_segment_sized_between_the_failure_and_the_check_is_opened(self):
        opens = [EMPTY_MMAP, fake_segment(5)]

        def open_untracked(norm, *, create, size):
            result = opens.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(synapse, "_open_untracked", open_untracked), \
                mock.patch.object(synapse, "_posix_segment_size", return_value=4, create=True):
            self.assertEqual(synapse.get_int("/race/late", 7), 5)

    def test_unrelated_value_errors_are_not_suppressed(self):
        for size in (None, 4):  # cannot attribute it, or the object is not empty
            with self.subTest(size=size), \
                    mock.patch.object(synapse, "_open_untracked", side_effect=ValueError("bad name")), \
                    mock.patch.object(synapse, "_posix_segment_size", return_value=size, create=True):
                with self.assertRaisesRegex(ValueError, "bad name"):
                    synapse.get_int("/race/broken", 7)
                with self.assertRaisesRegex(ValueError, "bad name"):
                    synapse.try_get_array("/race/broken", 1)

    def test_errors_raised_by_read_callers_are_untouched(self):
        with self.assertRaisesRegex(ValueError, "dtype must be"):
            synapse.try_get_array("/race/dtype", 1, "f64")

    def test_writer_that_loses_the_creation_race_reports_not_ready(self):
        # A second writer finds the segment already created (FileExistsError)
        # and unsized. It gets SegmentNotReady rather than a bare ValueError.
        def open_untracked(norm, *, create, size):
            if create:
                raise FileExistsError(norm)
            raise EMPTY_MMAP

        with mock.patch.object(synapse, "_open_untracked", open_untracked), \
                mock.patch.object(synapse, "_posix_segment_size", return_value=0, create=True):
            with self.assertRaises(synapse.SegmentNotReady):
                synapse.set_int("/race/writer", 1)


class MonotonicStampTests(unittest.TestCase):
    """Heartbeat stamps must always fit the signed 32-bit scalar segments."""

    def test_stamp_wraps_instead_of_overflowing_after_24_days(self):
        uptime_s = 30 * 86400.0  # beyond 2**31 ms (~24.8 days)
        with mock.patch.object(synapse.time, "monotonic", return_value=uptime_s):
            stamp = synapse.monotonic_ms()
        self.assertEqual(stamp, int(uptime_s * 1000) & 0x7FFFFFFF)
        struct.pack("<i", stamp)  # would raise struct.error if it overflowed

    def test_raw_monotonic_ms_would_have_overflowed(self):
        with self.assertRaises(struct.error):
            struct.pack("<i", int(30 * 86400.0 * 1000))

    def test_stamp_is_never_the_missing_value(self):
        with mock.patch.object(synapse.time, "monotonic", return_value=(2 ** 31) / 1000.0):
            self.assertEqual(synapse.monotonic_ms(), 1)

    def test_elapsed_is_correct_across_the_wrap(self):
        before = 0x7FFFFFFF - 499
        after = 500
        self.assertEqual(synapse.elapsed_ms(after, before), 1000)
        self.assertEqual(synapse.elapsed_ms(1500, 500), 1000)


class RealSegmentTests(unittest.TestCase):
    def setUp(self):
        self.prefix = f"/test/{os.getpid()}/{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        for handle in list(synapse._HANDLE_CACHE.values()):
            try:
                handle.unlink()
            except Exception:
                pass
        synapse.close_all()

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}"

    def test_missing_keys_return_defaults_without_creating_segments(self):
        self.assertEqual(synapse.get_int(self.key("absent"), 7), 7)
        self.assertEqual(synapse.get_float(self.key("absent_f"), 1.5), 1.5)
        self.assertIsNone(synapse.try_get_array(self.key("absent_a"), 4))
        self.assertEqual(synapse._HANDLE_CACHE, {})

    def test_scalar_and_array_round_trip(self):
        synapse.set_int(self.key("i"), -42)
        synapse.set_float(self.key("f"), 0.25)
        synapse.set_array(self.key("rgba"), [0.1, 0.2, 0.3, 1.0], "f32")
        synapse.set_array(self.key("text"), list(b"hi") + [0] * 6, "u8")
        synapse.set_array(self.key("ring"), [1, -2, 3], "i32")
        self.assertEqual(synapse.get_int(self.key("i")), -42)
        self.assertAlmostEqual(synapse.get_float(self.key("f")), 0.25)
        self.assertEqual([round(v, 3) for v in synapse.try_get_array(self.key("rgba"), 4)], [0.1, 0.2, 0.3, 1.0])
        self.assertEqual(synapse.try_get_array(self.key("text"), 8, "u8")[:2], [104, 105])
        self.assertEqual(synapse.try_get_array(self.key("ring"), 3, "i32"), [1, -2, 3])

    def test_first_writer_fixes_array_size(self):
        key = self.key("frame")
        synapse.set_array(key, [0.0] * 4, "f32")
        with self.assertRaises(synapse.SharedMemoryLayoutError):
            synapse.set_array(key, [0.0] * 8, "f32")
        with self.assertRaises(synapse.SharedMemoryLayoutError):
            synapse.try_get_array(key, 8, "f32")

    def test_overflowing_int_is_rejected(self):
        with self.assertRaises(struct.error):
            synapse.set_int(self.key("big"), 2 ** 31)

    def test_unknown_dtype_is_rejected(self):
        with self.assertRaises(ValueError):
            synapse.set_array(self.key("bad"), [1], "f64")

    @unittest.skipUnless(_posixshmem is not None, "POSIX shared memory required")
    def test_real_unsized_segment_reads_as_missing(self):
        # Recreate the state SharedMemory(create=True) passes through: the
        # object exists but has not been ftruncate'd yet.
        key = self.key("unsized")
        path = "/" + synapse._norm(key)
        fd = _posixshmem.shm_open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o600)
        self.addCleanup(_posixshmem.shm_unlink, path)
        try:
            self.assertEqual(synapse.get_int(key, 7), 7)
            self.assertIsNone(synapse.try_get_array(key, 1, "i32"))
            self.assertEqual(synapse._HANDLE_CACHE, {})
            os.ftruncate(fd, 4)  # the creator finishes
        finally:
            os.close(fd)
        synapse.set_int(key, 9)
        self.assertEqual(synapse.get_int(key, 7), 9)


if __name__ == "__main__":
    unittest.main()
