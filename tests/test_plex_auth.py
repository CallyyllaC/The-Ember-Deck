"""Plex credentials, secret-file handling and unattended re-authentication.

A small fake "Plex world" stands in for PlexServer/MyPlexAccount: it knows
which tokens are valid, which password is right, and issues a fresh token on
each successful account login, so the full refresh cycle can run offline.
"""

import io
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import ENGINE, FakeBus, capture_whisper, has_module, import_engine_module  # noqa: E402

import contracts  # noqa: E402
import plex_auth  # noqa: E402
import secret_store  # noqa: E402

PASSWORD = "correct-horse-battery"
TOKEN = "cachedTOKEN123"
# A requests/urllib3 timeout through a *.plex.direct host, whose hex id
# happens to contain both "401" and "403".
PLEX_DIRECT_TIMEOUT = (
    "HTTPSConnectionPool(host='192-168-1-20.9e4013a40373c5d2b1f0e6a7c8d9e0f1.plex.direct', port=32400): "
    "Max retries exceeded with url: /status/sessions (Caused by ConnectTimeoutError("
    "<urllib3.connection.HTTPSConnection object at 0x7f40312403e0>, 'Connection to "
    "192-168-1-20.9e4013a40373c5d2b1f0e6a7c8d9e0f1.plex.direct timed out. (connect timeout=5.0)'))"
)


class Unauthorized(Exception):
    pass


class PlexWorld:
    """Fake Plex server + plex.tv account."""

    def __init__(self, *, password=PASSWORD, server_name="Media", valid_tokens=(TOKEN,)):
        self.password = password
        self.server_name = server_name
        self.valid_tokens = set(valid_tokens)
        self.down = False
        self.internet_down = False  # plex.tv unreachable while the LAN server may be up
        self.server_error = None  # exception the LAN server raises instead of answering
        self.url = "https://media.local:32400"  # where the server actually is
        self.logins = 0
        self.token_attempts = []
        self.issued = 0

    def server(self, url, token):
        world = self

        def sections():
            if world.down or url != world.url:
                raise ConnectionError(f"Max retries exceeded with url: /library/sections?X-Plex-Token={token}")
            if world.server_error is not None:
                raise world.server_error
            if token not in world.valid_tokens:
                raise Unauthorized(f"(401) unauthorized; {url}/library/sections?X-Plex-Token={token}")
            return []

        return SimpleNamespace(library=SimpleNamespace(sections=sections), friendlyName="Media",
                               _token=token, _baseurl=url, sessions=lambda: [])

    def server_factory(self, url, token, timeout=None):
        self.token_attempts.append(token)
        return self.server(url, token)

    def account_factory(self, username, password, timeout=None):
        self.logins += 1
        if self.down or self.internet_down:
            raise ConnectionError("plex.tv unreachable")
        if password != self.password:
            raise Unauthorized(f"(401) unauthorized; invalid credentials for {username}/{password}")
        world = self

        class Resource:
            def connect(self, timeout=None):
                world.issued += 1
                token = f"freshTOKEN{world.issued:03d}"
                world.valid_tokens.add(token)
                return world.server(world.url, token)

        def resource(name):
            if name != world.server_name:
                raise LookupError(f"Unable to find resource {name}")
            return Resource()

        return SimpleNamespace(resource=resource)


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.secrets = self.tmp / plex_auth.SECRETS_FILENAME
        self.cfg = {"plex_client_name": "Ember Deck", "server_name": "Media", "_config_dir": str(self.tmp),
                    "plex_retry_initial_s": 2.0, "plex_retry_max_s": 60.0, "plex_timeout_s": 1.0}
        self.world = PlexWorld()
        whisper = capture_whisper()
        self.messages = whisper.__enter__()
        self.addCleanup(whisper.__exit__, None, None, None)

    def write_secrets(self, text):
        self.secrets.write_text(text, encoding="utf-8")
        if os.name == "posix":
            self.secrets.chmod(0o600)

    def connector(self, environ=None):
        return plex_auth.PlexConnector(
            lambda: dict(self.cfg), environ=environ or {},
            server_factory=self.world.server_factory, account_factory=self.world.account_factory,
        )

    def events(self):
        return [m.event for m in self.messages]

    def all_log_text(self):
        return " ".join(f"{m.event} {m.metadata}" for m in self.messages)


