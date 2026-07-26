#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import ipaddress
import inspect
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from echem_platform.analysis import AnalysisValidationError
from echem_platform.configuration import load_config, resolve_watch_roots
from echem_platform.control import (
    STAGE_C_ID,
    ControlSafetyError,
    MacroValidationError,
    ProtocolValidationError,
    StageCManager,
    build_dry_run,
    default_dry_run_draft,
    dry_run_capabilities,
)
from echem_platform.parsers import (
    PARSER_VERSION,
    ParsedCurve,
    parse_curve,
    parse_numeric_table,
)


APP_NAME = "电化学测试平台 V0.3 Stage C"
APP_VERSION = "0.3.0-dev.10"
APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
DEFAULT_CONFIG = APP_ROOT / "config.json"
DEFAULT_DATABASE = APP_ROOT / "state" / "echem-platform.sqlite3"
MAX_JSON_REQUEST_BYTES = 1024 * 1024
ANALYSIS_SCHEMA_VERSION = 1


class AnalysisRequestError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = int(status)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def as_local_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return value


def source_is_available(source_path: str) -> bool:
    try:
        return Path(source_path).is_file()
    except OSError:
        return False


def resolve_path_within_roots(
    path: Path,
    roots: Iterable[Path],
    *,
    strict: bool = True,
) -> Path:
    """Resolve a source path and require it to remain under an active root."""
    try:
        resolved = path.resolve(strict=strict)
    except RuntimeError as exc:
        raise OSError("unable to resolve source path") from exc
    for root in roots:
        try:
            resolved_root = root.resolve(strict=False)
            resolved.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            continue
        return resolved
    raise ValueError("path is outside the configured data folders")


