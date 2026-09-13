from __future__ import annotations

import datetime as dt
import http.client
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
import unicodedata
import urllib.parse
from pathlib import Path

import start_stop_service as SERVICE
from echem_platform.start_stop import StartStopWorkspace, StartStopWorkspaceError
from echem_platform.start_stop_database import StartStopDatabase

TEST_LAN_AUTH = SERVICE.BasicAuthCredentials("reader", "test-password")
TEST_LAN_AUTH_HEADER = "Basic cmVhZGVyOnRlc3QtcGFzc3dvcmQ="

GALVANOSTATIC_START_STOP = (
    b"ID_GalSquareWave\n"
    b"Time/s,Potential/V\n"
    b"0,-0.300\n"
)


class RecordingStartStopDatabase(StartStopDatabase):
    def __init__(self, path: str | Path):
        self.freeze_calls: list[dict[str, object]] = []
        super().__init__(path)

    def freeze_snapshot(
        self,
        candidate_only: bool = True,
        batch_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        self.freeze_calls.append(
            {
                "candidate_only": candidate_only,
                "batch_id": batch_id,
                "metadata": dict(metadata or {}),
            }
        )
        return super().freeze_snapshot(
            candidate_only=candidate_only,
            batch_id=batch_id,
            metadata=metadata,
        )


class FailAfterBlobDatabase(StartStopDatabase):
    """Inject a failure after the BLOB write but before the transaction commits."""

    def _ensure_blob_from_path(self, connection, path, sha256, size_bytes):
        super()._ensure_blob_from_path(
            connection,
            path,
            sha256,
            size_bytes,
        )
        raise OSError("injected failure after blob write")


class UploadPrepareWorkspace(StartStopWorkspace):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.process_commands: list[tuple[str, ...]] = []
        self.materialized_files: list[str] = []

    def _run_process(self, command, *, cwd, environment):
        del cwd
        command_tuple = tuple(str(item) for item in command)
        self.process_commands.append(command_tuple)
        if "echem_platform.start_stop_collection" in command_tuple:
            return subprocess.CompletedProcess(
                command_tuple,
                99,
                "",
                "remote collection must not run for prepare_upload",
            )

        source_root = Path(environment["START_STOP_SOURCE_ROOT"])
        output_root = Path(environment["START_STOP_OUTPUT_DIR"])
        self.materialized_files = sorted(
            item.relative_to(source_root).as_posix()
            for item in source_root.rglob("*")
            if item.is_file() and item.name != ".start-stop-manifest.json"
        )
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "upload-prepare-result.json").write_text(
            json.dumps({"prepared": True}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command_tuple,
            0,
            json.dumps({"stage": "prepared", "materials": 1}),
            "",
        )


class UploadMetadataValidationTests(unittest.TestCase):
    def test_safe_nested_path_is_normalized_and_mapped_into_manual_upload(self):
        decomposed_name = "Cafe\u0301.TXT"
        modified_ms = 1_767_225_600_123

        metadata = StartStopWorkspace.validate_upload_metadata(
            filename=decomposed_name,
            relative_path=f"批次甲/{decomposed_name}",
            group="NiMo 数据",
            last_modified=modified_ms,
            size_bytes=123,
        )

        normalized_name = unicodedata.normalize("NFC", decomposed_name)
        self.assertEqual(metadata["filename"], normalized_name)
        self.assertEqual(metadata["relative_path"], f"批次甲/{normalized_name}")
        self.assertEqual(
            metadata["repository_path"],
            f"手动上传/NiMo 数据/批次甲/{normalized_name}",
        )
        self.assertEqual(metadata["size_bytes"], 123)
        self.assertEqual(metadata["last_modified_ms"], modified_ms)
        self.assertEqual(metadata["modified_ns"], modified_ms * 1_000_000)
        self.assertEqual(
            metadata["modified_utc"],
            dt.datetime.fromtimestamp(
                modified_ms / 1000,
                tz=dt.timezone.utc,
            ).isoformat(timespec="milliseconds"),
        )

    def test_every_declared_upload_extension_is_accepted_case_insensitively(self):
        for extension in StartStopWorkspace.UPLOAD_EXTENSIONS:
            with self.subTest(extension=extension):
                filename = f"sample{extension.upper()}"
                metadata = StartStopWorkspace.validate_upload_metadata(
                    filename=filename,
                    relative_path=filename,
                    group="batch",
                    last_modified=0,
                    size_bytes=1,
                )
                self.assertEqual(metadata["filename"], filename)

    def test_unsafe_paths_and_disallowed_extensions_are_rejected(self):
        base = {
            "filename": "启停.txt",
            "relative_path": "启停.txt",
            "group": "batch",
            "last_modified": 0,
            "size_bytes": 1,
        }
        invalid_cases = (
            {"relative_path": "../启停.txt"},
            {"relative_path": "/启停.txt"},
            {"relative_path": "C:\\data\\启停.txt"},
            {"relative_path": "folder//启停.txt"},
            {"relative_path": "folder/./启停.txt"},
            {"relative_path": "folder/other.txt"},
            {"group": "folder/group"},
            {"filename": "启停.exe", "relative_path": "启停.exe"},
            {"filename": "no-extension", "relative_path": "no-extension"},
        )
        for changes in invalid_cases:
            with self.subTest(changes=changes):
                payload = {**base, **changes}
                with self.assertRaises(StartStopWorkspaceError):
                    StartStopWorkspace.validate_upload_metadata(**payload)

        unsupported = {**base, "filename": "启停.exe", "relative_path": "启停.exe"}
        with self.assertRaises(StartStopWorkspaceError) as caught:
            StartStopWorkspace.validate_upload_metadata(**unsupported)
        self.assertEqual(caught.exception.status, 415)

    def test_size_and_modified_time_bounds_are_enforced(self):
        base = {
            "filename": "启停.txt",
            "relative_path": "启停.txt",
            "group": "batch",
            "last_modified": 0,
            "size_bytes": 1,
        }
        accepted = StartStopWorkspace.validate_upload_metadata(
            **{**base, "size_bytes": StartStopWorkspace.MAX_UPLOAD_BYTES}
        )
        self.assertEqual(accepted["size_bytes"], 128 * 1024 * 1024)

        with self.assertRaises(StartStopWorkspaceError) as too_large:
            StartStopWorkspace.validate_upload_metadata(
                **{
                    **base,
                    "size_bytes": StartStopWorkspace.MAX_UPLOAD_BYTES + 1,
                }
            )
        self.assertEqual(too_large.exception.status, 413)

        for changes in (
            {"size_bytes": 0},
            {"size_bytes": "not-a-number"},
            {"last_modified": -1},
            {"last_modified": 9_223_372_036_855},
            {"last_modified": "not-a-time"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(StartStopWorkspaceError):
                    StartStopWorkspace.validate_upload_metadata(
                        **{**base, **changes}
                    )


class UploadRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_workspace(
        self,
        database: StartStopDatabase,
        workspace_type=StartStopWorkspace,
    ) -> StartStopWorkspace:
        analysis_script = self.root / "analysis" / "analyze.py"
        analysis_script.parent.mkdir(parents=True, exist_ok=True)
        analysis_script.write_text("# test analysis script", encoding="utf-8")
        return workspace_type(
            database,
            self.root / "published",
            analysis_script=analysis_script,
            scratch_dir=self.root / "scratch",
            repository_mode=True,
            python_executable=sys.executable,
            timeout_seconds=60,
        )

    @staticmethod
    def upload(
        workspace: StartStopWorkspace,
        staged: Path,
        *,
        modified_ms: int,
    ) -> dict:
        return workspace.upload_file(
            staged,
            filename="启停.txt",
            relative_path="材料甲/启停.txt",
            group="手工实验",
            last_modified=modified_ms,
            size_bytes=staged.stat().st_size,
        )

    def test_upload_stores_exact_blob_versions_without_leaking_staging_path(self):
        database = StartStopDatabase(self.root / "state" / "repository.sqlite3")
        workspace = self.make_workspace(database)
        staged = self.root / "browser-request-body.tmp"
        staged.write_bytes(GALVANOSTATIC_START_STOP)

        first = self.upload(workspace, staged, modified_ms=1_000)
        duplicate = self.upload(workspace, staged, modified_ms=1_000)
        changed_content = GALVANOSTATIC_START_STOP + b"1,-0.310\n"
        staged.write_bytes(changed_content)
        second = self.upload(workspace, staged, modified_ms=2_000)

        self.assertTrue(first["changed"])
        self.assertFalse(first["duplicate"])
        self.assertFalse(duplicate["changed"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["upload_id"], first["upload_id"])
        self.assertTrue(second["changed"])
        self.assertNotEqual(second["upload_id"], first["upload_id"])
        self.assertEqual(first["repository_path"], "手动上传/手工实验/材料甲/启停.txt")
        self.assertTrue(first["is_candidate"])
        self.assertEqual(first["candidate_kind"], "gal_square_wave")
        self.assertNotIn(str(staged), json.dumps([first, duplicate, second]))

        with database.session() as connection:
            versions = connection.execute(
                """SELECT sv.id,sv.version_number,sv.modified_ns,sv.metadata_json,
                          b.sha256,b.size_bytes,b.content
                   FROM source_versions sv
                   JOIN content_blobs b ON b.id=sv.blob_id
                   ORDER BY sv.version_number"""
            ).fetchall()
            current = connection.execute(
                """SELECT sv.id,sv.version_number
                   FROM source_current sc
                   JOIN source_selections ss ON ss.id=sc.selection_id
                   JOIN source_versions sv ON sv.id=ss.source_version_id"""
            ).fetchone()
            source = connection.execute(
                "SELECT machine_id,root_label,remote_path FROM sources"
            ).fetchone()
            dump = "\n".join(connection.iterdump())

        self.assertEqual(len(versions), 2)
        self.assertEqual([row["version_number"] for row in versions], [1, 2])
        self.assertEqual(bytes(versions[0]["content"]), GALVANOSTATIC_START_STOP)
        self.assertEqual(bytes(versions[1]["content"]), changed_content)
        self.assertEqual(versions[0]["modified_ns"], 1_000_000_000)
        self.assertEqual(versions[1]["modified_ns"], 2_000_000_000)
        self.assertEqual(current["id"], second["upload_id"])
        self.assertEqual(current["version_number"], 2)
        self.assertEqual(source["machine_id"], "browser-upload")
        self.assertEqual(source["root_label"], "manual-upload")
        self.assertEqual(
            source["remote_path"],
            "手动上传\\手工实验\\材料甲\\启停.txt",
        )
        self.assertNotIn(str(staged), dump)
        for row in versions:
            self.assertNotIn(str(staged), row["metadata_json"])
        self.assertTrue(database.integrity_check(verify_blobs=True)["ok"])

    def test_blob_write_failure_rolls_back_the_entire_upload(self):
        database = FailAfterBlobDatabase(
            self.root / "state" / "failed-upload.sqlite3"
        )
        workspace = self.make_workspace(database)
        staged = self.root / "failed-request-body.tmp"
        staged.write_bytes(GALVANOSTATIC_START_STOP)

        with self.assertRaisesRegex(OSError, "injected failure"):
            self.upload(workspace, staged, modified_ms=1_000)

        with database.session() as connection:
            for table in (
                "content_blobs",
                "sources",
                "source_versions",
                "source_selections",
                "source_current",
            ):
                with self.subTest(table=table):
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    self.assertEqual(count, 0)

    def test_prepare_upload_requires_local_ids_freezes_snapshot_and_skips_collection(self):
        database = RecordingStartStopDatabase(
            self.root / "state" / "prepare-upload.sqlite3"
        )
        workspace = self.make_workspace(database, UploadPrepareWorkspace)
        self.assertIsInstance(workspace, UploadPrepareWorkspace)

        directly_imported = self.root / "direct-import.txt"
        directly_imported.write_bytes(GALVANOSTATIC_START_STOP)
        direct = database.import_source_path(
            directly_imported,
            {
                "machine_id": "direct-import",
                "root_label": "fixture",
                "remote_path": "direct/启停.txt",
                "repository_path": "direct/启停.txt",
                "size_bytes": directly_imported.stat().st_size,
                "last_write_ticks": 1,
            },
        )

        with self.assertRaises(StartStopWorkspaceError) as missing:
            workspace.start_job("prepare_upload")
        self.assertEqual(missing.exception.status, 400)
        with self.assertRaises(StartStopWorkspaceError) as foreign:
            workspace.start_job(
                "prepare_upload",
                upload_ids=[direct["version_id"]],
            )
        self.assertEqual(foreign.exception.status, 409)

        staged = self.root / "accepted-request-body.tmp"
        staged.write_bytes(GALVANOSTATIC_START_STOP + b"2,-0.320\n")
        accepted = self.upload(workspace, staged, modified_ms=3_000)
        with self.assertRaises(StartStopWorkspaceError) as duplicate:
            workspace.start_job(
                "prepare_upload",
                upload_ids=[accepted["upload_id"], accepted["upload_id"]],
            )
        self.assertEqual(duplicate.exception.status, 400)
        queued = workspace.start_job(
            "prepare_upload",
            upload_ids=[accepted["upload_id"]],
        )
        self.assertEqual(queued["action"], "prepare_upload")
        job = self.wait_for_job(workspace)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["uploaded_files"], 1)
        self.assertEqual(len(database.freeze_calls), 1)
        self.assertEqual(
            database.freeze_calls[0],
            {
                "candidate_only": True,
                "batch_id": None,
                "metadata": {
                    "trigger": "prepare_upload",
                    "upload_ids": [accepted["upload_id"]],
                },
            },
        )
        self.assertTrue(workspace.process_commands)
        self.assertTrue(
            any("--prepare-config" in command for command in workspace.process_commands)
        )
        self.assertFalse(
            any(
                "echem_platform.start_stop_collection" in command
                for command in workspace.process_commands
            )
        )
        self.assertIn(
            "手动上传/手工实验/材料甲/启停.txt",
            workspace.materialized_files,
        )
        snapshot = database.latest_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["metadata"]["trigger"], "prepare_upload")
        self.assertEqual(
            snapshot["metadata"]["upload_ids"],
            [accepted["upload_id"]],
        )
        remaining = list(workspace.scratch_dir.iterdir())
        self.assertEqual(
            remaining,
            [workspace.scratch_dir / workspace.SNAPSHOT_CACHE_DIR_NAME],
        )
        cached_snapshots = list(remaining[0].iterdir())
        self.assertEqual(len(cached_snapshots), 1)
        self.assertRegex(cached_snapshots[0].name, r"^[0-9a-f]{64}$")
        self.assertFalse(any(workspace.scratch_dir.glob("start-stop-*-*")))

    def wait_for_job(
        self,
        workspace: StartStopWorkspace,
        timeout_seconds: float = 5,
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            job = workspace._public_job()
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("upload preparation job did not finish")


class UploadHttpWorkspace:
    def __init__(self, root: Path):
        self.scratch_dir = root / "http-upload-scratch"
        self.upload_calls: list[dict[str, object]] = []
        self.job_calls: list[tuple[str, object]] = []

    def upload_file(self, staged_path, **metadata):
        staged = Path(staged_path)
        self.upload_calls.append(
            {
                "path": staged,
                "existed_during_call": staged.is_file(),
                "content": staged.read_bytes(),
                "metadata": dict(metadata),
            }
        )
        return {
            "upload_id": 41,
            "changed": True,
            "filename": metadata["filename"],
            "repository_path": "手动上传/batch/启停.txt",
            "size_bytes": int(metadata["size_bytes"]),
            "sha256": "a" * 64,
            "is_candidate": True,
            "candidate_kind": "gal_square_wave",
            "prepare_required": True,
        }

    def start_job(self, action, *, upload_ids=None):
        self.job_calls.append((action, upload_ids))
        return {"id": "job-upload", "action": action, "status": "queued"}


class UploadHttpHarness:
    def __init__(
        self,
        workspace: UploadHttpWorkspace,
        *,
        lan_read_only: bool = False,
        public_host: str = "127.0.0.1",
    ):
        self.workspace = workspace
        self.lan_read_only = lan_read_only
        self.public_host = public_host
        self.port = 18788 if lan_read_only else 18787
        self.handler_type = SERVICE.create_handler(
            workspace,
            lan_read_only=lan_read_only,
            lan_auth_credentials=TEST_LAN_AUTH if lan_read_only else None,
            public_host=public_host,
            public_port=self.port,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | dict | None = None,
        content_type: str | None = None,
        declared_length: int | None = None,
        duplicate_content_length: bool = False,
        host: str | None = None,
        origin: str | None = None,
    ) -> tuple[int, object]:
        if isinstance(body, dict):
            encoded = json.dumps(body).encode("utf-8")
        else:
            encoded = body or b""
        headers = http.client.HTTPMessage()
        headers.add_header(
            "Host",
            host or f"{self.public_host}:{self.port}",
        )
        if self.lan_read_only:
            headers.add_header("Authorization", TEST_LAN_AUTH_HEADER)
        if body is not None or declared_length is not None:
            length = len(encoded) if declared_length is None else declared_length
            headers.add_header("Content-Length", str(length))
            if duplicate_content_length:
                headers.add_header("Content-Length", str(length))
        if content_type is not None:
            headers.add_header("Content-Type", content_type)
        if origin is not None:
            headers.add_header("Origin", origin)

        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = headers
        handler.rfile = io.BytesIO(encoded)
        handler.client_address = ("127.0.0.1", 55000)
        handler.server = type(
            "UploadTestServer",
            (),
            {"server_address": (self.public_host, self.port)},
        )()
        responses: list[tuple[int, object]] = []
        handler.send_json = lambda payload, status=200: responses.append(
            (int(status), payload)
        )
        getattr(handler, f"do_{method}")()
        if len(responses) != 1:
            raise AssertionError(f"expected one response, got {responses!r}")
        return responses[0]


class UploadHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = UploadHttpWorkspace(self.root)
        self.server = UploadHttpHarness(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def upload_path(**changes: str) -> str:
        metadata = {
            "filename": "启停.txt",
            "relative_path": "材料甲/启停.txt",
            "group": "batch",
            "last_modified": "3000",
            "size": str(len(GALVANOSTATIC_START_STOP)),
        }
        metadata.update(changes)
        return "/api/start-stop/uploads?" + urllib.parse.urlencode(metadata)

    def test_binary_upload_returns_201_and_always_removes_staging_file(self):
        status, payload = self.server.request(
            "POST",
            self.upload_path(),
            body=GALVANOSTATIC_START_STOP,
            content_type="application/octet-stream",
            origin="http://127.0.0.1:18787",
        )

        self.assertEqual(status, 201)
        self.assertEqual(payload["upload_id"], 41)
        self.assertEqual(len(self.workspace.upload_calls), 1)
        call = self.workspace.upload_calls[0]
        self.assertTrue(call["existed_during_call"])
        self.assertEqual(call["content"], GALVANOSTATIC_START_STOP)
        self.assertEqual(call["metadata"]["filename"], "启停.txt")
        self.assertEqual(call["metadata"]["relative_path"], "材料甲/启停.txt")
        self.assertEqual(call["metadata"]["size_bytes"], str(len(GALVANOSTATIC_START_STOP)))
        self.assertFalse(call["path"].exists())
        self.assertEqual(list(self.workspace.scratch_dir.iterdir()), [])

    def test_upload_rejects_size_mismatch_content_type_and_oversize_before_staging(self):
        mismatch_status, _ = self.server.request(
            "POST",
            self.upload_path(size=str(len(GALVANOSTATIC_START_STOP) + 1)),
            body=GALVANOSTATIC_START_STOP,
            content_type="application/octet-stream",
        )
        media_status, _ = self.server.request(
            "POST",
            self.upload_path(),
            body=GALVANOSTATIC_START_STOP,
            content_type="text/plain",
        )
        oversize = StartStopWorkspace.MAX_UPLOAD_BYTES + 1
        oversize_status, _ = self.server.request(
            "POST",
            self.upload_path(size=str(oversize)),
            body=b"x",
            content_type="application/octet-stream",
            declared_length=oversize,
        )
        duplicate_length_status, _ = self.server.request(
            "POST",
            self.upload_path(),
            body=GALVANOSTATIC_START_STOP,
            content_type="application/octet-stream",
            duplicate_content_length=True,
        )

        self.assertEqual(mismatch_status, 400)
        self.assertEqual(media_status, 415)
        self.assertEqual(oversize_status, 413)
        self.assertEqual(duplicate_length_status, 400)
        self.assertEqual(self.workspace.upload_calls, [])
        self.assertFalse(self.workspace.scratch_dir.exists())

    def test_upload_rejects_traversal_unknown_and_duplicate_query_fields(self):
        traversal_status, _ = self.server.request(
            "POST",
            self.upload_path(relative_path="../启停.txt"),
            body=GALVANOSTATIC_START_STOP,
            content_type="application/octet-stream",
        )
        unknown_status, _ = self.server.request(
            "POST",
            self.upload_path() + "&unexpected=1",
            body=GALVANOSTATIC_START_STOP,
            content_type="application/octet-stream",
        )
        duplicate_status, _ = self.server.request(
            "POST",
            self.upload_path() + "&size=" + str(len(GALVANOSTATIC_START_STOP)),
            body=GALVANOSTATIC_START_STOP,
            content_type="application/octet-stream",
        )

        self.assertEqual(traversal_status, 400)
        self.assertEqual(unknown_status, 400)
        self.assertEqual(duplicate_status, 400)
        self.assertEqual(self.workspace.upload_calls, [])

    def test_short_body_returns_400_and_removes_partial_file(self):
        status, _ = self.server.request(
            "POST",
            self.upload_path(size=str(len(GALVANOSTATIC_START_STOP) + 8)),
            body=GALVANOSTATIC_START_STOP,
            content_type="application/octet-stream",
            declared_length=len(GALVANOSTATIC_START_STOP) + 8,
        )

        self.assertEqual(status, 400)
        self.assertEqual(self.workspace.upload_calls, [])
        self.assertTrue(self.workspace.scratch_dir.is_dir())
        self.assertEqual(list(self.workspace.scratch_dir.iterdir()), [])

    def test_lan_upload_is_forbidden_before_body_or_metadata_processing(self):
        lan_host = "192.168.110.158"
        lan_server = UploadHttpHarness(
            self.workspace,
            lan_read_only=True,
            public_host=lan_host,
        )

        status, _ = lan_server.request(
            "POST",
            "/api/start-stop/uploads?bad-query",
            host=f"{lan_host}:18788",
        )

        self.assertEqual(status, 403)
        self.assertEqual(self.workspace.upload_calls, [])

    def test_prepare_upload_job_requires_array_and_forwards_upload_ids(self):
        status, payload = self.server.request(
            "POST",
            "/api/start-stop/jobs",
            body={"action": "prepare_upload", "upload_ids": [41, 42]},
            content_type="application/json",
        )
        missing_status, _ = self.server.request(
            "POST",
            "/api/start-stop/jobs",
            body={"action": "prepare_upload"},
            content_type="application/json",
        )
        scalar_status, _ = self.server.request(
            "POST",
            "/api/start-stop/jobs",
            body={"action": "prepare_upload", "upload_ids": 41},
            content_type="application/json",
        )
        unrelated_status, _ = self.server.request(
            "POST",
            "/api/start-stop/jobs",
            body={"action": "render", "upload_ids": [41]},
            content_type="application/json",
        )

        self.assertEqual(status, 202)
        self.assertEqual(payload["action"], "prepare_upload")
        self.assertEqual(self.workspace.job_calls, [("prepare_upload", [41, 42])])
        self.assertEqual(missing_status, 400)
        self.assertEqual(scalar_status, 400)
        self.assertEqual(unrelated_status, 400)

    def test_status_exposes_upload_capability_and_limits_only_locally(self):
        payload = {
            "available": True,
            "execution": {
                "ready": True,
                "update_ready": True,
                "render_ready": True,
                "upload_ready": True,
                "prepare_upload_ready": True,
                "collection_ready": True,
            },
            "job": {"status": "idle", "result": {}},
        }

        local = SERVICE.public_status(payload, lan_read_only=False)["capabilities"]
        lan = SERVICE.public_status(payload, lan_read_only=True)["capabilities"]

        self.assertTrue(local["can_upload_data"])
        self.assertTrue(local["can_prepare_upload"])
        self.assertFalse(lan["can_upload_data"])
        self.assertFalse(lan["can_prepare_upload"])
        self.assertEqual(
            local["upload_max_file_bytes"],
            StartStopWorkspace.MAX_UPLOAD_BYTES,
        )
        self.assertEqual(
            local["upload_max_batch_files"],
            StartStopWorkspace.MAX_UPLOAD_BATCH_FILES,
        )
        self.assertEqual(
            local["upload_extensions"],
            sorted(StartStopWorkspace.UPLOAD_EXTENSIONS),
        )


if __name__ == "__main__":
    unittest.main()
