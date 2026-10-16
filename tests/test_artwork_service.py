"""Discord Plex artwork: persistent cache, readable versioned URLs, image-only HTTP origin.

The HTTP tests run the real ThreadingHTTPServer on an ephemeral loopback port.
Nothing here talks to Plex, Cloudflare or Discord.
"""

import hashlib
import http.client
import io
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, has_module  # noqa: E402

import artwork_cache  # noqa: E402
import runtime_config  # noqa: E402

HAS_PIL = has_module("PIL")
BASE = "https://artwork.example.org"
URL_PATTERN = re.compile(r"^https://artwork\.example\.org/([a-z0-9]+(?:-[a-z0-9]+)*)\.png\?v=([0-9a-f]{12})$")


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def cover(colour=(200, 40, 40), size=(640, 640), fmt="JPEG"):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


def noisy_cover(seed, size=(512, 512)):
    """Incompressible content, so PNG sizes are large and predictable."""
    from PIL import Image
    image = Image.frombytes("RGB", size, hashlib.shake_256(str(seed).encode()).digest(size[0] * size[1] * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_cache(test, clock=None, **kwargs):
    tmp = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, tmp, True)
    return artwork_cache.ArtworkCache(tmp / "artwork", BASE, clock=clock or Clock(), **kwargs), tmp


def parts(url):
    match = URL_PATTERN.match(url)
    assert match, url
    return match.group(1), match.group(2)


class SlugTests(unittest.TestCase):
    def test_album_first_then_artist(self):
        slug = artwork_cache.slugify
        self.assertEqual(slug("Come Clarity", "In Flames"), "come-clarity-in-flames")
        self.assertEqual(slug("Last Fair Deal Gone Down", "Katatonia"), "last-fair-deal-gone-down-katatonia")

    def test_unicode_and_punctuation(self):
        slug = artwork_cache.slugify
        cases = {
            ("Ace of Spades", "Motörhead"): "ace-of-spades-motorhead",
            ("Ágætis byrjun", "Sigur Rós"): "agaetis-byrjun-sigur-ros",
            ("Straße", "Kraftwerk"): "strasse-kraftwerk",
            ("Œuvres", "Björk"): "oeuvres-bjork",
            ("Don't Stop Me Now!!", "Queen"): "dont-stop-me-now-queen",
            ("Rock & Roll", "AC/DC"): "rock-and-roll-ac-dc",
            ("  ...And Justice for All  ", "Metallica"): "and-justice-for-all-metallica",
            ("ｆｕｌｌｗｉｄｔｈ", "Ｔｅｓｔ"): "fullwidth-test",
            ("A -- B", "C__D"): "a-b-c-d",
        }
        for (album, artist), expected in cases.items():
            with self.subTest(album=album):
                self.assertEqual(slug(album, artist), expected)
                self.assertRegex(slug(album, artist), r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_missing_non_latin_and_long_metadata(self):
        slug = artwork_cache.slugify
        self.assertEqual(slug(None, None), "unknown-album")
        self.assertEqual(slug("", "Artist"), "unknown-album-artist")
        self.assertEqual(slug("Album", None), "album")
        self.assertEqual(slug("千と千尋", "久石譲"), "unknown-album")
        self.assertEqual(slug("千と千尋 OST", "Joe Hisaishi"), "ost-joe-hisaishi")
        long = slug("word " * 80, "artist " * 40)
        self.assertLessEqual(len(long), artwork_cache.MAX_SLUG_CHARS)
        self.assertRegex(long, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertEqual(slug("word " * 80, "artist " * 40), long, "deterministic")


@unittest.skipUnless(HAS_PIL, "Pillow is required")
class ArtworkCacheTests(unittest.TestCase):
    def test_url_format_and_content_version(self):
        cache, _ = make_cache(self)
        url = cache.store_cover(cover(), "Come Clarity", "In Flames")
        slug, version = parts(url)
        self.assertEqual(slug, "come-clarity-in-flames")
        png = cache.read(slug, version)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(hashlib.sha256(png).hexdigest()[:12], version, "version is the image's own hash")

    def test_identical_artwork_same_url_and_changed_artwork_new_version(self):
        cache, _ = make_cache(self)
        first = cache.store_cover(cover(), "Album", "Artist")
        self.assertEqual(cache.store_cover(cover(), "Album", "Artist"), first)
        changed = cache.store_cover(cover((10, 200, 10)), "Album", "Artist")
        self.assertEqual(parts(changed)[0], parts(first)[0])
        self.assertNotEqual(parts(changed)[1], parts(first)[1])
        self.assertIsNotNone(cache.read(*parts(first)), "the old version still resolves to the old image")
        self.assertNotEqual(cache.read(*parts(first)), cache.read(*parts(changed)))

    def test_editions_and_slug_collisions_never_overwrite(self):
        cache, _ = make_cache(self)
        standard = cache.store_cover(cover((1, 2, 3)), "Album!", "Artist")
        deluxe = cache.store_cover(cover((200, 100, 50)), "Album?", "Artist")  # same slug, other cover
        self.assertEqual(parts(standard)[0], parts(deluxe)[0])
        self.assertNotEqual(standard, deluxe)
        self.assertEqual(hashlib.sha256(cache.read(*parts(standard))).hexdigest()[:12], parts(standard)[1])
        self.assertEqual(hashlib.sha256(cache.read(*parts(deluxe))).hexdigest()[:12], parts(deluxe)[1])
        self.assertIsNone(cache.read(parts(standard)[0], "0" * 12), "an unknown version never falls back")

    def test_identical_artwork_is_stored_once(self):
        cache, _ = make_cache(self)
        a = cache.store_cover(cover(), "Album", "Artist")
        b = cache.store_cover(cover(), "Compilation", "Various Artists")
        self.assertEqual(parts(a)[1], parts(b)[1])
        objects, _bytes, names = cache.usage()
        self.assertEqual((objects, names), (1, 2))

    def test_conversion_happens_once_per_cover_even_across_restarts(self):
        cache, tmp = make_cache(self)
        source = cover()
        with mock.patch.object(artwork_cache, "prepare_png", wraps=artwork_cache.prepare_png) as prepare:
            url = cache.store_cover(source, "Album", "Artist")
            cache.store_cover(source, "Album", "Artist")       # next track of the album
            restarted = artwork_cache.ArtworkCache(tmp / "artwork", BASE, clock=cache.clock)
            self.assertEqual(restarted.store_cover(source, "Album", "Artist"), url)
            self.assertEqual(restarted.read(*parts(url)), cache.read(*parts(url)))
        self.assertEqual(prepare.call_count, 1)

    def test_index_is_rebuilt_from_the_directories(self):
        cache, tmp = make_cache(self)
        url = cache.store_cover(cover(), "Album", "Artist")
        slug, version = parts(url)
        restarted = artwork_cache.ArtworkCache(tmp / "artwork", BASE)
        self.assertIsNotNone(restarted.read(slug, version))
        # A damaged source memo or object is detected, never served, and repaired.
        for path in (restarted.sources.iterdir()):
            path.write_bytes(b"garbage")
        digest = (restarted.names / f"{slug}.{version}").read_text()
        (restarted.objects / f"{digest}.png").write_bytes(b"corrupted")
        self.assertIsNone(restarted.read(slug, version), "a corrupt object is not served")
        (restarted.objects / f"{digest}.png").unlink()
        self.assertEqual(restarted.store_cover(cover(), "Album", "Artist"), url)
        self.assertIsNotNone(restarted.read(slug, version))

    def test_preserves_aspect_ratio_on_a_square_canvas(self):
        from PIL import Image
        cache, _ = make_cache(self)
        url = cache.store_cover(cover(size=(1000, 500)), "Wide", "Artist")
        with Image.open(io.BytesIO(cache.read(*parts(url)))) as image:
            self.assertEqual((image.format, image.size), ("PNG", (512, 512)))
            rgba = image.convert("RGBA")
            self.assertEqual(rgba.getpixel((256, 10))[3], 0, "letterbox, not stretch")
            self.assertEqual(rgba.getpixel((256, 256))[:3], (200, 40, 40))

    def test_malformed_and_oversized_images_are_refused(self):
        cache, _ = make_cache(self)
        self.assertIsNone(cache.store_cover(b"", "A", "B"))
        self.assertIsNone(cache.store_cover(b"not an image at all", "A", "B"))
        self.assertIsNone(cache.store_cover(b"\xff\xd8" + b"\x00" * (artwork_cache.MAX_SOURCE_BYTES + 1), "A", "B"))
        self.assertEqual(cache.usage()[0], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_private_permissions(self):
        cache, _ = make_cache(self)
        url = cache.store_cover(cover(), "Album", "Artist")
        for directory in (cache.cache_dir, cache.objects, cache.names, cache.sources):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in list(cache.objects.iterdir()) + list(cache.names.iterdir()):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(url)

    def test_atomic_writes_leave_no_partial_files(self):
        cache, _ = make_cache(self)
        cache.store_cover(cover(), "Album", "Artist")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(artwork_cache.ArtworkCacheError):
                cache.store_cover(cover((5, 5, 5)), "Other", "Artist")
        for directory in (cache.objects, cache.names, cache.sources):
            self.assertEqual([p.name for p in directory.iterdir() if p.name.startswith(".tmp-")], [])
        self.assertEqual(cache.usage()[0], 1, "nothing half-written was added")

    def test_size_limit_evicts_least_recently_used_and_keeps_current(self):
        clock = Clock()
        cache, _ = make_cache(self, clock=clock)
        one_size = len(artwork_cache.prepare_png(noisy_cover(0)))
        cache.max_bytes = int(one_size * 3.5)
        urls = []
        for n in range(3):
            clock.now += 10
            urls.append(cache.store_cover(noisy_cover(n), f"Album {n}", "Artist"))
        clock.now += 10
        cache.store_cover(noisy_cover(0), "Album 0", "Artist")   # album 0 played again: most recent
        clock.now += 10
        newest = cache.store_cover(noisy_cover(9), "Album 9", "Artist")
        self.assertLessEqual(cache.usage()[1], cache.max_bytes)
        self.assertIsNone(cache.read(*parts(urls[1])), "least recently used is evicted")
        self.assertFalse((cache.names / "album-1-artist.{}".format(parts(urls[1])[1])).exists())
        for kept in (urls[0], urls[2], newest):
            self.assertIsNotNone(cache.read(*parts(kept)))
        # An evicted cover is regenerated (same URL) when it plays again.
        clock.now += 10
        self.assertEqual(cache.store_cover(noisy_cover(1), "Album 1", "Artist"), urls[1])
        self.assertIsNotNone(cache.read(*parts(urls[1])))

    def test_nothing_expires_by_age(self):
        clock = Clock()
        cache, _ = make_cache(self, clock=clock)
        url = cache.store_cover(cover(), "Album", "Artist")
        clock.now += 5 * 365 * 86400
        cache.store_cover(cover((9, 9, 9)), "Other", "Artist")
        self.assertIsNotNone(cache.read(*parts(url)))

    def test_only_valid_names_and_versions_are_looked_up(self):
        cache, _ = make_cache(self)
        slug, version = parts(cache.store_cover(cover(), "Album", "Artist"))
        for bad_slug, bad_version in (("../objects", version), ("Album-Artist", version), ("", version),
                                      (slug + "/", version), ("a" * 101, version), (slug, version.upper()),
                                      (slug, version[:-1]), (slug, version + "0"), (slug, "../../x"),
                                      (None, version), (slug, None)):
            with self.subTest(slug=bad_slug, version=bad_version):
                self.assertIsNone(cache.read(bad_slug, bad_version))

    def test_legacy_random_id_files_are_removed(self):
        cache, _ = make_cache(self)
        cache.cache_dir.mkdir(parents=True)
        (cache.cache_dir / ("a" * 32 + ".jpg")).write_bytes(b"\xff\xd8old")
        (cache.cache_dir / ".key").write_bytes(b"k" * 32)
        cache.store_cover(cover(), "Album", "Artist")
        self.assertFalse((cache.cache_dir / ("a" * 32 + ".jpg")).exists())
        self.assertFalse((cache.cache_dir / ".key").exists())


@unittest.skipUnless(has_module("yaml"), "PyYAML is required")
class ArtworkServiceConfigTests(unittest.TestCase):
    def setUp(self):
        import artwork_service
        self.svc = artwork_service
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_missing_file_means_disabled(self):
        cfg = self.svc.load_cfg(self.tmp / "artwork_service.yaml")
        self.assertFalse(cfg["enabled"])
        self.assertIsNone(self.svc.cache_from_cfg(cfg))
        self.assertIsNone(self.svc.cache_for_config_dir(self.tmp))

    def test_shipped_file_is_disabled_and_valid(self):
        # The shipped template alone: a developer's artwork_service.local.yaml must not affect this.
        shutil.copy(ENGINE / "configs" / "artwork_service.yaml", self.tmp / "artwork_service.yaml")
        cfg = self.svc.load_cfg(self.tmp / "artwork_service.yaml")
        self.assertFalse(cfg["enabled"])
        self.assertEqual((cfg["port"], cfg["public_base_url"], cfg["max_cache_mb"]), (8765, None, 2048))
        self.assertIsNone(self.svc.cache_from_cfg(cfg))

    def test_enabled_config_builds_the_cache_and_old_files_still_validate(self):
        (self.tmp / "artwork_service.yaml").write_text(
            f"proc_name: artwork_service\nenabled: true\nretention_days: 7\nmax_cache_mb: 64\n"
            f"public_base_url: {BASE}\ncache_dir: {self.tmp.as_posix()}/cache\n", encoding="utf-8")
        cache = self.svc.cache_for_config_dir(self.tmp)
        self.assertIsNotNone(cache)
        self.assertEqual(cache.url_for("album-artist", "0" * 12), f"{BASE}/album-artist.png?v=000000000000")
        self.assertEqual(cache.max_bytes, 64 * 1024 * 1024)

    def test_invalid_values_are_rejected(self):
        cases = [
            ({"public_base_url": "http://artwork.example.org"}, "public_base_url"),
            ({"public_base_url": "https://artwork.example.org/images"}, "public_base_url"),
            ({"public_base_url": "https://user:pw@artwork.example.org"}, "public_base_url"),
            ({"public_base_url": "https://artwork.example.org:8443"}, "public_base_url"),
            ({"public_base_url": "https://localhost"}, "public_base_url"),
            ({"port": 80}, "port"),
            ({"port": "8765"}, "port"),
            ({"enabled": "yes"}, "enabled"),
            ({"max_cache_mb": 1}, "max_cache_mb"),
            ({"listen": "0.0.0.0"}, "unknown option"),
        ]
        for values, message in cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(runtime_config.ConfigError, message):
                    runtime_config.validate_artwork_service(dict(self.svc.DEFAULTS, **values))


@unittest.skipUnless(HAS_PIL, "Pillow is required")
class ArtworkHttpTests(unittest.TestCase):
    """The real HTTP service on 127.0.0.1 (ephemeral port)."""

    def setUp(self):
        import artwork_service
        self.svc = artwork_service
        self.cache, _ = make_cache(self)
        self.url = self.cache.store_cover(cover(), "Come Clarity", "In Flames")
        self.slug, self.version = parts(self.url)
        self.path = f"/{self.slug}.png?v={self.version}"
        self.png = self.cache.read(self.slug, self.version)
        self.server = artwork_service.ArtworkServer(self.cache, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            conn.endheaders()
            response = conn.getresponse()
            return response, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def assert_failure(self, status, path, method="GET"):
        response, body, headers = self.request(method, path)
        self.assertEqual(response.status, status, path)
        self.assertEqual(headers.get("Cache-Control"), "no-store", f"{status} must not be cached")
        self.assertEqual(body, b"")
        self.assertNotIn("image", headers.get("Content-Type", ""))
        return headers

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.svc.LISTEN_HOST, "127.0.0.1")

    def test_get_and_head_serve_the_png_with_one_year_caching(self):
        response, body, headers = self.request("GET", self.path)
        self.assertEqual((response.status, body), (200, self.png))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(headers["Content-Length"], str(len(self.png)))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Server"], "artwork", "no Python version banner")
        self.assertNotIn("Location", headers, "never a redirect")
        response, body, headers = self.request("HEAD", self.path)
        self.assertEqual((response.status, body, headers["Content-Length"]), (200, b"", str(len(self.png))))
        self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")

    def test_unknown_and_evicted_artwork_is_404_no_store(self):
        self.assert_failure(404, f"/{self.slug}.png?v=000000000000")
        self.assert_failure(404, f"/other-album-artist.png?v={self.version}")
        digest = (self.cache.names / f"{self.slug}.{self.version}").read_text()
        (self.cache.objects / f"{digest}.png").unlink()  # evicted
        self.assert_failure(404, self.path)

    def test_malformed_requests_are_400_or_404_and_never_cached(self):
        cases = {
            f"/{self.slug}.png": 400,                                   # no version
            f"/{self.slug}.png?v=": 400,
            f"/{self.slug}.png?v={self.version.upper()}": 400,
            f"/{self.slug}.png?v={self.version}&x=1": 400,              # extra parameters
            f"/{self.slug}.png?v={self.version}&v={self.version}": 400,
            f"/{self.slug.upper()}.png?v={self.version}": 400,
            f"/{self.slug}%2e%2e.png?v={self.version}": 400,
            f"/..%2f..%2f.lock.png?v={self.version}": 400,
            f"/{'a' * 120}.png?v={self.version}": 400,
            "/" + "x" * 600 + ".png?v=" + self.version: 400,
            "/": 404,
            "/.lock": 404,
            f"/objects/{self.version}.png?v={self.version}": 404,
            f"/names/{self.slug}.{self.version}": 404,
            f"/../{self.slug}.png?v={self.version}": 404,
            f"/{self.slug}.jpg?v={self.version}": 404,
            "/favicon.ico": 404,
        }
        for path, status in cases.items():
            with self.subTest(path=path[:60]):
                self.assert_failure(status, path)

    def test_other_methods_are_405_no_store(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                headers = self.assert_failure(405, self.path, method)
                self.assertEqual(headers["Allow"], "GET, HEAD")

    def test_overload_is_503_no_store(self):
        for _ in range(self.svc.MAX_CONCURRENT):
            self.server.slots.acquire()
        try:
            headers = self.assert_failure(503, self.path)
            self.assertEqual(headers["Retry-After"], "5")
        finally:
            for _ in range(self.svc.MAX_CONCURRENT):
                self.server.slots.release()

    def test_internal_errors_are_500_no_store(self):
        with mock.patch.object(self.cache, "read", side_effect=RuntimeError("boom /home/pi/.cache")):
            self.assert_failure(500, self.path)

    def test_requests_only_read_the_cache_and_are_not_logged(self):
        spy = mock.Mock(wraps=self.cache)
        self.server.cache = spy
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.request("GET", self.path)
            self.request("GET", "/../.lock")
        self.assertEqual([call[0] for call in spy.method_calls], ["read"])
        self.assertEqual(errors.getvalue(), "")

    def test_responses_never_contain_plex_details(self):
        response, body, headers = self.request("GET", self.path)
        text = (" ".join(f"{k}: {v}" for k, v in headers.items())).lower()
        for private in ("plex", "token", "32400", "192.168", "library/metadata", str(self.cache.cache_dir).lower()):
            self.assertNotIn(private, text)
        self.assertNotIn(b"X-Plex-Token", body)

    def test_service_never_reaches_plex_or_the_network(self):
        for name in ("artwork_service.py", "artwork_cache.py"):
            source = (ENGINE / name).read_text(encoding="utf-8")
            for module in ("plexapi", "minstrel", "requests", "urllib.request", "http.client", "socket"):
                self.assertNotRegex(source, rf"(?m)^\s*(import|from) {re.escape(module)}\b", f"{name}: {module}")
            self.assertNotIn("create_connection", source)


if __name__ == "__main__":
    unittest.main()
