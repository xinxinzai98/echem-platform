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
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


APP_NAME = "电化学测试平台 V0"
APP_VERSION = "0.1.4"
PARSER_VERSION = "2026.07.24.1"
APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
DEFAULT_CONFIG = APP_ROOT / "config.json"
DEFAULT_DATABASE = APP_ROOT / "state" / "echem-platform.sqlite3"


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


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    config = {
        "bind": str(raw.get("bind", "127.0.0.1")),
        "port": int(raw.get("port", 8787)),
        "scan_interval_seconds": max(3, int(raw.get("scan_interval_seconds", 15))),
        "stable_age_seconds": max(0, int(raw.get("stable_age_seconds", 2))),
        "max_file_bytes": max(1024, int(raw.get("max_file_bytes", 50 * 1024 * 1024))),
        "max_points_per_curve": max(100, int(raw.get("max_points_per_curve", 2000))),
        "watch_roots": list(raw.get("watch_roots", [])),
        "extensions": [
            str(item).lower() if str(item).startswith(".") else "." + str(item).lower()
            for item in raw.get("extensions", [])
        ],
    }
    if config["bind"] not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("V0 safety policy only permits a loopback bind address.")
    return config


def resolve_watch_roots(config: dict[str, Any], base: Path) -> list[Path]:
    roots: list[Path] = []
    for raw in config["watch_roots"]:
        candidate = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if not candidate.is_absolute():
            candidate = base / candidate
        roots.append(candidate.resolve())
    return roots


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def decode_bytes(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def split_fields(line: str, delimiter: str) -> list[str]:
    if delimiter == "whitespace":
        return [part.strip() for part in re.split(r"\s+", line.strip()) if part.strip()]
    return [part.strip().strip('"') for part in line.split(delimiter)]


def choose_delimiter(line: str) -> str:
    counts = {"\t": line.count("\t"), ",": line.count(","), ";": line.count(";")}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count else "whitespace"


def parse_float(value: str) -> float | None:
    cleaned = (
        value.strip()
        .replace("\u2212", "-")
        .replace("−", "-")
        .replace("D+", "E+")
        .replace("D-", "E-")
        .replace("d+", "e+")
        .replace("d-", "e-")
    )
    cleaned = cleaned.strip("()[]")
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def unit_from_header(header: str) -> str:
    match = re.search(r"\(([^)]+)\)", header)
    if match:
        return match.group(1).strip()
    if "/" in header:
        return header.rsplit("/", 1)[-1].strip()
    return ""


def normalized_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.lower())


def find_axis_indices(headers: list[str], technique: str = "") -> tuple[int, int]:
    normalized = [normalized_header(item) for item in headers]

    def first_matching(patterns: Iterable[str], excluded: set[int] | None = None) -> int | None:
        excluded = excluded or set()
        for index, header in enumerate(normalized):
            if index in excluded:
                continue
            if any(pattern in header for pattern in patterns):
                return index
        return None

    real_index = first_matching(("zreal", "z'", "rez", "zre"))
    imag_index = first_matching(("zimag", "z''", "imz", "zim"))
    if real_index is not None and imag_index is not None and real_index != imag_index:
        return real_index, imag_index

    frequency_index = first_matching(("frequency", "freq", "f(hz", "f/"))
    if frequency_index is not None:
        y_index = first_matching(
            ("|z|", "zmod", "modulus", "phase", "zimag", "zreal"),
            {frequency_index},
        )
        if y_index is not None:
            return frequency_index, y_index

    time_index = first_matching(("time", "t(s", "t/sec", "t/second"))
    potential_index = first_matching(("potential", "voltage", "e(v", "e/v"))
    current_index = first_matching(("current", "i(a", "i/ma", "i/ua", "i/a"))

    if time_index is not None:
        if technique in {"CP/GCD", "OCP"}:
            y_index = potential_index if potential_index is not None else current_index
        elif technique == "CA":
            y_index = current_index if current_index is not None else potential_index
        else:
            y_index = current_index if current_index is not None else potential_index
        if y_index is not None and y_index != time_index:
            return time_index, y_index
    if potential_index is not None and current_index is not None:
        return potential_index, current_index

    return (0, 1 if len(headers) > 1 else 0)