@unittest.skipUnless(has_module("yaml"), "PyYAML required to parse secret files")
class PrecedenceTests(AuthTestCase):
    def test_credentials_come_from_the_secret_file(self):
        self.write_secrets(f"credentials:\n  username: deck\n  password: {PASSWORD}\n")
        auth = plex_auth.resolve_plex_auth(self.cfg, {})
        self.assertEqual((auth.username, auth.password, auth.credential_source), ("deck", PASSWORD, "secrets file"))
        self.assertTrue(auth.has_account and auth.configured)
        self.assertNotIn(PASSWORD, repr(auth))

    def test_environment_overrides_file_and_yaml(self):
        self.write_secrets("credentials:\n  username: file-user\n  password: file-pass\n"
                           "cache:\n  url: https://cache\n  token: cache-token\n")
        env = {plex_auth.ENV_USERNAME: "env-user", plex_auth.ENV_PASSWORD: "env-pass",
               plex_auth.ENV_URL: "https://env", plex_auth.ENV_TOKEN: "env-token"}
        cfg = dict(self.cfg, username="yaml-user", password="yaml-pass")
        auth = plex_auth.resolve_plex_auth(cfg, env)
        self.assertEqual((auth.username, auth.password, auth.url, auth.token, auth.token_source),
                         ("env-user", "env-pass", "https://env", "env-token", "environment"))

    def test_cached_pair_beats_legacy_yaml_and_half_pairs_are_ignored(self):
        self.write_secrets("cache:\n  url: https://cache\n  token: cache-token\n")
        auth = plex_auth.resolve_plex_auth(dict(self.cfg, url="https://old", token="old"), {plex_auth.ENV_TOKEN: "lonely"})
        self.assertEqual((auth.url, auth.token, auth.token_source), ("https://cache", "cache-token", "cache"))

    def test_legacy_credentials_in_tracked_yaml_still_work_but_are_flagged(self):
        auth = plex_auth.resolve_plex_auth(dict(self.cfg, username="u", password=PASSWORD), {})
        self.assertTrue(auth.has_account)
        self.assertIn("plex_credentials_in_tracked_config", [event for _, event, _ in auth.problems])

    def test_missing_identity_or_login_is_unconfigured_with_actionable_text(self):
        auth = plex_auth.resolve_plex_auth(dict(self.cfg, plex_client_name=None), {})
        self.assertFalse(auth.configured)
        self.assertIn("plex_client_name", auth.missing_description())
        self.assertIn("minstrel_secrets.yaml", auth.missing_description())


