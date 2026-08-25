from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import unicodedata
from pathlib import Path

from echem_platform.start_stop_database import (
    SCHEMA_VERSION,
    StartStopDatabase,
    seal_artifact_directory,
    validate_sealed_artifact_directory,
)


class StartStopFileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = StartStopDatabase(self.root / "state" / "start-stop.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def batch(self) -> str:
        return self.database.begin_collection_batch(
            machine_count=1, metadata={"run_id": "fixture"}
        )

    def ingest(
        self,
        content: bytes,
        *,
        name: str = "样品/启停.txt",
        remote_path: str | None = None,
        ticks: int = 638_898_000_000_000_000,
        batch_id: str | None = None,
        candidate: bool | None = True,
        **metadata,
    ) -> dict:
        staged = self.root / f"staged-{hashlib.sha256(content).hexdigest()[:12]}-{ticks}"
        staged.write_bytes(content)
        record = {
            "machine_id": "machine-a",
            "root_label": "desktop-data",
            "remote_path": remote_path or "C:\\Users\\lab\\data\\" + name.replace("/", "\\"),
            "remote_relative_path": name.replace("/", "\\"),
            "size": len(content),
            "last_write_ticks": ticks,
            "last_write_utc": "2026-08-03T05:04:03Z",
            **metadata,
        }
        if candidate is not None:
            record["is_candidate"] = candidate
        return self.database.ingest_staged_file(staged, record, batch_id or self.batch())

    def test_initializes_formal_schema_and_connection_pragmas(self) -> None:
        with self.database.session() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0], 15_000
            )

    def test_delete_journal_mode_disables_mmap_and_uses_full_sync(self) -> None:
        database_path = self.root / "delete-mode" / "start-stop.sqlite3"
        database = StartStopDatabase(database_path, journal_mode="delete")

        with database.session() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA mmap_size").fetchone()[0], 0)

        self.assertFalse(Path(f"{database_path}-wal").exists())
        self.assertFalse(Path(f"{database_path}-shm").exists())

    def test_rejects_unknown_journal_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "journal mode must be WAL or DELETE"):
            StartStopDatabase(
                self.root / "invalid-mode" / "start-stop.sqlite3",
                journal_mode="memory",
            )

    def test_delete_mode_ingests_previous_sigbus_file_size_atomically(self) -> None:
        database_path = self.root / "sigbus-regression" / "start-stop.sqlite3"
        database = StartStopDatabase(database_path, journal_mode="DELETE")
        batch_id = database.begin_collection_batch(
            machine_count=1,
            metadata={"run_id": "sigbus-regression"},
        )
        size_bytes = 11_440_612
        prefix = b"ID_GalSquareWave\nmeta\nmeta\n"
        payload = prefix + (b"x" * (size_bytes - len(prefix)))
        staged = self.root / "qiting_2026_08_04_11_45_12.txt"
        staged.write_bytes(payload)

        result = database.ingest_staged_file(
            staged,
            {
                "machine_id": "AGHID-G",
                "root_label": "desktop-process",
                "remote_path": r"C:\data\qiting_2026_08_04_11_45_12.txt",
                "remote_relative_path": "sample/qiting_2026_08_04_11_45_12.txt",
                "size": size_bytes,
                "last_write_ticks": 639_214_419_235_308_002,
                "last_write_utc": "2026-08-04T12:05:23.5308002Z",
            },
            batch_id,
        )

        self.assertTrue(result["changed"])
        self.assertEqual(result["size_bytes"], size_bytes)
        with database.session() as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            stored = connection.execute(
                """SELECT length(cb.content) FROM source_versions sv
                   JOIN content_blobs cb ON cb.id=sv.blob_id
                   WHERE sv.id=?""",
                (result["version_id"],),
            ).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], size_bytes)

    def test_streaming_blob_path_remains_available_for_large_files(self) -> None:
        database = StartStopDatabase(
            self.root / "streaming" / "start-stop.sqlite3",
            inline_blob_max_bytes=0,
        )
        staged = self.root / "streaming-source.txt"
        staged.write_bytes(b"ID_GalSquareWave\nstreamed\n")
        result = database.ingest_staged_file(
            staged,
            {
                "machine_id": "machine-stream",
                "root_label": "root",
                "remote_path": r"C:\data\streaming-source.txt",
                "remote_relative_path": "sample/streaming-source.txt",
                "size": staged.stat().st_size,
                "last_write_ticks": 1,
            },
            database.begin_collection_batch(machine_count=1),
        )
        self.assertTrue(result["changed"])
        with database.session() as connection:
            row = connection.execute(
                """SELECT cb.content FROM source_versions sv
                   JOIN content_blobs cb ON cb.id=sv.blob_id
                   WHERE sv.id=?""",
                (result["version_id"],),
            ).fetchone()
        self.assertEqual(bytes(row[0]), staged.read_bytes())

    def test_v1_database_migrates_collection_config_without_changing_data(self) -> None:
        imported = self.ingest(b"ID_GalSquareWave\nmeta\nmeta\n")
        material = self.database.save_start_stop_config(
            dataset_fingerprint="fixture-fingerprint",
            expected_revision=0,
            materials=[
                {
                    "material_key": "sample",
                    "plot_name": "sample",
                    "include_in_summary_atlas": True,
                    "notes": "",
                    "source_fingerprint": "source-fixture",
                }
            ],
        )
        with self.database.session() as connection:
            before = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "content_blobs",
                    "sources",
                    "source_versions",
                    "material_config_revisions",
                )
            }
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TRIGGER collection_config_revisions_immutable_update;
                DROP TRIGGER collection_config_revisions_immutable_delete;
                DROP TABLE collection_config_current;
                DROP TABLE collection_config_revisions;
                PRAGMA user_version=1;
                COMMIT;
                """
            )

        migrated = StartStopDatabase(self.database.path)
        with migrated.session() as connection:
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                SCHEMA_VERSION,
            )
            after = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in before
            }
            self.assertEqual(before, after)
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM collection_config_revisions"
                    ).fetchone()[0]
                ),
                0,
            )
        self.assertGreater(imported["version_id"], 0)
        self.assertEqual(material["revision"], 1)
        self.assertEqual(migrated.get_start_stop_config()["revision"], 1)
        self.assertEqual(migrated.get_collection_config()["revision"], 0)
        saved_collection = migrated.save_collection_config(
            expected_revision=0,
            machines=[
                {
                    "id": "machine-a",
                    "paths": [
                        {
                            "label": "data",
                            "path": r"D:\data",
                            "enabled": True,
                        }
                    ],
                }
            ],
        )
        self.assertEqual(saved_collection["revision"], 1)

        # A second open is idempotent and leaves both old and new tables intact.
        reopened = StartStopDatabase(self.database.path)
        self.assertEqual(reopened.repository_status()["source_count"], 1)
        self.assertEqual(reopened.get_start_stop_config()["revision"], 1)
        self.assertEqual(reopened.get_collection_config(), saved_collection)

    def test_windows_source_identity_and_content_addressed_blob_deduplication(self) -> None:
        first = self.ingest(b"same", name="A/启停.txt")
        second = self.ingest(b"same", name="B/ADT.txt")

        self.assertTrue(first["changed"])
        self.assertTrue(second["changed"])
        self.assertTrue(second["is_candidate"])
        self.assertFalse(
            self.database.needs_download(
                "machine-a",
                "desktop-data",
                "c:/users/lab/data/a/启停.txt",
                4,
                638_898_000_000_000_000,
            )
        )
        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0], 2)
            blob = connection.execute("SELECT content FROM content_blobs").fetchone()[0]
        self.assertEqual(blob, b"same")

    def test_content_candidate_detection_matches_analysis_gate(self) -> None:
        square = self.ingest(
            b"ID_GalSquareWave\nmeta\nmeta\n",
            name="sample/other-name.txt",
            candidate=None,
        )
        adt = self.ingest(
            b"ID_ScriptMethod\nmeta\nmeta\n",
            name="sample/ADT.txt",
            candidate=None,
        )
        misleading = self.ingest(
            b"plain text\n",
            name="sample/启停.txt",
            ticks=2,
            candidate=None,
        )
        self.assertEqual(square["candidate_kind"], "gal_square_wave")
        self.assertEqual(adt["candidate_kind"], "adt_script_method")
        self.assertFalse(misleading["is_candidate"])

    def test_new_version_preserves_history_and_unchanged_file_does_not_version(self) -> None:
        batch = self.batch()
        first = self.ingest(b"v1", ticks=1, batch_id=batch)
        unchanged = self.ingest(b"v1", ticks=1, batch_id=batch)
        second = self.ingest(b"v2", ticks=2, batch_id=batch)

        self.assertFalse(unchanged["changed"])
        self.assertNotEqual(first["version_id"], second["version_id"])
        with self.database.session() as connection:
            versions = connection.execute(
                "SELECT version_number FROM source_versions ORDER BY id"
            ).fetchall()
            history = connection.execute(
                "SELECT source_version_id FROM source_selections ORDER BY id"
            ).fetchall()
        self.assertEqual([row[0] for row in versions], [1, 2])
        self.assertEqual([row[0] for row in history], [first["version_id"], second["version_id"]])

    def test_failed_ingest_never_changes_current_selection(self) -> None:
        first = self.ingest(b"stable", ticks=1)
        staged = self.root / "failure.txt"
        staged.write_bytes(b"replacement")
        metadata = {
            "machine_id": "machine-a",
            "root_label": "desktop-data",
            "remote_path": "C:\\Users\\lab\\data\\样品\\启停.txt",
            "remote_relative_path": "样品\\启停.txt",
            "size": len(b"replacement"),
            "last_write_ticks": 2,
        }
        original = self.database._ensure_blob_from_path

        def fail_after_blob(connection, path, sha256, size_bytes):
            original(connection, path, sha256, size_bytes)
            raise OSError("simulated storage failure")

        self.database._ensure_blob_from_path = fail_after_blob  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            self.database.ingest_staged_file(staged, metadata, self.batch())
        self.database._ensure_blob_from_path = original  # type: ignore[method-assign]

        with self.database.session() as connection:
            current = connection.execute(
                """SELECT sv.id FROM source_current sc
                JOIN source_selections ss ON ss.id=sc.selection_id
                JOIN source_versions sv ON sv.id=ss.source_version_id"""
            ).fetchone()[0]
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0], 1)
        self.assertEqual(current, first["version_id"])

    def test_wrong_expected_sha_is_rejected_before_any_repository_write(self) -> None:
        staged = self.root / "wrong-sha.txt"
        staged.write_bytes(b"actual payload")
        with self.assertRaises(OSError):
            self.database.ingest_staged_file(
                staged,
                {
                    "machine_id": "machine-a",
                    "root_label": "desktop-data",
                    "remote_path": "C:\\data\\启停.txt",
                    "remote_relative_path": "启停.txt",
                    "size": staged.stat().st_size,
                    "sha256": "0" * 64,
                },
                self.batch(),
            )
        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_current").fetchone()[0], 0)

    def test_repository_paths_are_nfc_and_unsafe_paths_are_rejected(self) -> None:
        decomposed = "材料/" + unicodedata.normalize("NFD", "启停") + ".txt"
        saved = self.ingest(b"nfc", repository_path=decomposed)
        self.assertEqual(saved["repository_path"], unicodedata.normalize("NFC", decomposed))

        for unsafe in (
            "/tmp/data.txt",
            "C:\\data.txt",
            "\\\\server\\share\\data.txt",
            "a/../data.txt",
            "a\x00data.txt",
        ):
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                self.ingest(b"bad" + unsafe.encode("utf-8", "ignore"), repository_path=unsafe)

    def test_casefold_and_file_directory_conflicts_block_snapshot(self) -> None:
        self.ingest(b"one", name="one/启停.txt", repository_path="A/启停.txt")
        self.ingest(b"two", name="two/启停.txt", repository_path="a/启停.txt")
        with self.assertRaises(ValueError):
            self.database.freeze_snapshot()

    def test_collection_issues_and_terminal_status_are_recorded(self) -> None:
        batch = self.batch()
        issue_id = self.database.record_collection_issue(
            batch,
            severity="warning",
            code="unsettled",
            message="still changing",
            detail={"scope": "file"},
        )
        result = self.database.finish_collection_batch(
            batch, "completed_with_warnings", {"ingested": 0}
        )

        self.assertGreater(issue_id, 0)
        self.assertEqual(result["status"], "completed_with_warnings")
        self.assertEqual(result["totals"], {"ingested": 0})

    def test_abandoned_batch_recovery_is_exact_atomic_and_idempotent(self) -> None:
        failed_batch = self.database.begin_collection_batch(
            batch_id="collector-batch-failed",
            machine_count=3,
            metadata={"run_id": "run-failed"},
        )
        other_batch = self.database.begin_collection_batch(
            batch_id="collector-batch-other",
            machine_count=3,
            metadata={"run_id": "run-other"},
        )
        self.ingest(b"partial-data", batch_id=failed_batch)

        recovered = self.database.fail_collection_batch_if_running(
            failed_batch,
            reason="子进程被信号 SIGKILL 终止（returncode=-9）",
            returncode=-9,
            parent_job_id="job-123",
        )

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered["batch_id"], failed_batch)
        self.assertEqual(recovered["partial_file_count"], 1)
        self.assertEqual(recovered["partial_bytes"], len(b"partial-data"))
        self.assertEqual(recovered["returncode"], -9)
        self.assertIsNone(
            self.database.fail_collection_batch_if_running(
                failed_batch,
                reason="must not write twice",
                returncode=-9,
                parent_job_id="job-123",
            )
        )
        with self.database.session() as connection:
            batches = {
                row["id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM collection_batches WHERE id IN (?,?)",
                    (failed_batch, other_batch),
                ).fetchall()
            }
            issues = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM collection_issues WHERE batch_id=?",
                    (failed_batch,),
                ).fetchall()
            ]
        self.assertEqual(batches[failed_batch]["status"], "failed")
        self.assertTrue(batches[failed_batch]["finished_utc"])
        self.assertEqual(batches[failed_batch]["totals_json"], "{}")
        self.assertEqual(batches[other_batch]["status"], "running")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "collector_process_aborted")
        detail = json.loads(issues[0]["detail_json"])
        self.assertEqual(detail["parent_job_id"], "job-123")
        self.assertEqual(detail["returncode"], -9)

    def test_candidate_override_and_frozen_snapshot_are_immutable(self) -> None:
        candidate = self.ingest(b"candidate")
        ordinary = self.ingest(b"ordinary", name="notes/readme.txt", candidate=False)
        first = self.database.freeze_snapshot(candidate_only=True)
        self.assertEqual(first["file_count"], 1)

        self.database.mark_candidate(ordinary["version_id"], True, "manual", "approved")
        second = self.database.freeze_snapshot(candidate_only=True)
        self.assertEqual(second["file_count"], 2)
        with self.database.session() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE dataset_snapshots SET file_count=99 WHERE id=?", (first["id"],)
                )
        self.assertEqual(self.database._snapshot_by_id(first["id"])["file_count"], 1)

    def test_snapshot_materialization_verifies_bytes_sha_size_mtime_and_manifest(self) -> None:
        saved = self.ingest(b"payload")
        snapshot = self.database.freeze_snapshot()
        target = self.root / "materialized"
        result = self.database.materialize_snapshot(snapshot["id"], target)
        restored = target / saved["repository_path"]

        self.assertEqual(restored.read_bytes(), b"payload")
        self.assertEqual(restored.stat().st_size, 7)
        self.assertEqual(restored.stat().st_mtime_ns, 1_785_733_443_000_000_000)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(b"payload").hexdigest())

        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "keep").write_text("safe", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.database.materialize_snapshot(snapshot["id"], occupied)
        self.assertEqual((occupied / "keep").read_text(encoding="utf-8"), "safe")

    def test_artifact_generations_restore_global_latest_current_cache(self) -> None:
        self.ingest(b"source")
        snapshot = self.database.freeze_snapshot()
        first_dir = self.root / "artifacts-1"
        first_dir.mkdir()
        (first_dir / "summary.json").write_text('{"generation":1}', encoding="utf-8")
        first = self.database.publish_artifacts(first_dir, snapshot["id"], 1, "scan")
        second_dir = self.root / "artifacts-2"
        second_dir.mkdir()
        (second_dir / "summary.json").write_text('{"generation":2}', encoding="utf-8")
        second = self.database.publish_artifacts(second_dir, snapshot["id"], 2, "render")

        self.assertLess(first["generation_id"], second["generation_id"])
        restored = self.root / "published"
        result = self.database.restore_current_artifacts(restored)
        self.assertEqual(result["generation_id"], second["generation_id"])
        self.assertEqual((restored / "summary.json").read_text(encoding="utf-8"), '{"generation":2}')
        self.assertEqual(
            self.database.restore_current_artifacts(self.root / "scan-cache", "scan")["generation_id"],
            first["generation_id"],
        )

    def test_restored_internal_marker_is_not_republished_into_next_generation(self) -> None:
        self.ingest(b"source")
        snapshot = self.database.freeze_snapshot()
        first_dir = self.root / "generation-1"
        first_dir.mkdir()
        (first_dir / "summary.json").write_text('{"generation":1}', encoding="utf-8")
        self.database.publish_artifacts(first_dir, snapshot["id"], 1, "scan")

        restored = self.root / "restored-generation-1"
        self.database.restore_current_artifacts(restored)
        self.assertTrue((restored / ".start-stop-artifacts.json").is_file())
        (restored / "summary.json").write_text('{"generation":2}', encoding="utf-8")
        second = self.database.publish_artifacts(restored, snapshot["id"], 2, "render")

        paths = {item["path"] for item in second["files"]}
        self.assertNotIn(".start-stop-artifacts.json", paths)
        final = self.root / "restored-generation-2"
        self.database.restore_current_artifacts(final)
        self.assertEqual(
            (final / "summary.json").read_text(encoding="utf-8"),
            '{"generation":2}',
        )

    def test_sealed_artifacts_are_verified_before_publication(self) -> None:
        self.ingest(b"source")
        snapshot = self.database.freeze_snapshot()
        artifacts = self.root / "sealed-artifacts"
        artifacts.mkdir()
        (artifacts / "figure.svg").write_text("<svg/>", encoding="utf-8")
        (artifacts / "analysis_provenance.json").write_text(
            json.dumps({"analysis_script": {"sha256": "a" * 64}}),
            encoding="utf-8",
        )

        sealed = seal_artifact_directory(
            artifacts,
            snapshot_id=snapshot["id"],
            config_revision=3,
            kind="render",
            provenance={"analysis_script": {"sha256": "a" * 64}},
        )
        verified = validate_sealed_artifact_directory(artifacts)
        published = self.database.publish_sealed_artifacts(
            artifacts, snapshot["id"], 3, "render"
        )

        self.assertEqual(sealed["manifest_sha256"], verified["manifest_sha256"])
        self.assertIn("artifact_manifest.json", {row["path"] for row in published["files"]})
        self.assertIn("SHA256SUMS.txt", {row["path"] for row in published["files"]})

        (artifacts / "figure.svg").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.database.publish_sealed_artifacts(
                artifacts, snapshot["id"], 3, "render"
            )

    def test_legacy_checksum_only_generation_remains_migratable(self) -> None:
        self.ingest(b"source")
        snapshot = self.database.freeze_snapshot()
        artifacts = self.root / "legacy-checksums"
        artifacts.mkdir()
        (artifacts / "figure.pdf").write_bytes(b"%PDF-fixture")
        (artifacts / "SHA256SUMS.txt").write_text(
            hashlib.sha256(b"%PDF-fixture").hexdigest() + "  figure.pdf\n",
            encoding="utf-8",
        )

        published = self.database.publish_artifacts(
            artifacts, snapshot["id"], 0, "legacy"
        )

        self.assertEqual(published["kind"], "legacy")
        self.assertEqual(published["file_count"], 2)

    def test_selective_artifact_restore_only_materializes_allowlisted_inputs(self) -> None:
        self.ingest(b"source")
        snapshot = self.database.freeze_snapshot()
        artifacts = self.root / "seed-source"
        workbook = (
            artifacts
            / "outputs"
            / "019fa91d-602a-79a2-8637-6e9a3f699f66"
            / "启停绘图材料配置.xlsx"
        )
        workbook.parent.mkdir(parents=True)
        workbook.write_bytes(b"workbook")
        (artifacts / "material_config_snapshot.json").write_text(
            '{"dataset_fingerprint":"one"}', encoding="utf-8"
        )
        stale = artifacts / "figures" / "old.png"
        stale.parent.mkdir()
        stale.write_bytes(b"old image")
        self.database.publish_artifacts(artifacts, snapshot["id"], 1, "scan")

        target = self.root / "selected-seed"
        result = self.database.restore_current_artifact_files(
            target,
            ["material_config_snapshot.json"],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["restored"], ["material_config_snapshot.json"])
        self.assertTrue((target / "material_config_snapshot.json").is_file())
        self.assertFalse((target / "figures" / "old.png").exists())
        self.assertFalse((target / workbook.relative_to(artifacts)).exists())
        self.assertEqual(
            [path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()],
            ["material_config_snapshot.json"],
        )

    def test_empty_repository_has_no_artifact_generation_to_restore(self) -> None:
        target = self.root / "not-created"
        self.assertIsNone(self.database.restore_current_artifacts(target))
        self.assertFalse(target.exists())

    def test_material_configuration_revision_audit_and_preserving_import(self) -> None:
        imported = self.database.import_start_stop_config(
            {
                "revision": 7,
                "dataset_fingerprint": "legacy-data",
                "updated_utc": "2026-08-02T01:00:00Z",
                "materials": [
                    {
                        "material_key": "A",
                        "plot_name": "完整名称 A",
                        "include_in_summary_atlas": 1,
                        "favorite": 1,
                        "notes": "",
                        "source_fingerprint": "source-a",
                    }
                ],
            }
        )
        self.assertEqual(imported["revision"], 7)
        self.assertTrue(imported["materials"][0]["favorite"])
        saved = self.database.save_start_stop_config(
            dataset_fingerprint="new-data",
            expected_revision=7,
            materials=[
                {
                    "material_key": "A",
                    "plot_name": "完整名称 A2",
                    "include_in_summary_atlas": False,
                    "favorite": True,
                    "notes": "reviewed",
                    "source_fingerprint": "source-a2",
                }
            ],
        )
        self.assertEqual(saved["revision"], 8)
        self.assertFalse(saved["materials"][0]["include_in_summary_atlas"])
        self.assertTrue(saved["materials"][0]["favorite"])
        with self.assertRaises(ValueError):
            self.database.save_start_stop_config(
                dataset_fingerprint="stale", expected_revision=7, materials=[]
            )
        with self.database.session() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM material_config_revisions").fetchone()[0], 2)

    def test_local_import_uses_logical_path_without_host_path_leakage(self) -> None:
        source = self.root / "beijing.txt"
        source.write_bytes(b"ID_ScriptMethod\nadt")
        saved = self.database.import_source_path(
            source,
            {
                "source_kind": "local_import",
                "logical_path": "北京数据列/材料/ADT.txt",
                "size": len(b"ID_ScriptMethod\nadt"),
                "last_write_utc": "2026-08-01T00:00:00Z",
            },
            self.batch(),
        )
        self.assertEqual(saved["repository_path"], "北京数据列/材料/ADT.txt")
        self.assertTrue(saved["is_candidate"])

    def test_repository_status_and_integrity_cover_all_repository_layers(self) -> None:
        batch = self.batch()
        self.ingest(b"raw", batch_id=batch)
        self.database.finish_collection_batch(batch, totals={"ingested": 1})
        snapshot = self.database.freeze_snapshot(batch_id=batch)
        artifact_dir = self.root / "artifacts"
        artifact_dir.mkdir()
        (artifact_dir / "figure.pdf").write_bytes(b"pdf")
        self.database.publish_artifacts(artifact_dir, snapshot["id"], 0, "scan")

        status = self.database.repository_status()
        self.assertEqual(status["schema_version"], SCHEMA_VERSION)
        self.assertEqual(status["sources"]["current_count"], 1)
        self.assertEqual(status["snapshots"]["count"], 1)
        self.assertEqual(status["artifacts"]["generation_count"], 1)
        self.assertEqual(status["source_first_modified_utc"], "2026-08-03T05:04:03Z")
        self.assertTrue(status["latest_collection_utc"])
        self.assertEqual(self.database.integrity_check(), {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "checked_blobs": 2,
            "errors": [],
        })


if __name__ == "__main__":
    unittest.main()
