from __future__ import annotations

import argparse
import ast
import http.client
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "echem_start_stop_service_scope_tests",
    ROOT / "start_stop_service.py",
)
assert SPEC and SPEC.loader
SERVICE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVICE
SPEC.loader.exec_module(SERVICE)

TEST_LAN_AUTH = SERVICE.BasicAuthCredentials("reader", "test-password")
TEST_LAN_AUTH_HEADER = "Basic cmVhZGVyOnRlc3QtcGFzc3dvcmQ="


class FakeWorkspace:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[str, object]] = []
        self.pdf_path = root / "atlas.pdf"
        self.pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    def status(self):
        return {
            "available": True,
            "execution": {
                "ready": True,
                "update_ready": True,
                "render_ready": True,
                "collection_ready": True,
            },
            "repository": {
                "storage_mode": "sqlite_blob_repository",
                "files": 9,
                "bytes": 1234,
            },
            "job": {"status": "idle", "result": {}},
        }

    def materials(self):
        return {"materials": [], "counts": {"materials": 0}}

    def series(self):
        return {"series": [], "count": 0}

    def chart_data(self, **kwargs):
        self.calls.append(("chart", kwargs))
        return {"series": [], **kwargs}

    def pdf(self, kind):
        self.calls.append(("pdf", kind))
        return self.pdf_path, "启停图集.pdf"

    def save_materials(self, **kwargs):
        self.calls.append(("materials", kwargs))
        return {"saved": True, **kwargs}

    def start_job(self, action, **kwargs):
        self.calls.append(("job", {"action": action, **kwargs}))
        return {"id": "job-1", "action": action, "status": "queued"}


class FakeCvEisAnalyzer:
    def catalog(self):
        return {
            "counts": {"paired": 1, "ready": 1},
            "materials": [{"analysis_id": "a" * 20, "status": "ready"}],
        }

    def curve(self, analysis_id):
        if analysis_id != "a" * 20:
            raise SERVICE.CvEisAnalysisError("找不到结果。", 404)
        return {"analysis_id": analysis_id, "points": [{"current": -10}]}


class LiveServer:
    def __init__(
        self,
        workspace: FakeWorkspace,
        *,
        lan_read_only: bool = False,
        public_host: str = "127.0.0.1",
        trusted_container_proxy: bool = False,
        cv_eis_analyzer=None,
    ):
        self.lan_read_only = lan_read_only
        self.handler_type = SERVICE.create_handler(
            workspace,
            lan_read_only=lan_read_only,
            lan_auth_credentials=TEST_LAN_AUTH if lan_read_only else None,
            public_host=public_host,
            trusted_container_proxy=trusted_container_proxy,
            cv_eis_analyzer=cv_eis_analyzer or FakeCvEisAnalyzer(),
        )
        self.port = 18787 if not lan_read_only else 18788

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        body: dict | bytes | None = None,
        content_type: str | None = None,
        origin: str | None = None,
        peer: str = "127.0.0.1",
    ) -> tuple[int, dict | bytes, dict[str, str]]:
        encoded: bytes | None
        if isinstance(body, dict):
            encoded = json.dumps(body).encode("utf-8")
        else:
            encoded = body
        message = http.client.HTTPMessage()
        message.add_header("Host", host or f"127.0.0.1:{self.port}")
        if self.lan_read_only:
            message.add_header("Authorization", TEST_LAN_AUTH_HEADER)
        if encoded is not None:
            message.add_header("Content-Length", str(len(encoded)))
        if content_type is not None:
            message.add_header("Content-Type", content_type)
        if origin is not None:
            message.add_header("Origin", origin)
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = message
        handler.rfile = io.BytesIO(encoded or b"")
        handler.client_address = (peer, 55000)
        handler.server = type(
            "TestServer",
            (),
            {"server_address": ("127.0.0.1", self.port)},
        )()
        responses: list[tuple[int, object, dict[str, str]]] = []
        handler.send_json = lambda payload, status=200: responses.append(
            (int(status), payload, {"content-type": "application/json"})
        )
        handler.send_static = lambda relative: responses.append(
            (200, {"static": relative}, {"content-type": "text/plain"})
        )
        handler.send_file_download = lambda file_path, filename: responses.append(
            (
                200,
                {"download": str(file_path), "filename": filename},
                {"content-type": "application/pdf"},
            )
        )
        getattr(handler, f"do_{method}")()
        if len(responses) != 1:
            raise AssertionError(f"expected one response, received {responses!r}")
        return responses[0]


