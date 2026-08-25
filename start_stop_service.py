#!/usr/bin/env python3
"""Minimal HTTP service for the database-backed start-stop repository.

This entrypoint deliberately does not import the general platform application.
Its public surface is limited to start-stop analysis, collection jobs, and the
small set of static assets required by that page.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import errno
import fcntl
import gzip
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

from echem_platform.start_stop import (
    StartStopWorkspace,
    StartStopWorkspaceError,
    duplicate_plot_name_notice,
)
from echem_platform.start_stop_auto_update import AutoUpdateScheduler
from echem_platform.start_stop_collection import (
    DEFAULT_CONNECTIVITY_TIMEOUT_SECONDS,
    CollectionError,
    SSHWindowsTransport,
)
from echem_platform.start_stop_collection_config import (
    CollectionConfigConflict,
    CollectionConfigError,
    CollectionConfigStore,
    normalize_windows_directory,
)
from echem_platform.start_stop_cv_eis import (
    CvEisAnalysisError,
    CvEisRepositoryAnalyzer,
    ReadOnlyCvEisDatabase,
)
from echem_platform.start_stop_database import (
    AutoUpdateConfigConflict,
    StartStopDatabase,
)
from echem_platform.start_stop_live_preview import (
    LivePreviewScheduler,
    LivePreviewStateFile,
)
from echem_platform.start_stop_workstations import (
    DEFAULT_DISCOVERY_SECONDS as WORKSTATION_DISCOVERY_SECONDS,
    DEFAULT_POLL_SECONDS as WORKSTATION_POLL_SECONDS,
    WorkstationMonitor,
)


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
MAX_JSON_REQUEST_BYTES = 1024 * 1024
MAX_LAN_AUTH_FILE_BYTES = 4096
MAX_AUTHORIZATION_HEADER_BYTES = 8192
DEPLOYMENT_PROFILE = "start_stop_repository"

GET_API_PATHS = frozenset(
    {
        "/api/start-stop/status",
        "/api/start-stop/materials",
        "/api/start-stop/series",
        "/api/start-stop/chart",
        "/api/start-stop/cv-eis",
        "/api/start-stop/cv-eis/curve",
        "/api/start-stop/pdf",
        "/api/start-stop/connectivity",
        "/api/start-stop/workstations",
        "/api/start-stop/collection-config",
        "/api/start-stop/auto-update",
        "/api/start-stop/live-preview",
    }
)
POST_API_PATHS = frozenset(
    {
        "/api/start-stop/materials",
        "/api/start-stop/jobs",
        "/api/start-stop/uploads",
        "/api/start-stop/connectivity/check",
        "/api/start-stop/connectivity/path-check",
    }
)
PUT_API_PATHS = frozenset(
    {
        "/api/start-stop/collection-config",
        "/api/start-stop/auto-update",
        "/api/start-stop/live-preview",
    }
)
STATIC_FILES = {
    "/static/styles.css": "styles.css",
    "/static/workbench.css": "workbench.css",
    "/static/start-stop.css": "start-stop.css",
    "/static/start-stop.js": "start-stop.js",
    "/static/start-stop-shell.js": "start-stop-shell.js",
    "/static/start-stop-config.css": "start-stop-config.css",
    "/static/start-stop-config.html": "start-stop-config.html",
    "/static/start-stop-config.js": "start-stop-config.js",
    "/static/start-stop-materials.html": "start-stop-materials.html",
    "/static/start-stop-materials.js": "start-stop-materials.js",
    "/static/start-stop-cv-eis.css": "start-stop-cv-eis.css",
    "/static/start-stop-cv-eis.html": "start-stop-cv-eis.html",
    "/static/start-stop-cv-eis.js": "start-stop-cv-eis.js",
    "/static/start-stop-workstations.css": "start-stop-workstations.css",
    "/static/start-stop-workstations.html": "start-stop-workstations.html",
    "/static/start-stop-workstations.js": "start-stop-workstations.js",
}
ICON_PATH = re.compile(r"^/static/icons/([A-Za-z0-9][A-Za-z0-9_.-]*\.svg)$")
RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
LAN_JOB_RESULT_KEYS = frozenset(
    {
        "stage",
        "analysis_series",
        "series_in_atlas",
        "materials_analyzed",
        "materials_available",
        "materials_skipped_unchanged",
        "materials_in_atlas",
        "materials_excluded_from_atlas",
        "included_files",
        "included_standard_files",
        "included_start_stop_files",
        "included_adt_files",
        "complete_cycles",
        "normal_cycles",
        "abnormal_cycles",
        "collection_outcome",
        "collection_machines_total",
        "collection_machines_with_errors",
        "collection_roots_total",
        "collection_roots_ok",
        "collection_roots_failed",
        "collection_inventoried",
        "collection_already_collected",
        "collection_planned_files",
        "collection_planned_bytes",
        "collection_copied",
        "collection_downloaded",
        "collection_ingested",
        "collection_versioned",
        "collection_unchanged_content",
        "collection_unsettled_skipped",
        "collection_changed_during_collection",
        "collection_errors",
        "repository_files",
        "repository_bytes",
        "repository_new_versions",
        "repository_reused_blobs",
        "snapshot_cache_hits",
        "snapshot_materialized_files",
        "analysis_skipped_unchanged",
        "uploaded_files",
    }
)
JOB_ACTIONS = frozenset({"scan", "prepare_upload", "render"})
JOB_STATUSES = frozenset(
    {
        "idle", "queued", "running", "completed", "completed_with_warnings",
        "failed", "interrupted",
    }
)
JOB_FAILURE_CLASSES = frozenset({"none", "partial", "fatal", "interrupted"})
JOB_REQUEST_ORIGINS = frozenset({"manual", "automatic", "upload"})
JOB_STAGES = frozenset(
    {
        "idle",
        "queued",
        "collecting_remote",
        "connecting_remote",
        "connecting_machines",
        "scanning_remote",
        "scanning_files",
        "downloading_files",
        "storing_files",
        "writing_database",
        "preparing_upload",
        "freezing_snapshot",
        "materializing_snapshot",
        "refreshing_material_table",
        "rendering",
        "publishing",
        "finalizing",
        "prepared",
        "rendered",
        "completed",
        "failed",
    }
)
JOB_PROGRESS_MODES = frozenset({"determinate", "indeterminate"})
JOB_PROGRESS_UNITS = frozenset(
    {"files", "machines", "steps", "items", "bytes", "materials", "pages"}
)
JOB_PROGRESS_MACHINE_STATUSES = frozenset(
    {
        "pending",
        "queued",
        "connecting",
        "scanning",
        "downloading",
        "storing",
        "importing",
        "refreshing",
        "running",
        "completed",
        "completed_with_warnings",
        "skipped",
        "unreachable",
        "failed",
    }
)
JOB_PROGRESS_MAX_COUNT = 9_007_199_254_740_991
JOB_PROGRESS_MAX_PHASES = 1_000
JOB_PROGRESS_MAX_MACHINES = 32
JOB_RESULT_COUNT_KEYS = LAN_JOB_RESULT_KEYS - {"stage", "collection_outcome"}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_UTC_TIMESTAMP = re.compile(
    r"^$|^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)
SENSITIVE_PROGRESS_TEXT = re.compile(
    r"(?:traceback|identity(?:file)?|private[ _-]?key|stderr|stdout|exception)",
    re.IGNORECASE,
)


class StartStopRequestError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = int(status)


class BasicAuthCredentials:
    """Hold only fixed-length credential digests for constant-time matching."""

    __slots__ = ("_username_digest", "_password_digest")

    def __init__(self, username: str, password: str) -> None:
        self._username_digest = hashlib.sha256(username.encode("utf-8")).digest()
        self._password_digest = hashlib.sha256(password.encode("utf-8")).digest()

    def matches(self, username: str, password: str) -> bool:
        username_digest = hashlib.sha256(username.encode("utf-8")).digest()
        password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        username_ok = secrets.compare_digest(username_digest, self._username_digest)
        password_ok = secrets.compare_digest(password_digest, self._password_digest)
        return username_ok and password_ok


class DatabaseInstanceLock:
    """Hold an OS lock for one local service process per database."""

    def __init__(self, database_path: str | Path) -> None:
        try:
            database = Path(database_path).expanduser().resolve()
            self._path = database.with_name(f"{database.name}.service.lock")
        except (OSError, ValueError) as exc:
            raise ValueError("无法安全确定启停数据库单实例锁。") from exc
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        if not getattr(os, "O_NOFOLLOW", 0):
            raise ValueError("当前系统不支持安全的启停数据库单实例锁。")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self._path,
                os.O_RDWR
                | os.O_CREAT
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as exc:
            raise ValueError("无法安全创建启停数据库单实例锁。") from exc

        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                raise ValueError("启停数据库单实例锁文件不安全。")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ValueError("同一启停数据库已有本机服务运行。") from exc
                raise ValueError("无法安全获取启停数据库单实例锁。") from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> DatabaseInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def load_lan_auth_file(path: str | Path) -> BasicAuthCredentials:
    """Read a small owner-only, non-symlinked ``username:password`` file."""
    auth_path = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(auth_path, flags)
    except OSError as exc:
        raise ValueError("LAN 认证凭据文件不可安全读取。") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("LAN 认证凭据必须是普通文件。")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("LAN 认证凭据文件权限必须为 0600。")
        if metadata.st_uid != os.geteuid():
            raise ValueError("LAN 认证凭据文件必须归当前服务用户所有。")
        if metadata.st_size <= 0 or metadata.st_size > MAX_LAN_AUTH_FILE_BYTES:
            raise ValueError("LAN 认证凭据文件为空或过大。")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(MAX_LAN_AUTH_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_LAN_AUTH_FILE_BYTES:
        raise ValueError("LAN 认证凭据文件过大。")
    stripped = raw.rstrip(b"\r\n")
    if not stripped or b"\n" in stripped or b"\r" in stripped or b"\x00" in stripped:
        raise ValueError("LAN 认证凭据文件格式无效。")
    try:
        credential_text = stripped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("LAN 认证凭据文件必须使用 UTF-8。") from exc
    username, separator, password = credential_text.partition(":")
    if (
        not separator
        or not username
        or not password
        or username != username.strip()
        or len(username.encode("utf-8")) > 256
        or len(password.encode("utf-8")) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in username + password)
    ):
        raise ValueError("LAN 认证凭据文件必须是 username:password 格式。")
    return BasicAuthCredentials(username, password)


def _utc_now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _machine_name(machine: dict[str, Any]) -> str:
    configured = str(machine.get("name") or "").strip()
    if configured:
        return configured
    machine_id = str(machine.get("id") or "").strip()
    parts = machine_id.split("_")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1]:
        return parts[1]
    return str(machine.get("hostname") or machine_id or "未命名实验机")


def _safe_connectivity_failure(error: Exception) -> str:
    """Map SSH failures to useful public messages without exposing local paths."""
    detail = str(error)
    if "超时" in detail:
        return "SSH 连接超时"
    if "密钥不存在" in detail or "known_hosts 不存在" in detail:
        return "SSH 密钥或主机指纹配置未就绪"
    if "地址必须" in detail:
        return "实验机 IP 配置无效"
    if "无法启动 SSH" in detail:
        return "服务器 SSH 客户端不可用"
    return "SSH 服务不可达或密钥认证失败"


class MachineConnectivityMonitor:
    """Expose fixed configured machines and cache read-only SSH probe results."""

    def __init__(
        self,
        collection_config: str | Path | None,
        *,
        transport_factory: Any = SSHWindowsTransport,
        timeout_seconds: int = DEFAULT_CONNECTIVITY_TIMEOUT_SECONDS,
        config_loader: Any | None = None,
    ) -> None:
        self.collection_config = (
            Path(collection_config).expanduser()
            if collection_config is not None and str(collection_config).strip()
            else None
        )
        self.transport_factory = transport_factory
        self.timeout_seconds = max(1, min(int(timeout_seconds), 30))
        self.config_loader = config_loader
        self._results: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._checking: set[str] = set()

    def _config(self) -> dict[str, Any]:
        if self.config_loader is not None:
            payload = self.config_loader()
        else:
            if self.collection_config is None or not self.collection_config.is_file():
                raise OSError("实验机采集配置不可用")
            payload = json.loads(self.collection_config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("实验机采集配置无效")
        machines = payload.get("machines")
        if not isinstance(machines, list) or not machines:
            raise ValueError("实验机采集配置没有机器")
        seen: set[str] = set()
        for machine in machines:
            if not isinstance(machine, dict):
                raise ValueError("实验机采集配置无效")
            machine_id = str(machine.get("id") or "").strip()
            hostname = str(machine.get("hostname") or "").strip()
            ip = str(machine.get("ip") or "").strip()
            if not machine_id or not hostname or not ip or machine_id in seen:
                raise ValueError("实验机采集配置无效")
            address = ipaddress.ip_address(ip)
            if address.version != 4 or not any(address in network for network in RFC1918_NETWORKS):
                raise ValueError("实验机必须使用 RFC1918 IPv4 地址")
            seen.add(machine_id)
        return payload

    @staticmethod
    def _base(machine: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(machine["id"]),
            "name": _machine_name(machine),
            "hostname": str(machine["hostname"]),
            "ip": str(machine["ip"]),
        }

    def snapshot(self, *, can_check: bool) -> dict[str, Any]:
        try:
            config = self._config()
            machines = config["machines"]
        except (OSError, ValueError, json.JSONDecodeError):
            return {
                "available": False,
                "can_check": False,
                "message": "实验机采集配置当前不可用。",
                "machines": [],
                "total": 0,
                "checked": 0,
                "reachable": 0,
            }
        with self._lock:
            results = dict(self._results)
            checking = set(self._checking)
        public_machines: list[dict[str, Any]] = []
        for machine in machines:
            machine_id = str(machine["id"])
            result = results.get(machine_id)
            item = self._base(machine)
            if machine_id in checking:
                item.update(
                    {
                        "status": "checking",
                        "reachable": None,
                        "message": "正在检查 SSH 连通性…",
                        "elapsed_ms": None,
                        "checked_at_utc": None,
                    }
                )
            elif result is not None:
                item.update(result)
            else:
                item.update(
                    {
                        "status": "unchecked",
                        "reachable": None,
                        "message": "尚未检查",
                        "elapsed_ms": None,
                        "checked_at_utc": None,
                    }
                )
            public_machines.append(item)
        checked = sum(item["reachable"] is not None for item in public_machines)
        reachable = sum(item["reachable"] is True for item in public_machines)
        return {
            "available": True,
            "can_check": bool(can_check),
            "message": (
                "可逐台检查 SSH 连通性。"
                if can_check
                else "局域网只读入口不能发起连通性检查。"
            ),
            "machines": public_machines,
            "total": len(public_machines),
            "checked": checked,
            "reachable": reachable,
        }

    def check(self, machine_id: str) -> dict[str, Any]:
        config = self._config()
        matches = [
            machine
            for machine in config["machines"]
            if str(machine.get("id") or "") == machine_id
        ]
        if len(matches) != 1:
            raise KeyError(machine_id)
        machine = matches[0]
        with self._lock:
            if machine_id in self._checking:
                raise RuntimeError("该实验机正在检查中")
            self._checking.add(machine_id)
        started = time.monotonic()
        try:
            known_hosts = config.get("known_hosts_file")
            transport = self.transport_factory(
                known_hosts_file=(
                    Path(str(known_hosts)).expanduser() if known_hosts else None
                )
            )
            try:
                probe = transport.check_connectivity(
                    machine,
                    timeout=self.timeout_seconds,
                )
                result = {
                    "status": "reachable",
                    "reachable": True,
                    "message": str(probe.get("message") or "SSH 连接正常"),
                    "elapsed_ms": max(0, int(probe.get("elapsed_ms", 0))),
                    "checked_at_utc": str(probe.get("checked_at_utc") or _utc_now_text()),
                }
            except (CollectionError, OSError, ValueError) as exc:
                result = {
                    "status": "unreachable",
                    "reachable": False,
                    "message": _safe_connectivity_failure(exc),
                    "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
                    "checked_at_utc": _utc_now_text(),
                }
            with self._lock:
                self._results[machine_id] = result
            return {**self._base(machine), **result}
        finally:
            with self._lock:
                self._checking.discard(machine_id)

    def check_path(self, machine_id: str, remote_path: str) -> dict[str, Any]:
        """Check one validated directory without inventorying any children."""
        config = self._config()
        matches = [
            machine
            for machine in config["machines"]
            if str(machine.get("id") or "") == machine_id
        ]
        if len(matches) != 1:
            raise KeyError(machine_id)
        machine = matches[0]
        path = normalize_windows_directory(remote_path)
        with self._lock:
            if machine_id in self._checking:
                raise RuntimeError("该实验机正在检查中")
            self._checking.add(machine_id)
        started = time.monotonic()
        try:
            known_hosts = config.get("known_hosts_file")
            transport = self.transport_factory(
                known_hosts_file=(
                    Path(str(known_hosts)).expanduser() if known_hosts else None
                )
            )
            try:
                probe = transport.check_directory(
                    machine,
                    path,
                    timeout=self.timeout_seconds,
                )
                reachable = probe.get("reachable") is True
                return {
                    "machine_id": machine_id,
                    "path": path,
                    "status": "reachable" if reachable else "unreachable",
                    "reachable": reachable,
                    "message": (
                        "目录可访问"
                        if reachable
                        else "未找到目录或当前账户无权访问"
                    ),
                    "elapsed_ms": max(0, int(probe.get("elapsed_ms", 0))),
                    "checked_at_utc": str(
                        probe.get("checked_at_utc") or _utc_now_text()
                    ),
                }
            except (CollectionError, OSError, ValueError) as exc:
                return {
                    "machine_id": machine_id,
                    "path": path,
                    "status": "unreachable",
                    "reachable": False,
                    "message": _safe_connectivity_failure(exc),
                    "elapsed_ms": max(
                        0, round((time.monotonic() - started) * 1000)
                    ),
                    "checked_at_utc": _utc_now_text(),
                }
        finally:
            with self._lock:
                self._checking.discard(machine_id)


def _authority(value: str) -> tuple[str, int | None] | None:
    """Parse a plain HTTP authority without accepting credentials or paths."""
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
    return parsed.hostname.lower().rstrip("."), port


def _loopback_authority(value: str) -> tuple[str, int | None] | None:
    authority = _authority(value)
    if authority is None:
        return None
    hostname, port = authority
    if hostname == "localhost":
        return hostname, port
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    return (address.compressed, port) if address.is_loopback else None


def _numeric_authority(value: str) -> tuple[str, int | None] | None:
    authority = _authority(value)
    if authority is None:
        return None
    hostname, port = authority
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    return address.compressed, port


def _effective_port(port: int | None) -> int:
    return int(port) if port is not None else 80


def validate_bind(
    bind: str,
    *,
    lan_read_only: bool,
    container_mode: bool,
) -> str:
    value = str(bind or "").strip()
    if value == "localhost":
        if lan_read_only:
            raise ValueError("局域网只读入口必须绑定明确的 RFC1918 IPv4 地址。")
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("监听地址必须是 localhost 或明确的数字 IP 地址。") from exc
    if address.is_loopback:
        if lan_read_only:
            raise ValueError("局域网只读入口不能绑定回环地址。")
        return address.compressed
    if container_mode and address.version == 4 and address.is_unspecified:
        return address.compressed
    if not lan_read_only:
        raise ValueError("本地读写入口只能绑定回环地址。")
    if address.version != 4 or not any(address in network for network in RFC1918_NETWORKS):
        raise ValueError("局域网只读入口仅允许 RFC1918 IPv4 地址。")
    return address.compressed


def validate_public_endpoint(
    *,
    public_host: str,
    public_port: int,
    lan_read_only: bool,
) -> tuple[str, int]:
    if not 1 <= int(public_port) <= 65535:
        raise ValueError("公开端口必须位于 1 到 65535。")
    if lan_read_only:
        parsed = _numeric_authority(public_host)
        if parsed is None or parsed[1] is not None:
            raise ValueError("局域网公开地址必须是明确的 RFC1918 IPv4 地址。")
        host = parsed[0]
        address = ipaddress.ip_address(host)
        if address.version != 4 or not any(address in network for network in RFC1918_NETWORKS):
            raise ValueError("局域网公开地址仅允许 RFC1918 IPv4 地址。")
    else:
        parsed = _loopback_authority(public_host)
        if parsed is None or parsed[1] is not None:
            raise ValueError("本地公开地址必须是回环地址。")
        host = parsed[0]
    return host, int(public_port)


def validate_request_host(
    *,
    host: str,
    expected_host: str,
    expected_port: int,
    lan_read_only: bool,
) -> tuple[str, int]:
    parsed = _numeric_authority(host) if lan_read_only else _loopback_authority(host)
    if parsed is None:
        raise StartStopRequestError(
            "局域网只读访问必须使用服务器的数字 IP 地址。"
            if lan_read_only
            else "本地访问必须使用回环 Host。",
            HTTPStatus.FORBIDDEN,
        )
    request_host, request_port = parsed
    if _effective_port(request_port) != int(expected_port):
        raise StartStopRequestError("请求 Host 端口与服务公开端口不一致。", HTTPStatus.FORBIDDEN)
    if lan_read_only and request_host != expected_host:
        raise StartStopRequestError("请求 Host 与局域网服务地址不一致。", HTTPStatus.FORBIDDEN)
    return request_host, _effective_port(request_port)


def validate_local_origin(
    *,
    origin: str | None,
    request_host: str,
    request_port: int,
) -> None:
    if origin is None:
        return
    try:
        parsed = urllib.parse.urlsplit(origin.strip())
    except ValueError as exc:
        raise StartStopRequestError("写请求的 Origin 无效。", HTTPStatus.FORBIDDEN) from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise StartStopRequestError("写请求的 Origin 必须与本机服务同源。", HTTPStatus.FORBIDDEN)
    authority = _loopback_authority(parsed.netloc)
    if authority is None:
        raise StartStopRequestError("写请求的 Origin 必须与本机服务同源。", HTTPStatus.FORBIDDEN)
    origin_host, origin_port = authority
    if origin_host != request_host or _effective_port(origin_port) != request_port:
        raise StartStopRequestError("写请求的 Origin 必须与本机服务同源。", HTTPStatus.FORBIDDEN)


def _capabilities(payload: dict[str, Any], *, lan_read_only: bool) -> dict[str, Any]:
    execution = payload.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    available = payload.get("available") is True
    update_ready = execution.get("update_ready", execution.get("ready")) is True
    upload_ready = execution.get("upload_ready") is True
    prepare_upload_ready = execution.get("prepare_upload_ready") is True
    render_ready = execution.get("render_ready", execution.get("ready")) is True
    job = payload.get("job")
    busy = bool(isinstance(job, dict) and job.get("status") in {"queued", "running"})
    allowed_here = not lan_read_only
    can_update = available and update_ready and allowed_here and not busy
    can_upload = available and upload_ready and allowed_here and not busy
    can_prepare_upload = (
        available and prepare_upload_ready and allowed_here and not busy
    )
    can_render = available and render_ready and allowed_here and not busy
    if lan_read_only:
        message = "当前为局域网只读入口；数据下载、保存与重新分析请在服务器本机执行。"
    elif not available:
        message = str(payload.get("message") or "启停数据仓库当前不可用。")
    elif not update_ready and upload_ready:
        message = "可从本机上传数据并更新启停分析；实验电脑下载环境当前未就绪。"
    elif not update_ready:
        message = str(execution.get("message") or "实验电脑数据下载环境尚未就绪。")
    elif busy:
        message = "启停数据任务正在运行，请等待当前任务完成。"
    else:
        message = "可从实验电脑增量下载数据到独立数据库，并更新启停分析。"
    return {
        "server_ready": available and update_ready,
        "render_server_ready": available and render_ready,
        "allowed_here": allowed_here,
        "busy": busy,
        "can_start_update": can_update,
        "can_update_data": can_update,
        "can_upload_data": can_upload,
        "can_prepare_upload": can_prepare_upload,
        "can_check_connectivity": allowed_here,
        "can_save_configuration": available and allowed_here and not busy,
        "can_render_atlas": can_render,
        "can_export_pdf": can_render,
        "data_scope": "remote_collect_or_local_upload_to_database_then_start_stop_analysis",
        "database_backed": True,
        "remote_sync_available": execution.get("collection_ready") is True,
        "upload_max_file_bytes": StartStopWorkspace.MAX_UPLOAD_BYTES,
        "upload_max_batch_files": StartStopWorkspace.MAX_UPLOAD_BATCH_FILES,
        "upload_extensions": sorted(StartStopWorkspace.UPLOAD_EXTENSIONS),
        "message": message,
    }


def _parse_upload_query(raw_query: str) -> dict[str, str]:
    if len(raw_query.encode("utf-8")) > 8192:
        raise StartStopRequestError("上传元数据过长。")
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw_query):
        raise StartStopRequestError("上传元数据的 URL 编码无效。")
    try:
        parsed = urllib.parse.parse_qs(
            raw_query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=5,
        )
    except ValueError as exc:
        raise StartStopRequestError("上传元数据无效。") from exc
    required = {"filename", "relative_path", "group", "last_modified", "size"}
    unknown = sorted(set(parsed) - required)
    if unknown:
        raise StartStopRequestError("上传请求包含未知字段：" + ", ".join(unknown))
    missing = sorted(required - set(parsed))
    if missing:
        raise StartStopRequestError("上传请求缺少字段：" + ", ".join(missing))
    if any(len(parsed[key]) != 1 or parsed[key][0] == "" for key in required):
        raise StartStopRequestError("每个上传元数据字段必须且只能提供一个非空值。")
    return {key: parsed[key][0] for key in required}


def _bounded_integer(
    value: Any,
    *,
    maximum: int = JOB_PROGRESS_MAX_COUNT,
) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        parsed = int(value)
    else:
        return None
    return max(0, min(parsed, maximum))


def _bounded_percent(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        return None
    parsed = max(0.0, min(parsed, 100.0))
    if parsed.is_integer():
        return int(parsed)
    return round(parsed, 2)


def _safe_progress_text(value: Any, *, maximum: int) -> str | None:
    """Return a short display-only label, rejecting paths and diagnostic text."""
    if not isinstance(value, str):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    cleaned = " ".join(value.split()).strip()
    if (
        not cleaned
        or len(cleaned) > maximum
        or "/" in cleaned
        or "\\" in cleaned
        or SENSITIVE_PROGRESS_TEXT.search(cleaned)
    ):
        return None
    return cleaned


def _safe_current_item(value: Any) -> str | None:
    """Expose only a filename-like basename, never a host or remote path."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or SENSITIVE_PROGRESS_TEXT.search(raw):
        return None
    candidate = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    candidate = re.sub(r"^[A-Za-z]:", "", candidate).strip()
    if candidate in {"", ".", ".."}:
        return None
    return _safe_progress_text(candidate, maximum=180)


