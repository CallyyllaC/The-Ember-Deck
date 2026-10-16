"""
Shared-memory helper using per-variable segments. No classes, minimal ceremony.

Each key maps to one named SharedMemory segment. Handles are cached per process.

Python 3.13 note:
SharedMemory gained a public ``track`` argument. Ember Deck deliberately keeps
its named segments alive across worker restarts, so every process opens them
with tracking disabled. This replaces the previous private resource_tracker
unregister workaround, which can break on Python 3.13 when a fresh UI process
opens an existing segment.
"""
from multiprocessing import shared_memory
import struct
import sys
import time
from typing import Dict
import numpy as _np

try:
    from multiprocessing import resource_tracker as _rt  # fallback for pre-3.13
except Exception:
    _rt = None

_HANDLE_CACHE: Dict[str, shared_memory.SharedMemory] = {}


class SharedMemoryLayoutError(RuntimeError):
    """A persistent segment is smaller than the current reader/writer contract."""


def _norm(name: str) -> str:
    """Return the norm result."""
    return ("smem__" + name.strip().replace(" ", "_").replace("/", "__"))[:254]


def _open_untracked(norm: str, *, create: bool, size: int) -> shared_memory.SharedMemory:
    """Open a segment without asking Python to auto-unlink Deck-owned memory."""
    kwargs = {"name": norm, "create": create}
    if create:
        kwargs["size"] = size

    # Python 3.13 provides ``track``. Use the public API whenever possible.
    try:
        return shared_memory.SharedMemory(**kwargs, track=False)
    except TypeError:
        # Older Python releases lack ``track``. Retain the old compatibility
        # behaviour only there, never on the Python 3.13 path.
        h = shared_memory.SharedMemory(**kwargs)
        if _rt is not None:
            try:
                _rt.unregister(h._name, "shared_memory")
            except Exception:
                pass
        return h


def _get_handle(name: str, size: int, create: bool):
    """Return handle."""
    norm = _norm(name)
    h = _HANDLE_CACHE.get(norm)
    if h is not None:
        if size > h.size:
            raise SharedMemoryLayoutError(
                f"shared-memory key {name!r} is {h.size} bytes but this process requires {size}; "
                "stop Ember Deck, remove the stale /dev/shm segment or reboot, then restart"
            )
        return h

    try:
        h = _open_untracked(norm, create=create, size=size)
    except FileExistsError:
        # First writer may have already created it. Re-open without creation.
        h = _open_untracked(norm, create=False, size=0)

    if size > h.size:
        actual_size = h.size
        h.close()
        raise SharedMemoryLayoutError(
            f"shared-memory key {name!r} is {actual_size} bytes but this process requires {size}; "
            "stop Ember Deck, remove the stale /dev/shm segment or reboot, then restart"
        )
    _HANDLE_CACHE[norm] = h
    return h


# ---------- Scalars ----------
def get_int(name: str, default: int = 0) -> int:
    """Read a 32-bit signed int. Returns default if segment is missing."""
    try:
        h = _get_handle(name, 4, create=False)
        return int(struct.unpack_from("<i", h.buf, 0)[0])
    except FileNotFoundError:
        return int(default)


def set_int(name: str, value: int) -> None:
    """Write a 32-bit signed int."""
    h = _get_handle(name, 4, create=True)
    struct.pack_into("<i", h.buf, 0, int(value))


def get_float(name: str, default: float = 0.0) -> float:
    """Read a 32-bit float. Returns default if segment is missing."""
    try:
        h = _get_handle(name, 4, create=False)
        return float(struct.unpack_from("<f", h.buf, 0)[0])
    except FileNotFoundError:
        return float(default)


def set_float(name: str, value: float) -> None:
    """Write a 32-bit float."""
    h = _get_handle(name, 4, create=True)
    struct.pack_into("<f", h.buf, 0, float(value))


# ---------- Arrays ----------
def try_get_array(name: str, length: int, dtype: str = "f32"):
    """Return a list, or None if the segment does not exist yet."""
    if dtype == "f32":
        item_size, fmt = 4, f"<{length}f"
    elif dtype == "u8":
        item_size, fmt = 1, None
    elif dtype == "i32":
        item_size, fmt = 4, f"<{length}i"
    else:
        raise ValueError("dtype must be 'f32', 'u8', or 'i32'")

    size = length * item_size
    try:
        h = _get_handle(name, size, create=False)
    except FileNotFoundError:
        return None

    if dtype == "u8":
        return list(bytes(h.buf[:size]))
    return list(struct.unpack_from(fmt, h.buf, 0))


