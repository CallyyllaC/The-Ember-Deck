"""Tests for Foundry's externally visible restart policy."""

import io
import os
import queue
import re
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import FoundryCore  # noqa: E402
import whisper_daemon  # noqa: E402
from FoundryCore import evaluate_worker_health, next_restart_backoff  # noqa: E402
from _support import FakeBus, capture_whisper  # noqa: E402


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
        self.assertEqual(next_restart_backoff(40.0, FoundryCore.RESTART_BACKOFF_CAP_S), 60.0)


class FakeProcess:
    """Popen-shaped child whose liveness and termination behaviour is scripted."""

    def __init__(self, *, alive=True, ignores_terminate=False):
        self.alive = alive
        self.ignores_terminate = ignores_terminate
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        if not self.ignores_terminate:
            self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False


class WorkerTableTests(unittest.TestCase):
    """Foundry's WORKERS table is the contract between supervisor and workers."""

    def test_every_worker_script_and_config_exists(self):
        for worker in FoundryCore.WORKERS:
            with self.subTest(worker=worker["name"]):
                self.assertTrue((ENGINE / worker["script"]).is_file())
                self.assertTrue((ENGINE / "configs" / worker["config"]).is_file())

    def test_worker_names_are_unique_and_whisper_starts_first(self):
        names = [worker["name"] for worker in FoundryCore.WORKERS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], "whisper_daemon")

    def test_config_proc_name_matches_supervised_heartbeat_name(self):
        # Foundry watches /proc/<WORKERS name>/heartbeat_ms while the worker
        # writes /proc/<proc_name>/... A mismatch is a guaranteed restart loop.
        for worker in FoundryCore.WORKERS:
            with self.subTest(worker=worker["name"]):
                text = (ENGINE / "configs" / worker["config"]).read_text(encoding="utf-8")
                match = re.search(r"^proc_name:\s*([A-Za-z0-9_]+)\s*$", text, flags=re.MULTILINE)
                self.assertIsNotNone(match, "proc_name must be explicit in the worker YAML")
                self.assertEqual(match.group(1), worker["name"])

    def test_runtime_unit_keeps_one_shutdown_boundary(self):
        unit = (ENGINE / "emberdeck.service").read_text(encoding="utf-8")
        self.assertIn("KillMode=control-group", unit)
        stop_timeout = int(re.search(r"^TimeoutStopSec=(\d+)", unit, flags=re.MULTILINE).group(1))
        self.assertGreater(stop_timeout, FoundryCore.CHILD_TERM_GRACE_S + 5)
        for observer in ("ember-ui.service", "ember-browser.service"):
            with self.subTest(unit=observer):
                text = (ENGINE / observer).read_text(encoding="utf-8")
                self.assertIn("PartOf=emberdeck.service", text)

    def test_spawn_passes_config_path_and_unbuffered_output(self):
        entry = FoundryCore.WORKERS[1]
        with capture_whisper(), mock.patch.object(FoundryCore, "manager", None), \
                mock.patch.object(FoundryCore.subprocess, "Popen") as popen:
            FoundryCore._spawn(entry)
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertEqual(Path(argv[1]), ENGINE / entry["script"])
        self.assertEqual(Path(env["CONFIG_PATH"]), ENGINE / "configs" / entry["config"])
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")


class HeartbeatSignalTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        patcher = mock.patch.object(FoundryCore, "bus", self.bus)
        patcher.start()
        self.addCleanup(patcher.stop)
        FoundryCore.hb_seq_cache.clear()
        self.addCleanup(FoundryCore.hb_seq_cache.clear)

    def test_missing_heartbeat_is_not_fresh(self):
        self.assertFalse(FoundryCore._hb_fresh("echo", 30.0))

    def test_fresh_and_stale_heartbeats(self):
        with mock.patch.object(self.bus, "monotonic_ms", return_value=100_000):
            self.bus.values["/proc/echo/heartbeat_ms"] = 99_000
            self.assertTrue(FoundryCore._hb_fresh("echo", 30.0))
            self.bus.values["/proc/echo/heartbeat_ms"] = 69_000
            self.assertFalse(FoundryCore._hb_fresh("echo", 30.0))

    def test_heartbeat_written_just_before_the_31_bit_wrap_is_fresh_after_it(self):
        self.bus.values["/proc/echo/heartbeat_ms"] = 0x7FFFFFFF - 500
        with mock.patch.object(self.bus, "monotonic_ms", return_value=1500):
            self.assertTrue(FoundryCore._hb_fresh("echo", 30.0))

    def test_sequence_progress_detects_change_only(self):
        self.bus.values["/proc/echo/hb_seq"] = 10
        self.assertTrue(FoundryCore._hb_progress("echo"))   # first observation
        self.assertFalse(FoundryCore._hb_progress("echo"))
        self.bus.values["/proc/echo/hb_seq"] = 11
        self.assertTrue(FoundryCore._hb_progress("echo"))


class SupervisorHarness:
    """A Supervisor over fake children, a fake clock and an in-memory Synapse."""

    def __init__(self, test, workers):
        self.test = test
        self.clock = [1000.0]
        self.bus = FakeBus()
        self.spawned = []
        self.children = {}
        self.die_on_spawn = set()
        self.stop = [False]
        patches = [
            mock.patch.object(FoundryCore, "bus", self.bus),
            mock.patch.object(FoundryCore, "_spawn", self.spawn),
            mock.patch.object(FoundryCore, "FAIL_CONSEC_N", 3),
            mock.patch.object(FoundryCore, "INTER_START_DELAY_S", 0.0),
            mock.patch.object(FoundryCore, "CHILD_TERM_GRACE_S", 0.01),
            mock.patch.dict(FoundryCore.last_start, {}, clear=True),
            mock.patch.dict(FoundryCore.fail_streak, {}, clear=True),
            mock.patch.dict(FoundryCore.hb_seq_cache, {}, clear=True),
            mock.patch.object(self.bus, "monotonic_ms", lambda: int(self.clock[0] * 1000)),
        ]
        for patcher in patches:
            patcher.start()
            test.addCleanup(patcher.stop)
        whisper = capture_whisper()
        self.messages = whisper.__enter__()
        test.addCleanup(whisper.__exit__, None, None, None)
        self.supervisor = FoundryCore.Supervisor(workers, should_stop=lambda: self.stop[0])

    def spawn(self, entry):
        name = entry["name"]
        process = FakeProcess(alive=name not in self.die_on_spawn)
        self.spawned.append(name)
        self.children[name] = process
        FoundryCore.last_start[name] = self.clock[0]
        FoundryCore.fail_streak[name] = 0
        FoundryCore.hb_seq_cache.pop(name, None)
        return process

    def beat(self, *names):
        for name in names:
            self.bus.publish_heartbeat(name)

    def tick(self, seconds=1.0):
        self.clock[0] += seconds
        return self.supervisor.tick(self.clock[0])

    def run_healthy(self, seconds, *names):
        for _ in range(int(seconds)):
            self.beat(*names)
            reason = self.tick()
            if reason is not None:
                return reason
        return None

    def events(self):
        return [(m.process, m.event) for m in self.messages]


GROUP = [dict(name="alpha", script="a.py", config="a.yaml"),
         dict(name="beta", script="b.py", config="b.yaml")]
MIXED = GROUP + [dict(name="presence", script="p.py", config="p.yaml",
                      restart_scope=FoundryCore.RESTART_SCOPE_WORKER)]


class RestartScopeTests(unittest.TestCase):
    def test_default_scope_is_group_and_only_discord_integrations_are_worker_scoped(self):
        self.assertEqual(FoundryCore.restart_scope({"name": "x"}), FoundryCore.RESTART_SCOPE_GROUP)
        for entry in FoundryCore.WORKERS:
            expected = (FoundryCore.RESTART_SCOPE_WORKER if entry["name"] in ("rich_presence", "artwork_service")
                        else FoundryCore.RESTART_SCOPE_GROUP)
            with self.subTest(worker=entry["name"]):
                self.assertEqual(FoundryCore.restart_scope(entry), expected)

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            FoundryCore.Supervisor([dict(name="x", script="x.py", config="x.yaml", restart_scope="planet")])