def _loopback_authority(value: str) -> tuple[str, int | None] | None:
    """Return a canonical loopback host and port for a valid HTTP authority."""
    authority = str(value or "").strip()
    if (
        not authority
        or any(character.isspace() for character in authority)
        or any(character in authority for character in "/?#@\\")
    ):
        return None
    try:
        parsed = urllib.parse.urlsplit(f"//{authority}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost":
        return hostname, port
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    return address.compressed, port


def validate_local_json_request(
    *,
    host: str,
    origin: str | None,
    content_type: str,
    server_port: int,
) -> None:
    """Protect local mutation endpoints from cross-origin and rebinding writes."""
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise AnalysisRequestError(
            "分析请求必须使用 application/json。",
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        )

    request_authority = _loopback_authority(host)
    if request_authority is None:
        raise AnalysisRequestError(
            "分析请求的 Host 必须是本机回环地址。",
            HTTPStatus.FORBIDDEN,
        )
    request_host, request_port = request_authority
    effective_request_port = request_port if request_port is not None else 80
    if effective_request_port != int(server_port):
        raise AnalysisRequestError(
            "分析请求的 Host 端口与本机服务不一致。",
            HTTPStatus.FORBIDDEN,
        )

    if origin is None:
        return
    origin_value = origin.strip()
    try:
        parsed_origin = urllib.parse.urlsplit(origin_value)
    except ValueError as exc:
        raise AnalysisRequestError(
            "分析请求的 Origin 无效。",
            HTTPStatus.FORBIDDEN,
        ) from exc
    if (
        parsed_origin.scheme.lower() != "http"
        or not parsed_origin.netloc
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.username is not None
        or parsed_origin.password is not None
    ):
        raise AnalysisRequestError(
            "分析请求的 Origin 必须与本机服务同源。",
            HTTPStatus.FORBIDDEN,
        )
    origin_authority = _loopback_authority(parsed_origin.netloc)
    if origin_authority is None:
        raise AnalysisRequestError(
            "分析请求的 Origin 必须与本机服务同源。",
            HTTPStatus.FORBIDDEN,
        )
    origin_host, origin_port = origin_authority
    effective_origin_port = origin_port if origin_port is not None else 80
    if (
        origin_host != request_host
        or effective_origin_port != effective_request_port
    ):
        raise AnalysisRequestError(
            "分析请求的 Origin 必须与本机服务同源。",
            HTTPStatus.FORBIDDEN,
        )


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def session(self):
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    source_path TEXT NOT NULL UNIQUE,
                    source_name TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    technique TEXT NOT NULL,
                    parse_status TEXT NOT NULL,
                    parse_error TEXT NOT NULL DEFAULT '',
                    parser_version TEXT NOT NULL DEFAULT '',
                    parser_id TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_utc TEXT NOT NULL,
                    imported_utc TEXT NOT NULL,
                    encoding TEXT NOT NULL DEFAULT '',
                    delimiter TEXT NOT NULL DEFAULT '',
                    headers_json TEXT NOT NULL DEFAULT '[]',
                    x_name TEXT NOT NULL DEFAULT '',
                    x_unit TEXT NOT NULL DEFAULT '',
                    y_name TEXT NOT NULL DEFAULT '',
                    y_unit TEXT NOT NULL DEFAULT '',
                    point_count INTEGER NOT NULL DEFAULT 0,
                    points_json TEXT NOT NULL DEFAULT '[]',
                    sample_id TEXT NOT NULL DEFAULT '',
                    material TEXT NOT NULL DEFAULT '',
                    electrolyte TEXT NOT NULL DEFAULT '',
                    area_cm2 REAL,
                    tags TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_runs_instrument ON runs(instrument);
                CREATE INDEX IF NOT EXISTS idx_runs_technique ON runs(technique);
                CREATE INDEX IF NOT EXISTS idx_runs_sha256 ON runs(sha256);
                CREATE TABLE IF NOT EXISTS analysis_records (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL
                        REFERENCES runs(id) ON DELETE RESTRICT,
                    analysis_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    algorithm_id TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    parser_id TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_records_run
                    ON analysis_records(run_id, id DESC);
                CREATE TRIGGER IF NOT EXISTS analysis_records_no_update
                BEFORE UPDATE ON analysis_records
                BEGIN
                    SELECT RAISE(ABORT, 'analysis records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS analysis_records_no_delete
                BEFORE DELETE ON analysis_records
                BEGIN
                    SELECT RAISE(ABORT, 'analysis records are immutable');
                END;
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY,
                    created_utc TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS protocol_drafts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    protocol_json TEXT NOT NULL,
                    output_folder TEXT NOT NULL,
                    allowed_run_root TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_runs (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    protocol_sha256 TEXT NOT NULL,
                    profile_sha256 TEXT NOT NULL,
                    macro_sha256 TEXT NOT NULL,
                    snapshot_manifest_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step_index INTEGER NOT NULL DEFAULT 0,
                    created_utc TEXT NOT NULL,
                    started_utc TEXT NOT NULL DEFAULT '',
                    completed_utc TEXT NOT NULL DEFAULT '',
                    chi_pid INTEGER,
                    chi_exit_code INTEGER,
                    output_root TEXT NOT NULL,
                    run_directory TEXT NOT NULL,
                    macro_path TEXT NOT NULL,
                    completion_confirmed INTEGER NOT NULL DEFAULT 0,
                    failure_reason TEXT NOT NULL DEFAULT '',
                    arm_token_sha256 TEXT NOT NULL DEFAULT '',
                    arm_expires_utc TEXT NOT NULL DEFAULT '',
                    confirmations_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_automation_runs_status
                    ON automation_runs(status);
                CREATE TABLE IF NOT EXISTS automation_steps (
                    id INTEGER PRIMARY KEY,
                    automation_run_id INTEGER NOT NULL
                        REFERENCES automation_runs(id) ON DELETE CASCADE,
                    step_index INTEGER NOT NULL,
                    step_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    technique TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    save_basename TEXT NOT NULL,
                    expected_seconds REAL,
                    status TEXT NOT NULL,
                    started_utc TEXT NOT NULL DEFAULT '',
                    completed_utc TEXT NOT NULL DEFAULT '',
                    binary_run_id INTEGER,
                    text_run_id INTEGER,
                    data_status TEXT NOT NULL DEFAULT '',
                    binary_sha256 TEXT NOT NULL DEFAULT '',
                    text_sha256 TEXT NOT NULL DEFAULT '',
                    source_unchanged INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(automation_run_id, step_index)
                );
                CREATE TABLE IF NOT EXISTS automation_events (
                    id INTEGER PRIMARY KEY,
                    automation_run_id INTEGER NOT NULL
                        REFERENCES automation_runs(id) ON DELETE CASCADE,
                    created_utc TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_automation_events_run
                    ON automation_events(automation_run_id, id);
                CREATE TABLE IF NOT EXISTS automation_control_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    run_id TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    acquired_utc TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "parser_version" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''"
                )
            if "parser_id" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN parser_id TEXT NOT NULL DEFAULT ''"
                )

    def audit(self, action: str, target: str, detail: str) -> None:
        with self._lock, self.session() as connection:
            connection.execute(
                "INSERT INTO audit(created_utc, action, target, detail) VALUES (?, ?, ?, ?)",
                (utc_now(), action, target, detail),
            )

    def save_protocol_draft(
        self,
        draft_id: str,
        protocol: dict[str, Any],
        output_folder: str,
        allowed_run_root: str,
    ) -> dict[str, Any]:
        if not isinstance(draft_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}",
            draft_id,
        ):
            raise ValueError("草稿 id 只允许小写字母、数字、下划线和连字符。")
        if not isinstance(protocol, dict):
            raise ValueError("protocol 必须是 JSON 对象。")
        if not isinstance(output_folder, str) or len(output_folder) > 240:
            raise ValueError("output_folder 必须是长度不超过 240 的字符串。")
        if not isinstance(allowed_run_root, str) or len(allowed_run_root) > 240:
            raise ValueError("allowed_run_root 必须是长度不超过 240 的字符串。")
        serialized = json.dumps(
            protocol,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(serialized.encode("utf-8")) > MAX_JSON_REQUEST_BYTES:
            raise ValueError("协议草稿超过 1 MiB。")
        name = str(protocol.get("name") or "未命名协议").strip()[:160] or "未命名协议"
        now = utc_now()
        with self._lock, self.session() as connection:
            existing = connection.execute(
                "SELECT revision, created_utc FROM protocol_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if existing:
                revision = int(existing["revision"]) + 1
                connection.execute(
                    """
                    UPDATE protocol_drafts
                    SET name=?, protocol_json=?, output_folder=?, allowed_run_root=?,
                        revision=?, updated_utc=?
                    WHERE id=?
                    """,
                    (
                        name,
                        serialized,
                        output_folder,
                        allowed_run_root,
                        revision,
                        now,
                        draft_id,
                    ),
                )
            else:
                revision = 1
                connection.execute(
                    """
                    INSERT INTO protocol_drafts(
                        id, name, protocol_json, output_folder, allowed_run_root,
                        revision, created_utc, updated_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft_id,
                        name,
                        serialized,
                        output_folder,
                        allowed_run_root,
                        revision,
                        now,
                        now,
                    ),
                )
        saved = self.get_protocol_draft(draft_id)
        assert saved is not None
        return saved

    def get_protocol_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM protocol_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["protocol"] = json.loads(result.pop("protocol_json"))
        return result

    def list_protocol_drafts(self) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT id, name, output_folder, allowed_run_root, revision,
                       created_utc, updated_utc
                FROM protocol_drafts
                ORDER BY updated_utc DESC, id ASC
                LIMIT 100
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_automation_run(
        self,
        record: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> None:
        with self._lock, self.session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO automation_runs(
                    run_id, protocol_sha256, profile_sha256, macro_sha256,
                    snapshot_manifest_sha256, status, current_step_index,
                    created_utc, output_root, run_directory, macro_path,
                    completion_confirmed, failure_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["run_id"],
                    record["protocol_sha256"],
                    record["profile_sha256"],
                    record["macro_sha256"],
                    record["snapshot_manifest_sha256"],
                    record["status"],
                    int(record.get("current_step_index", 0)),
                    record["created_utc"],
                    record["output_root"],
                    record["run_directory"],
                    record["macro_path"],
                    int(bool(record.get("completion_confirmed", False))),
                    record.get("failure_reason", ""),
                ),
            )
            automation_run_id = int(cursor.lastrowid)
            for step in steps:
                connection.execute(
                    """
                    INSERT INTO automation_steps(
                        automation_run_id, step_index, step_id, name, technique,
                        params_json, save_basename, expected_seconds, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        automation_run_id,
                        int(step["step_index"]),
                        step["step_id"],
                        step["name"],
                        step["technique"],
                        json.dumps(
                            step["params"],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        step["save_basename"],
                        step.get("expected_seconds"),
                        step["status"],
                    ),
                )

    def _automation_run_from_row(
        self,
        row: sqlite3.Row,
        *,
        include_secret: bool,
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        payload = dict(row)
        payload["completion_confirmed"] = bool(payload["completion_confirmed"])
        try:
            payload["confirmations"] = json.loads(
                payload.pop("confirmations_json") or "{}"
            )
        except json.JSONDecodeError:
            payload["confirmations"] = {}
        if not include_secret:
            payload.pop("arm_token_sha256", None)
        step_rows = connection.execute(
            """
            SELECT step_index, step_id, name, technique, params_json,
                   save_basename, expected_seconds, status, started_utc,
                   completed_utc, binary_run_id, text_run_id, data_status,
                   binary_sha256, text_sha256, source_unchanged
            FROM automation_steps
            WHERE automation_run_id = ?
            ORDER BY step_index
            """,
            (row["id"],),
        ).fetchall()
        payload["steps"] = []
        for step_row in step_rows:
            step = dict(step_row)
            step["params"] = json.loads(step.pop("params_json"))
            step["source_unchanged"] = bool(step["source_unchanged"])
            payload["steps"].append(step)
        return payload

    def get_automation_run(
        self,
        run_id: str,
        *,
        include_secret: bool = False,
    ) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            return self._automation_run_from_row(
                row,
                include_secret=include_secret,
                connection=connection,
            )

    def list_automation_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                self._automation_run_from_row(
                    row,
                    include_secret=False,
                    connection=connection,
                )
                for row in rows
            ]

    def list_active_automation_runs(self) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_runs
                WHERE status IN ('starting', 'running', 'stop_requested')
                ORDER BY id
                """
            ).fetchall()
            return [
                self._automation_run_from_row(
                    row,
                    include_secret=False,
                    connection=connection,
                )
                for row in rows
            ]

    def active_automation_run_count(self) -> int:
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM automation_runs
                WHERE status IN ('starting', 'running', 'stop_requested')
                """
            ).fetchone()
        return int(row["count"])

    def automation_control_lock(self) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT run_id, acquired_utc
                FROM automation_control_lock
                WHERE id = 1
                """
            ).fetchone()
        return dict(row) if row else None

    def acquire_automation_control_lock(
        self,
        run_id: str,
        owner_token: str,
    ) -> bool:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT run_id, owner_token
                FROM automation_control_lock
                WHERE id = 1
                """
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO automation_control_lock(id, run_id, owner_token, acquired_utc)
                VALUES (1, ?, ?, ?)
                """,
                (run_id, owner_token, utc_now()),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    def release_automation_control_lock(self, run_id: str) -> None:
        with self._lock, self.session() as connection:
            connection.execute(
                """
                DELETE FROM automation_control_lock
                WHERE id = 1 AND run_id = ?
                """,
                (run_id,),
            )

    def update_automation_run(
        self,
        run_id: str,
        changes: dict[str, Any],
    ) -> None:
        allowed = {
            "status",
            "current_step_index",
            "started_utc",
            "completed_utc",
            "chi_pid",
            "chi_exit_code",
            "completion_confirmed",
            "failure_reason",
            "arm_token_sha256",
            "arm_expires_utc",
            "confirmations_json",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(
                "不允许更新自动化运行字段：" + ", ".join(unknown)
            )
        if not changes:
            return
        normalized = dict(changes)
        if "completion_confirmed" in normalized:
            normalized["completion_confirmed"] = int(
                bool(normalized["completion_confirmed"])
            )
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        values = [normalized[key] for key in normalized]
        with self._lock, self.session() as connection:
            cursor = connection.execute(
                f"UPDATE automation_runs SET {assignments} WHERE run_id = ?",
                (*values, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("自动化运行不存在。")

    def update_automation_step(
        self,
        run_id: str,
        step_index: int,
        changes: dict[str, Any],
    ) -> None:
        allowed = {
            "status",
            "started_utc",
            "completed_utc",
            "binary_run_id",
            "text_run_id",
            "data_status",
            "binary_sha256",
            "text_sha256",
            "source_unchanged",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(
                "不允许更新自动化工步字段：" + ", ".join(unknown)
            )
        if not changes:
            return
        normalized = dict(changes)
        if "source_unchanged" in normalized:
            normalized["source_unchanged"] = int(
                bool(normalized["source_unchanged"])
            )
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        values = [normalized[key] for key in normalized]
        with self._lock, self.session() as connection:
            cursor = connection.execute(
                f"""
                UPDATE automation_steps
                SET {assignments}
                WHERE automation_run_id = (
                    SELECT id FROM automation_runs WHERE run_id = ?
                ) AND step_index = ?
                """,
                (*values, run_id, int(step_index)),
            )
            if cursor.rowcount != 1:
                raise ValueError("自动化工步不存在。")

    def append_automation_event(
        self,
        run_id: str,
        event_type: str,
        severity: str,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        created = utc_now()
        serialized = json.dumps(
            detail,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self.session() as connection:
            row = connection.execute(
                "SELECT id FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                raise ValueError("自动化运行不存在。")
            cursor = connection.execute(
                """
                INSERT INTO automation_events(
                    automation_run_id, created_utc, event_type, severity, detail_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (row["id"], created, event_type, severity, serialized),
            )
        return {
            "id": int(cursor.lastrowid),
            "run_id": run_id,
            "created_utc": created,
            "event_type": event_type,
            "severity": severity,
            "detail": detail,
        }

    def automation_events(
        self,
        run_id: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.created_utc, e.event_type, e.severity, e.detail_json
                FROM automation_events AS e
                JOIN automation_runs AS r ON r.id = e.automation_run_id
                WHERE r.run_id = ?
                ORDER BY e.id
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": run_id,
                "created_utc": row["created_utc"],
                "event_type": row["event_type"],
                "severity": row["severity"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def upsert_run(
        self,
        path: Path,
        fingerprint: str,
        size: int,
        modified_utc: str,
        curve: ParsedCurve,
    ) -> str:
        now = utc_now()
        payload = (
            path.name,
            curve.instrument,
            curve.technique,
            curve.status,
            curve.error,
            PARSER_VERSION,
            curve.parser_id,
            fingerprint,
            size,
            modified_utc,
            now,
            curve.encoding,
            curve.delimiter,
            json.dumps(curve.headers, ensure_ascii=False),
            curve.x_name,
            curve.x_unit,
            curve.y_name,
            curve.y_unit,
            curve.point_count,
            json.dumps(curve.points, ensure_ascii=False, separators=(",", ":")),
            str(path),
        )
        with self._lock, self.session() as connection:
            existing = connection.execute(
                "SELECT id, sha256, parser_version FROM runs WHERE source_path = ?",
                (str(path),),
            ).fetchone()
            if (
                existing
                and existing["sha256"] == fingerprint
                and existing["parser_version"] == PARSER_VERSION
            ):
                return "unchanged"
            if existing:
                connection.execute(
                    """
                    UPDATE runs SET
                        source_name=?, instrument=?, technique=?, parse_status=?, parse_error=?,
                        parser_version=?, parser_id=?, sha256=?, size_bytes=?, modified_utc=?, imported_utc=?, encoding=?,
                        delimiter=?, headers_json=?, x_name=?, x_unit=?, y_name=?, y_unit=?,
                        point_count=?, points_json=?
                    WHERE source_path=?
                    """,
                    payload,
                )
                action = "updated"
            else:
                connection.execute(
                    """
                    INSERT INTO runs (
                        source_name, instrument, technique, parse_status, parse_error,
                        parser_version, parser_id, sha256, size_bytes, modified_utc, imported_utc, encoding,
                        delimiter, headers_json, x_name, x_unit, y_name, y_unit,
                        point_count, points_json, source_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                action = "imported"
        self.audit(
            action,
            path.name,
            f"{curve.instrument} / {curve.technique} / {curve.parser_id} / "
            f"{curve.point_count} points",
        )
        return action

    def list_runs(self, instrument: str = "", technique: str = "", query: str = "") -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if instrument:
            conditions.append("instrument = ?")
            parameters.append(instrument)
        if technique:
            conditions.append("technique = ?")
            parameters.append(technique)
        if query:
            conditions.append(
                "(source_name LIKE ? OR sample_id LIKE ? OR material LIKE ? OR tags LIKE ?)"
            )
            wildcard = f"%{query}%"
            parameters.extend([wildcard] * 4)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        with self.session() as connection:
            rows = connection.execute(
                f"""
                SELECT id, source_name, instrument, technique, parse_status, parser_id, sha256,
                       size_bytes, modified_utc, imported_utc, point_count, sample_id,
                       material, electrolyte, area_cm2, tags, x_name, x_unit, y_name, y_unit,
                       source_path
                FROM runs{where}
                ORDER BY modified_utc DESC, id DESC
                LIMIT 500
                """,
                parameters,
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            result["source_available"] = source_is_available(result.pop("source_path"))
            results.append(result)
        return results

    def list_run_sources(
        self,
        technique: str = "",
        query: str = "",
        limit: int = 2000,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return indexed source paths for trusted, server-side tree construction."""
        conditions: list[str] = []
        parameters: list[Any] = []
        if technique:
            conditions.append("technique = ?")
            parameters.append(technique)
        if query:
            conditions.append(
                """
                (
                    source_name LIKE ? OR source_path LIKE ? OR sample_id LIKE ?
                    OR material LIKE ? OR tags LIKE ?
                )
                """
            )
            wildcard = f"%{query[:200]}%"
            parameters.extend([wildcard] * 5)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        bounded_limit = min(max(int(limit), 1), 5000)
        with self.session() as connection:
            rows = connection.execute(
                f"""
                SELECT id, source_path, source_name, instrument, technique,
                       parse_status, parser_id, point_count, modified_utc,
                       sample_id
                FROM runs{where}
                ORDER BY modified_utc DESC, id DESC
                LIMIT ?
                """,
                [*parameters, bounded_limit + 1],
            ).fetchall()
        truncated = len(rows) > bounded_limit
        results: list[dict[str, Any]] = []
        for row in rows[:bounded_limit]:
            result = dict(row)
            result["source_available"] = source_is_available(result["source_path"])
            results.append(result)
        return results, truncated

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["headers"] = json.loads(result.pop("headers_json"))
        result["points"] = json.loads(result.pop("points_json"))
        result["source_available"] = source_is_available(result["source_path"])
        return result

    def get_run_by_source_path(self, source_path: Path) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT id, source_path, parse_status, parser_id, sha256, point_count
                FROM runs
                WHERE source_path = ?
                """,
                (str(source_path),),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _analysis_record_from_row(row: sqlite3.Row) -> dict[str, Any]:
        record = {
            "id": int(row["id"]),
            "run_id": int(row["run_id"]),
            "analysis_type": row["analysis_type"],
            "schema_version": int(row["schema_version"]),
            "algorithm_id": row["algorithm_id"],
            "algorithm_version": row["algorithm_version"],
            "source_sha256": row["source_sha256"],
            "parser_id": row["parser_id"],
            "parser_version": row["parser_version"],
            "parameters": json.loads(row["parameters_json"]),
            "result": json.loads(row["result_json"]),
            "created_utc": row["created_utc"],
            "preview": False,
        }
        keys = set(row.keys())
        if "current_sha256" in keys:
            record["stale"] = row["source_sha256"] != row["current_sha256"]
        return record

    def list_analyses(self, run_id: int) -> list[dict[str, Any]] | None:
        with self.session() as connection:
            run = connection.execute(
                "SELECT id FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                return None
            rows = connection.execute(
                """
                SELECT a.*, r.sha256 AS current_sha256
                FROM analysis_records AS a
                JOIN runs AS r ON r.id = a.run_id
                WHERE a.run_id = ?
                ORDER BY a.id DESC
                LIMIT 200
                """,
                (run_id,),
            ).fetchall()
        return [self._analysis_record_from_row(row) for row in rows]

    def save_analysis(
        self,
        *,
        run_id: int,
        analysis_type: str,
        schema_version: int,
        algorithm_id: str,
        algorithm_version: str,
        source_sha256: str,
        parser_id: str,
        parser_version: str,
        parameters: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        parameters_json = json.dumps(
            parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(parameters_json.encode("utf-8")) > MAX_JSON_REQUEST_BYTES:
            raise ValueError("分析参数超过 1 MiB。")
        if len(result_json.encode("utf-8")) > MAX_JSON_REQUEST_BYTES:
            raise ValueError("分析结果超过 1 MiB。")
        created = utc_now()
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT sha256 FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise AnalysisRequestError("记录不存在。", HTTPStatus.NOT_FOUND)
            if run["sha256"] != source_sha256:
                raise AnalysisRequestError(
                    "数据文件索引已变化，请重新预览后再保存。",
                    HTTPStatus.CONFLICT,
                )
            cursor = connection.execute(
                """
                INSERT INTO analysis_records(
                    run_id, analysis_type, schema_version, algorithm_id,
                    algorithm_version, source_sha256, parser_id, parser_version,
                    parameters_json, result_json, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    analysis_type,
                    int(schema_version),
                    algorithm_id,
                    algorithm_version,
                    source_sha256,
                    parser_id,
                    parser_version,
                    parameters_json,
                    result_json,
                    created,
                ),
            )
            row = connection.execute(
                """
                SELECT a.*, r.sha256 AS current_sha256
                FROM analysis_records AS a
                JOIN runs AS r ON r.id = a.run_id
                WHERE a.id = ?
                """,
                (int(cursor.lastrowid),),
            ).fetchone()
        assert row is not None
        return self._analysis_record_from_row(row)

    def update_metadata(self, run_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = ("sample_id", "material", "electrolyte", "area_cm2", "tags", "notes")
        updates: dict[str, Any] = {}
        for key in allowed:
            if key not in values:
                continue
            value = values[key]
            if key == "area_cm2":
                if value in ("", None):
                    updates[key] = None
                else:
                    number = float(value)
                    if number <= 0 or number > 100000:
                        raise ValueError("电极面积必须是合理的正数。")
                    updates[key] = number
            else:
                text = str(value).strip()
                limit = 4000 if key == "notes" else 500
                updates[key] = text[:limit]
        if not updates:
            return self.get_run(run_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        parameters = [*updates.values(), run_id]
        with self._lock, self.session() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                parameters,
            )
            if cursor.rowcount == 0:
                return None
        self.audit("metadata_updated", str(run_id), ", ".join(updates.keys()))
        return self.get_run(run_id)

    def status_counts(self) -> dict[str, Any]:
        with self.session() as connection:
            total = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            parsed = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE parse_status = 'parsed'"
            ).fetchone()[0]
            metadata_only = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE parse_status = 'metadata_only'"
            ).fetchone()[0]
            instruments = [
                dict(row)
                for row in connection.execute(
                    "SELECT instrument AS name, COUNT(*) AS count FROM runs GROUP BY instrument"
                ).fetchall()
            ]
            source_paths = [
                row["source_path"]
                for row in connection.execute("SELECT source_path FROM runs").fetchall()
            ]
        unavailable_sources = sum(
            not source_is_available(source_path) for source_path in source_paths
        )
        return {
            "total": total,
            "parsed": parsed,
            "metadata_only": metadata_only,
            "available_sources": total - unavailable_sources,
            "unavailable_sources": unavailable_sources,
            "instruments": instruments,
        }

    def recent_audit(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),)
            ).fetchall()
        return [dict(row) for row in rows]


class Scanner:
    def __init__(
        self,
        database: Database,
        roots: list[Path],
        extensions: list[str],
        max_file_bytes: int,
        max_points: int,
        stable_age_seconds: int,
    ):
        self.database = database
        self.roots = roots
        self.extensions = set(extensions)
        self.max_file_bytes = max_file_bytes
        self.max_points = max_points
        self.stable_age_seconds = stable_age_seconds
        self.lock = threading.Lock()
        self.last_scan: str | None = None
        self.last_result: dict[str, Any] = {}

    def iter_files(self) -> Iterable[Path]:
        for root in self.roots:
            try:
                resolved_root = root.resolve(strict=True)
            except OSError:
                continue
            if not resolved_root.is_dir():
                continue
            for directory, dirnames, filenames in os.walk(resolved_root):
                dirnames[:] = [name for name in dirnames if not name.startswith(".")]
                for filename in filenames:
                    path = Path(directory) / filename
                    if path.suffix.lower() not in self.extensions:
                        continue
                    try:
                        yield resolve_path_within_roots(
                            path,
                            [resolved_root],
                            strict=True,
                        )
                    except (OSError, ValueError):
                        continue

    def scan(self) -> dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            return {"status": "busy"}
        counters = {
            "status": "ok",
            "seen": 0,
            "imported": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "errors": 0,
        }
        try:
            for path in self.iter_files():
                counters["seen"] += 1
                try:
                    path = resolve_path_within_roots(
                        path,
                        self.roots,
                        strict=True,
                    )
                    before = path.stat()
                    if before.st_size > self.max_file_bytes:
                        counters["skipped"] += 1
                        continue
                    if time.time() - before.st_mtime < self.stable_age_seconds:
                        counters["skipped"] += 1
                        continue
                    data = path.read_bytes()
                    after = path.stat()
                    if (
                        before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                    ):
                        counters["skipped"] += 1
                        self.database.audit("deferred", path.name, "File changed while being read.")
                        continue
                    fingerprint = hashlib.sha256(data).hexdigest()
                    modified = dt.datetime.fromtimestamp(
                        after.st_mtime, tz=dt.timezone.utc
                    ).isoformat(timespec="seconds")
                    curve = parse_curve(path, data, self.max_points)
                    action = self.database.upsert_run(
                        path, fingerprint, after.st_size, modified, curve
                    )
                    counters[action] += 1
                except (OSError, UnicodeError, ValueError) as exc:
                    counters["errors"] += 1
                    self.database.audit("read_error", path.name, str(exc)[:500])
            self.last_scan = utc_now()
            self.last_result = counters.copy()
            return counters
        finally:
            self.lock.release()


class RuntimeState:
    def __init__(self, database: Database, scanner: Scanner, config: dict[str, Any]):
        self.database = database
        self.scanner = scanner
        self.config = config
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.control = StageCManager(
            database,
            config,
            output_importer=self._import_control_outputs,
        )

    def _import_control_outputs(
        self,
        binary_path: Path,
        text_path: Path,
    ) -> dict[str, Any]:
        self.scanner.scan()
        binary = self.database.get_run_by_source_path(binary_path)
        text = self.database.get_run_by_source_path(text_path)
        return {
            "binary_run_id": binary["id"] if binary else None,
            "text_run_id": text["id"] if text else None,
            "parse_status": text["parse_status"] if text else "not_imported",
        }

    def watcher(self) -> None:
        while not self.stop_event.is_set():
            self.scanner.scan()
            self.stop_event.wait(self.config["scan_interval_seconds"])

    def control_watcher(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.control.poll_all()
            except Exception:
                traceback.print_exc()
            self.stop_event.wait(1.0)

    def control_capabilities(self) -> dict[str, Any]:
        capabilities = self.control.capabilities()
        capabilities["dry_run"] = dry_run_capabilities()
        return capabilities

    @staticmethod
    def _json_object(value: Any, label: str) -> dict[str, Any]:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            value = dataclasses.asdict(value)
        elif hasattr(value, "to_dict") and callable(value.to_dict):
            value = value.to_dict()
        if not isinstance(value, dict):
            raise ValueError(f"{label}必须是 JSON 对象。")
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}包含无法保存的值。") from exc
        if len(serialized.encode("utf-8")) > MAX_JSON_REQUEST_BYTES:
            raise ValueError(f"{label}超过 1 MiB。")
        return json.loads(serialized)

    @classmethod
    def _public_analysis_value(cls, value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            value = dataclasses.asdict(value)
        elif hasattr(value, "to_dict") and callable(value.to_dict):
            value = value.to_dict()
        if isinstance(value, dict):
            return {
                str(key): cls._public_analysis_value(item)
                for key, item in value.items()
                if str(key).lower() not in {"source_path", "absolute_path"}
            }
        if isinstance(value, (list, tuple)):
            return [cls._public_analysis_value(item) for item in value]
        return value

    @staticmethod
    def _canonical_analysis_type(value: Any) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        aliases = {
            "eis": "eis_resistance",
            "eis_resistance": "eis_resistance",
            "resistance": "eis_resistance",
            "cv": "cv_overpotential",
            "cv_overpotential": "cv_overpotential",
            "overpotential": "cv_overpotential",
        }
        analysis_type = aliases.get(normalized)
        if not analysis_type:
            raise ValueError("analysis_type 必须是 eis_resistance 或 cv_overpotential。")
        return analysis_type

    @staticmethod
    def _call_analysis_function(
        function: Any,
        table: Any,
        parameters: dict[str, Any],
    ) -> Any:
        signature = inspect.signature(function)
        try:
            signature.bind(table, parameters)
        except TypeError:
            try:
                signature.bind(table, params=parameters)
            except TypeError:
                signature.bind(table)
                return function(table)
            return function(table, params=parameters)
        return function(table, parameters)

    def _analysis_source(self, run_id: int) -> tuple[dict[str, Any], bytes]:
        run = self.database.get_run(run_id)
        if not run:
            raise AnalysisRequestError("记录不存在。", HTTPStatus.NOT_FOUND)
        try:
            source_path = resolve_path_within_roots(
                Path(run["source_path"]),
                self.scanner.roots,
                strict=True,
            )
            before = source_path.stat()
            if not source_path.is_file():
                raise OSError("not a regular file")
            if before.st_size > self.scanner.max_file_bytes:
                raise AnalysisRequestError(
                    "源数据文件超过当前安全读取上限。",
                    HTTPStatus.CONFLICT,
                )
            with source_path.open("rb") as source:
                data = source.read()
            after_path = resolve_path_within_roots(
                source_path,
                self.scanner.roots,
                strict=True,
            )
            if after_path != source_path:
                raise AnalysisRequestError(
                    "源数据文件在读取过程中离开了当前数据目录。",
                    HTTPStatus.CONFLICT,
                )
            after = after_path.stat()
        except AnalysisRequestError:
            raise
        except ValueError as exc:
            raise AnalysisRequestError(
                "源数据文件不在当前配置的数据目录内。",
                HTTPStatus.FORBIDDEN,
            ) from exc
        except OSError as exc:
            raise AnalysisRequestError(
                "源数据文件当前不可读取，无法进行全分辨率分析。",
                HTTPStatus.CONFLICT,
            ) from exc
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise AnalysisRequestError(
                "源数据文件在读取过程中发生变化，请稍后重试。",
                HTTPStatus.CONFLICT,
            )
        source_sha256 = hashlib.sha256(data).hexdigest()
        if source_sha256 != run["sha256"]:
            raise AnalysisRequestError(
                "源数据文件已变化，请先重新扫描后再分析。",
                HTTPStatus.CONFLICT,
            )
        run = dict(run)
        run["source_path"] = str(source_path)
        return run, data

    def calculate_analysis(
        self,
        run_id: int,
        analysis_type: str,
        parameters: dict[str, Any],
        *,
        persist: bool,
    ) -> dict[str, Any]:
        from echem_platform.analysis import (
            calculate_cv_overpotential,
            calculate_eis_resistance,
        )

        analysis_type = self._canonical_analysis_type(analysis_type)
        parameters = self._json_object(parameters, "parameters")
        run, data = self._analysis_source(run_id)
        technique = str(run.get("technique") or "").strip().upper()
        expected = "EIS" if analysis_type == "eis_resistance" else "CV"
        if technique != expected:
            raise ValueError(f"当前记录是 {technique or '未知'} 数据，不能执行 {expected} 分析。")

        table = parse_numeric_table(Path(run["source_path"]), data)
        function = (
            calculate_eis_resistance
            if analysis_type == "eis_resistance"
            else calculate_cv_overpotential
        )
        calculated = self._call_analysis_function(function, table, parameters)
        calculated = self._public_analysis_value(calculated)
        if not isinstance(calculated, dict):
            raise ValueError("分析算法返回了无效结果。")

        algorithm = calculated.pop("algorithm", {})
        if not isinstance(algorithm, dict):
            algorithm = {}
        default_algorithm = (
            "eis_high_frequency_intercept"
            if analysis_type == "eis_resistance"
            else "cv_rhe_ir_target_current_interpolation"
        )
        algorithm_id = str(
            calculated.pop("algorithm_id", "")
            or algorithm.get("id")
            or default_algorithm
        )[:160]
        algorithm_version = str(
            calculated.pop("algorithm_version", "")
            or algorithm.get("version")
            or "2026.07.26.1"
        )[:80]
        normalized_parameters = calculated.pop("parameters", parameters)
        normalized_parameters = self._json_object(
            self._public_analysis_value(normalized_parameters),
            "分析参数",
        )
        calculated.pop("analysis_type", None)
        calculated.pop("schema_version", None)
        warnings = calculated.pop("warnings", [])
        nested_result = calculated.pop("result", None)
        if nested_result is not None:
            if not isinstance(nested_result, dict):
                raise ValueError("分析算法的 result 必须是 JSON 对象。")
            result_payload = dict(nested_result)
            for key, value in calculated.items():
                result_payload.setdefault(key, value)
        else:
            result_payload = calculated
        if warnings:
            result_payload.setdefault("warnings", warnings)
        result = self._json_object(
            self._public_analysis_value(result_payload),
            "分析结果",
        )

        common = {
            "run_id": int(run_id),
            "analysis_type": analysis_type,
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "algorithm_id": algorithm_id,
            "algorithm_version": algorithm_version,
            "source_sha256": run["sha256"],
            "parser_id": run.get("parser_id", ""),
            "parser_version": run.get("parser_version", "") or PARSER_VERSION,
            "parameters": normalized_parameters,
            "result": result,
        }
        if not persist:
            return {
                **common,
                "created_utc": utc_now(),
                "preview": True,
                "stale": False,
            }
        saved = self.database.save_analysis(**common)
        self.database.audit(
            "analysis_saved",
            str(run_id),
            f"{analysis_type} / {algorithm_id} / source {run['sha256'][:12]}",
        )
        return saved

    def list_analyses(self, run_id: int) -> list[dict[str, Any]]:
        records = self.database.list_analyses(run_id)
        if records is None:
            raise AnalysisRequestError("记录不存在。", HTTPStatus.NOT_FOUND)
        return records

    def file_tree(self, technique: str = "", query: str = "") -> dict[str, Any]:
        """Build a read-only folder/file index without exposing absolute source paths."""
        rows, truncated = self.database.list_run_sources(
            technique=technique,
            query=query,
        )
        roots: list[dict[str, Any]] = []
        root_entries: list[tuple[Path, dict[str, Any]]] = []
        label_counts: dict[str, int] = {}
        for index, root in enumerate(self.scanner.roots, start=1):
            base_label = root.name or str(root)
            label_counts[base_label] = label_counts.get(base_label, 0) + 1
            suffix = label_counts[base_label]
            entry = {
                "id": f"root-{index}",
                "name": base_label if suffix == 1 else f"{base_label} ({suffix})",
                "available": root.exists() and root.is_dir(),
                "file_count": 0,
                "files": [],
            }
            roots.append(entry)
            try:
                comparable_root = root.resolve()
            except OSError:
                comparable_root = root
            root_entries.append((comparable_root, entry))

        cached_entry: dict[str, Any] | None = None
        for row in rows:
            source = Path(row.pop("source_path"))
            matches: list[tuple[Path, dict[str, Any], Path]] = []
            for root, entry in root_entries:
                try:
                    relative = source.relative_to(root)
                except ValueError:
                    continue
                matches.append((root, entry, relative))
            if matches:
                _, entry, relative = max(
                    matches,
                    key=lambda item: len(item[0].parts),
                )
            else:
                if cached_entry is None:
                    cached_entry = {
                        "id": "cached",
                        "name": "历史缓存",
                        "available": False,
                        "file_count": 0,
                        "files": [],
                    }
                    roots.append(cached_entry)
                entry = cached_entry
                relative = Path(row["source_name"])

            file_entry = {
                **row,
                "relative_path": relative.as_posix(),
            }
            entry["files"].append(file_entry)
            entry["file_count"] += 1

        return {
            "roots": roots,
            "total": sum(root["file_count"] for root in roots),
            "truncated": truncated,
        }

    def status(self) -> dict[str, Any]:
        counts = self.database.status_counts()
        roots = [
            {"path": str(root), "available": root.exists() and root.is_dir()}
            for root in self.scanner.roots
        ]
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "mode": (
                "read_only_sources_and_stage_c_ocp_control"
                if self.config["instrument_control_enabled"]
                else "read_only_sources_and_stage_c_locked"
            ),
            "loopback_only": True,
            "instrument_control": self.config["instrument_control_enabled"],
            "control_stage": STAGE_C_ID,
            "launch_available": self.config["instrument_control_enabled"],
            "serial_access": False,
            "local_override_active": self.config["local_override_active"],
            "config_sources": self.config["config_sources"],
            "uptime_seconds": round(time.monotonic() - self.started),
            "last_scan": self.scanner.last_scan,
            "last_scan_result": self.scanner.last_result,
            "watch_roots": roots,
            **counts,
        }


def create_handler(runtime: RuntimeState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EchemPlatform/0.3"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stdout.write(
                f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                f"{self.client_address[0]} {fmt % args}\n"
            )

        def send_json(self, payload: Any, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def send_error_json(self, status: int, message: str) -> None:
            self.send_json({"error": message}, status)

        def require_local_json_request(self) -> None:
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1:
                raise AnalysisRequestError(
                    "分析请求必须且只能声明一个 application/json Content-Type。",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise AnalysisRequestError(
                    "分析请求必须且只能使用一个本机回环 Host。",
                    HTTPStatus.FORBIDDEN,
                )
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1:
                raise AnalysisRequestError(
                    "分析请求的 Origin 无效。",
                    HTTPStatus.FORBIDDEN,
                )
            validate_local_json_request(
                host=hosts[0],
                origin=origins[0] if origins else None,
                content_type=content_types[0],
                server_port=int(self.server.server_address[1]),
            )

        def read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_JSON_REQUEST_BYTES:
                raise ValueError("请求内容为空或过大。")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError("请求 JSON 必须使用 UTF-8。") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求 JSON 顶层必须是对象。")
            return payload

        def send_static(self, relative: str) -> None:
            candidate = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() not in candidate.parents and candidate != STATIC_ROOT.resolve():
                self.send_error_json(HTTPStatus.FORBIDDEN, "Forbidden")
                return
            if not candidate.exists() or not candidate.is_file():
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                return
            data = candidate.read_bytes()
            content_type, _ = mimetypes.guess_type(str(candidate))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", (content_type or "application/octet-stream") + (
                "; charset=utf-8" if content_type and content_type.startswith(("text/", "application/javascript")) else ""
            ))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path == "/api/status":
                    self.send_json(runtime.status())
                elif path == "/api/control/capabilities":
                    self.send_json(runtime.control_capabilities())
                elif path == "/api/control/preflight":
                    self.send_json(runtime.control.preflight())
                elif path == "/api/control/runs":
                    if not runtime.config["instrument_control_enabled"]:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                    else:
                        limit = int(query.get("limit", ["50"])[0])
                        self.send_json(runtime.database.list_automation_runs(limit))
                elif re.fullmatch(
                    r"/api/control/runs/RUN-[A-Z0-9-]+/events",
                    path,
                ):
                    if not runtime.config["instrument_control_enabled"]:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                    else:
                        run_id = path.split("/")[-2]
                        self.send_json(runtime.database.automation_events(run_id))
                elif re.fullmatch(r"/api/control/runs/RUN-[A-Z0-9-]+", path):
                    if not runtime.config["instrument_control_enabled"]:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                    else:
                        run_id = path.rsplit("/", 1)[-1]
                        run = runtime.database.get_automation_run(run_id)
                        if run:
                            self.send_json(run)
                        else:
                            self.send_error_json(
                                HTTPStatus.NOT_FOUND,
                                "自动化运行不存在。",
                            )
                elif path == "/api/protocols":
                    self.send_json(runtime.database.list_protocol_drafts())
                elif re.fullmatch(r"/api/protocols/[a-z0-9][a-z0-9_-]{0,63}", path):
                    draft_id = path.rsplit("/", 1)[-1]
                    draft = runtime.database.get_protocol_draft(draft_id)
                    if draft:
                        self.send_json(draft)
                    elif draft_id == "draft-main":
                        self.send_json(
                            {
                                **default_dry_run_draft(),
                                "revision": 0,
                                "persisted": False,
                            }
                        )
                    else:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "协议草稿不存在。")
                elif path == "/api/runs":
                    self.send_json(
                        runtime.database.list_runs(
                            instrument=query.get("instrument", [""])[0],
                            technique=query.get("technique", [""])[0],
                            query=query.get("q", [""])[0],
                        )
                    )
                elif path == "/api/files/tree":
                    self.send_json(
                        runtime.file_tree(
                            technique=query.get("technique", [""])[0],
                            query=query.get("q", [""])[0],
                        )
                    )
                elif re.fullmatch(r"/api/runs/\d+/analyses", path):
                    run_id = int(path.split("/")[-2])
                    self.send_json(runtime.list_analyses(run_id))
                elif re.fullmatch(r"/api/runs/\d+", path):
                    run_id = int(path.rsplit("/", 1)[-1])
                    run = runtime.database.get_run(run_id)
                    if run:
                        self.send_json(run)
                    else:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "记录不存在。")
                elif path == "/api/audit":
                    limit = int(query.get("limit", ["30"])[0])
                    self.send_json(runtime.database.recent_audit(limit))
                elif path == "/":
                    self.send_static("index.html")
                elif path in {"/steps", "/steps.html", "/protocol", "/protocol.html"}:
                    self.send_static("protocol.html")
                elif path in {"/analysis", "/analysis.html"}:
                    self.send_static("analysis.html")
                elif path in {
                    "/environment",
                    "/environment.html",
                    "/monitor",
                    "/monitor.html",
                }:
                    self.send_static("monitor.html")
                elif path.startswith("/static/"):
                    self.send_static(path[len("/static/") :])
                else:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            except AnalysisRequestError as exc:
                self.send_error_json(exc.status, str(exc))
            except ControlSafetyError as exc:
                self.send_json(exc.to_dict(), exc.status)
            except Exception:
                traceback.print_exc()
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误。")

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/api/scan":
                    self.send_json(runtime.scanner.scan())
                    return
                if path == "/api/protocols":
                    payload = self.read_json()
                    unknown = sorted(
                        set(payload)
                        - {
                            "id",
                            "protocol",
                            "output_folder",
                            "allowed_run_root",
                        }
                    )
                    if unknown:
                        raise ValueError(
                            f"协议草稿请求包含未知字段：{', '.join(unknown)}"
                        )
                    draft = runtime.database.save_protocol_draft(
                        str(payload.get("id", "draft-main")),
                        payload.get("protocol"),
                        payload.get("output_folder", ""),
                        payload.get("allowed_run_root", ""),
                    )
                    self.send_json(draft)
                    return
                if path == "/api/protocols/validate":
                    self.send_json(
                        build_dry_run(
                            self.read_json(),
                            include_macro_preview=False,
                        )
                    )
                    return
                if path == "/api/protocols/compile":
                    self.send_json(
                        build_dry_run(
                            self.read_json(),
                            include_macro_preview=True,
                        )
                    )
                    return
                if path == "/api/control/runs":
                    payload = self.read_json()
                    unknown = sorted(set(payload) - {"protocol"})
                    if unknown:
                        raise ValueError(
                            "创建运行请求包含未知字段：" + ", ".join(unknown)
                        )
                    protocol = payload.get("protocol")
                    if not isinstance(protocol, dict):
                        raise ValueError("protocol 必须是 JSON 对象。")
                    self.send_json(
                        runtime.control.create_run(protocol),
                        HTTPStatus.CREATED,
                    )
                    return
                control_match = re.fullmatch(
                    r"/api/control/runs/(RUN-[A-Z0-9-]+)/(arm|start|request-stop)",
                    path,
                )
                if control_match:
                    run_id, action = control_match.groups()
                    payload = self.read_json()
                    if action == "arm":
                        unknown = sorted(
                            set(payload) - {"confirmations", "typed_confirmation"}
                        )
                        if unknown:
                            raise ValueError(
                                "确认请求包含未知字段：" + ", ".join(unknown)
                            )
                        confirmations = payload.get("confirmations")
                        if not isinstance(confirmations, dict):
                            raise ValueError("confirmations 必须是 JSON 对象。")
                        self.send_json(
                            runtime.control.arm(
                                run_id,
                                confirmations,
                                str(payload.get("typed_confirmation", "")),
                            )
                        )
                        return
                    if action == "start":
                        unknown = sorted(set(payload) - {"arm_token"})
                        if unknown:
                            raise ValueError(
                                "启动请求包含未知字段：" + ", ".join(unknown)
                            )
                        self.send_json(
                            runtime.control.start(
                                run_id,
                                str(payload.get("arm_token", "")),
                            ),
                            HTTPStatus.ACCEPTED,
                        )
                        return
                    if payload:
                        raise ValueError("停止请求不接受参数。")
                    self.send_json(runtime.control.request_stop(run_id))
                    return
                analysis_match = re.fullmatch(
                    r"/api/runs/(\d+)/analyses(?:/(preview))?",
                    path,
                )
                if analysis_match:
                    self.require_local_json_request()
                    payload = self.read_json()
                    unknown = sorted(
                        set(payload)
                        - {"analysis_type", "type", "parameters", "params"}
                    )
                    if unknown:
                        raise ValueError(
                            "分析请求包含未知字段：" + ", ".join(unknown)
                        )
                    analysis_type = payload.get(
                        "analysis_type",
                        payload.get("type", ""),
                    )
                    parameters = payload.get(
                        "parameters",
                        payload.get("params", {}),
                    )
                    if not isinstance(parameters, dict):
                        raise ValueError("parameters 必须是 JSON 对象。")
                    preview = analysis_match.group(2) == "preview"
                    result = runtime.calculate_analysis(
                        int(analysis_match.group(1)),
                        str(analysis_type),
                        parameters,
                        persist=not preview,
                    )
                    self.send_json(
                        result,
                        HTTPStatus.OK if preview else HTTPStatus.CREATED,
                    )
                    return
                match = re.fullmatch(r"/api/runs/(\d+)/metadata", path)
                if match:
                    payload = self.read_json()
                    run = runtime.database.update_metadata(int(match.group(1)), payload)
                    if run:
                        self.send_json(run)
                    else:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "记录不存在。")
                    return
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            except AnalysisRequestError as exc:
                self.send_error_json(exc.status, str(exc))
            except AnalysisValidationError as exc:
                self.send_json(
                    {
                        "error": exc.message,
                        "analysis_error": exc.as_dict(),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            except ProtocolValidationError as exc:
                self.send_json(exc.to_dict(), HTTPStatus.BAD_REQUEST)
            except MacroValidationError as exc:
                self.send_json(exc.to_dict(), HTTPStatus.BAD_REQUEST)
            except ControlSafetyError as exc:
                self.send_json(exc.to_dict(), exc.status)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception:
                traceback.print_exc()
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误。")

    return Handler


def build_runtime(config_path: Path, database_path: Path) -> RuntimeState:
    config = load_config(config_path)
    roots = resolve_watch_roots(config, config_path.parent)
    if config["instrument_control_enabled"]:
        control_run_root = Path(config["run_root"]).resolve()
        if control_run_root not in roots:
            roots.append(control_run_root)
    database = Database(database_path)
    scanner = Scanner(
        database=database,
        roots=roots,
        extensions=config["extensions"],
        max_file_bytes=config["max_file_bytes"],
        max_points=config["max_points_per_curve"],
        stable_age_seconds=config["stable_age_seconds"],
    )
    return RuntimeState(database, scanner, config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--scan-once", action="store_true")
    parser.add_argument("--no-watch", action="store_true")
    parser.add_argument("--port", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime = build_runtime(args.config.resolve(), args.database.resolve())
    result = runtime.scanner.scan()
    if args.scan_once:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("errors", 0) == 0 else 1

    if not args.no_watch:
        watcher = threading.Thread(target=runtime.watcher, name="file-watcher", daemon=True)
        watcher.start()
    control_watcher = threading.Thread(
        target=runtime.control_watcher,
        name="stage-c-supervisor",
        daemon=True,
    )
    control_watcher.start()

    bind = runtime.config["bind"]
    port = args.port or runtime.config["port"]
    server = ThreadingHTTPServer((bind, port), create_handler(runtime))
    print(f"{APP_NAME} {APP_VERSION}")
    if runtime.config["instrument_control_enabled"]:
        print("只读源文件模式：开启；阶段 C 60 s OCP：本机私有配置已启用；串口直连：关闭")
    else:
        print("只读源文件模式：开启；阶段 C 60 s OCP：锁定；串口和仪器控制：关闭")
    print(f"浏览器地址：http://{bind}:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
