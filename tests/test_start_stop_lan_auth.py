from __future__ import annotations

import base64
import gzip
import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path

import start_stop_service as SERVICE


USERNAME = "reader"
PASSWORD = "test-password"
VALID_AUTHORIZATION = "Basic " + base64.b64encode(
    f"{USERNAME}:{PASSWORD}".encode("utf-8")
).decode("ascii")


class ExplodingBody:
    def read(self, *_args, **_kwargs):
        raise AssertionError("LAN write request body must not be read")


class FakeWorkspace:
    collection_config = None

    @staticmethod
    def status():
        return {
            "available": True,
            "execution": {"ready": True},
            "job": {"status": "idle", "result": {}},
        }

    @staticmethod
    def materials():
        return {"materials": []}


class HandlerHarness:
    def __init__(
        self,
        *,
        lan_read_only: bool = True,
        lan_no_auth: bool = False,
    ) -> None:
        self.lan_read_only = lan_read_only
        self.public_host = "192.168.110.158" if lan_read_only else "127.0.0.1"
        self.port = 18788 if lan_read_only else 18787
        self.handler_type = SERVICE.create_handler(
            FakeWorkspace(),
            lan_read_only=lan_read_only,
            lan_auth_credentials=(
                SERVICE.BasicAuthCredentials(USERNAME, PASSWORD)
                if lan_read_only and not lan_no_auth
                else None
            ),
            lan_no_auth=lan_no_auth,
            public_host=self.public_host,
            public_port=self.port,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        authorization: str | None = None,
        host: str | None = None,
        exploding_body: bool = False,
        accept_encoding: str | None = None,
        if_none_match: str | None = None,
    ) -> tuple[int, dict[str, str], object]:
        message = http.client.HTTPMessage()
        message.add_header("Host", host or f"{self.public_host}:{self.port}")
        if authorization is not None:
            message.add_header("Authorization", authorization)
        if accept_encoding is not None:
            message.add_header("Accept-Encoding", accept_encoding)
        if if_none_match is not None:
            message.add_header("If-None-Match", if_none_match)
        if method in {"POST", "PUT"}:
            message.add_header("Content-Type", "application/json")
            message.add_header("Content-Length", "128")

        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.headers = message
        handler.rfile = ExplodingBody() if exploding_body else io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.client_address = (
            "192.168.110.50" if self.lan_read_only else "127.0.0.1",
            55000,
        )
        handler.server = type(
            "TestServer",
            (),
            {"server_address": (self.public_host, self.port)},
        )()
        statuses: list[int] = []
        response_headers: dict[str, str] = {}
        handler.send_response = lambda status: statuses.append(int(status))
        handler.send_header = lambda name, value: response_headers.__setitem__(
            str(name).lower(), str(value)
        )
        handler.end_headers = lambda: None

        getattr(handler, f"do_{method}")()
        if len(statuses) != 1:
            raise AssertionError(f"expected one response, got {statuses!r}")
        raw = handler.wfile.getvalue()
        payload = (
            json.loads(raw.decode("utf-8"))
            if raw and response_headers.get("content-type", "").startswith("application/json")
            else raw
        )
        return statuses[0], response_headers, payload