class GroupScopeTests(unittest.TestCase):
    def setUp(self):
        self.h = SupervisorHarness(self, MIXED)
        self.assertIsNone(self.h.supervisor.start_group())
        self.h.clock[0] += FoundryCore.WARMUP_GRACE_S

    def test_healthy_group_needs_no_restart(self):
        self.assertIsNone(self.h.run_healthy(5, "alpha", "beta", "presence"))

    def test_group_scoped_exit_requests_a_group_restart(self):
        self.h.children["beta"].alive = False
        self.assertEqual(self.h.tick(), "beta: process exited")

    def test_group_scoped_stale_heartbeat_requests_a_group_restart(self):
        # The first post-warm-up look at hb_seq counts as progress, then three
        # consecutive stale ticks (FAIL_CONSEC_N in this harness) restart.
        reasons = [self.h.run_healthy(1, "alpha", "presence") for _ in range(4)]
        self.assertEqual(reasons[:3], [None, None, None])
        self.assertEqual(reasons[3], "beta: heartbeat stale x3")

    def test_exit_during_warmup_still_restarts(self):
        FoundryCore.last_start["beta"] = self.h.clock[0]
        self.h.children["beta"].alive = False
        self.assertEqual(self.h.tick(), "beta: process exited")

    def test_temporary_heartbeat_gap_resets_the_failure_streak(self):
        self.h.run_healthy(3, "alpha", "presence")
        self.assertEqual(FoundryCore.fail_streak["beta"], 2)
        self.assertIsNone(self.h.run_healthy(1, "alpha", "beta", "presence"))
        self.assertEqual(FoundryCore.fail_streak["beta"], 0)

    def test_hang_outside_the_heartbeat_path_is_not_detected(self):
        # Documented limitation: liveness means "the heartbeat advances".
        self.assertIsNone(self.h.run_healthy(10, "alpha", "beta", "presence"))


class WorkerScopeTests(unittest.TestCase):
    def setUp(self):
        self.h = SupervisorHarness(self, MIXED)
        self.assertIsNone(self.h.supervisor.start_group())
        self.h.clock[0] += FoundryCore.WARMUP_GRACE_S
        self.first = dict(self.h.children)

    def test_local_exit_restarts_only_that_worker_after_its_backoff(self):
        self.h.children["presence"].alive = False
        self.assertIsNone(self.h.tick())
        self.assertIn(("FoundryCore", "process_worker_restart"), self.h.events())
        self.assertEqual(self.h.spawned.count("presence"), 1, "not respawned before its backoff")
        self.assertIsNone(self.h.run_healthy(FoundryCore.WORKER_BACKOFF_INITIAL_S, "alpha", "beta"))
        self.assertEqual(self.h.spawned.count("presence"), 2)
        for name in ("alpha", "beta"):
            self.assertIs(self.h.children[name], self.first[name], f"{name} must not be restarted")
            self.assertFalse(self.h.children[name].terminated)

    def test_local_stale_heartbeat_restarts_only_that_worker(self):
        reasons = [self.h.run_healthy(1, "alpha", "beta") for _ in range(4)]
        self.assertEqual(reasons, [None, None, None, None])
        self.assertTrue(self.first["presence"].terminated)
        self.assertEqual(self.h.supervisor.pending.keys(), {"presence"})

    def test_restarted_local_worker_gets_a_fresh_warmup(self):
        self.h.children["presence"].alive = False
        self.h.tick()
        self.h.run_healthy(FoundryCore.WORKER_BACKOFF_INITIAL_S, "alpha", "beta")
        # No heartbeat from the new process yet, but it is warming up.
        self.assertIsNone(self.h.run_healthy(FoundryCore.WARMUP_GRACE_S - 2, "alpha", "beta"))
        self.assertEqual(self.h.spawned.count("presence"), 2)

    def test_repeated_local_failures_back_off_and_never_busy_loop(self):
        delays = []
        for _ in range(4):
            self.h.children["presence"].alive = False
            before = self.h.clock[0]
            self.h.tick()
            due = self.h.supervisor.pending["presence"]
            delays.append(round(due - (before + 1.0)))
            self.h.run_healthy(due - self.h.clock[0] + 1, "alpha", "beta")
        self.assertEqual(delays, [5, 10, 20, 40])

    def test_shutdown_while_a_local_restart_is_pending(self):
        self.h.children["presence"].alive = False
        self.h.tick()
        self.h.stop[0] = True
        self.h.run_healthy(FoundryCore.WORKER_BACKOFF_INITIAL_S + 5, "alpha", "beta")
        self.assertEqual(self.h.spawned.count("presence"), 1)
        self.h.supervisor.stop_all()
        self.assertEqual(self.h.supervisor.pending, {})
        self.assertTrue(self.first["alpha"].terminated)

    def test_local_worker_death_during_startup_does_not_abort_the_group(self):
        h = SupervisorHarness(self, MIXED[2:] + GROUP)  # presence starts first
        h.die_on_spawn.add("presence")
        self.assertIsNone(h.supervisor.start_group())
        self.assertEqual(h.spawned, ["presence", "alpha", "beta"])