def set_array(name: str, values, dtype: str = "f32") -> None:
    """Write a fixed-size array; the first writer defines the segment size."""
    if dtype == "f32":
        arr = _np.asarray(values, dtype=_np.float32)
    elif dtype == "u8":
        arr = _np.asarray(values, dtype=_np.uint8)
    elif dtype == "i32":
        arr = _np.asarray(values, dtype=_np.int32)
    else:
        raise ValueError("dtype must be 'f32', 'u8', or 'i32'")

    payload = memoryview(arr).cast("B")
    h = _get_handle(name, arr.nbytes, create=True)
    h.buf[:arr.nbytes] = payload


# ---------- Monotonic millisecond stamps ----------
# Scalars are signed 32-bit. CLOCK_MONOTONIC counts from boot, so a raw
# ``monotonic() * 1000`` exceeds that range after about 24.8 days of uptime and
# set_int() raises struct.error. Heartbeats and other ms stamps therefore wrap
# at 31 bits, and readers compare them with elapsed_ms().
MS_CLOCK_MASK = 0x7FFFFFFF


def monotonic_ms() -> int:
    """Return a positive 31-bit monotonic millisecond stamp safe for set_int()."""
    stamp = int(time.monotonic() * 1000) & MS_CLOCK_MASK
    # Zero is the conventional "never written" value for heartbeat readers.
    return stamp or 1


def elapsed_ms(now_ms: int, then_ms: int) -> int:
    """Return milliseconds from ``then_ms`` to ``now_ms`` across a 31-bit wrap."""
    return (int(now_ms) - int(then_ms)) & MS_CLOCK_MASK


# ---------- Process liveness ----------
# Foundry's supervision contract: /proc/<name>/heartbeat_ms holds a 31-bit
# monotonic_ms() stamp and /proc/<name>/hb_seq advances on every beat.

def publish_heartbeat(name: str) -> int:
    """Write one heartbeat for ``name`` and return its stamp."""
    stamp = monotonic_ms()
    set_int(f"/proc/{name}/heartbeat_ms", stamp)
    seq_key = f"/proc/{name}/hb_seq"
    set_int(seq_key, (get_int(seq_key, 0) + 1) & MS_CLOCK_MASK)
    return stamp


class Heartbeat:
    """Rate-limited heartbeat publisher for one worker's main loop.

    Call ``tick(now)`` every loop iteration (including wait/reconnect loops);
    it publishes at most once per ``interval_s``. ``on_beat`` runs after each
    publication for optional side effects such as diagnostic counters.
    """

    def __init__(self, name: str, interval_s: float = 1.0, *, on_beat=None, publish=None):
        self.name = str(name)
        self.interval_s = max(0.05, float(interval_s))
        self._on_beat = on_beat
        self._publish = publish if publish is not None else publish_heartbeat
        self.last_at = None

    @property
    def next_due(self) -> float:
        return float("-inf") if self.last_at is None else self.last_at + self.interval_s

    def tick(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else float(now)
        if self.last_at is not None and now - self.last_at < self.interval_s:
            return False
        self.beat(now)
        return True

    def beat(self, now: float | None = None) -> None:
        self.last_at = time.monotonic() if now is None else float(now)
        self._publish(self.name)
        if self._on_beat is not None:
            self._on_beat(self.name)


# ---------- Frame integrity ----------
def begin_frame(seq_key: str = "/frame/seq"):
    """Increment and publish a sequence number at frame start."""
    s = (get_int(seq_key, 0) + 1) & 0x7FFFFFFF
    set_int(seq_key, s)
    return s


def end_frame(seq2_key: str = "/frame/seq2", value: int | None = None):
    """Publish a matching end-of-frame sequence number."""
    if value is None:
        value = (get_int(seq2_key, 0) + 1) & 0x7FFFFFFF
    set_int(seq2_key, value)
    return value


def wait_consistent(seq_key: str = "/frame/seq", seq2_key: str = "/frame/seq2", spins: int = 1000, sleep_s: float = 0.0004):
    """Read until matching frame stamps are observed, or return the last one."""
    for _ in range(spins):
        a = get_int(seq_key, 0)
        b = get_int(seq2_key, 0)
        if a == b and a != 0:
            return a
        time.sleep(sleep_s)
    return get_int(seq_key, 0)


def close_all() -> None:
    """Close cached descriptors. Deliberately does not unlink named segments."""
    for h in list(_HANDLE_CACHE.values()):
        try:
            h.close()
        except Exception:
            pass
    _HANDLE_CACHE.clear()