def _safe_progress_machine(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    result: dict[str, Any] = {}
    machine_id = payload.get("machine_id", payload.get("id"))
    if isinstance(machine_id, str) and SAFE_IDENTIFIER.fullmatch(machine_id):
        result["machine_id"] = machine_id
    name = _safe_progress_text(payload.get("name"), maximum=100)
    if name is not None:
        result["name"] = name
    status = payload.get("status")
    if isinstance(status, str) and status in JOB_PROGRESS_MACHINE_STATUSES:
        result["status"] = status
    completed = _bounded_integer(payload.get("completed"))
    total = _bounded_integer(payload.get("total"))
    if total is not None:
        result["total"] = total
    if completed is not None:
        result["completed"] = min(completed, total) if total is not None else completed
    if status in {"failed", "unreachable"}:
        result["message"] = "该实验机当前未完成，请在服务器本机查看详情。"
    elif status == "completed_with_warnings":
        result["message"] = "该实验机已完成，但有文件被暂缓。"
    else:
        message = _safe_progress_text(payload.get("message"), maximum=200)
        if message is not None:
            result["message"] = message
    return result or None


def _sanitize_job_progress(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    result: dict[str, Any] = {}
    mode = payload.get("mode")
    if isinstance(mode, str) and mode in JOB_PROGRESS_MODES:
        result["mode"] = mode
    percent = _bounded_percent(payload.get("percent"))
    if percent is not None:
        result["percent"] = percent
    completed = _bounded_integer(payload.get("completed"))
    total = _bounded_integer(payload.get("total"))
    if total is not None:
        result["total"] = total
    if completed is not None:
        result["completed"] = min(completed, total) if total is not None else completed
    unit = payload.get("unit")
    if isinstance(unit, str) and unit in JOB_PROGRESS_UNITS:
        result["unit"] = unit
    phase_count = _bounded_integer(
        payload.get("phase_count"), maximum=JOB_PROGRESS_MAX_PHASES
    )
    phase_index = _bounded_integer(
        payload.get("phase_index"), maximum=JOB_PROGRESS_MAX_PHASES
    )
    if phase_count is not None:
        result["phase_count"] = phase_count
    if phase_index is not None:
        result["phase_index"] = (
            min(phase_index, phase_count) if phase_count is not None else phase_index
        )
    phase_label = _safe_progress_text(payload.get("phase_label"), maximum=100)
    if phase_label is not None:
        result["phase_label"] = phase_label
    current_item = _safe_current_item(payload.get("current_item"))
    if current_item is not None:
        result["current_item"] = current_item
    detail = _safe_progress_text(payload.get("detail"), maximum=240)
    if detail is not None:
        result["detail"] = detail
    machines = payload.get("machines")
    if isinstance(machines, list):
        safe_machines = [
            safe
            for item in machines[:JOB_PROGRESS_MAX_MACHINES]
            if (safe := _safe_progress_machine(item)) is not None
        ]
        result["machines"] = safe_machines
    return result


def _sanitize_job_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, Any] = {}
    for key in JOB_RESULT_COUNT_KEYS:
        if key not in payload:
            continue
        value = _bounded_integer(payload[key])
        if value is not None:
            result[key] = value
    stage = payload.get("stage")
    if isinstance(stage, str) and stage in JOB_STAGES:
        result["stage"] = stage
    outcome = payload.get("collection_outcome")
    if isinstance(outcome, str) and outcome in {
        "complete",
        "partial",
        "failed",
        "skipped",
    }:
        result["collection_outcome"] = outcome
    return result


def _sanitize_job(payload: dict[str, Any], *, lan_read_only: bool) -> dict[str, Any]:
    sanitized = dict(payload)
    job = payload.get("job")
    if not isinstance(job, dict):
        return sanitized
    safe_job: dict[str, Any] = {}
    job_id = job.get("id")
    if isinstance(job_id, str) and SAFE_IDENTIFIER.fullmatch(job_id):
        safe_job["id"] = job_id
    action = job.get("action")
    if isinstance(action, str) and action in JOB_ACTIONS:
        safe_job["action"] = action
    requested_via = job.get("requested_via")
    if isinstance(requested_via, str) and requested_via in JOB_REQUEST_ORIGINS:
        safe_job["requested_via"] = requested_via
    status = job.get("status")
    if isinstance(status, str) and status in JOB_STATUSES:
        safe_job["status"] = status
    stage = job.get("stage")
    if isinstance(stage, str) and stage in JOB_STAGES:
        safe_job["stage"] = stage
    for key in ("created_utc", "started_utc", "completed_utc", "updated_utc"):
        value = job.get(key)
        if isinstance(value, str) and SAFE_UTC_TIMESTAMP.fullmatch(value):
            safe_job[key] = value
    failure_class = job.get("failure_class")
    if isinstance(failure_class, str) and failure_class in JOB_FAILURE_CLASSES:
        safe_job["failure_class"] = failure_class
    failure_code = job.get("failure_code")
    if (
        isinstance(failure_code, str)
        and (not failure_code or SAFE_IDENTIFIER.fullmatch(failure_code))
    ):
        safe_job["failure_code"] = failure_code
    retry_of_job_id = job.get("retry_of_job_id")
    if isinstance(retry_of_job_id, str) and SAFE_IDENTIFIER.fullmatch(retry_of_job_id):
        safe_job["retry_of_job_id"] = retry_of_job_id
    event_cursor = _bounded_integer(job.get("event_cursor"))
    if event_cursor is not None:
        safe_job["event_cursor"] = event_cursor
    if isinstance(job.get("can_retry"), bool):
        safe_job["can_retry"] = bool(job["can_retry"])
    duplicate_notice = duplicate_plot_name_notice(job.get("message"))
    message = _safe_progress_text(
        duplicate_notice or job.get("message"), maximum=240
    )
    if message is not None:
        safe_job["message"] = message
    if duplicate_notice is not None:
        # Older jobs were persisted with the generic code before duplicate
        # names had a first-class, user-actionable failure state.
        safe_job["failure_code"] = "duplicate_material_name"
    safe_job["result"] = _sanitize_job_result(job.get("result"))
    progress = _sanitize_job_progress(job.get("progress"))
    if progress is not None:
        safe_job["progress"] = progress
    if safe_job.get("status") in {"failed", "interrupted"}:
        if not (
            safe_job.get("failure_code") == "duplicate_material_name"
            and safe_job.get("message")
        ):
            safe_job["message"] = (
                "任务失败，请在服务器本机查看详情。"
                if lan_read_only
                else (
                    "任务因服务重启中断，可重新执行。"
                    if safe_job.get("status") == "interrupted"
                    else "任务失败，请查看 Docker 服务日志。"
                )
            )
    sanitized["job"] = safe_job
    return sanitized


def _sanitize_analysis_provenance(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"state": "none"}
    state = payload.get("state")
    if state not in {"none", "sealed", "legacy_unverified", "unavailable"}:
        state = "unavailable"
    result: dict[str, Any] = {"state": state}
    for key in (
        "analysis_run_id",
        "snapshot_id",
        "config_revision",
        "artifact_generation_id",
    ):
        value = _bounded_integer(payload.get(key))
        if value is not None:
            result[key] = value
    job_id = payload.get("job_id")
    if isinstance(job_id, str) and SAFE_IDENTIFIER.fullmatch(job_id):
        result["job_id"] = job_id
    for key in (
        "dataset_fingerprint",
        "artifact_manifest_sha256",
        "analysis_script_sha256",
        "material_config_sha256",
        "analysis_summary_sha256",
    ):
        value = payload.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            result[key] = value
    created_utc = payload.get("created_utc")
    if isinstance(created_utc, str) and SAFE_UTC_TIMESTAMP.fullmatch(created_utc):
        result["created_utc"] = created_utc
    rules = payload.get("rules")
    if isinstance(rules, dict):
        safe_rules: dict[str, Any] = {}
        for key in (
            "rhe_offset_v",
            "potential_reference",
            "potential_basis",
            "reference_conversion_applied",
            "endpoint",
            "anomaly",
            "water_reference",
            "water_reference_display_name",
            "water_reference_series_id",
            "water_slope_mv_per_h",
        ):
            value = rules.get(key)
            if isinstance(value, (int, float, bool)) and not isinstance(value, dict):
                safe_rules[key] = value
            elif isinstance(value, str):
                safe = _safe_progress_text(value, maximum=160)
                if safe is not None:
                    safe_rules[key] = safe
        material_key = rules.get("water_reference_key")
        if isinstance(material_key, str):
            candidate = material_key.strip()
            parts = candidate.replace("\\", "/").split("/")
            if (
                0 < len(candidate) <= 512
                and not candidate.startswith(("/", "\\"))
                and not re.match(r"^[A-Za-z]:", candidate)
                and all(part not in {"", ".", ".."} for part in parts)
                and not any(ord(character) < 32 or ord(character) == 127 for character in candidate)
            ):
                safe_rules["water_reference_key"] = candidate
        result["rules"] = safe_rules
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        safe_runtime: dict[str, Any] = {}
        for key in (
            "python_version",
            "container_mode",
            "image_or_service_version",
            "artifact_seal_schema_version",
            "analysis_workflow_version",
        ):
            value = runtime.get(key)
            if isinstance(value, (int, float, bool)):
                safe_runtime[key] = value
            elif isinstance(value, str):
                if key == "image_or_service_version":
                    candidate = value.strip()
                    safe = (
                        candidate
                        if re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,119}",
                            candidate,
                        )
                        and ".." not in candidate
                        else None
                    )
                else:
                    safe = _safe_progress_text(value, maximum=120)
                if safe is not None:
                    safe_runtime[key] = safe
        result["runtime"] = safe_runtime
    return result


