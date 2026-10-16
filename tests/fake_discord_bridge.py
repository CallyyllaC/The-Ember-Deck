"""Stand-in for discord_bridge used by tests: speaks protocol v1 over stdin/stdout.

Usage: python fake_discord_bridge.py <mode>
  normal      answer every request; emit a status event after set_token
  garbage     also write malformed lines before each response
  silent      never answer (timeout tests)
  exit        exit with status 3 after the hello response
  stubborn    ignore shutdown and SIGTERM
  loader      write a loader-style error (plus secrets) to stderr and exit 127
  noisy       write 100 KiB to stderr, then behave normally
"""

import json
import os
import signal
import sys
import time


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    if mode == "loader":
        secret = os.environ.get("FAKE_BRIDGE_SECRET", "")
        sys.stderr.write("discord_bridge: error while loading shared libraries: libpulse.so.0: "
                         "cannot open shared object file: No such file or directory\n"
                         f"sdk: UpdateToken {secret} refresh_token=Zq81Lm0x\n"
                         "AWOO1.awoo-2026-01.eyJ2IjoxfQ.c2lnbmF0dXJl\n")
        sys.stderr.flush()
        sys.exit(127)
    if mode == "noisy":
        for i in range(1600):
            sys.stderr.write(f"noise line {i:05d} " + "x" * 48 + "\n")
        sys.stderr.write("last line\n")
        sys.stderr.flush()
    if mode == "stubborn" and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        op, request_id = request.get("op"), request.get("id")
        if mode == "silent":
            continue
        if mode == "garbage":
            sys.stdout.write("this is not json\n[1, 2, 3]\n{\"neither\": \"id nor event\"}\n")
        if op == "shutdown":
            if mode == "stubborn":
                continue
            send({"id": request_id, "ok": True, "result": "success"})
            return
        if op == "set_token":
            send({"id": request_id, "ok": True, "result": "success"})
            send({"event": "status", "state": "ready", "error": 0, "detail": 0})
            continue
        send({"id": request_id, "ok": True, "result": "success", "protocol": 1, "echo_op": op})
        if mode == "exit" and op == "hello":
            sys.exit(3)
    if mode == "stubborn":
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
