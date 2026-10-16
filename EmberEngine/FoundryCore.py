"""Foundry: the supervisor that owns Ember Deck's worker processes.

Every worker in ``WORKERS`` is spawned in order with its YAML in
``CONFIG_PATH`` and watched through its Synapse heartbeat. Each entry has a
restart scope:

* ``"group"`` (the default, used by every hardware and media worker): an exit or a stale
  heartbeat stops and restarts the whole group in order, returning GPIO, I2C,
  USB, uinput and the HDMI-2 slot to a known owner.
* ``"worker"``: only that worker is stopped and respawned after its own
  backoff. Intended for optional integrations that own no shared hardware.

Foundry also hosts the Whisper log queue in a ``multiprocessing`` manager and
restores it on the same address if it dies.
"""

import os
import sys
import time
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from whisper_daemon import set_queue, log_info, log_error, log_recovery

import queue
from multiprocessing.managers import BaseManager, BaseProxy
import synapse as bus  # shared-memory helpers

# -------------------- Settings --------------------

RESTART_SCOPE_GROUP = "group"
RESTART_SCOPE_WORKER = "worker"
RESTART_SCOPES = (RESTART_SCOPE_GROUP, RESTART_SCOPE_WORKER)

# Omitting restart_scope means "group". Every worker that owns hardware or
# feeds a hardware owner is group-scoped; only the optional Discord
# integrations (artwork service, Rich Presence) are worker-scoped.
WORKERS: List[dict] = [
    dict(name="whisper_daemon", script="whisper_daemon.py", config="whisper_daemon.yaml"),
    dict(name="pawprint",       script="pawprint.py",       config="pawprint.yaml"),
    dict(name="hdmi2_source_controller", script="hdmi2_source_controller.py", config="hdmi2_source_controller.yaml"),
    dict(name="mpris_bridge",   script="mpris_bridge.py",   config="mpris_bridge.yaml"),
    dict(name="echo",           script="echo.py",           config="echo.yaml"),
    dict(name="minstrel",       script="minstrel.py",       config="minstrel.yaml"),
    dict(name="aurora",         script="aurora.py",         config="aurora.yaml"),
    dict(name="foxfire",        script="foxfire.py",        config="foxfire.yaml"),
    dict(name="willo_wisp",     script="willo_wisp.py",     config="willo_wisp.yaml"),
    # Optional local artwork origin for Discord (Cloudflare Tunnel). Owns no
    # hardware; idle unless artwork_service.yaml enables it.
    dict(name="artwork_service", script="artwork_service.py", config="artwork_service.yaml",
         restart_scope=RESTART_SCOPE_WORKER),
    # Optional Discord Rich Presence. Owns no hardware, so a crash, a stale
    # heartbeat or Box64/Discord trouble restarts only this worker.
    dict(name="rich_presence",  script="rich_presence.py",  config="rich_presence.yaml",
         restart_scope=RESTART_SCOPE_WORKER),
]

HEARTBEAT_GRACE_S = 30.0      # how long a heartbeat may be silent before we call it dead
WARMUP_GRACE_S    = 30.0      # ignore health checks for this long after spawn
FAIL_CONSEC_N     = 30        # require N consecutive failures before restart

INTER_START_DELAY_S       = 5.0
TICK_S                    = 1.0
CHILD_TERM_GRACE_S        = 5.0

# Group restarts: 5, 10, 20, 40, 60, 60 ... seconds. A persistent fault should
# not cycle hardware ownership every ~50 s forever.
RESTART_BACKOFF_INITIAL_S = 5.0
RESTART_BACKOFF_CAP_S     = 60.0
# Worker-local restarts are cheap but must never busy-loop: 5 s doubling to 5 min.
WORKER_BACKOFF_INITIAL_S  = 5.0
WORKER_BACKOFF_CAP_S      = 300.0
# A backoff returns to its initial delay only after this much continuous,
# post-warm-up health. It must exceed warm-up plus heartbeat detection (~90 s)
# so a worker that hangs a minute after every start keeps escalating.
BACKOFF_RESET_HEALTHY_S   = 300.0

BASE       = Path(__file__).resolve().parent
CONFIG_DIR = BASE / "configs"

# per-worker state
last_start: Dict[str, float]   = {}
fail_streak: Dict[str, int]    = {}
hb_seq_cache: Dict[str, int]   = {}

manager = None
daemon_queue = None

_manager_queue = queue.Queue()


def _get_manager_queue():
    """Return manager queue."""
    return _manager_queue


class WhisperQueueManager(BaseManager):
    """Manage WhisperQueueManager state and behaviour."""
    pass


