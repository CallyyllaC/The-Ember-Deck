"""Tests for Foundry's externally visible restart policy."""

import sys
import unittest
from pathlib import Path


ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
sys.path.insert(0, str(ENGINE))

from FoundryCore import evaluate_worker_health, next_restart_backoff  # noqa: E402


class SupervisionTests(unittest.TestCase):
    def test_runtime_stays_available_when_plexamp_is_down(self):
        unit = (ENGINE / "emberdeck.service").read_text(encoding="utf-8")
        self.assertIn("Wants=plexamp.service", unit)
        self.assertNotIn("Requires=plexamp.service", unit)
        self.assertNotIn("wait_for_plexamp", unit)

    def test_systemd_execstart_targets_exist_in_deployment_tree(self):
        for service_path in ENGINE.glob("*.service"):
            with self.subTest(service=service_path.name):
                for line in service_path.read_text(encoding="utf-8").splitlines():
                    if not line.startswith("ExecStart=%h/EmberDeck/"):
                        continue
                    relative = line.removeprefix("ExecStart=%h/EmberDeck/").split()[0]
                    self.assertTrue((ENGINE / relative).is_file(), relative)

    def test_exited_worker_restarts_immediately(self):
        restart, failures, reason = evaluate_worker_health(
            alive=False,
            in_warmup=False,
            heartbeat_fresh=False,
            heartbeat_progressed=False,
            failure_count=0,
            failure_limit=30,
        )
        self.assertTrue(restart)
        self.assertEqual(failures, 0)
        self.assertEqual(reason, "process exited")

    def test_warmup_does_not_accumulate_failures(self):
        self.assertEqual(evaluate_worker_health(
            alive=True,
            in_warmup=True,
            heartbeat_fresh=False,
            heartbeat_progressed=False,
            failure_count=12,
            failure_limit=30,
        ), (False, 0, "warming up"))

    def test_stale_heartbeat_requires_consecutive_failures(self):
        restart, failures, _ = evaluate_worker_health(
            alive=True,
            in_warmup=False,
            heartbeat_fresh=False,
            heartbeat_progressed=False,
            failure_count=28,
            failure_limit=30,
        )
        self.assertFalse(restart)
        self.assertEqual(failures, 29)
        restart, failures, reason = evaluate_worker_health(
            alive=True,
            in_warmup=False,
            heartbeat_fresh=False,
            heartbeat_progressed=False,
            failure_count=failures,
            failure_limit=30,
        )
        self.assertTrue(restart)
        self.assertEqual(reason, "heartbeat stale x30")

    def test_heartbeat_recovery_clears_failure_streak(self):
        self.assertEqual(evaluate_worker_health(
            alive=True,
            in_warmup=False,
            heartbeat_fresh=True,
            heartbeat_progressed=False,
            failure_count=29,
            failure_limit=30,
        ), (False, 0, "healthy"))

    def test_restart_backoff_is_capped(self):
        self.assertEqual(next_restart_backoff(5.0, 15.0), 10.0)
        self.assertEqual(next_restart_backoff(10.0, 15.0), 15.0)


if __name__ == "__main__":
    unittest.main()
