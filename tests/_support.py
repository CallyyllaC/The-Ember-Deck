"""Shared fakes for hardware-free Ember Deck tests.

Workers talk to each other only through Synapse and Whisper, so most
behavioural contracts can be exercised by swapping a module's ``bus`` for an
in-memory dictionary and capturing Whisper messages instead of printing them.
Optional third-party packages (GPIO, audio, BlinkStick, plexapi) are replaced
with inert stand-ins only when the real package is not installed.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Dict, Iterator, List
from unittest import mock


ENGINE = Path(__file__).resolve().parents[1] / "EmberEngine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import synapse  # noqa: E402
import whisper_daemon  # noqa: E402


def has_module(name: str) -> bool:
    """Return whether the real package (not one of our stubs) is importable."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return not getattr(loaded, "__ember_stub__", False)
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class FakeBus:
    """In-memory stand-in for the ``synapse`` module API used by workers."""

    MS_CLOCK_MASK = synapse.MS_CLOCK_MASK
    SharedMemoryLayoutError = synapse.SharedMemoryLayoutError

    def __init__(self, initial: Dict[str, object] | None = None):
        self.values: Dict[str, object] = dict(initial or {})
        self.writes: List[str] = []

    # Scalars
    def get_int(self, name: str, default: int = 0) -> int:
        return int(self.values.get(name, default))

    def set_int(self, name: str, value: int) -> None:
        value = int(value)
        if not -(2 ** 31) <= value < 2 ** 31:
            # Mirror struct.pack_into("<i") so overflow bugs surface in tests.
            raise OverflowError(f"{name} value {value} does not fit a signed 32-bit int")
        self.values[name] = value
        self.writes.append(name)

    def get_float(self, name: str, default: float = 0.0) -> float:
        return float(self.values.get(name, default))

    def set_float(self, name: str, value: float) -> None:
        self.values[name] = float(value)
        self.writes.append(name)

    # Arrays
    def try_get_array(self, name: str, length: int, dtype: str = "f32"):
        if name not in self.values:
            return None
        stored = list(self.values[name])
        if len(stored) < length:
            raise synapse.SharedMemoryLayoutError(f"{name} holds {len(stored)} items, reader needs {length}")
        return stored[:length]

    def set_array(self, name: str, values, dtype: str = "f32") -> None:
        items = [float(v) for v in values] if dtype == "f32" else [int(v) for v in values]
        previous = self.values.get(name)
        if isinstance(previous, list) and len(previous) < len(items):
            raise synapse.SharedMemoryLayoutError(f"{name} holds {len(previous)} items, writer needs {len(items)}")
        if isinstance(previous, list) and len(previous) > len(items):
            items = items + previous[len(items):]
        self.values[name] = items
        self.writes.append(name)

    # Helpers delegated to the real implementation
    monotonic_ms = staticmethod(synapse.monotonic_ms)
    elapsed_ms = staticmethod(synapse.elapsed_ms)

    def publish_heartbeat(self, name: str) -> int:
        stamp = self.monotonic_ms()
        self.set_int(f"/proc/{name}/heartbeat_ms", stamp)
        seq_key = f"/proc/{name}/hb_seq"
        self.set_int(seq_key, (self.get_int(seq_key, 0) + 1) & synapse.MS_CLOCK_MASK)
        return stamp

    def Heartbeat(self, name: str, interval_s: float = 1.0, *, on_beat=None):  # noqa: N802 - mirrors synapse.Heartbeat
        return synapse.Heartbeat(name, interval_s, on_beat=on_beat, publish=self.publish_heartbeat)

    def wait_consistent(self, *args, **kwargs) -> int:
        return self.get_int("/audio/seq", 0)

    def close_all(self) -> None:
        pass

    def text(self, name: str) -> str:
        raw = bytes(int(v) for v in self.values.get(name, []))
        return raw.split(b"\x00", 1)[0].decode("utf-8")


@contextlib.contextmanager
def capture_whisper() -> Iterator[list]:
    """Collect SpiritMessages that workers publish instead of printing them."""
    messages: list = []
    with mock.patch.object(whisper_daemon, "publish", messages.append):
        yield messages


def _stub(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    module.__ember_stub__ = True
    return module


class _StubButton:
    def __init__(self, *args, **kwargs):
        self.is_pressed = False


def _hardware_stubs() -> Dict[str, types.ModuleType]:
    """Inert modules for imports that only exist on the Raspberry Pi."""
    plexapi = _stub("plexapi")
    stubs = {
        "fcntl": _stub("fcntl", ioctl=None),  # POSIX-only; absent on Windows dev machines
        "board": _stub("board", SCL=None, SDA=None),
        "busio": _stub("busio", I2C=None),
        "gpiozero": _stub("gpiozero", Button=_StubButton),
        "adafruit_ads1x15": _stub("adafruit_ads1x15", ADS1115=None, AnalogIn=None, ads1x15=None),
        "adafruit_ht16k33": _stub("adafruit_ht16k33"),
        "adafruit_ht16k33.matrix": _stub("adafruit_ht16k33.matrix", Matrix16x8=None),
        "sounddevice": _stub("sounddevice", InputStream=None),
        "blinkstick": _stub("blinkstick", blinkstick=_stub("blinkstick.blinkstick", BlinkStick=object)),
        "plexapi": plexapi,
        "plexapi.myplex": _stub("plexapi.myplex", MyPlexAccount=None),
        "plexapi.server": _stub("plexapi.server", PlexServer=None),
        "plexapi.exceptions": _stub("plexapi.exceptions", PlexApiException=Exception),
    }
    class _Widget:
        def __init__(self, *args, **kwargs):
            pass

        def __class_getitem__(cls, item):
            return cls

    stubs.update({
        "textual": _stub("textual"),
        "textual.app": _stub("textual.app", App=_Widget, ComposeResult=object),
        "textual.containers": _stub("textual.containers", Container=_Widget),
        "textual.widget": _stub("textual.widget", Widget=_Widget),
        "textual.widgets": _stub("textual.widgets", Static=_Widget),
        "rich": _stub("rich"),
        "rich.cells": _stub("rich.cells", cell_len=len),
        "rich.text": _stub("rich.text", Text=_Widget),
        "rich.markup": _stub("rich.markup", escape=lambda text: text),
    })
    if not has_module("yaml"):
        stubs["yaml"] = _stub("yaml", safe_load=lambda *_: {}, safe_dump=lambda *_, **__: None)
    return stubs


def import_engine_module(name: str):
    """Import an EmberEngine module, stubbing only unavailable hardware/service packages."""
    if name in sys.modules:
        return sys.modules[name]
    missing = {key: value for key, value in _hardware_stubs().items()
               if key not in sys.modules and not has_module(key.split(".")[0])}
    # Stub entries are left in place: other worker modules expect the same objects.
    sys.modules.update(missing)
    return importlib.import_module(name)