WhisperQueueManager.register("get_queue", callable=_get_manager_queue)


def restart_scope(entry: dict) -> str:
    """Return a WORKERS entry's restart scope, defaulting to the group."""
    scope = entry.get("restart_scope", RESTART_SCOPE_GROUP)
    if scope not in RESTART_SCOPES:
        raise ValueError(f"{entry.get('name')}: restart_scope must be one of {RESTART_SCOPES}, got {scope!r}")
    return scope


# -------------------- Internals --------------------

def _spawn(entry: dict) -> subprocess.Popen:
    """Spawn one worker with CONFIG_PATH and unbuffered stdout.

    Used for the first start, group restarts and worker-local restarts alike,
    so a respawned worker always gets exactly the initial environment.
    """
    name   = entry["name"]
    script = BASE / entry["script"]
    cfg    = CONFIG_DIR / entry["config"]

    env = os.environ.copy()
    env["CONFIG_PATH"] = str(cfg)
    env["PYTHONUNBUFFERED"] = "1"
    if manager is not None:
        host, port = manager.address
        env["WHISPER_MANAGER_HOST"] = str(host)
        env["WHISPER_MANAGER_PORT"] = str(port)
        env["WHISPER_MANAGER_AUTH"] = manager._authkey.hex()
    log_info(name, "starting")
    p = subprocess.Popen([sys.executable, str(script)], env=env)
    last_start[name] = time.monotonic()
    fail_streak[name] = 0
    hb_seq_cache.pop(name, None)
    return p


def _hb_fresh(name: str, grace_s: float) -> bool:
    """Heartbeat freshness check using the 31-bit monotonic-ms stamp stored by workers."""
    last_ms = bus.get_int(f"/proc/{name}/heartbeat_ms", 0)
    if last_ms <= 0:
        return False
    return bus.elapsed_ms(bus.monotonic_ms(), last_ms) < int(grace_s * 1000)


def _hb_progress(name: str) -> bool:
    """True if hb_seq changed since last check."""
    cur = bus.get_int(f"/proc/{name}/hb_seq", -1)
    last = hb_seq_cache.get(name)
    hb_seq_cache[name] = cur
    return (last is None) or (cur != last)


def evaluate_worker_health(
    *,
    alive: bool,
    in_warmup: bool,
    heartbeat_fresh: bool,
    heartbeat_progressed: bool,
    failure_count: int,
    failure_limit: int,
) -> tuple[bool, int, str]:
    """Return ``(restart_required, new_failure_count, reason)`` for one worker."""
    if not alive:
        return True, failure_count, "process exited"
    if in_warmup:
        return False, 0, "warming up"
    if heartbeat_fresh or heartbeat_progressed:
        return False, 0, "healthy"
    failures = max(0, int(failure_count)) + 1
    return failures >= max(1, int(failure_limit)), failures, f"heartbeat stale x{failures}"


def next_restart_backoff(current: float, cap: float) -> float:
    """Double a restart delay without exceeding its configured ceiling."""
    return min(max(0.0, float(current)) * 2.0, max(0.0, float(cap)))


@dataclass
class RestartBackoff:
    """Escalating restart delay that resets only after sustained health.

    ``next_delay()`` is called once per restart: it returns the delay to use
    now and doubles the next one up to ``cap_s``. ``observe(now, healthy)`` is
    called every tick with True (healthy), False (unhealthy) or None (neutral,
    for example warming up or waiting to respawn); the delay returns to
    ``initial_s`` after ``reset_after_s`` of uninterrupted True observations.
    """

    initial_s: float
    cap_s: float
    reset_after_s: float = BACKOFF_RESET_HEALTHY_S
    delay_s: float = field(init=False)
    healthy_since: Optional[float] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.delay_s = float(self.initial_s)

    def next_delay(self) -> float:
        delay = self.delay_s
        self.delay_s = next_restart_backoff(self.delay_s, self.cap_s)
        self.healthy_since = None
        return delay

    def observe(self, now: float, healthy: Optional[bool]) -> None:
        if healthy is None:
            return
        if not healthy:
            self.healthy_since = None
            return
        if self.healthy_since is None:
            self.healthy_since = now
        elif now - self.healthy_since >= self.reset_after_s:
            self.delay_s = float(self.initial_s)


