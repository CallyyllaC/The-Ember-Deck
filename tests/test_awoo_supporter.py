"""Awoo supporter licence v1 verification (the reusable library only).

``awoo_supporter`` is retained infrastructure for possible future Awoo
Supporter Features. Ember Deck currently defines none, and no runtime service
(Discord Rich Presence included) uses it, so these tests cover the verifier
itself, not any Ember Deck behaviour.

Test licences are signed with an Ed25519 key generated in memory for each test
run and trusted only through ``trusted_keys=``. No private key is stored in the
repository, and production trust never includes a test key.
"""

import base64
import builtins
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, capture_whisper, has_module  # noqa: E402

import awoo_supporter  # noqa: E402

HAS_CRYPTO = has_module("cryptography")
VALID_PAYLOAD = {"v": 1, "iss": "awoo.ltd", "ent": "awoo.supporter", "iat": 1789308000,
                 "lid": "00000000-0000-4000-8000-000000000001"}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Signer:
    """Issues licences with a throwaway in-memory key."""

    def __init__(self, kid="test-2026"):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.kid = kid
        self.private = Ed25519PrivateKey.generate()
        self.trusted = {kid: self.private.public_key()}

    def licence(self, payload=None, *, kid=None, raw_payload=None, prefix="AWOO1"):
        kid = kid or self.kid
        body = raw_payload if raw_payload is not None else json.dumps(payload or VALID_PAYLOAD, separators=(",", ":"))
        payload_text = b64url(body.encode("utf-8"))
        signature = self.private.sign(f"{prefix}.{kid}.{payload_text}".encode("ascii"))
        return f"{prefix}.{kid}.{payload_text}.{b64url(signature)}"


