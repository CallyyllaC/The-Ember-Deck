"""Backend selection and newline-delimited JSON IPC with ``discord_bridge``.

The bridge is a child process that talks only over its own stdin/stdout. No
socket or network listener is opened. Python never loads the Discord SDK: an
x86_64 SDK is only ever reached through an x86_64 bridge run under Box64.

Protocol (version 1), one JSON object per line:

    -> {"id": 1, "op": "hello", "protocol": 1}
    <- {"id": 1, "ok": true, "result": "success", "protocol": 1, "sdk_version": "..."}
    <- {"event": "status", "state": "ready", "error": 0, "detail": 0}

Responses carry the request ``id``; events carry ``event`` instead. ``result``
is one of RESULTS. Token-bearing messages (``tokens`` events, ``set_token``,
``refresh_token`` and ``revoke`` requests) are never logged.

The bridge's stderr (dynamic-linker, Box64 and SDK start-up errors) is kept
only as a bounded, redacted in-memory tail. The worker reports it when the
bridge fails to start or exits unexpectedly, and never otherwise.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024
STDERR_TAIL_BYTES = 4096
REDACTED = "[REDACTED]"

RESULTS = (
    "success",
    "sdk_unavailable",
    "unauthenticated",
    "authorizing",
    "ready",
    "disconnected",
    "transient",
    "permanent",
    "cancelled",
)

ELF_MACHINES = {183: "aarch64", 62: "x86_64", 40: "arm", 3: "x86"}
_HOST_ALIASES = {"arm64": "aarch64", "amd64": "x86_64", "x64": "x86_64"}


# ------------------------------------------------------------------ backend selection

def host_machine() -> str:
    machine = platform.machine().lower()
    return _HOST_ALIASES.get(machine, machine)


def elf_machine(path: Path) -> Optional[str]:
    """Return the CPU architecture of a 64-bit little-endian ELF file, or None."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return None
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
        return None
    return ELF_MACHINES.get(int.from_bytes(header[18:20], "little"), "unknown")


@dataclass(frozen=True)
class Backend:
    kind: str                    # "native" or "box64"
    argv: List[str]
    env: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendChoice:
    backend: Optional[Backend]
    reason: str                  # safe, human-readable explanation


def _expand(path: Optional[str], base: Path) -> Optional[Path]:
    if not path:
        return None
    candidate = Path(os.path.expanduser(str(path)))
    return candidate if candidate.is_absolute() else base / candidate


def _native(discord: Dict[str, Any], base: Path, host: str) -> BackendChoice:
    bridge = _expand(discord.get("native_bridge"), base)
    if bridge is None:
        return BackendChoice(None, "no native bridge configured")
    if not bridge.is_file():
        return BackendChoice(None, f"native bridge not built ({bridge.name})")
    arch = elf_machine(bridge)
    if arch != host:
        return BackendChoice(None, f"native bridge is {arch or 'not an ELF binary'}, host is {host}")
    if not os.access(bridge, os.X_OK):
        return BackendChoice(None, "native bridge is not executable")
    env = {}
    lib_dir = _expand(discord.get("sdk_library_dir"), base)
    if lib_dir is not None:
        env["LD_LIBRARY_PATH"] = str(lib_dir)
    return BackendChoice(Backend("native", [str(bridge)], env), f"native {host} bridge")


LAUNCHER_DIR = Path(__file__).resolve().parent / "discord_bridge" / "launcher"


def _same_dir(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(str(b)))


def detached_opener_env(path: Optional[str] = None, which=shutil.which,
                        launcher_dir: Path = LAUNCHER_DIR) -> Dict[str, str]:
    """Environment that makes the SDK's ``xdg-open`` return immediately.

    During Client::Authorize the Discord SDK runs ``xdg-open <URL>`` (a PATH
    search) and waits for it to exit before serving its own OAuth callback.
    Under Box64 on the Pi the system xdg-open stayed alive with the browser,
    so linking hung until it was killed. ``launcher/xdg-open`` hands the URL
    to the real opener detached and exits at once. It is put first on PATH
    for the Box64 bridge only; the real opener is resolved here (never the
    launcher itself) and passed as EMBERDECK_XDG_OPEN. Returns {} when there
    is no system xdg-open or no launcher, leaving PATH untouched.
    """
    launcher = launcher_dir / "xdg-open"
    if not launcher.is_file():
        return {}
    search = os.environ.get("PATH", os.defpath) if path is None else path
    entries = [entry for entry in search.split(os.pathsep) if entry and not _same_dir(entry, launcher_dir)]
    real = which("xdg-open", path=os.pathsep.join(entries))
    if not real:
        return {}
    try:
        if not os.access(launcher, os.X_OK):
            os.chmod(launcher, 0o755)  # a copied tree may have lost the executable bit
    except OSError:
        pass
    return {"PATH": os.pathsep.join([str(launcher_dir), *entries]), "EMBERDECK_XDG_OPEN": os.path.abspath(real)}


