"""
synapse.py
Shared-memory helper using per-variable segments. No classes, minimal ceremony.

- Each key (e.g., "/leds/main/mode") maps to one SharedMemory segment.
- First write fixes the size; readers pass the expected length for arrays.
- Handles are cached to avoid reopen/close every frame.
- Arrays accept NumPy or Python lists. Writes use memoryview to avoid extra copies.
- Optional double-stamp helpers for frame integrity (seq/seq2).

Keys are normalized to small ASCII names internally:
  "/leds/main/mode" -> "smem__/leds__main__mode"
"""
from multiprocessing import shared_memory
import struct
import time
from typing import Dict

# Optional NumPy support for faster array handling
try:
    import numpy as _np
except Exception:  # numpy not required at import time
    _np = None

# Cache of opened SharedMemory handles so we don't thrash the kernel
_HANDLE_CACHE: Dict[str, shared_memory.SharedMemory] = {}


def _norm(name: str) -> str:
    """Normalize a human path-like key to a SHM-safe, short name."""
    return ("smem__" + name.strip().replace(" ", "_").replace("/", "__"))[:254]


def _get_handle(name: str, size: int, create: bool):
    """
    Return a cached handle; create if requested.
    Note: size is only used on first creation; segments are not resized later.
    """
    norm = _norm(name)
    h = _HANDLE_CACHE.get(norm)
    if h is not None:
        return h
    try:
        h = shared_memory.SharedMemory(name=norm, create=create, size=size if create else 0)
    except FileExistsError:
        h = shared_memory.SharedMemory(name=norm, create=False)
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
def get_array(name: str, length: int, dtype: str = "f32"):
    """
    Read a fixed-length array as a Python list.
    dtype: 'f32' (float32) or 'u8' (byte).
    """
    if dtype == "f32":
        item_size, fmt = 4, f"<{length}f"
    elif dtype == "u8":
        item_size, fmt = 1, None
    else:
        raise ValueError("dtype must be 'f32' or 'u8'")

    size = length * item_size
    h = _get_handle(name, size, create=False)

    if dtype == "u8":
        return list(bytes(h.buf[:size]))
    return list(struct.unpack_from(fmt, h.buf, 0))


def set_array(name: str, values, dtype: str = "f32") -> None:
    """
    Write an array; first call fixes the segment size permanently.
    Accepts lists or NumPy arrays. Uses memoryview to avoid extra packing copies.
    """
    if dtype == "f32":
        if _np is not None:
            arr = _np.asarray(values, dtype=_np.float32)
            payload = memoryview(arr).cast("B")
            size = arr.nbytes
        else:
            vals = [float(v) for v in values]
            payload = struct.pack(f"<{len(vals)}f", *vals)
            size = len(vals) * 4

    elif dtype == "u8":
        if _np is not None:
            arr = _np.asarray(values, dtype=_np.uint8)
            payload = memoryview(arr).cast("B")
            size = arr.nbytes
        else:
            vals = [int(max(0, min(255, v))) for v in values]
            payload = bytes(vals)
            size = len(vals)
    else:
        raise ValueError("dtype must be 'f32' or 'u8'")

    h = _get_handle(name, size, create=True)
    h.buf[:size] = payload


# ---------- Frame integrity ----------
def begin_frame(seq_key="/frame/seq"):
    """Increment and publish a sequence number at frame start (writer-side)."""
    s = (get_int(seq_key, 0) + 1) & 0x7FFFFFFF
    set_int(seq_key, s)
    return s


def end_frame(seq2_key="/frame/seq2", value: int = None):
    """Publish matching end-of-frame sequence number (writer-side)."""
    if value is None:
        value = (get_int(seq2_key, 0) + 1) & 0x7FFFFFFF
    set_int(seq2_key, value)
    return value


def wait_consistent(seq_key="/frame/seq", seq2_key="/frame/seq2", spins=1000, sleep_s=0.0004):
    """
    Reader-side: spin until seq and seq2 match, indicating a completed frame.
    Returns the stable sequence or last seen value after spins.
    """
    for _ in range(spins):
        a = get_int(seq_key, 0)
        b = get_int(seq2_key, 0)
        if a == b and a != 0:
            return a
        time.sleep(sleep_s)
    return get_int(seq_key, 0)


def close_all():
    """Close all cached SharedMemory handles. Call on process shutdown."""
    for h in list(_HANDLE_CACHE.values()):
        try:
            h.close()
        except Exception:
            pass
    _HANDLE_CACHE.clear()