def infer_instrument(path: Path, text: str) -> str:
    probe = f"{path.name}\n{text[:3000]}".lower()
    if path.suffix.lower() in {".cor", ".z60"} or "csstudiofile" in probe or "corrtest" in probe:
        return "CorrTest"
    if path.suffix.lower() == ".bin" or "chi instrument" in probe or re.search(r"\bchi\d", probe):
        return "CHI"
    return "未知"


def infer_technique(path: Path, headers: list[str], text: str) -> str:
    probe = " ".join([path.stem, *headers, text[:1000]]).lower()
    if path.suffix.lower() == ".z60" or any(
        token in probe for token in ("zreal", "zimag", "frequency", "freq(hz", "eis", "impedance")
    ):
        return "EIS"
    ordered = (
        ("OCP", ("ocp", "open circuit")),
        ("LSV", ("lsv", "linear sweep")),
        ("CV", ("cyclic volt", "cv_", "_cv", "cv1", "cv2")),
        ("CA", ("chronoamper", "ca_", "_ca", "it_", "_it")),
        ("CP/GCD", ("chronopot", "galstatic", "galvano", "gcd", "cp_", "_cp", "cc_")),
        ("Tafel", ("tafel",)),
    )
    for technique, tokens in ordered:
        if any(token in probe for token in tokens):
            return technique
    return "未识别"


def decimate_points(points: list[list[float]], limit: int) -> list[list[float]]:
    if len(points) <= limit:
        return points
    if limit <= 2:
        return [points[0], points[-1]]
    step = (len(points) - 1) / (limit - 1)
    indices = [round(position * step) for position in range(limit)]
    return [points[index] for index in indices]


@dataclass
class ParsedCurve:
    status: str
    encoding: str
    delimiter: str
    headers: list[str]
    x_name: str
    x_unit: str
    y_name: str
    y_unit: str
    point_count: int
    points: list[list[float]]
    instrument: str
    technique: str
    error: str = ""


