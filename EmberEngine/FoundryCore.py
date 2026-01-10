"""
FoundryCore.py
Process manager for EmberEngine. Spawns and monitors workers:

- echo.py     : audio producer -> /audio/*
- Aurora.py   : color manager  -> /theme/ultra/*
- foxfire.py  : LED renderer   -> /leds/main/*

Uses Synapse heartbeats and exponential backoff restarts.
"""

import os
import sys
import time
import subprocess
import signal
from pathlib import Path
from typing import Dict
import synapse as bus  # shared-memory helpers (per-variable)

# Base paths
BASE = Path(__file__).resolve().parent
CONFIG_DIR = BASE / "configs"

# Workers supervised by FoundryCore
# Each entry must have:
#   - name   : heartbeat name (writes /proc/<name>/heartbeat)
#   - script : python module file in repo root
#   - config : YAML file in ./configs passed via CONFIG_PATH
WORKERS = [
    dict(name="echocore", script="echo.py",    config="echo.yaml"),
    dict(name="aurora",   script="Aurora.py",  config="aurora.yaml"),
    dict(name="foxfire",  script="foxfire.py", config="foxfire.yaml"),
]


def spawn(entry: dict) -> subprocess.Popen:
    """
    Launch a worker as a child process with its CONFIG_PATH env set.
    Returns a Popen handle.
    """
    script = BASE / entry["script"]
    cfg    = CONFIG_DIR / entry["config"]
    env = os.environ.copy()
    env["CONFIG_PATH"] = str(cfg)
    # Inherit stdin/out/err; change to DEVNULL if you want silent children
    return subprocess.Popen([sys.executable, str(script)], env=env)


def hb_ok(name: str, grace: float = 5.0) -> bool:
    """
    A worker is considered healthy if its last heartbeat is recent.
    Heartbeats are floats (epoch seconds) stored at /proc/<name>/heartbeat.
    """
    last = bus.get_float(f"/proc/{name}/heartbeat", 0.0)
    return (time.time() - last) < grace


def main():
    procs: Dict[str, subprocess.Popen] = {}
    backoff: Dict[str, float] = {}
    stopping = False

    # Start everything once
    for w in WORKERS:
        procs[w["name"]] = spawn(w)
        backoff[w["name"]] = 1.0  # initial restart delay

    def stop_all(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    try:
        while not stopping:
            time.sleep(1.0)

            for w in WORKERS:
                name = w["name"]
                p = procs.get(name)
                alive = (p is not None) and (p.poll() is None)
                ok = hb_ok(name)

                if not alive or not ok:
                    # Try a gentle stop if still alive
                    if p and (p.poll() is None):
                        try:
                            p.terminate()
                            p.wait(timeout=2)
                        except Exception:
                            try:
                                p.kill()
                            except Exception:
                                pass

                    # Exponential backoff to avoid restart thrash
                    delay = backoff[name]
                    print(f"[FoundryCore] restarting {name} in {delay:.1f}s (alive={alive} hb_ok={ok})")
                    time.sleep(delay)
                    procs[name] = spawn(w)
                    backoff[name] = min(delay * 2.0, 30.0)
                else:
                    # Healthy: reset delay
                    backoff[name] = 1.0

    finally:
        # Best-effort graceful shutdown of all children
        for p in procs.values():
            try:
                if p and (p.poll() is None):
                    p.terminate()
            except Exception:
                pass

        time.sleep(1.0)

        for p in procs.values():
            try:
                if p and (p.poll() is None):
                    p.kill()
            except Exception:
                pass

        # Close shared-memory handles
        bus.close_all()


if __name__ == "__main__":
    main()