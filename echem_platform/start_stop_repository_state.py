"""Shared query-only repository state used by management and LAN readers."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


class RepositoryStateReader:
    def source_version_identity(self, source_version_id):
        """Verify provenance metadata without opening a potentially large raw BLOB."""
        if type(source_version_id) is not int or source_version_id <= 0:
            return None
        with self.session() as connection:
            row = connection.execute(
                "SELECT sv.id AS source_version_id,sv.repository_path,sv.size_bytes,b.sha256 "
                "FROM source_versions sv JOIN content_blobs b ON b.id=sv.blob_id WHERE sv.id=?",
                (source_version_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    """Requires a connection context via session(), plus path/schema_version."""

    def analysis_storage_estimate(self) -> dict[str, int]:
        with self.session() as connection:
            baseline = connection.execute(
                """SELECT ag.total_bytes,ds.total_bytes FROM artifact_current ac
                JOIN artifact_selections s ON s.id=ac.selection_id
                JOIN artifact_generations ag ON ag.id=s.generation_id
                JOIN dataset_snapshots ds ON ds.id=ag.snapshot_id
                WHERE ac.kind='render'"""
            ).fetchone()
            current = connection.execute("SELECT total_bytes FROM dataset_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return {"baseline_output": int(baseline[0]) if baseline else 0,
                "baseline_source": int(baseline[1]) if baseline else 0,
                "current_source": int(current[0]) if current else 0}

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _job_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        for stored, public in (
            ("progress_json", "progress"),
            ("result_json", "result"),
        ):
            try:
                value = json.loads(str(payload.pop(stored)))
            except (TypeError, ValueError, json.JSONDecodeError):
                value = {}
            payload[public] = value if isinstance(value, dict) else {}
        cursor = connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM job_events WHERE job_id=?",
            (str(payload["id"]),),
        ).fetchone()
        payload["event_cursor"] = int(cursor[0]) if cursor is not None else 0
        payload["can_retry"] = str(payload.get("status") or "") in {
            "failed",
            "interrupted",
        }
        return payload

    def latest_job(self) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM jobs ORDER BY created_utc DESC,rowid DESC LIMIT 1"
            ).fetchone()
            return self._job_from_row(connection, row)

    def current_analysis_run(self, kind: str = "render") -> dict[str, Any]:
        normalized_kind = str(kind or "render").strip()
        if not _SAFE_TOKEN.fullmatch(normalized_kind):
            raise ValueError("invalid artifact kind")
        with self.session() as connection:
            row = connection.execute(
                """SELECT ag.id AS generation_id,ag.snapshot_id,
                          ag.config_revision,ag.created_utc AS generation_created_utc,
                          ar.*
                   FROM artifact_current ac
                   JOIN artifact_selections ase ON ase.id=ac.selection_id
                   JOIN artifact_generations ag ON ag.id=ase.generation_id
                   LEFT JOIN analysis_runs ar ON ar.artifact_generation_id=ag.id
                   WHERE ac.kind=?""",
                (normalized_kind,),
            ).fetchone()
        if row is None:
            return {"state": "none"}
        payload = dict(row)
        if payload.get("id") is None:
            return {
                "state": "legacy_unverified",
                "artifact_generation_id": int(payload["generation_id"]),
                "snapshot_id": int(payload["snapshot_id"]),
                "config_revision": int(payload["config_revision"]),
                "created_utc": str(payload["generation_created_utc"]),
            }
        result = {
            "state": "sealed",
            "analysis_run_id": int(payload["id"]),
            "job_id": str(payload["job_id"]),
            "snapshot_id": int(payload["snapshot_id"]),
            "config_revision": int(payload["config_revision"]),
            "artifact_generation_id": int(payload["artifact_generation_id"]),
            "dataset_fingerprint": str(payload["dataset_fingerprint"]),
            "artifact_manifest_sha256": str(payload["artifact_manifest_sha256"]),
            "analysis_script_sha256": str(payload["analysis_script_sha256"]),
            "material_config_sha256": str(payload["material_config_sha256"]),
            "analysis_summary_sha256": str(payload["analysis_summary_sha256"]),
            "created_utc": str(payload["created_utc"]),
        }
        for stored, public in (
            ("rules_json", "rules"),
            ("runtime_json", "runtime"),
        ):
            try:
                value = json.loads(str(payload[stored]))
            except (TypeError, ValueError, json.JSONDecodeError):
                value = {}
            result[public] = value if isinstance(value, dict) else {}
        return result

    def get_start_stop_config(self) -> dict[str, Any]:
        with self.session() as connection:
            row = connection.execute(
                """SELECT r.* FROM material_config_current c
                JOIN material_config_revisions r ON r.revision=c.revision WHERE c.id=1"""
            ).fetchone()
        if row is None:
            return {"dataset_fingerprint": "", "revision": 0, "updated_utc": "", "materials": []}
        materials = json.loads(row["materials_json"])
        for item in materials:
            item.setdefault("updated_utc", row["created_utc"])
            if "include_in_summary_atlas" in item:
                item["include_in_summary_atlas"] = bool(item["include_in_summary_atlas"])
            item["favorite"] = bool(item.get("favorite", False))
        return {"dataset_fingerprint": row["dataset_fingerprint"], "revision": int(row["revision"]), "updated_utc": row["created_utc"], "materials": materials}

    def repository_status(self) -> dict[str, Any]:
        with self.session() as connection:
            scalar = lambda sql: connection.execute(sql).fetchone()[0]
            stored_blob_count = int(scalar("SELECT COUNT(*) FROM content_blobs"))
            stored_blob_bytes = int(scalar("SELECT COALESCE(SUM(size_bytes),0) FROM content_blobs"))
            raw_blob_row = connection.execute(
                """SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM content_blobs
                WHERE id IN (SELECT DISTINCT blob_id FROM source_versions)"""
            ).fetchone()
            blob_count = int(raw_blob_row[0])
            blob_bytes = int(raw_blob_row[1])
            source_count = int(scalar("SELECT COUNT(*) FROM sources"))
            version_count = int(scalar("SELECT COUNT(*) FROM source_versions"))
            current_count = int(scalar("SELECT COUNT(*) FROM source_current"))
            current_bytes = int(scalar("SELECT COALESCE(SUM(sv.size_bytes),0) FROM source_current sc JOIN source_selections ss ON ss.id=sc.selection_id JOIN source_versions sv ON sv.id=ss.source_version_id"))
            candidate_count = int(scalar("SELECT COUNT(*) FROM source_current sc JOIN source_selections ss ON ss.id=sc.selection_id JOIN source_versions sv ON sv.id=ss.source_version_id LEFT JOIN candidate_current cc ON cc.source_version_id=sv.id LEFT JOIN candidate_marks cm ON cm.id=cc.mark_id WHERE COALESCE(cm.is_candidate,sv.is_candidate)=1"))
            batch_count = int(scalar("SELECT COUNT(*) FROM collection_batches"))
            issue_count = int(scalar("SELECT COUNT(*) FROM collection_issues"))
            snapshot_count = int(scalar("SELECT COUNT(*) FROM dataset_snapshots"))
            generation_count = int(scalar("SELECT COUNT(*) FROM artifact_generations"))
            audit_count = int(scalar("SELECT COUNT(*) FROM audit_log"))
            latest_batch = self._row_dict(connection.execute("SELECT * FROM collection_batches ORDER BY started_utc DESC,id DESC LIMIT 1").fetchone())
            latest_snapshot_row = self._row_dict(connection.execute("SELECT id,dataset_fingerprint,file_count,total_bytes,created_utc FROM dataset_snapshots ORDER BY id DESC LIMIT 1").fetchone())
            current_artifacts = [dict(row) for row in connection.execute("""SELECT ac.kind,ag.id AS generation_id,ag.snapshot_id,ag.config_revision,ag.file_count,ag.total_bytes,ag.created_utc FROM artifact_current ac JOIN artifact_selections ase ON ase.id=ac.selection_id JOIN artifact_generations ag ON ag.id=ase.generation_id ORDER BY ac.kind""").fetchall()]
            config = self.get_start_stop_config()
            time_range = connection.execute("SELECT MIN(NULLIF(source_modified_utc,'')),MAX(NULLIF(source_modified_utc,'')) FROM source_versions").fetchone()
            pragmas = {
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
                "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout_ms": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
            }
        if latest_batch:
            latest_batch["metadata"] = json.loads(latest_batch.pop("metadata_json"))
            latest_batch["totals"] = json.loads(latest_batch.pop("totals_json"))
        latest_collection_utc = (latest_batch or {}).get("finished_utc") or (latest_batch or {}).get("started_utc") or ""
        result = {
            "schema_version": self.schema_version, "database_path": str(self.path),
            "database_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            **pragmas,
            "blobs": {"count": blob_count, "total_bytes": blob_bytes, "stored_count": stored_blob_count, "stored_bytes": stored_blob_bytes},
            "sources": {"count": source_count, "version_count": version_count, "current_count": current_count, "current_bytes": current_bytes, "candidate_count": candidate_count},
            "collections": {"batch_count": batch_count, "issue_count": issue_count, "latest": latest_batch},
            "snapshots": {"count": snapshot_count, "latest": latest_snapshot_row},
            "artifacts": {"generation_count": generation_count, "current": current_artifacts},
            "config": {"revision": config["revision"], "dataset_fingerprint": config["dataset_fingerprint"], "updated_utc": config["updated_utc"]},
            "audit_count": audit_count, "source_first_modified_utc": time_range[0] or "",
            "source_last_modified_utc": time_range[1] or "", "latest_collection_utc": latest_collection_utc,
            "source_count": source_count, "current_source_count": current_count,
            "current_file_count": current_count, "current_bytes": current_bytes,
            "blob_count": blob_count, "blob_bytes": blob_bytes, "snapshot_count": snapshot_count,
            "stored_blob_count": stored_blob_count, "stored_blob_bytes": stored_blob_bytes,
            "artifact_generation_count": generation_count,
        }
        return result
