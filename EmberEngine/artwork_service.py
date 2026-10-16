#!/usr/bin/env python3
"""Local, image-only HTTP origin for Discord Rich Presence album artwork.

Optional (``enabled: false`` by default). Serves the covers Minstrel stored in
artwork_cache.py, and nothing else, on ``http://127.0.0.1:<port>``. A Cloudflare
Tunnel (configured by the operator, not by Ember Deck) publishes that address
as the configured ``public_base_url`` (for example
``https://artwork.example.org``), so Discord fetches image bytes and never sees
Plex, its address or its token. Cached covers stay available while Plex is
down, as long as the Pi and this service are up.

Requests (``/{album}-{artist}.png?v={version}``):

* ``GET``/``HEAD`` with a well-formed name and version that exist: 200,
  ``Content-Type: image/png``, ``Cache-Control: public, max-age=31536000,
  immutable`` (the bytes behind a version never change, so Cloudflare and
  Discord may keep them for up to a year).
* Malformed name, missing/malformed ``v`` or other query parameters: 400.
  Unknown or evicted artwork: 404. Other methods: 405. Too many concurrent
  requests: 503. Unexpected failure: 500.
* Every unsuccessful response is empty and carries ``Cache-Control:
  no-store`` so no error is ever cached.

No request causes a Plex request, a download, a write, a redirect or a file
lookup outside the cache's ``names``/``objects`` entries. At most
``MAX_CONCURRENT`` requests run at once, with a short socket timeout, and
requests are not logged.

Foundry runs this worker with ``restart_scope="worker"``: it owns no hardware
and its failures restart only itself. Rich Presence uses its artwork only
while ``/artwork/service_ready`` is 1 and its heartbeat is fresh.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl
from typing import Any, Dict, Optional, Tuple

import synapse as bus
from artwork_cache import MAX_SLUG_CHARS, SLUG_PATTERN, VERSION_PATTERN, ArtworkCache
from runtime_config import ConfigError, load_yaml_mapping, validate_artwork_service
from whisper_daemon import log_error, log_heartbeat, log_info

DEFAULTS: Dict[str, Any] = {
    "proc_name": "artwork_service",
    "enabled": False,
    "port": 8765,
    "public_base_url": None,          # required when enabled; no built-in hostname
    "cache_dir": "~/.cache/emberdeck/artwork",
    "max_cache_mb": 2048,
}

LISTEN_HOST = "127.0.0.1"   # never configurable: the tunnel connects locally
MAX_CONCURRENT = 8
MAX_REQUEST_TARGET = 512
CACHE_SUCCESS = "public, max-age=31536000, immutable"
CACHE_FAILURE = "no-store"
REQUEST_TIMEOUT_S = 10.0
KEY_READY = "/artwork/service_ready"
CONFIG_FILE = "artwork_service.yaml"


# ------------------------------------------------------------------ configuration

def load_cfg(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and validate artwork_service.yaml; a missing file means disabled."""
    if path is None:
        raw = os.environ.get("CONFIG_PATH")
        path = Path(raw) if raw else Path(__file__).resolve().parent / "configs" / CONFIG_FILE
    loaded = load_yaml_mapping(path, "artwork_service") if path.is_file() else {}
    cfg = dict(DEFAULTS)
    cfg.update(loaded)
    validate_artwork_service(cfg)
    return cfg


def cache_from_cfg(cfg: Dict[str, Any]) -> Optional[ArtworkCache]:
    """The shared cache when the service is enabled (with a public base URL), else None."""
    if not cfg.get("enabled") or not cfg.get("public_base_url"):
        return None
    return ArtworkCache(Path(os.path.expanduser(str(cfg["cache_dir"]))), str(cfg["public_base_url"]),
                        max_bytes=int(cfg["max_cache_mb"]) * 1024 * 1024)


def cache_for_config_dir(config_dir: Path) -> Optional[ArtworkCache]:
    """For Minstrel: the cache described by ``<config_dir>/artwork_service.yaml``, or None."""
    return cache_from_cfg(load_cfg(Path(config_dir) / CONFIG_FILE))


