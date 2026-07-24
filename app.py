#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
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

from echem_platform.configuration import load_config, resolve_watch_roots
from echem_platform.control import (
    DRY_RUN_STAGE,
    MacroValidationError,
    ProtocolValidationError,
    build_dry_run,
    default_dry_run_draft,
    dry_run_capabilities,
)
from echem_platform.parsers import (
    PARSER_VERSION,
    ParsedCurve,
    parse_curve,
)


APP_NAME = "电化学测试平台 V0.3 Dry-run"
APP_VERSION = "0.3.0-dev.2"
APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
DEFAULT_CONFIG = APP_ROOT / "config.json"
DEFAULT_DATABASE = APP_ROOT / "state" / "echem-platform.sqlite3"
MAX_JSON_REQUEST_BYTES = 1024 * 1024


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
            if not root.exists() or not root.is_dir():
                continue
            for directory, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if not name.startswith(".")]
                for filename in filenames:
                    path = Path(directory) / filename
                    if path.suffix.lower() in self.extensions:
                        yield path

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
                    before = path.stat()
                    if before.st_size > self.max_file_bytes:
                        counters["skipped"] += 1
                        continue
                    if time.time() - before.st_mtime < self.stable_age_seconds:
                        counters["skipped"] += 1
                        continue
                    data = path.read_bytes()
                    after = path.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        counters["skipped"] += 1
                        self.database.audit("deferred", path.name, "File changed while being read.")
                        continue
                    fingerprint = hashlib.sha256(data).hexdigest()
                    modified = dt.datetime.fromtimestamp(
                        after.st_mtime, tz=dt.timezone.utc
                    ).isoformat(timespec="seconds")
                    curve = parse_curve(path, data, self.max_points)
                    action = self.database.upsert_run(
                        path.resolve(), fingerprint, after.st_size, modified, curve
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

    def watcher(self) -> None:
        while not self.stop_event.is_set():
            self.scanner.scan()
            self.stop_event.wait(self.config["scan_interval_seconds"])

    def status(self) -> dict[str, Any]:
        counts = self.database.status_counts()
        roots = [
            {"path": str(root), "available": root.exists() and root.is_dir()}
            for root in self.scanner.roots
        ]
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "mode": "read_only_sources_and_offline_dry_run",
            "loopback_only": True,
            "instrument_control": self.config["instrument_control_enabled"],
            "control_stage": DRY_RUN_STAGE,
            "launch_available": False,
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
                    self.send_json(dry_run_capabilities())
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
                elif path in {"/protocol", "/protocol.html"}:
                    self.send_static("protocol.html")
                elif path.startswith("/static/"):
                    self.send_static(path[len("/static/") :])
                else:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
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
            except ProtocolValidationError as exc:
                self.send_json(exc.to_dict(), HTTPStatus.BAD_REQUEST)
            except MacroValidationError as exc:
                self.send_json(exc.to_dict(), HTTPStatus.BAD_REQUEST)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception:
                traceback.print_exc()
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误。")

    return Handler


def build_runtime(config_path: Path, database_path: Path) -> RuntimeState:
    config = load_config(config_path)
    roots = resolve_watch_roots(config, config_path.parent)
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

    bind = runtime.config["bind"]
    port = args.port or runtime.config["port"]
    server = ThreadingHTTPServer((bind, port), create_handler(runtime))
    print(f"{APP_NAME} {APP_VERSION}")
    print("只读源文件模式：开启；网页 Dry-run：开启；串口和仪器控制：关闭")
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