class ShippedWorkerTableTests(unittest.TestCase):
    """The real WORKERS table: Discord trouble must never touch the hardware workers."""

    def setUp(self):
        self.h = SupervisorHarness(self, FoundryCore.WORKERS)
        self.assertIsNone(self.h.supervisor.start_group())
        self.h.clock[0] += FoundryCore.WARMUP_GRACE_S
        self.first = dict(self.h.children)
        self.hardware = [w["name"] for w in FoundryCore.WORKERS
                         if w["name"] not in ("rich_presence", "artwork_service")]

    def assert_hardware_untouched(self):
        for name in self.hardware:
            self.assertIs(self.h.children[name], self.first[name], name)
            self.assertFalse(self.first[name].terminated, name)

    def test_artwork_service_failure_restarts_only_itself(self):
        self.h.children["artwork_service"].alive = False
        self.assertIsNone(self.h.tick())
        self.assertIsNone(self.h.run_healthy(FoundryCore.WORKER_BACKOFF_INITIAL_S, *self.hardware))
        self.assertEqual(self.h.spawned.count("artwork_service"), 2)
        self.assert_hardware_untouched()

    def test_rich_presence_exit_restarts_only_rich_presence(self):
        self.h.children["rich_presence"].alive = False
        self.assertIsNone(self.h.tick())
        self.assertIsNone(self.h.run_healthy(FoundryCore.WORKER_BACKOFF_INITIAL_S, *self.hardware))
        self.assertEqual(self.h.spawned.count("rich_presence"), 2)
        self.assert_hardware_untouched()

    def test_stale_rich_presence_heartbeat_restarts_only_rich_presence(self):
        for _ in range(4):
            self.assertIsNone(self.h.run_healthy(1, *self.hardware))
        self.assertTrue(self.first["rich_presence"].terminated)
        self.assertIn("rich_presence", self.h.supervisor.pending)
        self.assert_hardware_untouched()

    def test_rich_presence_dying_during_startup_does_not_abort_the_group(self):
        h = SupervisorHarness(self, FoundryCore.WORKERS)
        h.die_on_spawn.add("rich_presence")
        self.assertIsNone(h.supervisor.start_group())

    def test_foundry_shutdown_stops_rich_presence_with_everyone_else(self):
        self.h.supervisor.stop_all()
        self.assertTrue(self.first["rich_presence"].terminated)