def _group_stop(procs: Dict[str, Optional[subprocess.Popen]]) -> None:
    """Stop all children gently, then force if needed."""
    for name, p in procs.items():
        try:
            if p and (p.poll() is None):
                log_info(name, "stopping")
                p.terminate()
        except Exception:
            pass

    deadline = time.time() + CHILD_TERM_GRACE_S
    while time.time() < deadline:
        if all((p is None) or (p.poll() is not None) for p in procs.values()):
            break
        time.sleep(0.05)

    for name, p in procs.items():
        try:
            if p and (p.poll() is None):
                log_info(name, "stopping_with_force")
                p.kill()
        except Exception:
            pass


def _sleep_unless(should_stop: Callable[[], bool], seconds: float, step_s: float = 0.1) -> bool:
    """Sleep in short slices; return False as soon as a stop is requested."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return True
        time.sleep(min(step_s, remaining))
    return False


def _config_logger():
    """Point Foundry's own log calls at the current manager queue."""
    set_queue(daemon_queue)


# -------------------- Whisper manager --------------------

def _start_manager(address: Tuple[str, int] = ("127.0.0.1", 0), authkey: Optional[bytes] = None) -> None:
    """Start the Whisper queue manager and route Foundry's logging into it."""
    global manager, daemon_queue
    started = WhisperQueueManager(address=address, authkey=authkey or os.urandom(32))
    started.start()
    manager = started
    daemon_queue = started.get_queue()
    _config_logger()


def _manager_alive() -> bool:
    process = getattr(manager, "_process", None)
    return process is not None and process.is_alive()


def _forget_manager_connections(address: Tuple[str, int]) -> None:
    """Drop this process's cached connections to a dead manager at ``address``.

    ``BaseProxy`` keeps one connection per manager address per thread and
    shares it between every proxy to that address. A manager restored on the
    same address would otherwise be reached through the dead manager's socket
    for as long as any old proxy is still alive.
    """
    with BaseProxy._mutex:
        BaseProxy._address_to_local.pop(tuple(address), None)


def restore_whisper_manager() -> str:
    """Restart a dead manager; return ``"alive"``, ``"restored"`` or ``"moved"``.

    Workers learned the manager's host, port and key at spawn time. Restarting
    on the *same* address with the same key lets every worker's existing
    reconnect-on-failure path find it again without being restarted. Only if
    that address cannot be bound does the manager move, which requires a group
    restart to hand workers the new address.
    """
    if manager is None or _manager_alive():
        return "alive"
    address, authkey = tuple(manager.address), manager._authkey
    set_queue(None)  # Foundry's own messages fall back to the journal meanwhile
    _forget_manager_connections(address)
    try:
        _start_manager(address, authkey)
        outcome = "restored"
    except Exception as exc:
        print(f"[FoundryCore] Whisper manager could not reuse {address}: {exc!r}", flush=True)
        _start_manager(("127.0.0.1", 0))
        outcome = "moved"
    log_error("FoundryCore", "Whisper log manager died", f"restarted ({outcome})",
              event="whisper_manager_died", dedupe_key="FoundryCore:whisper_manager")
    log_recovery("FoundryCore", "whisper_manager_restored", recovers_key="FoundryCore:whisper_manager")
    return outcome


# -------------------- Supervisor --------------------

