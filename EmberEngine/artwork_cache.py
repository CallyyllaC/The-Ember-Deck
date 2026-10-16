"""Persistent artwork store behind Discord's Plex album covers.

Minstrel already downloads each Plex cover (through the authenticated Plex
transcoder) for Ember UI. With the artwork service enabled it hands those
bytes here once; artwork_service.py serves the result to Discord through a
Cloudflare Tunnel under the configured public base URL::

    https://artwork.example.org/{album}-{artist}.png?v={version}

* ``{album}-{artist}`` is a readable, deterministic slug (``slugify``).
  These URLs are deliberately public and guessable from album metadata.
* ``version`` is the first 12 hex digits of SHA-256 over the stored PNG, so
  it comes from the image itself: identical artwork keeps its URL, changed
  artwork gets a new one (no cache purge needed), and it is the same after a
  restart. The full digest is the internal identity.

Layout under ``cache_dir`` (directory 0700, files 0600, atomic writes):

* ``objects/<sha256>.png``: the image, stored once however many albums
  share it. Its modification time is "last used".
* ``names/<slug>.<version>``: a published URL; the file holds the full
  SHA-256 of its object. Editions or albums whose slugs collide get
  different versions, so neither overwrites the other, and a URL always
  resolves to the image its version names (or to nothing).
* ``sources/<sha256 of the Plex response>``: the object made from that
  download, so a cover is converted once, not on every track of an album
  and not again after a restart.
* ``.lock``: serialises writers.

There is no index file to corrupt: the directories are the index, rebuilt by
listing them. Nothing here holds Plex URLs, IDs, tokens or server addresses.

Eviction: no routine expiry. When stored objects exceed ``max_bytes``
(2 GB by default, enough for thousands of covers) the least recently used
objects are removed, never the current one, together with every name and
source pointing at them. Their URLs then return 404 until Minstrel stores
the artwork again (same image, same URL).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import secrets
import stat
import time
import unicodedata
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

IMAGE_SIZE = 512
MAX_IMAGE_BYTES = 4_000_000        # a 512×512 RGBA PNG is at most ~1 MB; covers are usually 200–600 kB
MAX_SOURCE_BYTES = 20_000_000      # refuse absurd downloads before decoding
VERSION_CHARS = 12                 # 48 bits of the content hash in the URL
MAX_SLUG_CHARS = 100
ALBUM_SLUG_CHARS = 60
ARTIST_SLUG_CHARS = 40

SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION_PATTERN = re.compile(rf"[0-9a-f]{{{VERSION_CHARS}}}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")

# Letters that Unicode decomposition does not reduce to ASCII.
_TRANSLITERATION = str.maketrans({
    "ß": "ss", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d",
    "ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "ł": "l", "Ł": "l", "ı": "i", "ħ": "h", "Ħ": "h",
    "&": " and ", "+": " plus ", "@": " at ",
})


class ArtworkCacheError(RuntimeError):
    """Storage problem; messages never contain image data, URLs or tokens."""


def _slug_part(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).translate(_TRANSLITERATION)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"['’`]", "", value)              # "don't" -> "dont"
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if len(value) > limit:
        cut = value[:limit]
        value = cut.rsplit("-", 1)[0] if "-" in cut[limit // 2:] else cut
        value = value.strip("-")
    return value


def slugify(album: Optional[str], artist: Optional[str]) -> str:
    """``{album}-{artist}``: lowercase ASCII words joined by single hyphens.

    Deterministic. Accents are folded (Motörhead -> motorhead), common
    ligatures transliterated, punctuation removed. Text with no Latin
    equivalent (e.g. CJK) contributes nothing; if nothing usable remains the
    slug is ``unknown-album`` / ``unknown-artist`` style placeholders.
    """
    album_part = _slug_part(album, ALBUM_SLUG_CHARS) or "unknown-album"
    artist_part = _slug_part(artist, ARTIST_SLUG_CHARS)
    slug = f"{album_part}-{artist_part}" if artist_part else album_part
    return slug[:MAX_SLUG_CHARS].strip("-")


def prepare_png(source: bytes) -> bytes:
    """A square 512×512 PNG from any image Pillow can read, without distortion.

    The image is scaled to fit and centred on a transparent square, so
    non-square artwork keeps its aspect ratio. Metadata is not carried over.
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(source)) as image:
        image.load()
        rgba = image.convert("RGBA")
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    fitted = ImageOps.contain(rgba, (IMAGE_SIZE, IMAGE_SIZE), method=resampling)
    canvas = Image.new("RGBA", (IMAGE_SIZE, IMAGE_SIZE), (0, 0, 0, 0))
    canvas.paste(fitted, ((IMAGE_SIZE - fitted.width) // 2, (IMAGE_SIZE - fitted.height) // 2))
    if fitted.size == (IMAGE_SIZE, IMAGE_SIZE) and rgba.getextrema()[3] == (255, 255):
        canvas = canvas.convert("RGB")  # opaque square covers need no alpha channel
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


class ArtworkCache:
    def __init__(self, cache_dir: Path, public_base_url: str, *, max_bytes: int = 2048 * 1024 * 1024,
                 clock: Callable[[], float] = time.time):
        self.cache_dir = Path(os.path.expanduser(str(cache_dir)))
        self.public_base_url = str(public_base_url).rstrip("/")
        self.max_bytes = int(max_bytes)
        self.clock = clock

    # -- paths
    @property
    def objects(self) -> Path:
        return self.cache_dir / "objects"

    @property
    def names(self) -> Path:
        return self.cache_dir / "names"

    @property
    def sources(self) -> Path:
        return self.cache_dir / "sources"

    def url_for(self, slug: str, version: str) -> str:
        return f"{self.public_base_url}/{slug}.png?v={version}"

    # -- storing (Minstrel)
    def store_cover(self, source: bytes, album: Optional[str], artist: Optional[str]) -> Optional[str]:
        """Store a downloaded cover under its album/artist name; return the public URL.

        Converts only the first time these exact source bytes are seen; later
        tracks of the album (and later runs) just mark the cover as used.
        Returns None for empty, oversized or unreadable images.
        """
        if not source or len(source) > MAX_SOURCE_BYTES:
            return None
        slug = slugify(album, artist)
        source_key = hashlib.sha256(source).hexdigest()
        try:
            with self._writer():
                digest = self._read_digest(self.sources / source_key)
                if digest is None or not (self.objects / f"{digest}.png").is_file():
                    try:
                        png = prepare_png(source)
                    except Exception:
                        return None  # malformed or unsupported image
                    if len(png) > MAX_IMAGE_BYTES:
                        return None
                    digest = hashlib.sha256(png).hexdigest()
                    target = self.objects / f"{digest}.png"
                    if not target.is_file():
                        self._write_private(target, png)
                    self._write_private(self.sources / source_key, digest.encode("ascii"))
                version = digest[:VERSION_CHARS]
                name = self.names / f"{slug}.{version}"
                if self._read_digest(name) != digest:
                    self._write_private(name, digest.encode("ascii"))
                self._touch(self.objects / f"{digest}.png")
                self._evict(keep=digest)
        except OSError as exc:
            raise ArtworkCacheError(f"artwork cache write failed ({type(exc).__name__})") from None
        return self.url_for(slug, version)

    # -- reading (artwork service; never writes)
    def read(self, slug: str, version: str) -> Optional[bytes]:
        """The PNG a published URL names, or None (unknown, evicted, invalid or corrupt)."""
        if not isinstance(slug, str) or len(slug) > MAX_SLUG_CHARS or not SLUG_PATTERN.fullmatch(slug):
            return None
        if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
            return None
        digest = self._read_digest(self.names / f"{slug}.{version}")
        if digest is None or not digest.startswith(version):
            return None
        path = self.objects / f"{digest}.png"
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_IMAGE_BYTES:
                return None
            with open(path, "rb") as handle:
                data = handle.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != digest:
            return None  # never serve the wrong (or a damaged) image for a URL
        return data

    # -- usage
    def usage(self) -> Tuple[int, int, int]:
        """``(objects, bytes, names)`` currently stored."""
        objects = [p for p in self._listing(self.objects) if p.name.endswith(".png")]
        return (len(objects), sum(p.stat().st_size for p in objects),
                len(list(self._listing(self.names))))

    # -- eviction
    def _cleanup(self) -> None:
        """Remove interrupted writes and files of the superseded random-ID layout."""
        now = self.clock()
        for directory in (self.objects, self.names, self.sources):
            for path in self._listing(directory):
                if path.name.startswith(".tmp-") and now - path.stat().st_mtime > 60:
                    path.unlink(missing_ok=True)
        for path in self._listing(self.cache_dir):
            if path.is_file() and (path.name == ".key" or re.fullmatch(r"[0-9a-f]{32}\.jpg|\.tmp-.*", path.name)):
                path.unlink(missing_ok=True)

    def _evict(self, keep: Optional[str]) -> None:
        self._cleanup()
        entries = []
        for path in self._listing(self.objects):
            if not (path.name.endswith(".png") and DIGEST_PATTERN.fullmatch(path.name[:-4])):
                continue
            st = path.lstat()
            entries.append((st.st_mtime, st.st_size, path.name[:-4], path))
        total = sum(size for _, size, _, _ in entries)
        if total <= self.max_bytes:
            return
        evicted = set()
        for _mtime, size, digest, path in sorted(entries):  # least recently used first
            if total <= self.max_bytes:
                break
            if digest == keep:
                continue
            path.unlink(missing_ok=True)
            evicted.add(digest)
            total -= size
        for directory in (self.names, self.sources):
            for path in self._listing(directory):
                if self._read_digest(path) in evicted:
                    path.unlink(missing_ok=True)

    # -- filesystem helpers
    @staticmethod
    def _listing(directory: Path) -> Iterator[Path]:
        try:
            yield from directory.iterdir()
        except FileNotFoundError:
            return

    @staticmethod
    def _read_digest(path: Path) -> Optional[str]:
        try:
            with open(path, "rb") as handle:
                text = handle.read(65).decode("ascii", "replace").strip()
        except OSError:
            return None
        return text if DIGEST_PATTERN.fullmatch(text) else None

    def _touch(self, path: Path) -> None:
        now = self.clock()
        os.utime(path, (now, now))

    def _ensure_dirs(self) -> None:
        for directory in (self.cache_dir, self.objects, self.names, self.sources):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    @contextlib.contextmanager
    def _writer(self) -> Iterator[None]:
        """Exclusive writer lock (POSIX flock); readers never need it."""
        self._ensure_dirs()
        fd = os.open(self.cache_dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl
            except ImportError:  # Windows development machines: single writer by convention
                fcntl = None
            flock = getattr(fcntl, "flock", None)
            if flock is not None:
                flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def _write_private(self, path: Path, data: bytes) -> None:
        tmp = path.parent / f".tmp-{secrets.token_hex(8)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