def parse_curve(path: Path, data: bytes, max_points: int) -> ParsedCurve:
    if path.suffix.lower() == ".bin":
        return ParsedCurve(
            status="metadata_only",
            encoding="binary",
            delimiter="",
            headers=[],
            x_name="",
            x_unit="",
            y_name="",
            y_unit="",
            point_count=0,
            points=[],
            instrument="CHI",
            technique=infer_technique(path, [], ""),
        )

    text, encoding = decode_bytes(data)
    lines = [line.replace("\x00", "").strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith(("#", "//"))]
    candidates: list[tuple[int, int, str, list[str]]] = []

    for index, line in enumerate(lines):
        delimiter = choose_delimiter(line)
        headers = split_fields(line, delimiter)
        if not (2 <= len(headers) <= 30):
            continue
        if sum(parse_float(item) is None for item in headers) < 1:
            continue
        for next_index in range(index + 1, min(index + 3, len(lines))):
            values = split_fields(lines[next_index], delimiter)
            numeric_count = sum(parse_float(item) is not None for item in values)
            if len(values) >= 2 and numeric_count >= 2:
                header_probe = " ".join(normalized_header(item) for item in headers)
                axis_terms = (
                    "potential",
                    "voltage",
                    "current",
                    "frequency",
                    "freq",
                    "zreal",
                    "zimag",
                    "time",
                    "e(v",
                    "i(a",
                    "t(s",
                )
                axis_score = sum(term in header_probe for term in axis_terms)
                distance_penalty = next_index - index - 1
                score = axis_score * 100 + min(len(headers), 10) - distance_penalty
                candidates.append((score, index, delimiter, headers))
                break

    selected = max(candidates, default=None, key=lambda item: item[0])

    instrument = infer_instrument(path, text)
    if not selected:
        return ParsedCurve(
            status="unparsed",
            encoding=encoding,
            delimiter="",
            headers=[],
            x_name="",
            x_unit="",
            y_name="",
            y_unit="",
            point_count=0,
            points=[],
            instrument=instrument,
            technique=infer_technique(path, [], text),
            error="未找到至少包含两列数值的表格。",
        )

    _, header_index, delimiter, headers = selected
    technique = infer_technique(path, headers, text)
    x_index, y_index = find_axis_indices(headers, technique)
    points: list[list[float]] = []
    for line in lines[header_index + 1 :]:
        values = split_fields(line, delimiter)
        if len(values) <= max(x_index, y_index):
            continue
        x_value = parse_float(values[x_index])
        y_value = parse_float(values[y_index])
        if x_value is not None and y_value is not None:
            points.append([x_value, y_value])

    if not points:
        status = "unparsed"
        error = "表头已识别，但没有可用的二维数值点。"
    else:
        status = "parsed"
        error = ""

    return ParsedCurve(
        status=status,
        encoding=encoding,
        delimiter="TAB" if delimiter == "\t" else delimiter,
        headers=headers,
        x_name=headers[x_index] if headers else "",
        x_unit=unit_from_header(headers[x_index]) if headers else "",
        y_name=headers[y_index] if headers else "",
        y_unit=unit_from_header(headers[y_index]) if headers else "",
        point_count=len(points),
        points=decimate_points(points, max_points),
        instrument=instrument,
        technique=technique,
        error=error,
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
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "parser_version" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''"
                )

    def audit(self, action: str, target: str, detail: str) -> None:
        with self._lock, self.session() as connection:
            connection.execute(
                "INSERT INTO audit(created_utc, action, target, detail) VALUES (?, ?, ?, ?)",
                (utc_now(), action, target, detail),
            )

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
                        parser_version=?, sha256=?, size_bytes=?, modified_utc=?, imported_utc=?, encoding=?,
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
                        parser_version, sha256, size_bytes, modified_utc, imported_utc, encoding,
                        delimiter, headers_json, x_name, x_unit, y_name, y_unit,
                        point_count, points_json, source_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                action = "imported"
        self.audit(action, path.name, f"{curve.instrument} / {curve.technique} / {curve.point_count} points")
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
                SELECT id, source_name, instrument, technique, parse_status, sha256,
                       size_bytes, modified_utc, imported_utc, point_count, sample_id,
                       material, electrolyte, area_cm2, tags, x_name, x_unit, y_name, y_unit
                FROM runs{where}
                ORDER BY modified_utc DESC, id DESC
                LIMIT 500
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["headers"] = json.loads(result.pop("headers_json"))
        result["points"] = json.loads(result.pop("points_json"))
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
        return {
            "total": total,
            "parsed": parsed,
            "metadata_only": metadata_only,
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
            "mode": "read_only_sources",
            "loopback_only": True,
            "instrument_control": False,
            "serial_access": False,
            "uptime_seconds": round(time.monotonic() - self.started),
            "last_scan": self.scanner.last_scan,
            "last_scan_result": self.scanner.last_result,
            "watch_roots": roots,
            **counts,
        }


def create_handler(runtime: RuntimeState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EchemPlatform/0.1"

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
            self.end_headers()
            self.wfile.write(data)

        def send_error_json(self, status: int, message: str) -> None:
            self.send_json({"error": message}, status)

        def read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise ValueError("请求内容为空或过大。")
            return json.loads(self.rfile.read(length).decode("utf-8"))

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
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path == "/api/status":
                    self.send_json(runtime.status())
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
    print("只读源文件模式：开启；串口和仪器控制：关闭")
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
