"""Runtime support for the Ember Deck system."""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path
from typing import Dict, List
from whisper_daemon import set_queue, log_info, log_error

import queue
from multiprocessing.managers import BaseManager
import synapse as bus  # shared-memory helpers

# -------------------- Settings --------------------

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
]

HEARTBEAT_GRACE_S = 30.0      # how long a heartbeat may be silent before we call it dead
WARMUP_GRACE_S    = 30.0      # ignore health checks for this long after spawn
FAIL_CONSEC_N     = 30        # require N consecutive failures before restart

INTER_START_DELAY_S       = 5.0
RESTART_BACKOFF_INITIAL_S = 5.0
RESTART_BACKOFF_CAP_S     = 15.0
CHILD_TERM_GRACE_S        = 5.0

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
# -------------------- Internals --------------------

def _spawn(entry: dict) -> subprocess.Popen:
    """Spawn one worker with CONFIG_PATH and unbuffered stdout."""
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
    return p


def _hb_fresh(name: str, grace_s: float) -> bool:
    """Heartbeat freshness check using monotonic-ms stored by workers."""
    now_ms  = int(time.monotonic() * 1000)
    last_ms = bus.get_int(f"/proc/{name}/heartbeat_ms", 0)
    if last_ms <= 0:
        return False
    return (now_ms - last_ms) < int(grace_s * 1000)


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


def _group_stop(procs: Dict[str, subprocess.Popen]) -> None:
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


def _group_start() -> Dict[str, subprocess.Popen]:
    """Return the group start result."""
    _config_logger()
    
    """Start all workers in strict order with delays."""
    procs: Dict[str, subprocess.Popen] = {}
    for w in WORKERS:
        name = w["name"]
        procs[name] = _spawn(w)
        time.sleep(INTER_START_DELAY_S)
    return procs

def _config_logger():
    """Handle the config logger lifecycle step."""
    set_queue(daemon_queue)

# -------------------- Main --------------------

def main():
    """Configure and run the component until shutdown."""
    global manager, daemon_queue
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
    manager = WhisperQueueManager(address=("127.0.0.1", 0), authkey=os.urandom(32))
    manager.start()
    daemon_queue = manager.get_queue()
    
    procs = _group_start()
    next_backoff = RESTART_BACKOFF_INITIAL_S

    try:        
        while not stopping:
            time.sleep(1.0)

            unhealthy = False
            reason = "unknown"

            for w in WORKERS:
                name = w["name"]
                p = procs.get(name)
                alive = (p is not None) and (p.poll() is None)
                nowm = time.monotonic()

                in_warmup = nowm - last_start.get(name, 0.0) < WARMUP_GRACE_S
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
                    unhealthy = True
                    reason = f"{name}: {health_reason}"
                    break

            if unhealthy:
                log_error("FoundryCore", "group_restart", reason)
                time.sleep(max(0.0, next_backoff))
                _group_stop(procs)
                time.sleep(0.2)
                procs = _group_start()
                next_backoff = next_restart_backoff(next_backoff, RESTART_BACKOFF_CAP_S)
            else:
                next_backoff = RESTART_BACKOFF_INITIAL_S

    finally:
        _group_stop(procs)
        bus.close_all()
        if manager is not None:
            manager.shutdown()


if __name__ == "__main__":
    main()