class StartupFailureTests(unittest.TestCase):
    def test_group_worker_death_during_startup_stops_further_spawns(self):
        h = SupervisorHarness(self, GROUP + [dict(name="gamma", script="g.py", config="g.yaml")])
        h.die_on_spawn.add("alpha")
        self.assertEqual(h.supervisor.start_group(), "alpha: process exited during startup")
        self.assertEqual(h.spawned, ["alpha"])

    def test_death_noticed_during_the_inter_start_delay(self):
        h = SupervisorHarness(self, GROUP)
        spawn = h.spawn

        def spawn_then_die(entry):
            process = spawn(entry)
            threading.Timer(0.05, lambda: setattr(process, "alive", False)).start()
            return process

        with mock.patch.object(FoundryCore, "_spawn", spawn_then_die), \
                mock.patch.object(FoundryCore, "INTER_START_DELAY_S", 5.0):
            started = time.monotonic()
            reason = h.supervisor.start_group()
        self.assertEqual(reason, "alpha: process exited during startup")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(h.spawned, ["alpha"])

    def test_stop_interrupts_startup(self):
        h = SupervisorHarness(self, GROUP)
        h.stop[0] = True
        self.assertIsNone(h.supervisor.start_group())
        self.assertEqual(h.spawned, [])

    def test_spawn_failure_leaves_started_workers_visible_for_cleanup(self):
        h = SupervisorHarness(self, GROUP)
        spawn = h.spawn

        def failing_spawn(entry):
            if entry["name"] == "beta":
                raise FileNotFoundError("python")
            return spawn(entry)

        with mock.patch.object(FoundryCore, "_spawn", failing_spawn):
            with self.assertRaises(FileNotFoundError):
                h.supervisor.start_group()
        self.assertEqual(list(h.supervisor.procs), ["alpha"])
        h.supervisor.stop_all()
        self.assertTrue(h.children["alpha"].terminated)

    def test_run_restarts_the_group_after_a_startup_casualty(self):
        h = SupervisorHarness(self, GROUP)
        h.die_on_spawn.add("alpha")
        delays = []

        def fake_sleep_unless(should_stop, seconds, step_s=0.1):
            delays.append(seconds)
            if len(h.spawned) >= 2:
                h.stop[0] = True
            return not h.stop[0]

        def heal_after_first_spawn(entry):
            process = h.spawn(entry)
            h.die_on_spawn.discard("alpha")
            return process

        with mock.patch.object(FoundryCore, "_sleep_unless", fake_sleep_unless), \
                mock.patch.object(FoundryCore, "_spawn", heal_after_first_spawn), \
                mock.patch.object(FoundryCore, "restore_whisper_manager", lambda: "alive"), \
                mock.patch.object(FoundryCore.time, "sleep"):
            h.supervisor.run()
        self.assertEqual(delays[0], FoundryCore.RESTART_BACKOFF_INITIAL_S)
        self.assertIn(("FoundryCore", "process_group_restart"), h.events())
        self.assertEqual(h.spawned[:2], ["alpha", "alpha"])


class BackoffPolicyTests(unittest.TestCase):
    def test_delay_escalates_to_its_cap(self):
        backoff = FoundryCore.RestartBackoff(5.0, 60.0, 300.0)
        self.assertEqual([backoff.next_delay() for _ in range(6)], [5, 10, 20, 40, 60, 60])

    def test_reset_needs_sustained_health_and_warmup_is_neutral(self):
        backoff = FoundryCore.RestartBackoff(5.0, 60.0, 300.0)
        backoff.next_delay()
        backoff.next_delay()
        for second in range(0, 1000, 10):
            backoff.observe(float(second), None)  # warming up: never resets
        self.assertEqual(backoff.delay_s, 20.0)
        backoff.observe(1000.0, True)
        backoff.observe(1299.0, True)
        self.assertEqual(backoff.delay_s, 20.0)
        backoff.observe(1300.0, True)
        self.assertEqual(backoff.delay_s, 5.0)

    def test_any_unhealthy_observation_restarts_the_health_window(self):
        backoff = FoundryCore.RestartBackoff(5.0, 60.0, 300.0)
        backoff.next_delay()
        backoff.observe(0.0, True)
        backoff.observe(250.0, False)
        backoff.observe(260.0, True)
        backoff.observe(500.0, True)
        self.assertEqual(backoff.delay_s, 10.0)
        backoff.observe(560.0, True)
        self.assertEqual(backoff.delay_s, 5.0)

    def test_group_backoff_is_not_reset_by_the_ticks_right_after_a_restart(self):
        h = SupervisorHarness(self, GROUP)
        h.supervisor.group_backoff.next_delay()
        h.supervisor.start_group()
        h.run_healthy(FoundryCore.WARMUP_GRACE_S + 30, "alpha", "beta")
        self.assertEqual(h.supervisor.group_backoff.delay_s, 10.0)
        h.run_healthy(FoundryCore.BACKOFF_RESET_HEALTHY_S + 1, "alpha", "beta")
        self.assertEqual(h.supervisor.group_backoff.delay_s, FoundryCore.RESTART_BACKOFF_INITIAL_S)