@unittest.skipUnless(has_module("yaml"), "PyYAML required to parse secret files")
class SecretFileTests(AuthTestCase):
    def test_malformed_yaml_is_ignored_without_echoing_contents(self):
        self.write_secrets(f"credentials:\n  username: deck\n  password: [{PASSWORD}\n")
        auth = plex_auth.resolve_plex_auth(self.cfg, {})
        self.assertIsNone(auth.password)
        message = " ".join(text for _, _, text in auth.problems)
        self.assertIn("not valid YAML", message)
        self.assertNotIn(PASSWORD, message)

    def test_schema_errors_name_the_option_not_the_value(self):
        self.write_secrets(f"credentials:\n  username: deck\n  password: [{PASSWORD}]\n")
        auth = plex_auth.resolve_plex_auth(self.cfg, {})
        message = " ".join(text for _, _, text in auth.problems)
        self.assertIn("credentials.password", message)
        self.assertNotIn(PASSWORD, message)
        self.write_secrets("credentials:\n  username: deck\nsurprise: 1\n")
        self.assertIn("surprise", " ".join(t for _, _, t in plex_auth.resolve_plex_auth(self.cfg, {}).problems))

    def test_unreadable_file_is_reported_and_ignored(self):
        self.write_secrets(f"credentials:\n  username: deck\n  password: {PASSWORD}\n")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            auth = plex_auth.resolve_plex_auth(self.cfg, {})
        self.assertIsNone(auth.password)
        self.assertIn("Permission denied", auth.problems[0][2])

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_group_readable_file_is_flagged(self):
        self.write_secrets("credentials: {}\n")
        self.secrets.chmod(0o644)
        auth = plex_auth.resolve_plex_auth(self.cfg, {})
        self.assertIn("plex_secrets_file_permissions", [event for _, event, _ in auth.problems])

    def test_cache_update_is_atomic_private_and_keeps_credentials(self):
        self.write_secrets(f"version: 1\ncredentials:\n  username: deck\n  password: {PASSWORD}\n")
        plex_auth.store_cached_connection(self.secrets, "https://new", "new-token")
        import yaml
        saved = yaml.safe_load(self.secrets.read_text(encoding="utf-8"))
        self.assertEqual(saved["credentials"], {"username": "deck", "password": PASSWORD})
        self.assertEqual(saved["cache"], {"url": "https://new", "token": "new-token"})
        self.assertEqual([p.name for p in self.tmp.iterdir()], [plex_auth.SECRETS_FILENAME])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o600)

    def test_cache_update_refuses_to_overwrite_a_file_it_cannot_understand(self):
        self.write_secrets("credentials: [oops\n")
        before = self.secrets.read_bytes()
        with self.assertRaises(secret_store.SecretFileError):
            plex_auth.store_cached_connection(self.secrets, "https://new", "new-token")
        self.assertEqual(self.secrets.read_bytes(), before)

    def test_failed_write_leaves_the_old_file_and_no_temp_file(self):
        self.write_secrets("cache:\n  url: https://old\n  token: old\n")
        with mock.patch.object(secret_store.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                plex_auth.store_cached_connection(self.secrets, "https://new", "new-token")
        self.assertIn("old", self.secrets.read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in self.tmp.iterdir()], [plex_auth.SECRETS_FILENAME])


class AuthFailureDetectionTests(unittest.TestCase):
    """``is_auth_failure`` decides whether a cached token is given up."""

    def response_error(self, status, text="request failed"):
        return _ResponseError(status, text)

    def test_plexapi_401_and_403_are_rejections(self):
        # plexapi's format: "(<status>) <codename>; <url> <body>"
        self.assertTrue(plex_auth.is_auth_failure(Unauthorized(
            "(401) unauthorized; https://192-168-1-20.abc.plex.direct:32400/library/sections <html>401 Unauthorized</html>")))
        self.assertTrue(plex_auth.is_auth_failure(Exception(
            "(403) forbidden; https://plex.tv/api/v2/users/signin ")))

    def test_attached_http_response_decides(self):
        self.assertTrue(plex_auth.is_auth_failure(self.response_error(401)))
        self.assertTrue(plex_auth.is_auth_failure(self.response_error(403)))
        self.assertFalse(plex_auth.is_auth_failure(self.response_error(503, "503 Server Error: unauthorized proxy")))

    def test_other_statuses_are_not_rejections_even_with_auth_words_in_the_body(self):
        for message in ("(404) not_found; https://media.local:32400/library/sections ",
                        "(429) too_many_requests; https://plex.tv/api/v2/users/signin ",
                        "(500) internal_server_error; https://media.local:32400/status/sessions Unauthorized access log full"):
            self.assertFalse(plex_auth.is_auth_failure(Exception(message)), message)

    def test_digits_inside_hosts_ports_and_addresses_are_not_rejections(self):
        for exc in (ConnectionError(PLEX_DIRECT_TIMEOUT),
                    ConnectionError("Max retries exceeded with url: https://10.0.0.5:32401/library/sections"),
                    ConnectionError("[Errno 111] Connection refused (pid 4031)"),
                    TimeoutError("read timed out after 4.03 s"),
                    OSError(-3, "Temporary failure in name resolution")):
            self.assertFalse(plex_auth.is_auth_failure(exc), str(exc))

    def test_rejection_wording_without_a_status_is_still_recognised(self):
        for message in ("401 Client Error: Unauthorized for url: https://plex.tv/users/account",
                        "Invalid credentials", "The token is invalid", "Not authorized to access this server",
                        "Authentication required"):
            self.assertTrue(plex_auth.is_auth_failure(Exception(message)), message)