class Supervisor:
    """Owns the worker processes and both restart scopes."""

    def __init__(self, workers: Optional[List[dict]] = None, should_stop: Callable[[], bool] = lambda: False):
        self.workers = list(WORKERS if workers is None else workers)
        self.scopes = {entry["name"]: restart_scope(entry) for entry in self.workers}
        self.should_stop = should_stop
        self.procs: Dict[str, Optional[subprocess.Popen]] = {}
        self.pending: Dict[str, float] = {}  # worker-scoped respawn deadlines
        self.group_backoff = RestartBackoff(RESTART_BACKOFF_INITIAL_S, RESTART_BACKOFF_CAP_S)
        self.worker_backoff = {
            name: RestartBackoff(WORKER_BACKOFF_INITIAL_S, WORKER_BACKOFF_CAP_S)
            for name, scope in self.scopes.items() if scope == RESTART_SCOPE_WORKER
        }

    # -- group lifecycle
    def _startup_casualty(self) -> Optional[str]:
        """Return a reason if an already-started group-scoped worker has exited."""
        for name, process in self.procs.items():
            if self.scopes.get(name) == RESTART_SCOPE_GROUP and process is not None and process.poll() is not None:
                return f"{name}: process exited during startup"
        return None

    def start_group(self) -> Optional[str]:
        """Start every worker in order; return a reason if the start must be abandoned.

        Already-started group-scoped workers are checked between launches and
        throughout each inter-start delay, so a worker that dies early aborts
        the sequence instead of waiting ~45 s for it to finish. A worker-scoped
        worker that dies is left for its own restart path. ``None`` means the
        group started, or a stop was requested.
        """
        _config_logger()
        self.procs = {}
        self.pending.clear()
        for entry in self.workers:
            if self.should_stop():
                return None
            casualty = self._startup_casualty()
            if casualty:
                return casualty
            self.procs[entry["name"]] = _spawn(entry)
            deadline = time.monotonic() + INTER_START_DELAY_S
            while not self.should_stop():
                casualty = self._startup_casualty()
                if casualty:
                    return casualty
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                time.sleep(min(0.1, remaining))
        return None if self.should_stop() else self._startup_casualty()

    def stop_all(self) -> None:
        _group_stop(self.procs)
        self.pending.clear()

    # -- health
    def _worker_health(self, name: str, now: float) -> Tuple[Optional[str], Optional[bool]]:
        """Return ``(restart reason, health)``; health None means warming up."""
        p = self.procs.get(name)
        alive = (p is not None) and (p.poll() is None)
        in_warmup = now - last_start.get(name, 0.0) < WARMUP_GRACE_S
        fresh = _hb_fresh(name, HEARTBEAT_GRACE_S) if alive and not in_warmup else False
        step = _hb_progress(name) if alive and not in_warmup else False
        restart, failures, health_reason = evaluate_worker_health(
            alive=alive,
            in_warmup=in_warmup,
            heartbeat_fresh=fresh,
            heartbeat_progressed=step,
            failure_count=fail_streak.get(name, 0),
            failure_limit=FAIL_CONSEC_N,
        )
        fail_streak[name] = failures
        if restart:
            return f"{name}: {health_reason}", False
        if in_warmup:
            return None, None
        return None, failures == 0

    def tick(self, now: float) -> Optional[str]:
        """One supervision pass.

        Worker-scoped failures are handled here (stop that worker, respawn it
        after its backoff). The first group-scoped failure is returned as the
        reason for a group restart; nothing else is touched in that case.
        """
        group_health: Optional[bool] = True
        for entry in self.workers:
            name = entry["name"]
            local = self.scopes[name] == RESTART_SCOPE_WORKER
            if local and name in self.pending:
                self.worker_backoff[name].observe(now, None)
                continue
            reason, health = self._worker_health(name, now)
            if local:
                self.worker_backoff[name].observe(now, health)
                if reason:
                    self._restart_worker_later(entry, reason, now)
                continue
            if reason:
                self.group_backoff.observe(now, False)
                return reason
            if health is False:
                group_health = False
            elif health is None and group_health is not False:
                group_health = None
        self.group_backoff.observe(now, group_health)
        self._respawn_due(now)
        return None

    # -- worker-local restarts
    def _restart_worker_later(self, entry: dict, reason: str, now: float) -> None:
        name = entry["name"]
        delay = self.worker_backoff[name].next_delay()
        log_error("FoundryCore", "worker_restart", f"{reason}; restarting only {name} in {delay:g} s",
                  event="process_worker_restart", dedupe_key=f"FoundryCore:worker_restart:{name}")
        _group_stop({name: self.procs.get(name)})
        self.procs[name] = None
        self.pending[name] = now + delay

    def _respawn_due(self, now: float) -> None:
        for entry in self.workers:
            name = entry["name"]
            due = self.pending.get(name)
            if due is None or now < due or self.should_stop():
                continue
            del self.pending[name]
            self.procs[name] = _spawn(entry)

    # -- main loop
    def run(self) -> None:
        failure = self.start_group()
        while not self.should_stop():
            if failure is None:
                if not _sleep_unless(self.should_stop, TICK_S):
                    break
                if restore_whisper_manager() == "moved":
                    failure = "whisper manager moved to a new address"
                    continue
                failure = self.tick(time.monotonic())
                continue
            log_error("FoundryCore", "group_restart", failure)
            if not _sleep_unless(self.should_stop, self.group_backoff.next_delay()):
                break
            self.stop_all()
            time.sleep(0.2)
            failure = self.start_group()


# -------------------- Main --------------------

def main():
    """Configure and run the component until shutdown."""
    stopping = False

    def on_stop(signum, frame):
        """Mark the component for an orderly shutdown."""
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    # Host one queue behind a local manager server.  Workers are launched with
    # connection details in their environment, so their separately imported
    # whisper_daemon modules all publish to this same queue.
    _start_manager()
    supervisor = Supervisor(should_stop=lambda: stopping)
    try:
        supervisor.run()
    finally:
        supervisor.stop_all()
        bus.close_all()
        if manager is not None:
            try:
                manager.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