class GroupStopTests(unittest.TestCase):
    def test_interruptible_sleep_returns_early_on_stop(self):
        self.assertFalse(FoundryCore._sleep_unless(lambda: True, 60.0))
        self.assertTrue(FoundryCore._sleep_unless(lambda: False, 0.01))

    def test_group_stop_terminates_then_force_kills_stubborn_workers(self):
        polite = FakeProcess()
        stubborn = FakeProcess(ignores_terminate=True)
        exited = FakeProcess(alive=False)
        with capture_whisper() as messages, mock.patch.object(FoundryCore, "CHILD_TERM_GRACE_S", 0.05):
            FoundryCore._group_stop({"polite": polite, "stubborn": stubborn, "exited": exited, "never": None})
        self.assertTrue(polite.terminated and not polite.killed)
        self.assertTrue(stubborn.terminated and stubborn.killed)
        self.assertFalse(exited.terminated)
        events = [(m.process, m.event) for m in messages]
        self.assertIn(("stubborn", "process_force_stopping"), events)


class WhisperManagerRecoveryTests(unittest.TestCase):
    """Real manager processes: a dead manager is restored where workers can find it."""

    def setUp(self):
        patches = [
            mock.patch.object(FoundryCore, "manager", None),
            mock.patch.object(FoundryCore, "daemon_queue", None),
            mock.patch.object(whisper_daemon, "QUEUE", None),
            mock.patch.object(whisper_daemon, "QUEUE_CONNECT_ATTEMPTED", False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        try:
            if FoundryCore.manager is not None:
                FoundryCore.manager.shutdown()
        except Exception:
            pass

    def test_live_manager_is_left_alone(self):
        FoundryCore._start_manager()
        self.assertEqual(FoundryCore.restore_whisper_manager(), "alive")

    def test_dead_manager_is_restored_and_workers_reconnect_without_restart(self):
        FoundryCore._start_manager()
        original = FoundryCore.manager
        host, port = original.address
        env = {"WHISPER_MANAGER_HOST": str(host), "WHISPER_MANAGER_PORT": str(port),
               "WHISPER_MANAGER_AUTH": original._authkey.hex()}
        with mock.patch.dict(os.environ, env):
            whisper_daemon.set_queue(None)
            whisper_daemon.QUEUE_CONNECT_ATTEMPTED = False
            whisper_daemon.log_error("echo", "audio capture unavailable", "before")
            self.assertIsNotNone(whisper_daemon.QUEUE)

            original._process.terminate()
            original._process.join(5)
            outcome = FoundryCore.restore_whisper_manager()
            self.assertIn(outcome, ("restored", "moved"))
            if outcome == "moved":
                self.skipTest("this OS refused to rebind the old port; Foundry falls back to a group restart")
            self.assertEqual(tuple(FoundryCore.manager.address), (host, port))

            with redirect_stdout(io.StringIO()):
                whisper_daemon.log_error("echo", "audio capture unavailable", "after restore")
            delivered = []
            new_queue = FoundryCore.manager.get_queue()
            while True:
                try:
                    delivered.append(whisper_daemon._coerce_message(new_queue.get(timeout=2)))
                except queue.Empty:
                    break
        details = [m.metadata.get("details") for m in delivered if m and m.process == "echo"]
        self.assertIn("after restore", details)
        self.assertIn("whisper_manager_died", [m.event for m in delivered if m])


if __name__ == "__main__":
    unittest.main()