class _ResponseError(Exception):
    """Like ``requests.HTTPError``: the response is attached."""

    def __init__(self, status, text="request failed"):
        super().__init__(text)
        self.response = SimpleNamespace(status_code=status)


@unittest.skipUnless(has_module("yaml"), "PyYAML required")
class ReauthenticationTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.write_secrets(f"version: 1\ncredentials:\n  username: deck\n  password: {PASSWORD}\n"
                           f"cache:\n  url: https://media.local:32400\n  token: {TOKEN}\n")

    def cached_token(self):
        import yaml
        return yaml.safe_load(self.secrets.read_text(encoding="utf-8"))["cache"]["token"]

    def test_full_refresh_cycle(self):
        connector = self.connector()
        # 1. cached URL/token works: no account login
        self.assertIsNotNone(connector.poll(0.0))
        self.assertEqual((self.world.logins, connector.state), (0, contracts.PLEX_CONNECTED))

        # 2. the cached token is revoked and the next session query fails
        self.world.valid_tokens.discard(TOKEN)
        connector.connection_lost(Unauthorized("(401) unauthorized"), 10.0)
        self.assertIsNone(connector.server)
        self.assertIsNone(connector.poll(11.0), "retry waits for the backoff")

        # 3./4. username/password login, fresh token persisted securely
        self.assertIsNotNone(connector.poll(12.0))
        self.assertEqual(self.world.logins, 1)
        self.assertEqual(self.world.token_attempts, [TOKEN], "rejected token is not retried")
        self.assertEqual(self.cached_token(), "freshTOKEN001")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o600)

        # 5. recovered without any exit, with a recovery event
        self.assertEqual(connector.state, contracts.PLEX_CONNECTED)
        self.assertIn("plex_connection_recovered", self.events())

        # 6. the next start reuses the refreshed cache without logging in
        restarted = self.connector()
        self.assertIsNotNone(restarted.poll(0.0))
        self.assertEqual(self.world.logins, 1)
        self.assertEqual(self.world.token_attempts[-1], "freshTOKEN001")

    def test_corrupt_cached_token_falls_back_to_login_in_the_same_attempt(self):
        self.world.valid_tokens.clear()
        connector = self.connector()
        self.assertIsNotNone(connector.poll(0.0))
        self.assertEqual(self.world.logins, 1)
        self.assertEqual(self.cached_token(), "freshTOKEN001")

    def test_wrong_password_backs_off_stays_quiet_and_recovers(self):
        self.world.valid_tokens.clear()
        self.world.password = "something-else"
        connector = self.connector()
        attempts = []
        for second in range(0, 40):
            before = self.world.logins
            connector.poll(float(second))
            if self.world.logins > before:
                attempts.append(second)
        self.assertEqual(attempts, [0, 2, 6, 14, 30])
        self.assertEqual(connector.state, contracts.PLEX_AUTH_FAILED)
        self.assertEqual(self.events().count("plex_login_failed"), 1, "one deduplicated error")

        self.world.password = PASSWORD
        self.assertIsNotNone(connector.poll(1000.0))
        self.assertIn("plex_connection_recovered", self.events())

    def test_server_down_retries_are_bounded_by_the_cap(self):
        self.world.down = True
        connector = self.connector()
        now = 0.0
        for _ in range(12):
            connector.poll(now)
            now = connector.next_attempt_at
        self.assertEqual(connector.retry_s, 60.0)
        self.assertEqual(connector.state, contracts.PLEX_UNAVAILABLE)

    def test_network_blip_retries_the_token_before_logging_in_again(self):
        connector = self.connector()
        connector.poll(0.0)
        connector.connection_lost(ConnectionError("reset by peer"), 5.0)
        connector.poll(7.0)
        self.assertEqual(self.world.logins, 0)
        self.assertEqual(connector.state, contracts.PLEX_CONNECTED)

    def test_unreachable_server_does_not_disable_the_cached_token(self):
        # The LAN server reboots while plex.tv is also unreachable. Once the
        # server is back, the cached token must be used again even though
        # the account login is still failing.
        self.world.down = True
        connector = self.connector()
        self.assertIsNone(connector.poll(0.0))
        self.assertFalse(connector.token_rejected)
        self.assertEqual(connector.state, contracts.PLEX_UNAVAILABLE)

        self.world.down = False
        self.world.internet_down = True
        self.assertIsNotNone(connector.poll(connector.next_attempt_at))
        self.assertEqual(connector.state, contracts.PLEX_CONNECTED)
        self.assertEqual(self.world.token_attempts, [TOKEN, TOKEN])
        self.assertEqual(self.cached_token(), TOKEN, "the cache is left alone")
        self.assertIn("plex_token_unreachable_trying_account_login", self.events())
        self.assertNotIn("plex_token_rejected_trying_account_login", self.events())

    def test_server_errors_and_timeouts_keep_the_token(self):
        connector = self.connector()
        self.world.internet_down = True
        for error in (TimeoutError("timed out"), Unauthorized("(503) service_unavailable; https://media.local:32400/library/sections"),
                      Unauthorized("(404) not_found; https://media.local:32400/library/sections")):
            self.world.server_error = error
            connector.poll(connector.next_attempt_at)
            self.assertFalse(connector.token_rejected, repr(error))
            self.assertEqual(connector.state, contracts.PLEX_UNAVAILABLE, repr(error))
        self.world.server_error = None
        self.assertIsNotNone(connector.poll(connector.next_attempt_at))
        self.assertEqual(self.world.token_attempts, [TOKEN] * 4)

    def test_moved_server_is_found_through_the_account_without_rejecting_the_token(self):
        # A connection failure with the internet up still tries the account
        # in the same attempt (unchanged behaviour), which yields the
        # server's new address.
        self.world.url = "https://media.moved:32400"
        connector = self.connector()
        self.assertIsNotNone(connector.poll(0.0))
        self.assertEqual(self.world.logins, 1)
        self.assertFalse(connector.token_rejected)
        import yaml
        cache = yaml.safe_load(self.secrets.read_text(encoding="utf-8"))["cache"]
        self.assertEqual(cache, {"url": "https://media.moved:32400", "token": "freshTOKEN001"})

    def test_rejected_token_is_not_retried_while_login_keeps_failing(self):
        self.world.valid_tokens.clear()
        self.world.internet_down = True
        connector = self.connector()
        connector.poll(0.0)
        self.assertTrue(connector.token_rejected)
        connector.poll(connector.next_attempt_at)
        self.assertEqual(self.world.token_attempts, [TOKEN], "a rejected token is tried once")
        self.assertIn("plex_token_rejected_trying_account_login", self.events())

    def test_lost_connection_through_a_plex_direct_host_is_not_an_auth_failure(self):
        connector = self.connector()
        connector.poll(0.0)
        connector.connection_lost(ConnectionError(PLEX_DIRECT_TIMEOUT), 5.0)
        self.assertEqual(connector.state, contracts.PLEX_UNAVAILABLE)
        self.assertFalse(connector.token_rejected)
        connector.poll(connector.next_attempt_at)
        self.assertEqual((self.world.logins, connector.state), (0, contracts.PLEX_CONNECTED))

    def test_state_write_failure_keeps_the_session(self):
        self.world.valid_tokens.clear()
        connector = self.connector()
        with mock.patch.object(plex_auth, "store_cached_connection", side_effect=OSError(30, "Read-only file system")):
            self.assertIsNotNone(connector.poll(0.0))
        self.assertEqual(connector.state, contracts.PLEX_CONNECTED)
        self.assertIn("plex_auth_state_write_failed", self.events())

    def test_environment_credentials_are_never_written_to_the_file(self):
        self.secrets.unlink()
        self.world.valid_tokens.clear()
        env = {plex_auth.ENV_USERNAME: "deck", plex_auth.ENV_PASSWORD: PASSWORD}
        connector = self.connector(env)
        self.assertIsNotNone(connector.poll(0.0))
        text = self.secrets.read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("credentials", text)
        self.assertIn("freshTOKEN001", text)

    def test_secrets_never_reach_log_messages_or_stdout(self):
        self.world.valid_tokens.clear()
        self.world.password = "wrong"
        connector = self.connector()
        output = io.StringIO()
        with redirect_stdout(output):
            connector.poll(0.0)
            self.world.password = PASSWORD
            connector.poll(100.0)
            connector.connection_lost(ConnectionError(f"url: /status?X-Plex-Token=freshTOKEN001 ({PASSWORD})"), 101.0)
        logged = self.all_log_text() + output.getvalue()
        for secret in (PASSWORD, TOKEN, "freshTOKEN001"):
            self.assertNotIn(secret, logged)