@unittest.skipUnless(HAS_CRYPTO, "the 'cryptography' package is required")
class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.signer = Signer()

    def verify(self, text, keys=None):
        return awoo_supporter.verify_licence(text, trusted_keys=keys or self.signer.trusted)

    def reason(self, text, keys=None):
        with self.assertRaises(awoo_supporter.LicenceError) as raised:
            self.verify(text, keys)
        return raised.exception.reason

    def test_valid_v1_licence(self):
        licence = self.verify(self.signer.licence())
        self.assertEqual((licence.kid, licence.issued_at), ("test-2026", 1789308000))
        self.assertNotIn(licence.licence_id, repr(licence))

    def test_invalid_signature(self):
        other = Signer()
        forged = other.licence()
        self.assertEqual(self.reason(forged), "bad_signature")
        good = self.signer.licence()
        head, signature = good.rsplit(".", 1)
        flipped = signature[:-2] + ("A" if signature[-2] != "A" else "B") + signature[-1]
        self.assertIn(self.reason(f"{head}.{flipped}"), ("bad_signature", "noncanonical_base64url"))

    def test_signature_covers_the_exact_payload_text(self):
        # Same JSON meaning, different bytes: the signature must not transfer.
        original = self.signer.licence()
        _, kid, _, signature = original.split(".")
        respaced = b64url(json.dumps(VALID_PAYLOAD).encode())  # with spaces
        self.assertEqual(self.reason(f"AWOO1.{kid}.{respaced}.{signature}"), "bad_signature")

    def test_malformed_envelopes(self):
        good = self.signer.licence()
        for text in ("", "   ", "AWOO1", "AWOO1.test-2026.abc", good + ".extra", good.replace(".", "..", 1),
                     "AWOO1.Test Key.x.y", None, 123):
            with self.subTest(text=str(text)[:30]):
                self.assertIn(self.reason(text), ("malformed", "missing"))

    def test_wrong_prefix_and_unsupported_version(self):
        self.assertEqual(self.reason(self.signer.licence(prefix="AWOO2")), "unsupported_version")
        self.assertEqual(self.reason(self.signer.licence(prefix="MEOW1")), "malformed")
        self.assertEqual(self.reason(self.signer.licence(dict(VALID_PAYLOAD, v=2))), "unsupported_version")
        self.assertEqual(self.reason(self.signer.licence(dict(VALID_PAYLOAD, v=True))), "unsupported_version")

    def test_unknown_key_id(self):
        self.assertEqual(self.reason(self.signer.licence(kid="awoo-2099-01")), "unknown_key")

    def test_noncanonical_base64url(self):
        good = self.signer.licence()
        prefix, kid, payload, signature = good.split(".")
        cases = {
            "padding": f"{prefix}.{kid}.{payload}==.{signature}",
            "standard alphabet": f"{prefix}.{kid}.{payload.replace('-', '+').replace('_', '/')}x.{signature}",
            "impossible length": f"{prefix}.{kid}.{payload}A.{signature}"
            if len(payload) % 4 == 0 else f"{prefix}.{kid}.{payload[:len(payload) - len(payload) % 4]}A.{signature}",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.assertIn(self.reason(text), ("noncanonical_base64url", "malformed", "bad_signature"))
        # Non-zero trailing bits: decodes, but is not the canonical encoding.
        canonical = b64url(b"\x00")  # "AA"
        self.assertEqual(canonical, "AA")
        with self.assertRaises(awoo_supporter.LicenceError) as raised:
            awoo_supporter._b64url_decode("AB")
        self.assertEqual(raised.exception.reason, "noncanonical_base64url")

    def test_payload_rules(self):
        cases = {
            "wrong issuer": (dict(VALID_PAYLOAD, iss="example.org"), "wrong_issuer"),
            "wrong entitlement": (dict(VALID_PAYLOAD, ent="noctaxis.pro"), "wrong_entitlement"),
            "extra field": (dict(VALID_PAYLOAD, exp=1), "bad_payload"),
            "missing field": ({k: v for k, v in VALID_PAYLOAD.items() if k != "lid"}, "bad_payload"),
            "uuid v1": (dict(VALID_PAYLOAD, lid="00000000-0000-1000-8000-000000000001"), "bad_payload"),
            "uuid bad variant": (dict(VALID_PAYLOAD, lid="00000000-0000-4000-c000-000000000001"), "bad_payload"),
            "uuid uppercase": (dict(VALID_PAYLOAD, lid="0000000A-0000-4000-8000-000000000001"), "bad_payload"),
            "negative iat": (dict(VALID_PAYLOAD, iat=-1), "bad_payload"),
            "iat beyond safe integer": (dict(VALID_PAYLOAD, iat=2 ** 53), "bad_payload"),
            "float iat": (dict(VALID_PAYLOAD, iat=1.5), "bad_payload"),
            "string iat": (dict(VALID_PAYLOAD, iat="1789308000"), "bad_payload"),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.reason(self.signer.licence(payload)), expected)

    def test_issuance_timestamp_boundaries_are_accepted(self):
        for iat in (0, 2 ** 53 - 1):
            with self.subTest(iat=iat):
                self.assertEqual(self.verify(self.signer.licence(dict(VALID_PAYLOAD, iat=iat))).issued_at, iat)

    def test_signed_but_invalid_json_shapes(self):
        for raw in ("[1,2]", "not json", '{"v":1,"v":1,"iss":"awoo.ltd","ent":"awoo.supporter","iat":1,'
                    '"lid":"00000000-0000-4000-8000-000000000001"}'):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(self.reason(self.signer.licence(raw_payload=raw)), "bad_payload")

    def test_maximum_length(self):
        filler = "a" * 600
        self.assertEqual(self.reason("AWOO1.test-2026." + filler + ".x"), "too_long")
        good = self.signer.licence()
        self.assertLessEqual(len(good), awoo_supporter.MAX_LICENCE_LENGTH)
        self.assertEqual(self.reason("x" * (awoo_supporter.MAX_LICENCE_FILE_BYTES + 1)), "too_long")

    def test_only_surrounding_whitespace_is_trimmed(self):
        good = self.signer.licence()
        self.verify(f"  \n{good}\r\n\t")
        self.assertEqual(self.reason(good[:20] + " " + good[20:]), "malformed")
        self.assertEqual(self.reason(good[:20] + "\n" + good[20:]), "malformed")
        self.assertEqual(self.reason(good + " "), "malformed")

    def test_production_trust_does_not_accept_a_test_key(self):
        impostor = Signer(kid="awoo-2026-01")
        licence = impostor.licence()
        with self.assertRaises(awoo_supporter.LicenceError) as raised:
            awoo_supporter.verify_licence(licence)  # production key store only
        self.assertEqual(raised.exception.reason, "bad_signature")
        self.assertEqual(set(awoo_supporter.PRODUCTION_PUBLIC_KEYS), {"awoo-2026-01"})

    def test_production_key_is_the_published_ed25519_key(self):
        from cryptography.hazmat.primitives import serialization
        key = awoo_supporter._load_key(awoo_supporter.PRODUCTION_PUBLIC_KEYS["awoo-2026-01"])
        raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.assertEqual(raw.hex(), "2ff30183494a7f29163159193358356792cac57b24ca474f9adfe4c9314dc3ab")

    def test_rejections_never_contain_the_licence(self):
        good = self.signer.licence()
        payload, signature = good.split(".")[2:]
        output = io.StringIO()
        with capture_whisper() as messages, redirect_stdout(output):
            for text in (good + "x", Signer().licence(), good.replace("AWOO1", "AWOO9")):
                try:
                    self.verify(text)
                except awoo_supporter.LicenceError as exc:
                    message = f"{exc} {exc.reason} {awoo_supporter.describe(exc.reason)}"
                    for secret in (signature, payload, good):
                        self.assertNotIn(secret, message)
        self.assertEqual(messages, [])
        self.assertEqual(output.getvalue(), "")


class LicenceFileTests(unittest.TestCase):
    def test_missing_unreadable_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "supporter.licence")
            with self.assertRaises(awoo_supporter.LicenceError) as raised:
                awoo_supporter.read_licence_file(path)
            self.assertEqual(raised.exception.reason, "missing")
            path.write_bytes(b"x" * (awoo_supporter.MAX_LICENCE_FILE_BYTES + 1))
            with self.assertRaises(awoo_supporter.LicenceError) as raised:
                awoo_supporter.read_licence_file(path)
            self.assertEqual(raised.exception.reason, "too_long")
            path.write_bytes("AWOO1.é".encode("utf-8"))
            with self.assertRaises(awoo_supporter.LicenceError) as raised:
                awoo_supporter.read_licence_file(path)
            self.assertEqual(raised.exception.reason, "malformed")

    def test_tilde_is_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "supporter.licence").write_text("AWOO1.x.y.z\n", encoding="ascii")
            with mock.patch.dict("os.environ", {"HOME": tmp, "USERPROFILE": tmp}):
                self.assertEqual(awoo_supporter.read_licence_file("~/supporter.licence").strip(), "AWOO1.x.y.z")

    def test_missing_cryptography_fails_closed(self):
        real_import = builtins.__import__

        def no_crypto(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        licence = "AWOO1.awoo-2026-01." + b64url(b"{}") + "." + b64url(b"\x00" * 64)
        with mock.patch.object(builtins, "__import__", no_crypto):
            with self.assertRaises(awoo_supporter.LicenceError) as raised:
                awoo_supporter.verify_licence(licence)
        self.assertEqual(raised.exception.reason, "crypto_unavailable")

    def test_no_production_bypass_exists(self):
        source = (ENGINE / "awoo_supporter.py").read_text(encoding="utf-8")
        for marker in ("FORCE_LICENSED", "FORCE_LICENCED", "SKIP_LICENCE", "BEGIN PRIVATE KEY"):
            self.assertNotIn(marker, source)

    def test_licence_files_are_gitignored(self):
        import shutil
        import subprocess
        if shutil.which("git") is None or not (ENGINE.parent / ".git").exists():
            self.skipTest("git checkout required")
        self.assertEqual(subprocess.run(["git", "check-ignore", "-q", "supporter.licence"],
                                        cwd=ENGINE.parent).returncode, 0)


if __name__ == "__main__":
    unittest.main()