def _sanitize_safety_status(
    payload: Any,
    *,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Whitelist the path-free safety contract for both local and LAN status."""
    source = payload if isinstance(payload, dict) else {}
    raw_storage = source.get("storage")
    raw_storage = raw_storage if isinstance(raw_storage, dict) else {}
    storage: dict[str, Any] = {}
    for key in ("ok", "preflight_ok", "low_space"):
        if isinstance(raw_storage.get(key), bool):
            storage[key] = raw_storage[key]
    for key in (
        "total_bytes",
        "free_bytes",
        "used_bytes",
        "required_bytes",
        "shortfall_bytes",
        "volume_count",
    ):
        value = _bounded_integer(raw_storage.get(key))
        if value is not None:
            storage[key] = value
    used_percent = _bounded_percent(raw_storage.get("used_percent"))
    if used_percent is not None:
        storage["used_percent"] = used_percent
    ratio = raw_storage.get("low_space_ratio")
    if (
        isinstance(ratio, (int, float))
        and not isinstance(ratio, bool)
        and math.isfinite(float(ratio))
    ):
        storage["low_space_ratio"] = max(0.0, min(float(ratio), 1.0))
    blocked_roles = raw_storage.get("blocked_roles")
    if isinstance(blocked_roles, list):
        allowed_roles = {"database", "scratch", "cache"}
        storage["blocked_roles"] = sorted(
            {
                str(role)
                for role in blocked_roles
                if str(role) in allowed_roles
            }
        )

    raw_backup = source.get("backup")
    raw_backup = raw_backup if isinstance(raw_backup, dict) else {}
    backup: dict[str, Any] = {}
    for key in (
        "configured",
        "available",
        "latest_valid",
        "latest_created_with_full_verification",
        "destination_preflight_ok",
        "same_filesystem",
        "off_disk",
        "routine_full_backup_required",
        "schema_change_full_backup_required",
        "database_repair_full_backup_required",
        "scheduled_background_enabled",
    ):
        if isinstance(raw_backup.get(key), bool):
            backup[key] = raw_backup[key]
    for key in (
        "backup_count",
        "invalid_count",
        "orphan_database_count",
        "latest_size_bytes",
        "destination_required_bytes",
        "destination_shortfall_bytes",
    ):
        value = _bounded_integer(raw_backup.get(key))
        if value is not None:
            backup[key] = value
    latest_created_utc = raw_backup.get("latest_created_utc")
    if (
        isinstance(latest_created_utc, str)
        and SAFE_UTC_TIMESTAMP.fullmatch(latest_created_utc)
    ):
        backup["latest_created_utc"] = latest_created_utc
    verification_level = raw_backup.get("latest_verification_level")
    if verification_level in {"manifest_and_size", "full"}:
        backup["latest_verification_level"] = verification_level
    if raw_backup.get("policy_mode") == "risk_tiered":
        backup["policy_mode"] = "risk_tiered"
    scheduled_state = raw_backup.get("scheduled_state")
    if scheduled_state in {
        "never_run", "running", "completed", "failed", "skipped"
    }:
        backup["scheduled_state"] = scheduled_state
    schedule_label = raw_backup.get("scheduled_background_label")
    if (
        isinstance(schedule_label, str)
        and 0 < len(schedule_label) <= 80
        and not re.search(r"(?:[A-Za-z]:[\\/]|/[^ ]|\\\\)", schedule_label)
    ):
        backup["scheduled_background_label"] = schedule_label
    for key in (
        "scheduled_started_utc",
        "scheduled_completed_utc",
        "scheduled_due_utc",
        "scheduled_next_due_utc",
    ):
        value = raw_backup.get(key)
        if isinstance(value, str) and SAFE_UTC_TIMESTAMP.fullmatch(value):
            backup[key] = value
    scheduled_exit_code = _bounded_integer(raw_backup.get("scheduled_exit_code"))
    if scheduled_exit_code is not None and scheduled_exit_code <= 255:
        backup["scheduled_exit_code"] = scheduled_exit_code

    safety_provenance = dict(provenance)
    if safety_provenance.get("state") == "legacy_unverified":
        safety_provenance["state"] = "legacy"
    runtime = safety_provenance.get("runtime")
    if isinstance(runtime, dict):
        image_reference = runtime.get("image_or_service_version")
        if isinstance(image_reference, str):
            safe_reference = image_reference.strip()
            if (
                re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,119}",
                    safe_reference,
                )
                and ".." not in safe_reference
            ):
                safety_provenance["image_reference"] = safe_reference
    return {
        "storage": storage,
        "backup": backup,
        "provenance": safety_provenance,
    }


def public_status(payload: dict[str, Any], *, lan_read_only: bool) -> dict[str, Any]:
    result = _sanitize_job(payload, lan_read_only=lan_read_only)
    provenance = _sanitize_analysis_provenance(
        payload.get("analysis_provenance")
    )
    result["analysis_provenance"] = provenance
    result["safety"] = _sanitize_safety_status(
        payload.get("safety"),
        provenance=provenance,
    )
    result.update(
        {
            "deployment_profile": DEPLOYMENT_PROFILE,
            "access_mode": "lan_read_only" if lan_read_only else "local_read_write",
            "service": _public_service_identity(),
            "capabilities": _capabilities(payload, lan_read_only=lan_read_only),
        }
    )
    return result


def _public_service_identity() -> dict[str, str]:
    version = str(os.environ.get("START_STOP_SERVICE_VERSION") or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}", version)
        or ".." in version
    ):
        version = "unknown"
    image_reference = str(
        os.environ.get("START_STOP_IMAGE_REFERENCE") or ""
    ).strip()
    if (
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,119}",
            image_reference,
        )
        or ".." in image_reference
    ):
        image_reference = ""
    return {
        "version": version,
        "image_reference": image_reference,
    }


class StartStopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_handler(
    workspace: StartStopWorkspace,
    *,
    lan_read_only: bool = False,
    lan_auth_credentials: BasicAuthCredentials | None = None,
    lan_no_auth: bool = False,
    public_host: str = "127.0.0.1",
    public_port: int | None = None,
    trusted_container_proxy: bool = False,
    connectivity_monitor: MachineConnectivityMonitor | None = None,
    collection_config_store: CollectionConfigStore | None = None,
    auto_update_scheduler: AutoUpdateScheduler | None = None,
    live_preview_scheduler: LivePreviewScheduler | None = None,
    live_preview_snapshot_provider: Callable[[], dict[str, Any]] | None = None,
    cv_eis_analyzer: CvEisRepositoryAnalyzer | None = None,
    workstation_monitor: WorkstationMonitor | None = None,
):
    if lan_no_auth and not lan_read_only:
        raise ValueError("免登录模式仅可用于 LAN 只读服务。")
    if lan_no_auth and lan_auth_credentials is not None:
        raise ValueError("LAN 只读服务不能同时启用免登录和 Basic 认证。")
    if lan_read_only and not lan_no_auth and lan_auth_credentials is None:
        raise ValueError("LAN 只读服务必须配置 Basic 认证。")
    if not lan_read_only and lan_auth_credentials is not None:
        raise ValueError("Basic 认证凭据仅可用于 LAN 只读服务。")
    config_store = collection_config_store or getattr(
        workspace, "collection_config_provider", None
    )
    if config_store is None:
        base_config = getattr(workspace, "collection_config", None)
        database = getattr(workspace, "database", None)
        database_path = getattr(database, "path", None)
        if (
            base_config
            and database_path
            and callable(getattr(database, "get_collection_config", None))
            and callable(getattr(database, "save_collection_config", None))
        ):
            config_store = CollectionConfigStore(
                base_config,
                database,
            )
    monitor = connectivity_monitor or MachineConnectivityMonitor(
        getattr(workspace, "collection_config", None),
        config_loader=(
            config_store.fixed_config
            if isinstance(config_store, CollectionConfigStore)
            else None
        ),
    )
    auto_update_database = getattr(workspace, "database", None)
    auto_update_getter = getattr(auto_update_database, "get_auto_update_config", None)
    auto_update_saver = (
        auto_update_scheduler.save_config
        if auto_update_scheduler is not None
        else getattr(auto_update_database, "save_auto_update_config", None)
    )
    live_preview_getter = live_preview_snapshot_provider or getattr(
        auto_update_database, "get_live_preview_config", None
    )
    live_preview_saver = (
        live_preview_scheduler.save_config
        if live_preview_scheduler is not None
        else getattr(auto_update_database, "save_live_preview_config", None)
    )
    if cv_eis_analyzer is None:
        database = getattr(workspace, "database", None)
        if callable(getattr(database, "session", None)):
            cv_eis_analyzer = CvEisRepositoryAnalyzer(database)
    workstation_status = workstation_monitor or WorkstationMonitor(None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "StartStopRepository/1.0"
        protocol_version = "HTTP/1.1"

        def _accepts_gzip(self) -> bool:
            for value in self.headers.get_all("Accept-Encoding", []):
                for raw_token in value.split(","):
                    parts = [part.strip().lower() for part in raw_token.split(";")]
                    if not parts or parts[0] != "gzip":
                        continue
                    quality = 1.0
                    for parameter in parts[1:]:
                        if not parameter.startswith("q="):
                            continue
                        try:
                            quality = float(parameter[2:])
                        except ValueError:
                            quality = 0.0
                    if quality > 0.0:
                        return True
            return False

        def _compressible_payload(self, data: bytes) -> tuple[bytes, bool]:
            if len(data) < 1024 or not self._accepts_gzip():
                return data, False
            return gzip.compress(data, compresslevel=1, mtime=0), True

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            if urllib.parse.urlsplit(getattr(self, "path", "")).path == "/healthz":
                return
            super().log_request(code, size)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stdout.write(
                f"[{dt.datetime.now().isoformat(timespec='seconds')}] "
                f"{self.client_address[0]} {fmt % args}\n"
            )

        def _expected_port(self) -> int:
            return int(public_port or self.server.server_address[1])

        def _run_collection_admin(self, operation: Any) -> Any:
            runner = getattr(workspace, "run_collection_admin", None)
            if callable(runner):
                return runner(operation)
            # Test/minimal workspaces still get a deterministic busy gate.
            status = workspace.status()
            job = status.get("job") if isinstance(status, dict) else None
            if isinstance(job, dict) and job.get("status") in {"queued", "running"}:
                raise StartStopWorkspaceError(
                    "启停数据任务运行中，请等待完成后再修改或检查采集配置。",
                    HTTPStatus.CONFLICT,
                )
            return operation()

        def _require_host(self) -> tuple[str, int]:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise StartStopRequestError("请求必须且只能使用一个 Host。", HTTPStatus.FORBIDDEN)
            return validate_request_host(
                host=hosts[0],
                expected_host=public_host,
                expected_port=self._expected_port(),
                lan_read_only=lan_read_only,
            )

        def _require_lan_auth(self) -> bool:
            if not lan_read_only or lan_no_auth:
                return True
            authorization_values = self.headers.get_all("Authorization", [])
            if len(authorization_values) != 1:
                self.send_auth_required()
                return False
            authorization = authorization_values[0]
            if len(authorization.encode("utf-8", errors="ignore")) > MAX_AUTHORIZATION_HEADER_BYTES:
                self.send_auth_required()
                return False
            scheme, separator, token = authorization.partition(" ")
            if not separator or scheme.lower() != "basic" or not token or token != token.strip():
                self.send_auth_required()
                return False
            try:
                decoded = base64.b64decode(token, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                self.send_auth_required()
                return False
            username, credential_separator, password = decoded.partition(":")
            credentials = lan_auth_credentials
            if (
                not credential_separator
                or credentials is None
                or not credentials.matches(username, password)
            ):
                self.send_auth_required()
                return False
            return True

        def _send_common_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")

        def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            data, compressed = self._compressible_payload(raw)
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self._send_common_headers()
            self.end_headers()
            self.wfile.write(data)

        def send_error_json(self, status: int, message: str) -> None:
            self.send_json({"error": message}, status)

        def send_auth_required(self) -> None:
            data = json.dumps(
                {"error": "需要局域网访问认证。"},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "WWW-Authenticate",
                'Basic realm="Start-stop Analysis", charset="UTF-8"',
            )
            self._send_common_headers()
            self.end_headers()
            self.wfile.write(data)

        def send_static(self, relative: str) -> None:
            root = STATIC_ROOT.resolve()
            candidate = (STATIC_ROOT / relative).resolve()
            if candidate != root and root not in candidate.parents:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                return
            if not candidate.is_file():
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                return
            raw = candidate.read_bytes()
            content_type, _ = mimetypes.guess_type(str(candidate))
            media_type = content_type or "application/octet-stream"
            if media_type.startswith("text/") or media_type == "application/javascript":
                media_type += "; charset=utf-8"
            compressible = media_type.startswith("text/") or media_type.startswith(
                ("application/javascript", "application/json", "image/svg+xml")
            )
            data, compressed = (
                self._compressible_payload(raw)
                if compressible
                else (raw, False)
            )
            representation = "gzip" if compressed else "identity"
            etag = '"{}-{}"'.format(
                hashlib.sha256(raw).hexdigest(),
                representation,
            )
            validators = {
                token.strip()
                for value in self.headers.get_all("If-None-Match", [])
                for token in value.split(",")
            }
            if etag in validators or "*" in validators:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.send_header("Vary", "Accept-Encoding")
                self._send_common_headers()
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self._send_common_headers()
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def send_file_download(self, path: Path, filename: str) -> None:
            size = path.stat().st_size
            encoded_name = urllib.parse.quote(filename, safe="")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
            self.send_header("Cache-Control", "no-store")
            self._send_common_headers()
            self.end_headers()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    self.wfile.write(chunk)

        def _require_local_json_request(self) -> None:
            if lan_read_only:
                raise StartStopRequestError(
                    "局域网入口为只读模式，不能执行写操作。",
                    HTTPStatus.FORBIDDEN,
                )
            try:
                peer = ipaddress.ip_address(self.client_address[0])
            except (ValueError, IndexError) as exc:
                raise StartStopRequestError("写请求的客户端地址无效。", HTTPStatus.FORBIDDEN) from exc
            if not peer.is_loopback and not trusted_container_proxy:
                raise StartStopRequestError("写操作只允许来自本机回环地址。", HTTPStatus.FORBIDDEN)
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1:
                raise StartStopRequestError(
                    "写请求必须且只能声明一个 application/json Content-Type。",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
            media_type = content_types[0].split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise StartStopRequestError(
                    "写请求必须使用 application/json。",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
            if self.headers.get_all("Transfer-Encoding", []):
                raise StartStopRequestError("不支持 Transfer-Encoding。", HTTPStatus.BAD_REQUEST)
            request_host, request_port = self._require_host()
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1:
                raise StartStopRequestError("写请求的 Origin 无效。", HTTPStatus.FORBIDDEN)
            validate_local_origin(
                origin=origins[0] if origins else None,
                request_host=request_host,
                request_port=request_port,
            )

        def _require_local_binary_request(self) -> int:
            if lan_read_only:
                raise StartStopRequestError(
                    "局域网入口为只读模式，不能执行写操作。",
                    HTTPStatus.FORBIDDEN,
                )
            try:
                peer = ipaddress.ip_address(self.client_address[0])
            except (ValueError, IndexError) as exc:
                raise StartStopRequestError("写请求的客户端地址无效。", HTTPStatus.FORBIDDEN) from exc
            if not peer.is_loopback and not trusted_container_proxy:
                raise StartStopRequestError("写操作只允许来自本机回环地址。", HTTPStatus.FORBIDDEN)
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1:
                raise StartStopRequestError(
                    "文件上传必须且只能声明一个 application/octet-stream Content-Type。",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
            media_type = content_types[0].strip().lower()
            if media_type != "application/octet-stream":
                raise StartStopRequestError(
                    "文件上传必须使用 application/octet-stream。",
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
            if self.headers.get_all("Transfer-Encoding", []):
                raise StartStopRequestError("不支持 Transfer-Encoding。", HTTPStatus.BAD_REQUEST)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise StartStopRequestError("上传请求必须且只能声明一个 Content-Length。")
            if re.fullmatch(r"[0-9]+", lengths[0]) is None:
                raise StartStopRequestError("Content-Length 无效。")
            length = int(lengths[0])
            if length <= 0:
                raise StartStopRequestError("不能上传空文件。")
            if length > StartStopWorkspace.MAX_UPLOAD_BYTES:
                raise StartStopRequestError(
                    f"单个上传文件不能超过 {StartStopWorkspace.MAX_UPLOAD_BYTES // (1024 * 1024)} MiB。",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
            request_host, request_port = self._require_host()
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1:
                raise StartStopRequestError("写请求的 Origin 无效。", HTTPStatus.FORBIDDEN)
            validate_local_origin(
                origin=origins[0] if origins else None,
                request_host=request_host,
                request_port=request_port,
            )
            return length

        def _receive_upload(
            self,
            *,
            length: int,
            metadata: dict[str, str],
        ) -> dict[str, Any]:
            scratch = Path(workspace.scratch_dir)
            scratch.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".start-stop-upload-",
                suffix=".part",
                dir=str(scratch),
            )
            temporary = Path(temporary_name)
            try:
                remaining = length
                with os.fdopen(descriptor, "wb") as output:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise StartStopRequestError("上传内容在 Content-Length 之前意外结束。")
                        output.write(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                return workspace.upload_file(
                    temporary,
                    filename=metadata["filename"],
                    relative_path=metadata["relative_path"],
                    group=metadata["group"],
                    last_modified=metadata["last_modified"],
                    size_bytes=metadata["size"],
                )
            finally:
                temporary.unlink(missing_ok=True)

        def read_json(self) -> dict[str, Any]:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise StartStopRequestError("请求必须且只能声明一个 Content-Length。")
            try:
                length = int(lengths[0])
            except ValueError as exc:
                raise StartStopRequestError("Content-Length 无效。") from exc
            if length <= 0 or length > MAX_JSON_REQUEST_BYTES:
                raise StartStopRequestError("请求内容为空或过大。", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise StartStopRequestError("请求 JSON 必须使用 UTF-8。") from exc
            if not isinstance(payload, dict):
                raise StartStopRequestError("请求 JSON 顶层必须是对象。")
            return payload

        def do_GET(self) -> None:
            try:
                self._require_host()
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                if path == "/healthz":
                    self.send_json({"ok": True})
                    return
                if not self._require_lan_auth():
                    return
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                if path not in GET_API_PATHS and path not in {
                    "/",
                    "/start-stop",
                    "/start-stop/analysis",
                    "/start-stop/config",
                    "/start-stop/workstations",
                    "/start-stop/cv-eis",
                    "/start-stop/materials",
                }:
                    relative = STATIC_FILES.get(path)
                    icon = ICON_PATH.fullmatch(path)
                    if relative is not None:
                        self.send_static(relative)
                    elif icon is not None:
                        self.send_static(f"icons/{icon.group(1)}")
                    else:
                        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                    return
                if path in {"/", "/start-stop"}:
                    self.send_static("start-stop-config.html")
                elif path == "/start-stop/analysis":
                    self.send_static("start-stop.html")
                elif path == "/start-stop/config":
                    self.send_static("start-stop-config.html")
                elif path == "/start-stop/workstations":
                    self.send_static("start-stop-workstations.html")
                elif path == "/start-stop/cv-eis":
                    self.send_static("start-stop-cv-eis.html")
                elif path == "/start-stop/materials":
                    self.send_static("start-stop-materials.html")
                elif path == "/api/start-stop/status":
                    status_payload = public_status(
                        workspace.status(), lan_read_only=lan_read_only
                    )
                    status_payload["connectivity"] = monitor.snapshot(
                        can_check=not lan_read_only
                    )
                    self.send_json(status_payload)
                elif path == "/api/start-stop/connectivity":
                    self.send_json(monitor.snapshot(can_check=not lan_read_only))
                elif path == "/api/start-stop/workstations":
                    if query:
                        raise StartStopRequestError(
                            "工作站监控接口不接受查询参数。"
                        )
                    self.send_json(workstation_status.snapshot())
                elif path == "/api/start-stop/auto-update":
                    if lan_read_only:
                        raise StartStopRequestError(
                            "自动更新设置仅可在服务器本机查看和修改。",
                            HTTPStatus.FORBIDDEN,
                        )
                    if not callable(auto_update_getter):
                        raise StartStopRequestError(
                            "自动更新设置当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    payload = (
                        auto_update_scheduler.snapshot()
                        if auto_update_scheduler is not None
                        else auto_update_getter()
                    )
                    self.send_json({**payload, "can_edit": True})
                elif path == "/api/start-stop/live-preview":
                    if not callable(live_preview_getter):
                        raise StartStopRequestError(
                            "实时数据预览当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    payload = (
                        live_preview_scheduler.snapshot()
                        if live_preview_scheduler is not None
                        else live_preview_getter()
                    )
                    self.send_json(
                        {
                            **payload,
                            "can_edit": not lan_read_only,
                        }
                    )
                elif path == "/api/start-stop/collection-config":
                    if lan_read_only:
                        raise StartStopRequestError(
                            "搜索位置配置仅可在服务器本机查看和修改。",
                            HTTPStatus.FORBIDDEN,
                        )
                    if not isinstance(config_store, CollectionConfigStore):
                        raise StartStopRequestError(
                            "实验电脑搜索位置配置当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    try:
                        self.send_json(
                            config_store.snapshot(
                                include_paths=True,
                                can_edit=True,
                            )
                        )
                    except CollectionConfigError as exc:
                        raise StartStopRequestError(
                            str(exc), HTTPStatus.SERVICE_UNAVAILABLE
                        ) from exc
                elif path == "/api/start-stop/materials":
                    self.send_json(workspace.materials())
                elif path == "/api/start-stop/series":
                    self.send_json(workspace.series())
                elif path == "/api/start-stop/cv-eis":
                    if query:
                        raise StartStopRequestError("CV/EIS 列表接口不接受查询参数。")
                    if cv_eis_analyzer is None:
                        raise StartStopRequestError(
                            "CV/EIS 数据库分析当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    self.send_json(cv_eis_analyzer.catalog())
                elif path == "/api/start-stop/cv-eis/curve":
                    if set(query) != {"analysis_id"} or len(query["analysis_id"]) != 1:
                        raise StartStopRequestError(
                            "CV/EIS 曲线接口必须提供一个 analysis_id。"
                        )
                    if cv_eis_analyzer is None:
                        raise StartStopRequestError(
                            "CV/EIS 数据库分析当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    self.send_json(cv_eis_analyzer.curve(query["analysis_id"][0]))
                elif path == "/api/start-stop/chart":
                    series_ids = [
                        item
                        for value in query.get("series", [])
                        for item in value.split(",")
                        if item
                    ]
                    self.send_json(
                        workspace.chart_data(
                            series_ids=series_ids,
                            metric=query.get("metric", ["cathodic"])[0],
                            x_axis=query.get("x", ["cycle"])[0],
                            mode=query.get("mode", ["raw"])[0],
                            max_points=int(query.get("max_points", ["4000"])[0]),
                        )
                    )
                elif path == "/api/start-stop/pdf":
                    pdf_path, filename = workspace.pdf(query.get("kind", ["standard"])[0])
                    self.send_file_download(pdf_path, filename)
            except StartStopRequestError as exc:
                self.send_error_json(exc.status, str(exc))
            except StartStopWorkspaceError as exc:
                self.send_error_json(exc.status, str(exc))
            except CvEisAnalysisError as exc:
                self.send_error_json(exc.status, str(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception:
                traceback.print_exc()
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误。")

        def do_POST(self) -> None:
            try:
                self._require_host()
                if not self._require_lan_auth():
                    return
                if lan_read_only:
                    raise StartStopRequestError(
                        "局域网入口为只读模式，不能执行写操作。",
                        HTTPStatus.FORBIDDEN,
                    )
                parsed_request = urllib.parse.urlsplit(self.path)
                path = parsed_request.path
                if path not in POST_API_PATHS:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                    return
                if path == "/api/start-stop/uploads":
                    metadata = _parse_upload_query(parsed_request.query)
                    length = self._require_local_binary_request()
                    validated = StartStopWorkspace.validate_upload_metadata(
                        filename=metadata["filename"],
                        relative_path=metadata["relative_path"],
                        group=metadata["group"],
                        last_modified=metadata["last_modified"],
                        size_bytes=metadata["size"],
                    )
                    if validated["size_bytes"] != length:
                        raise StartStopRequestError(
                            "size 必须与 Content-Length 完全一致。"
                        )
                    self.send_json(
                        self._receive_upload(length=length, metadata=metadata),
                        HTTPStatus.CREATED,
                    )
                    return
                self._require_local_json_request()
                payload = self.read_json()
                if path == "/api/start-stop/connectivity/check":
                    unknown = sorted(set(payload) - {"machine_id"})
                    if unknown:
                        raise StartStopRequestError(
                            "连通性检查请求包含未知字段：" + ", ".join(unknown)
                        )
                    machine_id = payload.get("machine_id")
                    if (
                        not isinstance(machine_id, str)
                        or not machine_id.strip()
                        or len(machine_id.encode("utf-8")) > 256
                    ):
                        raise StartStopRequestError("machine_id 必须是有效的实验机 ID。")
                    try:
                        self.send_json(
                            self._run_collection_admin(
                                lambda: monitor.check(machine_id.strip())
                            )
                        )
                    except KeyError as exc:
                        raise StartStopRequestError(
                            "未找到指定的实验机。", HTTPStatus.NOT_FOUND
                        ) from exc
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        raise StartStopRequestError(
                            "实验机采集配置当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        ) from exc
                    except RuntimeError as exc:
                        raise StartStopRequestError(str(exc), HTTPStatus.CONFLICT) from exc
                elif path == "/api/start-stop/connectivity/path-check":
                    unknown = sorted(set(payload) - {"machine_id", "path"})
                    if unknown:
                        raise StartStopRequestError(
                            "搜索位置检查请求包含未知字段："
                            + ", ".join(unknown)
                        )
                    if set(payload) != {"machine_id", "path"}:
                        raise StartStopRequestError(
                            "搜索位置检查必须提供 machine_id 和 path。"
                        )
                    machine_id = payload.get("machine_id")
                    if (
                        not isinstance(machine_id, str)
                        or not machine_id.strip()
                        or len(machine_id.encode("utf-8")) > 256
                    ):
                        raise StartStopRequestError("machine_id 必须是有效的实验机 ID。")
                    try:
                        remote_path = normalize_windows_directory(payload.get("path"))
                        self.send_json(
                            self._run_collection_admin(
                                lambda: monitor.check_path(
                                    machine_id.strip(), remote_path
                                )
                            )
                        )
                    except KeyError as exc:
                        raise StartStopRequestError(
                            "未找到指定的实验机。", HTTPStatus.NOT_FOUND
                        ) from exc
                    except CollectionConfigError as exc:
                        raise StartStopRequestError(str(exc)) from exc
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        raise StartStopRequestError(
                            "实验机采集配置当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        ) from exc
                    except RuntimeError as exc:
                        raise StartStopRequestError(
                            str(exc), HTTPStatus.CONFLICT
                        ) from exc
                elif path == "/api/start-stop/materials":
                    unknown = sorted(set(payload) - {"dataset_fingerprint", "expected_revision", "materials"})
                    if unknown:
                        raise StartStopRequestError("材料配置请求包含未知字段：" + ", ".join(unknown))
                    self.send_json(
                        workspace.save_materials(
                            dataset_fingerprint=str(payload.get("dataset_fingerprint") or ""),
                            expected_revision=int(payload.get("expected_revision", -1)),
                            materials=payload.get("materials"),
                        )
                    )
                else:
                    unknown = sorted(
                        set(payload)
                        - {
                            "action",
                            "upload_ids",
                            "render_data_mode",
                            "render_material_scope",
                            "export_pdf",
                        }
                    )
                    if unknown:
                        raise StartStopRequestError("启停任务请求包含未知字段：" + ", ".join(unknown))
                    action = str(payload.get("action") or "")
                    upload_ids = payload.get("upload_ids")
                    render_data_mode = str(
                        payload.get("render_data_mode") or "both"
                    )
                    render_material_scope = str(
                        payload.get("render_material_scope") or "all"
                    )
                    export_pdf = payload.get("export_pdf", False)
                    if not isinstance(export_pdf, bool):
                        raise StartStopRequestError(
                            "export_pdf 必须是 JSON 布尔值。"
                        )
                    if action == "prepare_upload":
                        if not isinstance(upload_ids, list):
                            raise StartStopRequestError(
                                "prepare_upload 的 upload_ids 必须是 JSON 数组。"
                            )
                    elif "upload_ids" in payload:
                        raise StartStopRequestError(
                            "仅 prepare_upload 任务接受 upload_ids。"
                        )
                    if action != "render" and (
                        "render_data_mode" in payload
                        or "render_material_scope" in payload
                        or "export_pdf" in payload
                    ):
                        raise StartStopRequestError(
                            "绘图数据与范围选项仅可用于 render 任务。"
                        )
                    job_starter = (
                        auto_update_scheduler.start_manual_job
                        if auto_update_scheduler is not None
                        else workspace.start_job
                    )
                    if action == "prepare_upload":
                        job = job_starter(action, upload_ids=upload_ids)
                    elif action == "render":
                        render_arguments = {
                            "render_data_mode": render_data_mode,
                            "render_material_scope": render_material_scope,
                        }
                        if export_pdf:
                            render_arguments["export_pdf"] = True
                        job = job_starter(action, **render_arguments)
                    else:
                        job = job_starter(action)
                    self.send_json(job, HTTPStatus.ACCEPTED)
            except StartStopRequestError as exc:
                self.send_error_json(exc.status, str(exc))
            except StartStopWorkspaceError as exc:
                self.send_error_json(exc.status, str(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception:
                traceback.print_exc()
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误。")

        def do_PUT(self) -> None:
            try:
                self._require_host()
                if not self._require_lan_auth():
                    return
                # This gate intentionally runs before Content-Length, content
                # type or body parsing so the LAN read-only service never
                # consumes a write request body.
                if lan_read_only:
                    raise StartStopRequestError(
                        "局域网入口为只读模式，不能执行写操作。",
                        HTTPStatus.FORBIDDEN,
                    )
                path = urllib.parse.urlsplit(self.path).path
                if path not in PUT_API_PATHS:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
                    return
                if path == "/api/start-stop/auto-update":
                    if not callable(auto_update_saver):
                        raise StartStopRequestError(
                            "自动更新设置当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    self._require_local_json_request()
                    payload = self.read_json()
                    unknown = sorted(
                        set(payload)
                        - {"expected_revision", "enabled", "interval_minutes"}
                    )
                    if unknown:
                        raise StartStopRequestError(
                            "自动更新设置请求包含未知字段：" + ", ".join(unknown)
                        )
                    if set(payload) != {
                        "expected_revision",
                        "enabled",
                        "interval_minutes",
                    }:
                        raise StartStopRequestError(
                            "自动更新设置必须提供 expected_revision、enabled 和 interval_minutes。"
                        )
                    try:
                        saved = auto_update_saver(
                            expected_revision=payload.get("expected_revision"),
                            enabled=payload.get("enabled"),
                            interval_minutes=payload.get("interval_minutes"),
                        )
                    except AutoUpdateConfigConflict as exc:
                        raise StartStopRequestError(
                            str(exc), HTTPStatus.CONFLICT
                        ) from exc
                    self.send_json({**saved, "can_edit": True})
                    return
                if path == "/api/start-stop/live-preview":
                    if not callable(live_preview_saver):
                        raise StartStopRequestError(
                            "实时数据预览设置当前不可用。",
                            HTTPStatus.SERVICE_UNAVAILABLE,
                        )
                    self._require_local_json_request()
                    payload = self.read_json()
                    unknown = sorted(
                        set(payload) - {"expected_revision", "enabled"}
                    )
                    if unknown:
                        raise StartStopRequestError(
                            "实时数据预览设置请求包含未知字段："
                            + ", ".join(unknown)
                        )
                    if set(payload) != {"expected_revision", "enabled"}:
                        raise StartStopRequestError(
                            "实时数据预览设置必须提供 expected_revision 和 enabled。"
                        )
                    try:
                        saved = live_preview_saver(
                            expected_revision=payload.get("expected_revision"),
                            enabled=payload.get("enabled"),
                        )
                    except AutoUpdateConfigConflict as exc:
                        raise StartStopRequestError(
                            str(exc), HTTPStatus.CONFLICT
                        ) from exc
                    self.send_json({**saved, "can_edit": True})
                    return
                if not isinstance(config_store, CollectionConfigStore):
                    raise StartStopRequestError(
                        "实验电脑搜索位置配置当前不可用。",
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                self._require_local_json_request()
                payload = self.read_json()
                unknown = sorted(set(payload) - {"expected_revision", "machines"})
                if unknown:
                    raise StartStopRequestError(
                        "搜索位置配置请求包含未知字段："
                        + ", ".join(unknown)
                    )
                if set(payload) != {"expected_revision", "machines"}:
                    raise StartStopRequestError(
                        "搜索位置配置必须提供 expected_revision 和 machines。"
                    )
                try:
                    saved = self._run_collection_admin(
                        lambda: config_store.save(
                            expected_revision=payload.get("expected_revision"),
                            machines=payload.get("machines"),
                        )
                    )
                except CollectionConfigConflict as exc:
                    raise StartStopRequestError(
                        str(exc), HTTPStatus.CONFLICT
                    ) from exc
                except CollectionConfigError as exc:
                    raise StartStopRequestError(str(exc)) from exc
                self.send_json(saved)
            except StartStopRequestError as exc:
                self.send_error_json(exc.status, str(exc))
            except StartStopWorkspaceError as exc:
                self.send_error_json(exc.status, str(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception:
                traceback.print_exc()
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误。")

        def _reject_write_method(self) -> None:
            try:
                self._require_host()
                if not self._require_lan_auth():
                    return
                if lan_read_only:
                    self.send_error_json(HTTPStatus.FORBIDDEN, "局域网入口为只读模式，不能执行写操作。")
                else:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            except StartStopRequestError as exc:
                self.send_error_json(exc.status, str(exc))

        do_PATCH = _reject_write_method
        do_DELETE = _reject_write_method

        def _reject_other_method(self) -> None:
            try:
                self._require_host()
                if not self._require_lan_auth():
                    return
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            except StartStopRequestError as exc:
                self.send_error_json(exc.status, str(exc))

        do_HEAD = _reject_other_method
        do_OPTIONS = _reject_other_method
        do_TRACE = _reject_other_method

    return Handler


def build_workspace(
    args: argparse.Namespace,
    *,
    database_type: type[StartStopDatabase] = StartStopDatabase,
    workspace_type: type[StartStopWorkspace] = StartStopWorkspace,
) -> StartStopWorkspace:
    database_path = Path(args.database).expanduser().resolve()
    analysis_dir = Path(args.analysis_dir).expanduser().resolve()
    scratch_dir = Path(args.scratch_dir).expanduser().resolve()
    analysis_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    database = database_type(database_path)
    analysis_script = str(args.analysis_script or "").strip()
    collection_config = str(args.collection_config or "").strip()
    collection_config_path = (
        Path(collection_config).expanduser().resolve() if collection_config else None
    )
    raw_backup_dir = (
        ""
        if bool(args.lan_read_only)
        else str(getattr(args, "backup_dir", "") or "").strip()
    )
    backup_dir = (
        Path(raw_backup_dir).expanduser().resolve() if raw_backup_dir else None
    )
    estimated_output_bytes = (
        0
        if bool(args.lan_read_only)
        else int(getattr(args, "estimated_output_bytes", 0) or 0)
    )
    collection_config_store = (
        CollectionConfigStore(collection_config_path, database)
        if collection_config_path is not None
        and collection_config_path.is_file()
        and callable(getattr(database, "get_collection_config", None))
        and callable(getattr(database, "save_collection_config", None))
        else None
    )
    workspace = workspace_type(
        database,
        analysis_dir,
        analysis_script=(
            Path(analysis_script).expanduser().resolve()
            if analysis_script
            else None
        ),
        collection_config=collection_config_path,
        collection_config_provider=collection_config_store,
        scratch_dir=scratch_dir,
        backup_dir=backup_dir,
        estimated_output_bytes=estimated_output_bytes,
        python_executable=sys.executable,
        repository_mode=True,
    )
    if not args.lan_read_only:
        workspace.ensure_published_cache()
    return workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="数据库化启停数据下载与分析服务")
    parser.add_argument("--database", required=True, help="独立启停 SQLite 数据库")
    parser.add_argument(
        "--cv-eis-database",
        default="",
        help="CV/EIS 原始文件数据库的只读路径（LAN 服务可单独挂载）",
    )
    parser.add_argument("--analysis-dir", required=True, help="当前分析产物缓存目录")
    parser.add_argument("--analysis-script", default="", help="固定启停分析脚本")
    parser.add_argument("--collection-config", default="", help="固定实验电脑采集配置")
    parser.add_argument(
        "--workstation-state-file",
        default="",
        help="三台实验机工作站只读监控共享缓存",
    )
    parser.add_argument(
        "--workstation-poll-seconds",
        type=int,
        default=WORKSTATION_POLL_SECONDS,
        help="工作站轻量只读检查周期（秒）",
    )
    parser.add_argument(
        "--workstation-discovery-seconds",
        type=int,
        default=WORKSTATION_DISCOVERY_SECONDS,
        help="工作站文件夹重新发现周期（秒）",
    )
    parser.add_argument("--scratch-dir", required=True, help="任务临时工作目录")
    parser.add_argument(
        "--backup-dir",
        default="",
        help="只读备份状态目录（仅本机服务）",
    )
    parser.add_argument(
        "--estimated-output-bytes",
        type=int,
        default=0,
        help="下一次启停任务预计输出字节数",
    )
    parser.add_argument("--bind", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8787, help="容器内监听端口")
    parser.add_argument("--public-host", default="", help="浏览器实际使用的公开主机")
    parser.add_argument("--public-port", type=int, default=0, help="浏览器实际使用的公开端口")
    parser.add_argument("--lan-read-only", action="store_true", help="启用局域网只读入口")
    parser.add_argument(
        "--lan-no-auth",
        action="store_true",
        help="显式取消 LAN 只读入口登录（写操作仍保持禁止）",
    )
    parser.add_argument(
        "--lan-auth-file",
        default="",
        help="LAN 只读入口的 0600 username:password 凭据文件",
    )
    parser.add_argument("--container-mode", action="store_true", help="显式启用容器代理模式")
    return parser


def _validated_launch(args: argparse.Namespace) -> tuple[str, str, int]:
    if not 1 <= int(args.port) <= 65535:
        raise ValueError("监听端口必须位于 1 到 65535。")
    if args.container_mode and os.environ.get("ECHEM_CONTAINER_MODE") != "1":
        raise ValueError("--container-mode 只能在明确的容器环境中启用。")
    lan_auth_file = str(getattr(args, "lan_auth_file", "") or "").strip()
    lan_no_auth = bool(getattr(args, "lan_no_auth", False))
    if lan_no_auth and not bool(args.lan_read_only):
        raise ValueError("--lan-no-auth 仅可与 --lan-read-only 同时使用。")
    if lan_no_auth and lan_auth_file:
        raise ValueError("--lan-no-auth 不能与 --lan-auth-file 同时使用。")
    if bool(args.lan_read_only) and not lan_no_auth and not lan_auth_file:
        raise ValueError("LAN 只读服务必须声明 --lan-auth-file。")
    if not bool(args.lan_read_only) and lan_auth_file:
        raise ValueError("--lan-auth-file 仅可与 --lan-read-only 同时使用。")
    cv_eis_database = str(getattr(args, "cv_eis_database", "") or "").strip()
    if cv_eis_database and not bool(args.lan_read_only):
        raise ValueError("--cv-eis-database 仅用于 LAN 只读服务。")
    bind = validate_bind(
        args.bind,
        lan_read_only=bool(args.lan_read_only),
        container_mode=bool(args.container_mode),
    )
    if args.container_mode and bind != "0.0.0.0":
        raise ValueError("容器模式必须在容器内部监听 0.0.0.0。")
    if not args.container_mode and (args.public_host or args.public_port):
        raise ValueError("--public-host/--public-port 仅用于容器端口映射。")
    if args.container_mode:
        if not args.public_host:
            raise ValueError("容器模式必须声明浏览器实际使用的 --public-host。")
        public_host, public_port = validate_public_endpoint(
            public_host=args.public_host,
            public_port=int(args.public_port or args.port),
            lan_read_only=bool(args.lan_read_only),
        )
    else:
        public_host = bind
        public_port = int(args.port)
    return bind, public_host, public_port


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    database_instance_lock: DatabaseInstanceLock | None = None
    auto_update_scheduler: AutoUpdateScheduler | None = None
    live_preview_scheduler: LivePreviewScheduler | None = None
    live_preview_state_file: LivePreviewStateFile | None = None
    workstation_monitor: WorkstationMonitor | None = None
    server: StartStopHTTPServer | None = None
    try:
        try:
            bind, public_host, public_port = _validated_launch(args)
            lan_auth_credentials = (
                load_lan_auth_file(args.lan_auth_file)
                if args.lan_read_only and not args.lan_no_auth
                else None
            )
            if not args.lan_read_only:
                database_instance_lock = DatabaseInstanceLock(args.database)
                database_instance_lock.acquire()
            workspace = build_workspace(args)
            if not args.lan_read_only:
                recover = getattr(workspace, "recover_after_restart", None)
                if callable(recover):
                    recover()
            auto_update_scheduler = (
                None
                if args.lan_read_only
                else AutoUpdateScheduler(workspace, workspace.database)
            )
            cv_eis_analyzer = None
            cv_eis_database_path = str(
                getattr(args, "cv_eis_database", "") or ""
            ).strip()
            if cv_eis_database_path:
                cv_eis_analyzer = CvEisRepositoryAnalyzer(
                    ReadOnlyCvEisDatabase(cv_eis_database_path)
                )
            workstation_state_file = str(
                getattr(args, "workstation_state_file", "") or ""
            ).strip()
            if not workstation_state_file:
                workstation_state_file = str(
                    Path(args.analysis_dir).expanduser().resolve().parent
                    / "workstation-monitor.json"
                )
            collection_provider = getattr(
                workspace, "collection_config_provider", None
            )
            workstation_probe_enabled = bool(
                not args.lan_read_only
                and isinstance(collection_provider, CollectionConfigStore)
            )
            workstation_monitor = WorkstationMonitor(
                workstation_state_file,
                active_probe=workstation_probe_enabled,
                config_provider=(
                    collection_provider.effective_config
                    if workstation_probe_enabled
                    else None
                ),
                material_provider=(
                    workspace.materials if workstation_probe_enabled else None
                ),
                poll_seconds=int(args.workstation_poll_seconds),
                discovery_seconds=int(args.workstation_discovery_seconds),
            )
            live_preview_state_file = LivePreviewStateFile(
                Path(workstation_state_file).with_name("live-preview.json")
            )
            live_preview_scheduler = (
                LivePreviewScheduler(
                    workspace.database,
                    workstation_monitor,
                    collection_provider.effective_config,
                    workspace.scratch_dir,
                    state_file=live_preview_state_file,
                    formal_context_provider=getattr(
                        workspace, "live_analysis_context", None
                    ),
                )
                if workstation_probe_enabled
                and isinstance(collection_provider, CollectionConfigStore)
                else None
            )
            handler = create_handler(
                workspace,
                lan_read_only=bool(args.lan_read_only),
                lan_auth_credentials=lan_auth_credentials,
                lan_no_auth=bool(args.lan_no_auth),
                public_host=public_host,
                public_port=public_port,
                trusted_container_proxy=bool(
                    args.container_mode and not args.lan_read_only
                ),
                auto_update_scheduler=auto_update_scheduler,
                live_preview_scheduler=live_preview_scheduler,
                live_preview_snapshot_provider=(
                    live_preview_state_file.snapshot
                    if args.lan_read_only and live_preview_state_file is not None
                    else None
                ),
                cv_eis_analyzer=cv_eis_analyzer,
                workstation_monitor=workstation_monitor,
            )
            server = StartStopHTTPServer((bind, int(args.port)), handler)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        mode = "LAN read-only" if args.lan_read_only else "local read-write"
        print(
            f"Start-stop repository ({mode}) listening on "
            f"http://{bind}:{args.port}; public http://{public_host}:{public_port}/start-stop",
            flush=True,
        )
        if workstation_monitor is not None:
            workstation_monitor.start()
        if live_preview_scheduler is not None:
            live_preview_scheduler.start()
        if auto_update_scheduler is not None:
            auto_update_scheduler.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if auto_update_scheduler is not None:
                auto_update_scheduler.close()
        finally:
            try:
                if live_preview_scheduler is not None:
                    live_preview_scheduler.close()
            finally:
                try:
                    if workstation_monitor is not None:
                        workstation_monitor.close()
                finally:
                    try:
                        if server is not None:
                            server.server_close()
                    finally:
                        if database_instance_lock is not None:
                            database_instance_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