class UnconfiguredTests(AuthTestCase):
    def test_no_credentials_is_unconfigured_rechecked_and_logged_once(self):
        connector = self.connector()
        for second in range(0, 29):
            connector.poll(float(second))
        self.assertEqual(connector.state, contracts.PLEX_UNCONFIGURED)
        self.assertEqual(self.events().count("plex_not_configured"), 1)
        self.assertEqual(self.world.logins, 0)

        self.write_secrets(f"credentials:\n  username: deck\n  password: {PASSWORD}\n")
        self.assertIsNone(connector.poll(29.0), "not rechecked before the interval")
        if has_module("yaml"):
            self.assertIsNotNone(connector.poll(31.0))
            self.assertEqual(connector.state, contracts.PLEX_CONNECTED)
            self.assertIn("plex_configured", self.events())


# --------------------------------------------------------------------------- Minstrel process level

minstrel = import_engine_module("minstrel")


class _Stop(Exception):
    pass


@unittest.skipUnless(has_module("yaml"), "PyYAML required")
class MinstrelProcessTests(AuthTestCase):
    def run_minstrel(self, yaml_text, *, loops=4, environ=None):
        config = self.tmp / "minstrel.yaml"
        config.write_text(yaml_text, encoding="utf-8")
        bus = FakeBus()
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= loops:
                raise _Stop()

        env = {"CONFIG_PATH": str(config), **(environ or {})}
        with mock.patch.object(minstrel, "bus", bus), \
                mock.patch.object(minstrel, "PlexServer", self.world.server_factory), \
                mock.patch.object(minstrel, "MyPlexAccount", self.world.account_factory), \
                mock.patch.object(minstrel.signal, "signal"), \
                mock.patch.object(minstrel.time, "sleep", fake_sleep), \
                mock.patch.dict(os.environ, env):
            for name in (plex_auth.ENV_USERNAME, plex_auth.ENV_PASSWORD, plex_auth.ENV_URL, plex_auth.ENV_TOKEN):
                if not environ or name not in environ:
                    os.environ.pop(name, None)
            with self.assertRaises(_Stop):
                minstrel.main()
        return bus

    def test_valid_config_without_credentials_runs_unconfigured_and_healthy(self):
        bus = self.run_minstrel("proc_name: minstrel\nplex_client_name: Ember Deck\nserver_name: Media\n")
        self.assertGreater(bus.get_int("/proc/minstrel/heartbeat_ms"), 0)
        self.assertEqual(bus.get_int("/plex/connection_state", -1), contracts.PLEX_UNCONFIGURED)
        self.assertEqual(bus.get_int("/plex/play_state", -1), contracts.PLAY_STOPPED)
        self.assertEqual(bus.text("/plex/title_utf8"), "N/A")
        self.assertEqual(self.events().count("plex_not_configured"), 1)

    def test_wrong_credentials_keep_minstrel_alive(self):
        self.world.password = "different"
        self.write_secrets(f"credentials:\n  username: deck\n  password: {PASSWORD}\n")
        bus = self.run_minstrel("proc_name: minstrel\nplex_client_name: Ember Deck\nserver_name: Media\n")
        self.assertEqual(bus.get_int("/plex/connection_state", -1), contracts.PLEX_AUTH_FAILED)
        self.assertGreater(bus.get_int("/proc/minstrel/hb_seq"), 0)

    def test_presses_while_unavailable_are_consumed_not_replayed(self):
        bus = FakeBus({"/plex/control/next_seq": 3})
        with mock.patch.object(minstrel, "bus", bus):
            seen = {"playpause": 0, "stop": 0, "next": 0, "previous": 0}
            minstrel.discard_plex_control_requests(seen, "minstrel", "Plex is unavailable")
            minstrel.discard_plex_control_requests(seen, "minstrel", "Plex is unavailable")
        self.assertEqual(seen["next"], 3)
        self.assertEqual(bus.get_int("/plex/control/last_ok"), 0)
        self.assertEqual(self.events().count("plex_control_command_failed"), 1)  # consumed once, never replayed

    def test_malformed_yaml_and_bad_types_still_exit_with_status_2(self):
        for text in ("proc_name: [unclosed\n", "poll_interval_s: fast\n", "surprise_option: 1\n"):
            with self.subTest(yaml=text):
                config = self.tmp / "minstrel.yaml"
                config.write_text(text, encoding="utf-8")
                with mock.patch.dict(os.environ, {"CONFIG_PATH": str(config)}), \
                        redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        runpy.run_path(str(ENGINE / "minstrel.py"), run_name="__main__")
                self.assertEqual(raised.exception.code, 2)

    def test_identity_comes_from_environment_but_credentials_do_not_enter_cfg(self):
        config = self.tmp / "minstrel.yaml"
        config.write_text("plex_client_name: File\n", encoding="utf-8")
        env = {"CONFIG_PATH": str(config), "EMBERDECK_PLEX_CLIENT_NAME": "Env", plex_auth.ENV_PASSWORD: PASSWORD}
        with mock.patch.dict(os.environ, env):
            cfg = minstrel.load_cfg()
        self.assertEqual(cfg["plex_client_name"], "Env")
        self.assertNotIn(PASSWORD, repr(cfg))