def _box64(discord: Dict[str, Any], base: Path, which=shutil.which, opener_which=shutil.which) -> BackendChoice:
    box = discord.get("box64") or {}
    bridge = _expand(box.get("bridge"), base)
    if bridge is None:
        return BackendChoice(None, "no Box64 bridge configured")
    if not bridge.is_file():
        return BackendChoice(None, f"Box64 bridge not found ({bridge.name})")
    arch = elf_machine(bridge)
    if arch != "x86_64":
        return BackendChoice(None, f"Box64 bridge must be x86_64, found {arch or 'not an ELF binary'}")
    executable = str(box.get("executable") or "box64")
    resolved = which(executable) if os.sep not in executable else (executable if os.access(executable, os.X_OK) else None)
    if not resolved:
        return BackendChoice(None, "Box64 is not installed")
    # UV_USE_IO_URING=0: on a Raspberry Pi 5 (aarch64, 16 KiB pages, Box64
    # v0.4.5), Discord Social SDK 1.10.19337 never finished client init: its
    # WebSocketClient thread spun at ~100% CPU alternating epoll_pwait and
    # io_uring_enter (a libuv-style io_uring busy loop under emulation), and
    # init timed out. With io_uring disabled, hello took ~0.07 s and init
    # ~0.13 s on the same hardware. This is deliberately Box64-only
    # compatibility behaviour; it is not applied to native bridges or to the
    # rest of Ember Deck.
    env = {"BOX64_NOBANNER": "1", "BOX64_LOG": "0", "UV_USE_IO_URING": "0"}
    # Detached xdg-open so OAuth linking does not wait on the browser
    # (see detached_opener_env); Box64 backend only.
    env.update(detached_opener_env(which=opener_which))
    lib_dir = _expand(discord.get("sdk_library_dir"), base)
    if lib_dir is not None:
        env["BOX64_LD_LIBRARY_PATH"] = str(lib_dir)
    return BackendChoice(Backend("box64", [resolved, str(bridge)], env), "x86_64 bridge under Box64")


def select_backend(discord: Dict[str, Any], base: Path, *, host: Optional[str] = None, which=shutil.which) -> BackendChoice:
    """Apply ``discord.backend``: ``native``, ``box64`` or ``auto`` (native, then Box64)."""
    host = host or host_machine()
    mode = str(discord.get("backend", "auto"))
    if mode == "native":
        return _native(discord, base, host)
    if mode == "box64":
        return _box64(discord, base, which)
    native = _native(discord, base, host)
    if native.backend is not None:
        return native
    box = _box64(discord, base, which)
    if box.backend is not None:
        return box
    return BackendChoice(None, f"{native.reason}; {box.reason}")


# ------------------------------------------------------------------ stderr diagnostics

# Applied to every stderr tail before anything else sees it, in addition to the
# worker's redaction of known token values. Deliberately aggressive: a
# diagnostic only needs loader, Box64 and SDK error text.
_STDERR_PATTERNS = (
    (re.compile(r"AWOO\d*\.[^\s\"']*"), "AWOO." + REDACTED),
    (re.compile(r"(?i)(bearer\s+)[^\s\"',}]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)((?:access|refresh|id)?_?token[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)((?:client_)?secret[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+"), r"\1" + REDACTED),
)
# Long runs mixing letters and digits look like tokens; shared-library names
# ("....so", "....so.6") are kept because they are what a loader error is about.
_TOKENISH = re.compile(r"(?<![A-Za-z0-9._~+=-])(?=[A-Za-z0-9._~+=-]*\d)(?=[A-Za-z0-9._~+=-]*[A-Za-z])"
                       r"[A-Za-z0-9._~+=-]{24,}")
_LIBRARY_NAME = re.compile(r"[A-Za-z0-9_+-]+\.so(?:\.[0-9]+)*")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def redact_diagnostics(text: str) -> str:
    """Strip control characters and anything that could be a credential."""
    text = _CONTROL.sub("", text)
    for pattern, replacement in _STDERR_PATTERNS:
        text = pattern.sub(replacement, text)
    return _TOKENISH.sub(lambda m: m.group(0) if _LIBRARY_NAME.fullmatch(m.group(0)) else REDACTED, text)


# ------------------------------------------------------------------ bridge process

