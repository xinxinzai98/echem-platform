from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from echem_platform.start_stop_database import StartStopDatabase
from scripts.migrate_start_stop_repository import (
    MigrationError,
    build_plan,
    import_plan,
    migrate_legacy_config,
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def create_config_schema(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE start_stop_material_config (
                material_key TEXT PRIMARY KEY,
                plot_name TEXT NOT NULL,
                include_in_summary_atlas INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                source_fingerprint TEXT NOT NULL,
                updated_utc TEXT NOT NULL
            );
            CREATE TABLE start_stop_config_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                dataset_fingerprint TEXT NOT NULL,
                revision INTEGER NOT NULL,
                updated_utc TEXT NOT NULL
            );
            """
        )


class FakeRepositoryDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        create_config_schema(path)
        self.sources: dict[str, tuple[str, bytes, str]] = {}
        self.blobs: set[str] = set()
        self.batches: list[dict] = []
        self.finished: list[dict] = []
        self.published: list[tuple[Path, int, int, str]] = []

    def get_start_stop_config(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            state = connection.execute(
                "SELECT dataset_fingerprint, revision, updated_utc "
                "FROM start_stop_config_state WHERE id=1"
            ).fetchone()
            rows = connection.execute(
                "SELECT material_key, plot_name, include_in_summary_atlas, notes, "
                "source_fingerprint, updated_utc FROM start_stop_material_config "
                "ORDER BY material_key"
            ).fetchall()
        return {
            "dataset_fingerprint": state["dataset_fingerprint"] if state else "",
            "revision": int(state["revision"]) if state else 0,
            "updated_utc": state["updated_utc"] if state else "",
            "materials": [
                {
                    **dict(row),
                    "include_in_summary_atlas": bool(
                        row["include_in_summary_atlas"]
                    ),
                }
                for row in rows
            ],
        }

    def import_start_stop_config(self, config, *, overwrite=False):
        assert overwrite is False
        with closing(sqlite3.connect(self.path)) as connection, connection:
            current = connection.execute(
                "SELECT 1 FROM start_stop_config_state WHERE id=1"
            ).fetchone()
            if current:
                raise ValueError("material configuration already exists")
            connection.executemany(
                "INSERT INTO start_stop_material_config VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        row["material_key"],
                        row["plot_name"],
                        int(row["include_in_summary_atlas"]),
                        row["notes"],
                        row["source_fingerprint"],
                        row["updated_utc"],
                    )
                    for row in config["materials"]
                ],
            )
            connection.execute(
                "INSERT INTO start_stop_config_state VALUES (1, ?, ?, ?)",
                (
                    config["dataset_fingerprint"],
                    config["revision"],
                    config["updated_utc"],
                ),
            )
        return self.get_start_stop_config()

    def begin_collection_batch(self, *, machine_count=0, metadata=None):
        batch_id = len(self.batches) + 1
        self.batches.append(
            {"id": batch_id, "machine_count": machine_count, "metadata": metadata}
        )
        return batch_id

    def _import(self, path, logical_path, modified_utc):
        payload = Path(path).read_bytes()
        digest = sha256_bytes(payload)
        previous = self.sources.get(logical_path)
        self.sources[logical_path] = (digest, payload, modified_utc)
        self.blobs.add(digest)
        return {
            "changed": previous != self.sources[logical_path],
            "status": "unchanged" if previous == self.sources[logical_path] else "ingested",
            "sha256": digest,
            "size_bytes": len(payload),
            "repository_path": logical_path,
        }

    def ingest_staged_file(self, path, metadata, batch_id):
        assert any(row["id"] == batch_id for row in self.batches)
        logical_path = "/".join(
            (
                metadata["machine_id"],
                metadata["root_label"],
                metadata["remote_relative_path"].replace("\\", "/"),
            )
        )
        return self._import(path, logical_path, metadata["last_write_utc"])

    def import_source_path(self, path, metadata, batch_id=None):
        assert metadata["source_kind"] == "local_import"
        assert any(row["id"] == batch_id for row in self.batches)
        assert Path(path).stat().st_size == metadata["size"]
        assert sha256_bytes(Path(path).read_bytes()) == metadata["sha256"]
        return self._import(path, metadata["logical_path"], metadata["last_write_utc"])

    def finish_collection_batch(self, batch_id, *, status, totals):
        self.finished.append(
            {"batch_id": batch_id, "status": status, "totals": dict(totals)}
        )
        return {"batch_id": batch_id, "snapshot_id": 1}

    def freeze_snapshot(self, candidate_only=True):
        assert candidate_only is True
        return {
            "id": 1,
            "file_count": len(self.sources),
            "total_bytes": sum(len(row[1]) for row in self.sources.values()),
        }

    def publish_artifacts(self, output_dir, snapshot_id, config_revision, kind):
        call = (Path(output_dir), snapshot_id, config_revision, kind)
        if call not in self.published:
            self.published.append(call)
        return {"id": 1, "generation_id": "legacy-1"}

    def repository_status(self):
        return {
            "file_count": len(self.sources),
            "version_count": len(self.sources),
            "blob_count": len(self.blobs),
            "snapshot_count": 1,
            "artifact_generation_count": len(self.published),
            "total_bytes": sum(len(row[1]) for row in self.sources.values()),
        }


class StartStopRepositoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "原始数据"
        self.manifest = self.source_root / "_采集记录" / "文件清单.jsonl"
        self.manifest.parent.mkdir(parents=True)
        self.beijing_root = self.source_root / "北京数据列"
        self.beijing_root.mkdir()
        self.remote_payload = (
            b"CSStudioFile,ID_GalSquareWave,fixture\n"
            b"E(V)\ti(A/cm2)\tT(s)\n0\t0\t0\n"
        )
        self.remote_relative = Path(
            "machine-a", "desktop-data", "sample", "启停.txt"
        )
        self.remote_file = self.source_root / self.remote_relative
        self.remote_file.parent.mkdir(parents=True)
        self.remote_file.write_bytes(self.remote_payload)
        self.remote_record = {
            "collected_at_utc": "2026-08-03T01:02:03Z",
            "hostname": "A-9",
            "ip": "192.168.110.164",
            "local_path": self.remote_relative.as_posix(),
            "machine_id": "machine-a",
            "remote_last_write_ticks": 638898000000000000,
            "remote_last_write_utc": "2026-08-02T08:04:09.7277018Z",
            "remote_path": r"C:\data\sample\启停.txt",
            "remote_relative_path": r"sample\启停.txt",
            "remote_root": r"C:\data",
            "remote_size": len(self.remote_payload),
            "root_label": "desktop-data",
            "run_id": "legacy-run",
            "sha256": sha256_bytes(self.remote_payload),
            "status": "copied",
        }
        self.write_manifest(self.remote_record)

        self.local_file = self.beijing_root / "青绿" / "ADT.txt"
        self.local_file.parent.mkdir()
        self.local_payload = (
            b"CSStudioFile,ID_ScriptMethod,fixture\n"
            b"E(V)\ti(A/cm2)\tT(s)\n0\t0\t0\n"
        )
        self.local_file.write_bytes(self.local_payload)
        os.utime(self.local_file, (1_700_000_000, 1_700_000_000))
        (self.beijing_root / ".DS_Store").write_bytes(b"ignored")
        (self.beijing_root / "青绿" / "not-data.csv").write_bytes(b"ignored")
        ignored_dir = self.beijing_root / "_采集记录"
        ignored_dir.mkdir()
        (ignored_dir / "ignored.txt").write_bytes(b"ignored")

        self.legacy_database = self.root / "legacy.sqlite3"
        create_config_schema(self.legacy_database)
        with closing(sqlite3.connect(self.legacy_database)) as connection, connection:
            connection.execute(
                "INSERT INTO start_stop_material_config VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "machine-a/desktop-data/sample",
                    "完整样品名称",
                    1,
                    "legacy note",
                    "material-fingerprint",
                    "2026-08-03T04:58:44+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO start_stop_config_state VALUES (1, ?, ?, ?)",
                (
                    "dataset-fingerprint",
                    7,
                    "2026-08-03T04:58:44+00:00",
                ),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self, *records) -> None:
        self.manifest.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
            encoding="utf-8",
        )

    def plan(self, *, expect_current_baseline=False):
        return build_plan(
            self.source_root,
            self.manifest,
            self.beijing_root,
            expect_current_baseline=expect_current_baseline,
        )

    def test_validates_imports_preserves_paths_mtime_config_and_is_idempotent(self):
        original_remote = self.remote_file.read_bytes()
        original_local = self.local_file.read_bytes()
        original_remote_stat = self.remote_file.stat()
        original_local_stat = self.local_file.stat()
        plan = self.plan()
        destination = self.root / "repository.sqlite3"
        database = FakeRepositoryDatabase(destination)

        first = import_plan(
            database,
            destination,
            plan,
            legacy_database=self.legacy_database,
        )
        second = import_plan(
            database,
            destination,
            plan,
            legacy_database=self.legacy_database,
        )

        self.assertEqual(first["counts"]["remote_files"], 1)
        self.assertEqual(first["counts"]["local_import_files"], 1)
        self.assertEqual(first["counts"]["total_files"], 2)
        self.assertEqual(first["bytes"], len(self.remote_payload) + len(self.local_payload))
        self.assertEqual(first["unique_blobs"], 2)
        self.assertTrue(first["integrity"]["ok"])
        self.assertEqual(first["configuration"]["status"], "imported")
        self.assertEqual(second["configuration"]["status"], "unchanged")
        self.assertEqual(second["repository"]["file_count"], 2)
        self.assertEqual(second["repository"]["blob_count"], 2)
        self.assertEqual(database.finished[-1]["totals"]["unchanged_content"], 2)
        self.assertEqual(
            set(database.sources),
            {
                "machine-a/desktop-data/sample/启停.txt",
                "北京数据列/青绿/ADT.txt",
            },
        )
        self.assertEqual(
            database.sources["machine-a/desktop-data/sample/启停.txt"][2],
            self.remote_record["remote_last_write_utc"],
        )
        self.assertEqual(
            database.sources["北京数据列/青绿/ADT.txt"][2],
            "2023-11-14T22:13:20Z",
        )
        with closing(sqlite3.connect(destination)) as connection:
            state = connection.execute(
                "SELECT dataset_fingerprint, revision, updated_utc "
                "FROM start_stop_config_state WHERE id=1"
            ).fetchone()
            material = connection.execute(
                "SELECT plot_name, include_in_summary_atlas, notes, "
                "source_fingerprint, updated_utc FROM start_stop_material_config"
            ).fetchone()
        self.assertEqual(
            state,
            ("dataset-fingerprint", 7, "2026-08-03T04:58:44+00:00"),
        )
        self.assertEqual(
            material,
            (
                "完整样品名称",
                1,
                "legacy note",
                "material-fingerprint",
                "2026-08-03T04:58:44+00:00",
            ),
        )
        self.assertEqual(self.remote_file.read_bytes(), original_remote)
        self.assertEqual(self.local_file.read_bytes(), original_local)
        self.assertEqual(self.remote_file.stat().st_mtime_ns, original_remote_stat.st_mtime_ns)
        self.assertEqual(self.local_file.stat().st_mtime_ns, original_local_stat.st_mtime_ns)

    def test_manifest_size_or_sha_mismatch_fails_before_import(self):
        bad = dict(self.remote_record)
        bad["sha256"] = "0" * 64
        self.write_manifest(bad)

        with self.assertRaisesRegex(MigrationError, "SHA-256 mismatch"):
            self.plan()

        bad["sha256"] = self.remote_record["sha256"]
        bad["remote_size"] += 1
        self.write_manifest(bad)
        with self.assertRaisesRegex(MigrationError, "size mismatch"):
            self.plan()

    def test_manifest_traversal_and_duplicate_sources_are_rejected(self):
        unsafe = dict(self.remote_record)
        unsafe["local_path"] = "../outside.txt"
        self.write_manifest(unsafe)
        with self.assertRaisesRegex(MigrationError, "safe relative path"):
            self.plan()

        duplicate = dict(self.remote_record)
        duplicate["local_path"] = "machine-a/desktop-data/sample/copy.txt"
        duplicate_path = self.source_root / duplicate["local_path"]
        duplicate_path.write_bytes(self.remote_payload)
        self.write_manifest(self.remote_record, duplicate)
        with self.assertRaisesRegex(MigrationError, "duplicate remote source"):
            self.plan()

    def test_current_baseline_flag_is_a_strict_prewrite_gate(self):
        with self.assertRaisesRegex(MigrationError, "current baseline mismatch"):
            self.plan(expect_current_baseline=True)

    def test_same_size_change_after_plan_creates_no_blob_version_or_current(self):
        plan = self.plan()
        changed = bytearray(self.remote_payload)
        changed[-1] = ord("X")
        self.remote_file.write_bytes(bytes(changed))
        self.assertEqual(self.remote_file.stat().st_size, len(self.remote_payload))
        destination = self.root / "repository.sqlite3"
        database = StartStopDatabase(destination)

        with self.assertRaisesRegex(OSError, "SHA-256"):
            import_plan(
                database,
                destination,
                plan,
                legacy_database=self.legacy_database,
            )

        status = database.repository_status()
        self.assertEqual(status["blob_count"], 0)
        self.assertEqual(status["source_count"], 0)
        self.assertEqual(status["sources"]["version_count"], 0)
        self.assertEqual(status["current_file_count"], 0)

    def test_different_destination_config_is_not_overwritten(self):
        destination = self.root / "repository.sqlite3"
        create_config_schema(destination)
        with closing(sqlite3.connect(destination)) as connection, connection:
            connection.execute(
                "INSERT INTO start_stop_config_state VALUES (1, 'other', 2, 'now')"
            )

        database = FakeRepositoryDatabase.__new__(FakeRepositoryDatabase)
        database.path = destination
        with self.assertRaisesRegex(MigrationError, "differs from legacy"):
            migrate_legacy_config(database, self.legacy_database)

        with closing(sqlite3.connect(destination)) as connection:
            row = connection.execute(
                "SELECT dataset_fingerprint, revision FROM start_stop_config_state"
            ).fetchone()
        self.assertEqual(row, ("other", 2))

    def test_optional_legacy_artifacts_are_published_as_legacy_generation(self):
        plan = self.plan()
        destination = self.root / "repository.sqlite3"
        database = FakeRepositoryDatabase(destination)
        artifacts = self.root / "legacy-artifacts"
        artifacts.mkdir()
        (artifacts / "启停总结图集.pdf").write_bytes(b"pdf fixture")

        summary = import_plan(
            database,
            destination,
            plan,
            legacy_database=self.legacy_database,
            legacy_artifacts=artifacts,
        )

        self.assertEqual(
            database.published,
            [(artifacts, 1, 7, "legacy")],
        )
        self.assertEqual(summary["artifacts"]["generation_id"], "legacy-1")

    def test_real_database_roundtrip_snapshot_materialize_config_and_integrity(self):
        plan = self.plan()
        destination = self.root / "real-repository.sqlite3"
        database = StartStopDatabase(destination)

        first = import_plan(
            database,
            destination,
            plan,
            legacy_database=self.legacy_database,
        )
        second = import_plan(
            database,
            destination,
            plan,
            legacy_database=self.legacy_database,
        )

        self.assertEqual(first["snapshot"]["file_count"], 2)
        self.assertEqual(first["repository"]["current_file_count"], 2)
        self.assertEqual(first["repository"]["sources"]["version_count"], 2)
        self.assertEqual(first["repository"]["blob_count"], 2)
        self.assertEqual(second["configuration"]["status"], "unchanged")
        self.assertEqual(second["repository"]["sources"]["version_count"], 2)
        self.assertEqual(database.get_start_stop_config()["revision"], 7)
        target = self.root / "materialized"
        materialized = database.materialize_snapshot(first["snapshot"]["id"], target)
        self.assertEqual(materialized["file_count"], 2)
        remote = target / "machine-a" / "desktop-data" / "sample" / "启停.txt"
        local = target / "北京数据列" / "青绿" / "ADT.txt"
        self.assertEqual(remote.read_bytes(), self.remote_payload)
        self.assertEqual(local.read_bytes(), self.local_payload)
        self.assertEqual(int(local.stat().st_mtime), 1_700_000_000)
        self.assertTrue(first["integrity"]["ok"])


if __name__ == "__main__":
    unittest.main()