# ------------------------------------------------------------------ HTTP

class ArtworkHandler(BaseHTTPRequestHandler):
    server_version = "artwork"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_S

    def version_string(self) -> str:
        return self.server_version  # no Python/OS version banner

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
        return  # no request logging (album names and client addresses stay out of the journal)

    def _plain(self, status: int, extra: Optional[Dict[str, str]] = None) -> None:
        """An empty response (errors never carry a body)."""
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", CACHE_FAILURE)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        # Replaces the default HTML error page.
        self.close_connection = True
        self._plain(code)

    @staticmethod
    def parse_target(target: str) -> Tuple[int, Optional[Tuple[str, str]]]:
        """``(status, (slug, version))``: 200 with a lookup, or 400/404 without one."""
        if len(target) > MAX_REQUEST_TARGET or "#" in target:
            return 400, None
        path, _, query = target.partition("?")
        if not (path.startswith("/") and path.endswith(".png")) or "/" in path[1:]:
            return 404, None
        slug = path[1:-4]
        try:
            params = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query))
        except ValueError:
            return 400, None
        if len(params) != 1 or params[0][0] != "v":
            return 400, None
        version = params[0][1]
        if not SLUG_PATTERN.fullmatch(slug) or len(slug) > MAX_SLUG_CHARS or not VERSION_PATTERN.fullmatch(version):
            return 400, None
        return 200, (slug, version)

    def _image(self, head: bool) -> None:
        if not self.server.slots.acquire(blocking=False):
            self.close_connection = True
            self._plain(503, {"Retry-After": "5"})
            return
        try:
            status, lookup = self.parse_target(self.path)
            if lookup is None:
                self._plain(status)
                return
            data = self.server.cache.read(*lookup)
            if data is None:
                self._plain(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", CACHE_SUCCESS)
            self.send_header("ETag", f'"{lookup[1]}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            try:
                self._plain(500)
            except OSError:
                pass
        finally:
            self.server.slots.release()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._image(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._image(head=True)

    def _not_allowed(self) -> None:
        self.close_connection = True
        self._plain(405, {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed


class ArtworkServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cache: ArtworkCache, port: int):
        self.cache = cache
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        super().__init__((LISTEN_HOST, int(port)), ArtworkHandler)


# ------------------------------------------------------------------ worker

def main() -> None:
    cfg = load_cfg()
    proc_name = str(cfg["proc_name"])
    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    heartbeat = bus.Heartbeat(proc_name, on_beat=log_heartbeat)
    bus.set_int(KEY_READY, 0)
    log_info(proc_name, "started")
    server: Optional[ArtworkServer] = None
    cache = cache_from_cfg(cfg)
    next_attempt = 0.0
    retry_s = 5.0
    try:
        while not stopping.is_set():
            now = time.monotonic()
            heartbeat.tick(now)
            if cache is not None and server is None and now >= next_attempt:
                try:
                    server = ArtworkServer(cache, int(cfg["port"]))
                    threading.Thread(target=server.serve_forever, name="artwork-http", daemon=True).start()
                    bus.set_int(KEY_READY, 1)
                    retry_s = 5.0
                    log_info(proc_name, "artwork_service_listening")
                except OSError as exc:
                    server = None
                    log_error(proc_name, "Artwork service could not listen",
                              f"127.0.0.1:{int(cfg['port'])} unavailable ({type(exc).__name__}); retrying",
                              event="artwork_service_bind_failed", dedupe_key=f"{proc_name}:bind")
                    next_attempt = now + retry_s
                    retry_s = min(retry_s * 2.0, 300.0)
            stopping.wait(0.5)
    finally:
        bus.set_int(KEY_READY, 0)
        if server is not None:
            server.shutdown()
            server.server_close()
        bus.close_all()
        log_info(proc_name, "stopped")


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2) from None