class BridgeError(RuntimeError):
    """IPC failure. Messages are safe to log."""


def _set_parent_death_signal() -> None:  # pragma: no cover - Linux only
    """Ask the kernel to SIGTERM the bridge if the Python worker dies."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


class BridgeProcess:
    """One running bridge: send requests, collect responses and events.

    A reader thread turns stdout lines into parsed messages; the worker loop
    polls without blocking (``poll_messages``) or waits briefly for one
    response (``request``). A second thread keeps only the last
    STDERR_TAIL_BYTES of stderr in memory; it is never written anywhere and is
    read only through ``stderr_tail`` after a failure.
    """

    def __init__(self, backend: Backend, *, popen=subprocess.Popen):
        env = dict(os.environ)
        env.update(backend.env)
        kwargs: Dict[str, Any] = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env, bufsize=0)
        if sys.platform.startswith("linux"):
            kwargs["preexec_fn"] = _set_parent_death_signal
        self.backend = backend
        self.process = popen(list(backend.argv), **kwargs)
        self._next_id = 0
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.malformed_lines = 0
        self._reader = threading.Thread(target=self._read_loop, name="discord-bridge-reader", daemon=True)
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_reader = threading.Thread(target=self._stderr_loop, name="discord-bridge-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    # -- reading
    def _read_loop(self) -> None:
        stream = self.process.stdout
        while True:
            try:
                line = stream.readline(MAX_LINE_BYTES + 1)
            except (OSError, ValueError):
                break
            if not line:
                break
            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                # Oversized line: discard the rest of it.
                while line and not line.endswith(b"\n"):
                    try:
                        line = stream.readline(MAX_LINE_BYTES + 1)
                    except (OSError, ValueError):
                        line = b""
                self.malformed_lines += 1
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self.malformed_lines += 1
                continue
            if not isinstance(message, dict):
                self.malformed_lines += 1
                continue
            if isinstance(message.get("id"), int) and not isinstance(message.get("id"), bool):
                with self._arrived:
                    self._responses[message["id"]] = message
                    self._arrived.notify_all()
            elif isinstance(message.get("event"), str):
                self._events.put(message)
            else:
                self.malformed_lines += 1
        with self._arrived:
            self._arrived.notify_all()

    def _stderr_loop(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        read = getattr(stream, "read1", stream.read)
        while True:
            try:
                chunk = read(1024)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._stderr_lock:
                self._stderr += chunk
                del self._stderr[:-STDERR_TAIL_BYTES]

    def stderr_tail(self, redact: Optional[Any] = None) -> str:
        """The redacted end of the bridge's stderr, for failure reports only."""
        if not self.alive():
            self._stderr_reader.join(timeout=0.5)  # collect the last output of an exited bridge
        with self._stderr_lock:
            raw = bytes(self._stderr)
        text = raw.decode("utf-8", errors="replace")
        if len(raw) >= STDERR_TAIL_BYTES and "\n" in text:
            text = text.split("\n", 1)[1]  # drop the partial first line
        text = redact_diagnostics(text)
        if redact is not None:
            text = redact(text)
        return text.strip()

    # -- writing
    def alive(self) -> bool:
        return self.process.poll() is None

    def exit_status(self) -> Optional[int]:
        return self.process.poll()

    def send(self, op: str, **fields: Any) -> int:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        line = json.dumps({"id": request_id, "op": op, **fields}, separators=(",", ":")) + "\n"
        try:
            self.process.stdin.write(line.encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise BridgeError(f"bridge stopped accepting requests ({op})") from None
        return request_id

    def wait_response(self, request_id: int, timeout_s: float) -> Dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._arrived:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("bridge did not answer in time")
                if not self.alive() and not self._reader.is_alive():
                    raise BridgeError("bridge exited")
                self._arrived.wait(min(0.1, remaining))
            return self._responses.pop(request_id)

    def request(self, op: str, timeout_s: float = 5.0, **fields: Any) -> Dict[str, Any]:
        response = self.wait_response(self.send(op, **fields), timeout_s)
        result = response.get("result")
        if result not in RESULTS:
            raise BridgeError(f"bridge sent an unknown result for {op}")
        return response

    def poll_events(self) -> List[Dict[str, Any]]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    # -- shutdown
    def stop(self, timeout_s: float = 2.0) -> None:
        """Ask the bridge to shut down, then terminate and finally kill it."""
        if self.alive():
            try:
                self.send("shutdown")
            except BridgeError:
                pass
            try:
                self.process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=timeout_s)
        # stderr is left to its reader, which closes with the process, so
        # stderr_tail() still has the final output after stop().
        for stream in (self.process.stdin, self.process.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
