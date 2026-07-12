"""Runtime support for the Ember Deck system."""

import os
import sys
import time
import shlex
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
    
def launch_kiosk_browser(url="http://127.0.0.1:32500"):
    """Launch Firefox kiosk once the Wayland desktop is ready."""

    env = os.environ.copy()
    env["MOZ_ENABLE_WAYLAND"] = "1"

    runtime_dir = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    wayland_display = env.get("WAYLAND_DISPLAY", "wayland-0")
    wayland_socket = runtime_dir / wayland_display

    # Do not hold up the Deck forever, but give Labwc time to exist.
    deadline = time.monotonic() + 30.0
    while not wayland_socket.exists():
        if time.monotonic() >= deadline:
            print(
                f"[kiosk] Wayland socket did not appear: {wayland_socket}",
                flush=True,
            )
            return
        time.sleep(0.25)

    # Labwc may have created its socket a fraction before it is ready to map windows.
    time.sleep(0.75)

    subprocess.call(
        ["pkill", "-f", "firefox"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)

    for attempt in range(1, 16):
        process = subprocess.Popen(
            ["firefox", "--kiosk", url],
            env=env,
        )

        time.sleep(1.0)

        # Firefox is still alive, so it accepted the display.
        if process.poll() is None:
            print(f"[kiosk] Firefox launched on attempt {attempt}", flush=True)
            return

        print(
            f"[kiosk] Firefox exited during startup, retry {attempt}/15",
            flush=True,
        )
        time.sleep(1.0)

    print("[kiosk] Firefox failed to launch after 15 attempts", flush=True)

# --- Speaker self-test ---------------------------------------------------

SPEAKER_TEST_FILES = [
    "/usr/share/sounds/alsa/Front_Left.wav",    # left
    "/usr/share/sounds/alsa/Front_Right.wav",   # right
    "/usr/share/sounds/alsa/Front_Center.wav",  # both + sub punch
]

def boot_speaker_self_test(delay_between=0.3):
    """
    On boot, play short test sounds through:
      1. Left speaker
      2. Right speaker
      3. Center (sum -> mains + sub)

    This both:
      - Wakes up the audio graph (PipeWire / Pulse / DAC)
      - Confirms all speakers are alive
    """

    for wav in SPEAKER_TEST_FILES:
        subprocess.run(
            ["paplay", wav],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(delay_between)
        
def wait_for_audio_ready(timeout=10):
    """
    Wait until Pulse/PipeWire is reachable and at least one sink exists.
    We don't care if it's RUNNING/IDLE/SUSPENDED – paplay or Plexamp will wake it.
    """
    start = time.time()

    while time.time() - start < timeout:
        # 1. Does Pulse respond at all?
        info_rc = subprocess.run(
            ["pactl", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        ).returncode

        if info_rc != 0:
            time.sleep(0.3)
            continue

        # 2. Do we have *any* sink? (state doesn't matter)
        try:
            sinks = subprocess.check_output(
                ["pactl", "list", "short", "sinks"],
                stderr=subprocess.DEVNULL
            ).decode().strip()
        except subprocess.CalledProcessError:
            sinks = ""

        if sinks:
            return True

        time.sleep(0.3)

    return False


def start_plexamp():
    """
    Start Plexamp cleanly after confirming the audio backend is stable.
    Kills old zombie instances first, then launches with --device pulse.
    """

    print("[Deck] Preparing to start Plexamp…")

    # 1. Murder any leftover Plexamp processes (systemd left-behinds)
    subprocess.call(
        ["pkill", "-f", "plexamp/js/index.js"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(0.3)

    # 2. Ensure audio is actually ready before launching Plexamp
    boot_speaker_self_test()
    if not wait_for_audio_ready():
        print("[Deck] Warning: audio backend not fully ready, launching anyway.")

    # 3. Launch Plexamp as the current user
    cmd = f"/usr/bin/node {Path.home() / 'plexamp/js/index.js'} --device pulse"

    subprocess.Popen(
        shlex.split(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT
    )

    print("[Deck] Plexamp launched.")

# -------------------- Main --------------------

def main():
    """Configure and run the component until shutdown."""
    global manager, daemon_queue
    # start_plexamp()
    # launch_kiosk_browser()
    
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

                if not alive:
                    unhealthy = True
                    reason = f"{name}: process exited"
                    break

                # warmup grace
                if nowm - last_start.get(name, 0.0) < WARMUP_GRACE_S:
                    continue

                fresh = _hb_fresh(name, HEARTBEAT_GRACE_S)
                step  = _hb_progress(name)
                ok = fresh or step

                # debug print so you can see what it's doing
                #age_ms = int(time.monotonic() * 1000) - bus.get_int(f"/proc/{name}/heartbeat_ms", 0)
                #print(f"{name} hb: age_ms={age_ms} fresh={fresh} step={step} ok={ok}")

                if not ok:
                    fail_streak[name] = fail_streak.get(name, 0) + 1
                    if fail_streak[name] >= FAIL_CONSEC_N:
                        unhealthy = True
                        reason = f"{name}: heartbeat stale x{fail_streak[name]}"
                        break
                else:
                    fail_streak[name] = 0

            if unhealthy:
                log_error("FoundryCore", "group_restart", reason)
                time.sleep(max(0.0, next_backoff))
                _group_stop(procs)
                time.sleep(0.2)
                procs = _group_start()
                next_backoff = min(next_backoff * 2.0, RESTART_BACKOFF_CAP_S)
            else:
                next_backoff = RESTART_BACKOFF_INITIAL_S

    finally:
        _group_stop(procs)
        bus.close_all()
        if manager is not None:
            manager.shutdown()


if __name__ == "__main__":
    main()