class RepositoryHygieneTests(unittest.TestCase):
    ROOT = ENGINE.parent

    def test_secret_file_is_gitignored_and_template_is_not(self):
        if shutil.which("git") is None or not (self.ROOT / ".git").exists():
            self.skipTest("git checkout required")

        def ignored(path):
            return subprocess.run(["git", "check-ignore", "-q", path], cwd=self.ROOT).returncode == 0

        self.assertTrue(ignored("EmberEngine/configs/minstrel_secrets.yaml"))
        self.assertTrue(ignored("EmberEngine/configs/.minstrel_secrets.yaml.abc123.tmp"))
        self.assertFalse(ignored("EmberEngine/configs/minstrel_secrets.example.yaml"))

    def test_tracked_templates_hold_no_credentials(self):
        tracked = (ENGINE / "configs" / "minstrel.yaml").read_text(encoding="utf-8")
        for key in ("username:", "password:", "token:", "url:"):
            self.assertNotIn(f"\n{key}", tracked)
        example = (ENGINE / "configs" / "minstrel_secrets.example.yaml").read_text(encoding="utf-8")
        self.assertIn("YOUR_PLEX_PASSWORD", example)
        self.assertRegex(example, r"token: null")

    @unittest.skipUnless(has_module("yaml"), "PyYAML required")
    def test_example_secret_file_matches_the_schema(self):
        import yaml
        path = ENGINE / "configs" / "minstrel_secrets.example.yaml"
        credentials, cache = plex_auth.parse_secret_document(path, yaml.safe_load(path.read_text(encoding="utf-8")))
        self.assertEqual(set(credentials), {"username", "password"})
        self.assertEqual(cache, {"url": None, "token": None})


if __name__ == "__main__":
    unittest.main()
