"""Shared-state contract tests which do not create OS shared memory."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
sys.path.insert(0, str(ENGINE))

import synapse  # noqa: E402


class SynapseTests(unittest.TestCase):
    def tearDown(self):
        synapse._HANDLE_CACHE.clear()

    def test_stale_small_segment_has_actionable_error(self):
        key = "/test/configurable_array"
        synapse._HANDLE_CACHE[synapse._norm(key)] = SimpleNamespace(size=8)
        with self.assertRaisesRegex(synapse.SharedMemoryLayoutError, "requires 16.*reboot"):
            synapse._get_handle(key, 16, create=False)


if __name__ == "__main__":
    unittest.main()