class TransportPerformanceTests(unittest.TestCase):
    def test_handler_uses_http_11_keep_alive_capable_responses(self):
        handler_type = SERVICE.create_handler(FakeWorkspace())
        self.assertEqual(handler_type.protocol_version, "HTTP/1.1")

    def test_static_text_is_gzipped_and_etag_revalidated(self):
        harness = HandlerHarness(lan_no_auth=True)
        status, headers, payload = harness.request(
            "GET",
            "/static/start-stop.js",
            accept_encoding="gzip, deflate",
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertEqual(headers["vary"], "Accept-Encoding")
        self.assertTrue(headers["etag"].endswith('-gzip"'))
        self.assertIn(b"LIVE_COMPARISON_POLL_MS", gzip.decompress(payload))

        status, second_headers, second_payload = harness.request(
            "GET",
            "/static/start-stop.js",
            accept_encoding="gzip, deflate",
            if_none_match=headers["etag"],
        )
        self.assertEqual(status, 304)
        self.assertEqual(second_payload, b"")
        self.assertEqual(second_headers["etag"], headers["etag"])
        self.assertEqual(second_headers["content-length"], "0")


class LanAuthFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_auth(self, content: str, *, mode: int = 0o600) -> Path:
        path = self.root / "private-lan-credential"
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_owner_only_regular_utf8_file_is_loaded_without_retaining_plaintext(self):
        credentials = SERVICE.load_lan_auth_file(
            self.write_auth(f"{USERNAME}:{PASSWORD}\n")
        )

        self.assertTrue(credentials.matches(USERNAME, PASSWORD))
        self.assertFalse(credentials.matches(USERNAME, "wrong"))
        self.assertFalse(credentials.matches("wrong", PASSWORD))
        self.assertNotIn(USERNAME, repr(credentials))
        self.assertNotIn(PASSWORD, repr(credentials))

    def test_unsafe_mode_symlink_directory_and_malformed_content_fail_closed(self):
        unsafe_mode = self.write_auth(f"{USERNAME}:{PASSWORD}\n", mode=0o640)
        with self.assertRaisesRegex(ValueError, "0600"):
            SERVICE.load_lan_auth_file(unsafe_mode)

        unsafe_mode.chmod(0o600)
        linked = self.root / "linked-credential"
        try:
            linked.symlink_to(unsafe_mode)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(ValueError) as linked_error:
            SERVICE.load_lan_auth_file(linked)
        self.assertNotIn(str(linked), str(linked_error.exception))

        with self.assertRaisesRegex(ValueError, "\u666e\u901a\u6587\u4ef6"):
            SERVICE.load_lan_auth_file(self.root)

        malformed = self.write_auth("missing-separator\n")
        with self.assertRaisesRegex(ValueError, "username:password"):
            SERVICE.load_lan_auth_file(malformed)


class LanBasicAuthTests(unittest.TestCase):
    def test_lan_page_static_and_api_require_correct_basic_credentials(self):
        harness = HandlerHarness()
        for path in (
            "/start-stop",
            "/static/start-stop.js",
            "/api/start-stop/status",
        ):
            with self.subTest(path=path, credential="missing"):
                status, headers, payload = harness.request("GET", path)
                self.assertEqual(status, 401)
                self.assertIn("basic", headers["www-authenticate"].lower())
                self.assertNotIn(PASSWORD, json.dumps(payload, ensure_ascii=False))
            with self.subTest(path=path, credential="wrong"):
                status, headers, _ = harness.request(
                    "GET",
                    path,
                    authorization="Basic d3Jvbmc6d3Jvbmc=",
                )
                self.assertEqual(status, 401)
                self.assertIn("www-authenticate", headers)
            with self.subTest(path=path, credential="correct"):
                status, _, _ = harness.request(
                    "GET", path, authorization=VALID_AUTHORIZATION
                )
                self.assertEqual(status, 200)

    def test_host_validation_precedes_authentication(self):
        status, headers, _ = HandlerHarness().request(
            "GET",
            "/api/start-stop/status",
            host="attacker.example:18788",
        )
        self.assertEqual(status, 403)
        self.assertNotIn("www-authenticate", headers)

    def test_authenticated_lan_post_is_forbidden_without_reading_body(self):
        status, _, payload = HandlerHarness().request(
            "POST",
            "/api/start-stop/jobs",
            authorization=VALID_AUTHORIZATION,
            exploding_body=True,
        )
        self.assertEqual(status, 403)
        self.assertIn("只读", payload["error"])

    def test_unauthenticated_lan_post_challenges_without_reading_body(self):
        status, headers, _ = HandlerHarness().request(
            "POST",
            "/api/start-stop/jobs",
            exploding_body=True,
        )
        self.assertEqual(status, 401)
        self.assertIn("www-authenticate", headers)

    def test_local_service_remains_unauthenticated(self):
        status, headers, payload = HandlerHarness(lan_read_only=False).request(
            "GET", "/api/start-stop/status"
        )
        self.assertEqual(status, 200)
        self.assertNotIn("www-authenticate", headers)
        self.assertEqual(payload["access_mode"], "local_read_write")

    def test_explicit_no_auth_lan_mode_keeps_pages_public_and_writes_forbidden(self):
        harness = HandlerHarness(lan_no_auth=True)
        for path in (
            "/start-stop",
            "/static/start-stop.js",
            "/api/start-stop/status",
        ):
            with self.subTest(path=path):
                status, headers, _ = harness.request("GET", path)
                self.assertEqual(status, 200)
                self.assertNotIn("www-authenticate", headers)

        status, headers, payload = harness.request(
            "POST",
            "/api/start-stop/jobs",
            exploding_body=True,
        )
        self.assertEqual(status, 403)
        self.assertNotIn("www-authenticate", headers)
        self.assertIn("只读", payload["error"])


class HealthEndpointTests(unittest.TestCase):
    def test_healthz_is_minimal_and_auth_exempt_but_still_host_checked(self):
        for lan_read_only in (False, True):
            with self.subTest(lan_read_only=lan_read_only):
                harness = HandlerHarness(lan_read_only=lan_read_only)
                status, headers, payload = harness.request("GET", "/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(payload, {"ok": True})
                self.assertNotIn("www-authenticate", headers)

                status, _, payload = harness.request(
                    "GET", "/healthz", host="attacker.example:18788"
                )
                self.assertEqual(status, 403)
                self.assertEqual(set(payload), {"error"})

    def test_healthz_is_omitted_from_access_logs(self):
        handler_type = SERVICE.create_handler(FakeWorkspace())
        handler = handler_type.__new__(handler_type)
        logged: list[tuple] = []
        handler.log_message = lambda *args: logged.append(args)
        handler.requestline = "GET /healthz HTTP/1.1"
        handler.path = "/healthz"

        handler.log_request(200, 11)
        self.assertEqual(logged, [])

        handler.path = "/api/start-stop/status"
        handler.requestline = "GET /api/start-stop/status HTTP/1.1"
        handler.log_request(200, 11)
        self.assertEqual(len(logged), 1)


class LanAuthLaunchTests(unittest.TestCase):
    def test_cli_requires_auth_file_exactly_for_lan_mode(self):
        parser = SERVICE.build_parser()
        required = [
            "--database", "/tmp/test.sqlite3",
            "--analysis-dir", "/tmp/published",
            "--scratch-dir", "/tmp/scratch",
        ]
        local_with_auth = parser.parse_args(
            required + ["--lan-auth-file", "/tmp/credential"]
        )
        with self.assertRaisesRegex(ValueError, "\u4ec5可"):
            SERVICE._validated_launch(local_with_auth)

        lan_without_auth = parser.parse_args(
            required + ["--lan-read-only", "--bind", "192.168.110.158"]
        )
        with self.assertRaisesRegex(ValueError, "\u5fc5须声明"):
            SERVICE._validated_launch(lan_without_auth)

        lan_no_auth = parser.parse_args(
            required
            + ["--lan-read-only", "--lan-no-auth", "--bind", "192.168.110.158"]
        )
        self.assertEqual(
            SERVICE._validated_launch(lan_no_auth),
            ("192.168.110.158", "192.168.110.158", 8787),
        )

        local_no_auth = parser.parse_args(required + ["--lan-no-auth"])
        with self.assertRaisesRegex(ValueError, "仅可"):
            SERVICE._validated_launch(local_no_auth)

        lan_both = parser.parse_args(
            required
            + [
                "--lan-read-only",
                "--lan-no-auth",
                "--lan-auth-file",
                "/tmp/credential",
            ]
        )
        with self.assertRaisesRegex(ValueError, "不能与"):
            SERVICE._validated_launch(lan_both)

    def test_handler_rejects_missing_lan_credentials_and_local_credentials(self):
        with self.assertRaisesRegex(ValueError, "\u5fc5须配置"):
            SERVICE.create_handler(FakeWorkspace(), lan_read_only=True)
        with self.assertRaisesRegex(ValueError, "\u4ec5可"):
            SERVICE.create_handler(
                FakeWorkspace(),
                lan_auth_credentials=SERVICE.BasicAuthCredentials(
                    USERNAME, PASSWORD
                ),
            )
        with self.assertRaisesRegex(ValueError, "不能同时"):
            SERVICE.create_handler(
                FakeWorkspace(),
                lan_read_only=True,
                lan_no_auth=True,
                lan_auth_credentials=SERVICE.BasicAuthCredentials(
                    USERNAME, PASSWORD
                ),
            )


class LanAuthDeploymentTests(unittest.TestCase):
    def test_compose_mounts_secret_only_into_lan_and_uses_healthz(self):
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(
            encoding="utf-8"
        )
        local = compose.split("  local:", 1)[1].split("  lan-readonly:", 1)[0]
        lan = compose.split("  lan-readonly:", 1)[1]

        self.assertNotIn("--lan-auth-file", local)
        self.assertNotIn("lan-basic-auth.txt", local)
        self.assertIn("--lan-auth-file", lan)
        self.assertIn("lan-basic-auth.txt", lan)
        self.assertIn("create_host_path: false", lan)
        self.assertIn("/healthz", local)
        self.assertIn("/healthz", lan)
        self.assertNotIn("/api/start-stop/status", local)
        self.assertNotIn("/api/start-stop/status", lan)

        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("/run/start-stop-secrets", dockerfile)
        self.assertIn("chmod 700 /run/start-stop-secrets", dockerfile)

    def test_wrapper_only_creates_secret_directory_not_credentials(self):
        wrapper = (
            Path(__file__).resolve().parents[1] / "scripts" / "docker-compose.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"$project_dir/state/docker/secrets"', wrapper)
        self.assertNotIn("username:password\n", wrapper)
        self.assertNotIn("lan-basic-auth.txt", wrapper)


if __name__ == "__main__":
    unittest.main()