class MinimalServiceImportTests(unittest.TestCase):
    def test_entrypoint_does_not_import_general_platform_modules(self):
        source = (ROOT / "start_stop_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        self.assertNotIn("app", imports)
        for forbidden in (
            "echem_platform.analysis",
            "echem_platform.parsers",
            "echem_platform.configuration",
            "echem_platform.control",
            "echem_platform.protocols",
        ):
            self.assertNotIn(forbidden, imports)
        echem_imports = [name for name in imports if name.startswith("echem_platform")]
        self.assertEqual(
            echem_imports,
            [
                "echem_platform.start_stop",
                "echem_platform.start_stop_auto_update",
                "echem_platform.start_stop_collection",
                "echem_platform.start_stop_collection_config",
                "echem_platform.start_stop_cv_eis",
                "echem_platform.start_stop_database",
                "echem_platform.start_stop_live_preview",
                "echem_platform.start_stop_workstations",
            ],
        )

    def test_cli_exposes_only_fixed_repository_inputs(self):
        options = {
            option
            for action in SERVICE.build_parser()._actions
            for option in action.option_strings
        }
        for option in (
            "--database",
            "--cv-eis-database",
            "--analysis-dir",
            "--analysis-script",
            "--collection-config",
            "--workstation-state-file",
            "--workstation-poll-seconds",
            "--workstation-discovery-seconds",
            "--scratch-dir",
            "--backup-dir",
            "--estimated-output-bytes",
            "--bind",
            "--port",
            "--public-host",
            "--public-port",
            "--lan-read-only",
            "--lan-no-auth",
            "--lan-auth-file",
            "--container-mode",
        ):
            self.assertIn(option, options)
        for forbidden in ("--instrument", "--protocol", "--run-root", "--watch-root"):
            self.assertNotIn(forbidden, options)

    def test_compose_mounts_backup_status_only_into_local_service(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        local = compose.split("  local:", 1)[1].split("  lan-readonly:", 1)[0]
        lan = compose.split("  lan-readonly:", 1)[1]

        self.assertIn("--backup-dir", local)
        self.assertIn("/app/state/backups", local)
        self.assertIn("--estimated-output-bytes", local)
        self.assertIn('"400000000"', local)
        self.assertIn("read_only: true", local)
        self.assertNotIn("--backup-dir", lan)
        self.assertNotIn("/app/state/backups", lan)
        self.assertIn("--workstation-state-file", local)
        self.assertIn("--workstation-state-file", lan)
        self.assertIn("/app/state/published-cache/workstation-monitor.json", local)
        self.assertIn("/app/state/published-cache/workstation-monitor.json", lan)


class MinimalServiceRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = FakeWorkspace(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_start_stop_pages_are_visible(self):
        with LiveServer(self.workspace) as server:
            for path in ("/", "/start-stop", "/start-stop/config"):
                with self.subTest(path=path):
                    status, body, _ = server.request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertEqual(body, {"static": "start-stop-config.html"})
            status, body, _ = server.request("GET", "/start-stop/analysis")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"static": "start-stop.html"})
            status, body, _ = server.request("GET", "/start-stop/workstations")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"static": "start-stop-workstations.html"})
            status, body, _ = server.request("GET", "/start-stop/materials")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"static": "start-stop-materials.html"})
            status, body, _ = server.request("GET", "/start-stop/cv-eis")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"static": "start-stop-cv-eis.html"})
            for path in (
                "/start-stop.html",
                "/start-stop/",
                "/start-stop/config/",
                "/start-stop/analysis/",
                "/start-stop/workstations/",
                "/start-stop/cv-eis/",
                "/start-stop/materials/",
                "/analysis",
                "/protocol",
                "/steps",
                "/environment",
                "/monitor",
                "/index.html",
            ):
                with self.subTest(path=path):
                    self.assertEqual(server.request("GET", path)[0], 404)

    def test_only_start_stop_apis_are_visible(self):
        with LiveServer(self.workspace) as server:
            for path in (
                "/api/start-stop/status",
                "/api/start-stop/materials",
                "/api/start-stop/series",
                "/api/start-stop/connectivity",
                "/api/start-stop/workstations",
                "/api/start-stop/cv-eis",
            ):
                with self.subTest(path=path):
                    self.assertEqual(server.request("GET", path)[0], 200)
            status, chart, _ = server.request(
                "GET",
                "/api/start-stop/chart?series=A&metric=cathodic&x=cycle&mode=raw",
            )
            self.assertEqual(status, 200)
            self.assertEqual(chart["series_ids"], ["A"])
            status, curve, _ = server.request(
                "GET",
                f"/api/start-stop/cv-eis/curve?analysis_id={'a' * 20}",
            )
            self.assertEqual(status, 200)
            self.assertEqual(curve["analysis_id"], "a" * 20)
            self.assertEqual(
                server.request("GET", "/api/start-stop/cv-eis?unexpected=1")[0],
                400,
            )
            self.assertEqual(
                server.request("GET", "/api/start-stop/cv-eis/curve")[0],
                400,
            )
            self.assertEqual(server.request("GET", "/api/start-stop/pdf?kind=standard")[0], 200)
            for path in (
                "/api/status",
                "/api/runs",
                "/api/files/tree",
                "/api/audit",
                "/api/protocols",
                "/api/control/capabilities",
                "/api/start-stop/status/..",
                "/api/start-stop%2fstatus",
            ):
                with self.subTest(path=path):
                    self.assertEqual(server.request("GET", path)[0], 404)

    def test_static_allowlist_excludes_the_general_application(self):
        with LiveServer(self.workspace) as server:
            for path in (
                "/static/styles.css",
                "/static/workbench.css",
                "/static/start-stop.css",
                "/static/start-stop.js",
                "/static/start-stop-shell.js",
                "/static/start-stop-config.css",
                "/static/start-stop-config.html",
                "/static/start-stop-config.js",
                "/static/start-stop-cv-eis.css",
                "/static/start-stop-cv-eis.html",
                "/static/start-stop-cv-eis.js",
                "/static/start-stop-workstations.css",
                "/static/start-stop-workstations.html",
                "/static/start-stop-workstations.js",
                "/static/icons/gear.svg",
            ):
                with self.subTest(path=path):
                    self.assertEqual(server.request("GET", path)[0], 200)
            for path in (
                "/static/app.js",
                "/static/analysis.js",
                "/static/protocol.js",
                "/static/monitor.js",
                "/static/../app.py",
                "/static/icons/../app.js",
                "/static/icons/gear.svg/extra",
            ):
                with self.subTest(path=path):
                    self.assertEqual(server.request("GET", path)[0], 404)

    def test_status_declares_repository_profile_and_access_capabilities(self):
        with LiveServer(self.workspace) as server:
            status, payload, _ = server.request("GET", "/api/start-stop/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["deployment_profile"], "start_stop_repository")
        self.assertEqual(payload["access_mode"], "local_read_write")
        self.assertTrue(payload["capabilities"]["database_backed"])
        self.assertTrue(payload["capabilities"]["can_update_data"])
        self.assertTrue(payload["capabilities"]["can_render_atlas"])
        self.assertIn("database", payload["capabilities"]["data_scope"])

    def test_status_exposes_the_live_service_version_without_private_details(self):
        with mock.patch.dict(
            SERVICE.os.environ,
            {
                "START_STOP_SERVICE_VERSION": "0.4.0-dev.12",
                "START_STOP_IMAGE_REFERENCE": "start-stop-analysis:0.4.0-dev.12-windows",
            },
            clear=False,
        ):
            public = SERVICE.public_status(
                self.workspace.status(),
                lan_read_only=True,
            )

        self.assertEqual(public["service"]["version"], "0.4.0-dev.12")
        self.assertEqual(
            public["service"]["image_reference"],
            "start-stop-analysis:0.4.0-dev.12-windows",
        )
        self.assertNotIn("path", json.dumps(public["service"]).lower())

    def test_status_safety_is_path_free_and_provenance_is_mapped(self):
        digest = "a" * 64
        payload = {
            "available": True,
            "job": {"status": "idle", "result": {}},
            "analysis_provenance": {
                "state": "legacy_unverified",
                "artifact_generation_id": 9,
                "artifact_manifest_sha256": digest,
                "runtime": {
                    "image_or_service_version": "registry.example/start-stop:0.4.0",
                    "private_path": "/private/runtime",
                },
            },
            "safety": {
                "storage": {
                    "preflight_ok": True,
                    "free_bytes": 1234,
                    "database_path": "/private/database.sqlite3",
                },
                "backup": {
                    "configured": True,
                    "available": False,
                    "backup_dir": "/private/backups",
                },
                "provenance": {"secret": "/private/provenance"},
            },
        }

        for lan_read_only in (False, True):
            with self.subTest(lan_read_only=lan_read_only):
                public = SERVICE.public_status(payload, lan_read_only=lan_read_only)
                self.assertTrue(public["safety"]["storage"]["preflight_ok"])
                self.assertTrue(public["safety"]["backup"]["configured"])
                self.assertEqual(public["safety"]["provenance"]["state"], "legacy")
                self.assertEqual(
                    public["safety"]["provenance"]["image_reference"],
                    "registry.example/start-stop:0.4.0",
                )
                self.assertNotIn("/private", json.dumps(public["safety"]))

    def test_every_get_requires_the_expected_host(self):
        with LiveServer(self.workspace) as server:
            self.assertEqual(
                server.request(
                    "GET",
                    "/api/start-stop/status",
                    host=f"attacker.example:{server.port}",
                )[0],
                403,
            )
            self.assertEqual(
                server.request(
                    "GET",
                    "/api/start-stop/status",
                    host=f"127.0.0.1:{server.port + 1}",
                )[0],
                403,
            )

    def test_local_posts_keep_loopback_origin_and_content_type_guards(self):
        body = {
            "action": "render",
            "render_data_mode": "raw",
            "render_material_scope": "updated",
        }
        with LiveServer(self.workspace) as server:
            status, payload, _ = server.request(
                "POST",
                "/api/start-stop/jobs",
                body=body,
                content_type="application/json; charset=utf-8",
                origin=f"http://127.0.0.1:{server.port}",
            )
            self.assertEqual(status, 202)
            self.assertEqual(payload["action"], "render")
            self.assertEqual(
                self.workspace.calls[-1],
                (
                    "job",
                    {
                        "action": "render",
                        "render_data_mode": "raw",
                        "render_material_scope": "updated",
                    },
                ),
            )
            export_body = {
                "action": "render",
                "render_data_mode": "both",
                "render_material_scope": "all",
                "export_pdf": True,
            }
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    body=export_body,
                    content_type="application/json",
                )[0],
                202,
            )
            self.assertTrue(self.workspace.calls[-1][1]["export_pdf"])
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    body={**export_body, "export_pdf": "true"},
                    content_type="application/json",
                )[0],
                400,
            )
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    body=body,
                    content_type="text/plain",
                )[0],
                415,
            )
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    body=body,
                    content_type="application/json",
                    origin="https://attacker.example",
                )[0],
                403,
            )
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    host=f"192.168.1.8:{server.port}",
                    body=body,
                    content_type="application/json",
                )[0],
                403,
            )
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/scan",
                    body={},
                    content_type="application/json",
                )[0],
                404,
            )

    def test_container_proxy_is_explicit_and_still_requires_loopback_host(self):
        body = {"action": "render"}
        with LiveServer(self.workspace) as server:
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    body=body,
                    content_type="application/json",
                    peer="172.20.0.1",
                )[0],
                403,
            )
        with LiveServer(self.workspace, trusted_container_proxy=True) as server:
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    body=body,
                    content_type="application/json",
                    peer="172.20.0.1",
                )[0],
                202,
            )
            self.assertEqual(
                server.request(
                    "POST",
                    "/api/start-stop/jobs",
                    host=f"192.168.110.158:{server.port}",
                    body=body,
                    content_type="application/json",
                    peer="172.20.0.1",
                )[0],
                403,
            )

    def test_lan_is_read_only_for_all_write_methods(self):
        lan_host = "192.168.110.158"
        with LiveServer(
            self.workspace,
            lan_read_only=True,
            public_host=lan_host,
        ) as server:
            expected_host = f"{lan_host}:{server.port}"
            status, payload, _ = server.request(
                "GET",
                "/api/start-stop/status",
                host=expected_host,
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["access_mode"], "lan_read_only")
            self.assertFalse(payload["capabilities"]["allowed_here"])
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                for path in (
                    "/api/start-stop/materials",
                    "/api/start-stop/jobs",
                    "/api/control/runs",
                    "/anything",
                ):
                    with self.subTest(method=method, path=path):
                        self.assertEqual(
                            server.request(method, path, host=expected_host)[0],
                            403,
                        )
            self.assertEqual(
                server.request(
                    "GET",
                    "/api/start-stop/status",
                    host=f"192.168.110.159:{server.port}",
                )[0],
                403,
            )


class RepositoryStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def args(self, *, lan_read_only: bool) -> argparse.Namespace:
        return argparse.Namespace(
            database=str(self.root / "state" / "start-stop.sqlite3"),
            analysis_dir=str(self.root / "published"),
            analysis_script=str(self.root / "analyze.py"),
            collection_config=str(self.root / "collection.json"),
            scratch_dir=str(self.root / "scratch"),
            lan_read_only=lan_read_only,
        )

    def test_local_startup_restores_published_cache_from_database(self):
        calls: list[tuple] = []

        class Database:
            def __init__(self, path):
                self.path = path

            def restore_current_artifacts(self, target):
                calls.append(("restore", target))

        class Workspace:
            def __init__(self, database, analysis_dir, **kwargs):
                self.database = database
                self.analysis_dir = analysis_dir
                calls.append(("workspace", database, analysis_dir, kwargs))

            def ensure_published_cache(self):
                calls.append(("ensure", self.analysis_dir))
                self.database.restore_current_artifacts(self.analysis_dir)

        SERVICE.build_workspace(
            self.args(lan_read_only=False),
            database_type=Database,
            workspace_type=Workspace,
        )
        self.assertEqual(calls[0][0], "workspace")
        self.assertTrue(calls[0][3]["repository_mode"])
        self.assertEqual(calls[1], ("ensure", (self.root / "published").resolve()))
        self.assertEqual(calls[2], ("restore", (self.root / "published").resolve()))

    def test_lan_startup_never_restores_or_publishes_database_artifacts(self):
        calls: list[tuple] = []

        class Database:
            def __init__(self, path):
                self.path = path

            def restore_current_artifacts(self, target):
                calls.append(("restore", target))

        class Workspace:
            def __init__(self, database, analysis_dir, **kwargs):
                calls.append(("workspace", analysis_dir, kwargs))

            def ensure_published_cache(self):
                calls.append(("ensure",))

        SERVICE.build_workspace(
            self.args(lan_read_only=True),
            database_type=Database,
            workspace_type=Workspace,
        )
        self.assertFalse(any(call[0] == "restore" for call in calls))
        self.assertFalse(any(call[0] == "ensure" for call in calls))
        self.assertEqual(calls[0][0], "workspace")

    def test_backup_safety_inputs_are_local_only(self):
        calls: list[dict] = []

        class Database:
            def __init__(self, path):
                self.path = path

        class Workspace:
            def __init__(self, _database, _analysis_dir, **kwargs):
                self.database = _database
                calls.append(kwargs)

            def ensure_published_cache(self):
                return None

        local_args = self.args(lan_read_only=False)
        local_args.backup_dir = str(self.root / "backups")
        local_args.estimated_output_bytes = 400_000_000
        SERVICE.build_workspace(
            local_args,
            database_type=Database,
            workspace_type=Workspace,
        )
        lan_args = self.args(lan_read_only=True)
        lan_args.backup_dir = "/must/not/reach/lan"
        lan_args.estimated_output_bytes = 999
        SERVICE.build_workspace(
            lan_args,
            database_type=Database,
            workspace_type=Workspace,
        )

        self.assertEqual(calls[0]["backup_dir"], (self.root / "backups").resolve())
        self.assertEqual(calls[0]["estimated_output_bytes"], 400_000_000)
        self.assertIsNone(calls[1]["backup_dir"])
        self.assertEqual(calls[1]["estimated_output_bytes"], 0)

    def test_container_public_endpoint_separates_local_and_lan_hosts(self):
        local = argparse.Namespace(
            port=8787,
            bind="0.0.0.0",
            public_host="127.0.0.1",
            public_port=18787,
            lan_read_only=False,
            lan_auth_file="",
            container_mode=True,
        )
        lan = argparse.Namespace(
            port=8787,
            bind="0.0.0.0",
            public_host="192.168.110.158",
            public_port=18788,
            lan_read_only=True,
            lan_auth_file="/run/secrets/test",
            container_mode=True,
        )
        original = SERVICE.os.environ.get("ECHEM_CONTAINER_MODE")
        SERVICE.os.environ["ECHEM_CONTAINER_MODE"] = "1"
        try:
            self.assertEqual(
                SERVICE._validated_launch(local),
                ("0.0.0.0", "127.0.0.1", 18787),
            )
            self.assertEqual(
                SERVICE._validated_launch(lan),
                ("0.0.0.0", "192.168.110.158", 18788),
            )
            local.public_host = "192.168.110.158"
            with self.assertRaises(ValueError):
                SERVICE._validated_launch(local)
        finally:
            if original is None:
                SERVICE.os.environ.pop("ECHEM_CONTAINER_MODE", None)
            else:
                SERVICE.os.environ["ECHEM_CONTAINER_MODE"] = original


if __name__ == "__main__":
    unittest.main()
