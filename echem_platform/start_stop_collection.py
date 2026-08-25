#!/usr/bin/env python3
"""Incrementally collect Windows electrochemistry files into SQLite.

The remote side is strictly read-only.  Files are streamed through one scratch
file at a time, revalidated against their remote size and modification time,
and handed to :class:`StartStopDatabase` for atomic publication.  The scratch
directory is never a source of truth and is removed after every run.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, TypeVar


UTC = dt.timezone.utc
BASE64_LINE = re.compile(rb"^[A-Za-z0-9+/]+={0,2}$")
STREAM_VERIFICATION_PREFIX = b"START_STOP_STREAM_VERIFICATION:"
LIVE_PREVIEW_STREAM_PREFIX = b"START_STOP_LIVE_PREVIEW_STREAM:"
DEFAULT_SETTLE_SECONDS = 5 * 60
DEFAULT_CONNECTIVITY_TIMEOUT_SECONDS = 12
RESERVED_FREE_BYTES = 1024 * 1024 * 1024
FORBIDDEN_EXTENSIONS = {".exp"}
T = TypeVar("T")


class CollectionError(RuntimeError):
    """Base class for stable, operator-safe collection failures.

    ``str(error)`` is intentionally suitable for a job/result payload.  Local
    paths, SSH key locations and remote stderr must only be attached as an
    exception cause or written to private diagnostics; they never belong in
    this public message.
    """

    failure_class = "fatal"
    code = "collection_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.public_message = message
        if code is not None:
            self.code = code


class FatalCollectionError(CollectionError):
    """A local/configuration/program failure that must abort the whole run."""


class CollectionConfigurationError(FatalCollectionError):
    """The collection configuration is invalid or incomplete."""

    code = "collection_config_invalid"


class CredentialConfigurationError(FatalCollectionError):
    """Required local SSH credentials or trust configuration are unavailable."""

    code = "ssh_credentials_invalid"


class LocalFilesystemError(FatalCollectionError):
    """A local scratch/output filesystem operation failed."""

    code = "local_filesystem_error"


class LocalStorageError(LocalFilesystemError):
    """There is not enough verified local capacity for the next file."""

    code = "local_storage_insufficient"


class LocalDatabaseError(FatalCollectionError):
    """The local SQLite repository could not complete an operation."""

    code = "local_database_error"


class LocalProgramError(FatalCollectionError):
    """A required local program could not be started."""

    code = "local_program_error"


class UnexpectedCollectionError(FatalCollectionError):
    """An unclassified implementation failure; never safe to downgrade."""

    code = "unexpected_internal_error"


class PartialCollectionError(CollectionError):
    """A failure isolated to one remote root or file."""

    failure_class = "partial"
    code = "remote_read_failed"


class RemoteRootError(PartialCollectionError):
    """One configured remote data root could not be inventoried."""

    code = "remote_root_failed"


class RemoteFileError(PartialCollectionError):
    """One remote file could not be stat'ed or downloaded."""

    code = "remote_file_failed"


class RemoteProtocolError(PartialCollectionError):
    """The remote host returned an invalid structured response."""

    code = "remote_response_invalid"


class SourceChangedError(PartialCollectionError):
    """The source changed between validation and publication."""

    code = "source_changed"


@dataclass(frozen=True)
class PlannedFile:
    machine: dict[str, Any]
    root: dict[str, Any]
    remote_path: str
    relative_path: str
    size: int
    last_write_ticks: int
    last_write_utc: str


class RemoteTransport(Protocol):
    def inventory_root(
        self,
        machine: dict[str, Any],
        root: dict[str, Any],
        extensions: list[str],
    ) -> dict[str, Any]: ...

    def stat_file(
        self,
        machine: dict[str, Any],
        remote_path: str,
    ) -> dict[str, Any]: ...

    def fetch_file(
        self,
        machine: dict[str, Any],
        remote_path: str,
        destination: Path,
        expected_size: int,
    ) -> None: ...


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_utc(value: dt.datetime | None = None) -> str:
    return (value or now_utc()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> dt.datetime:
    normalized = value.strip().replace("Z", "+00:00")
    # .NET round-trip timestamps may contain seven fractional digits.
    normalized = re.sub(r"(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)", r"\1", normalized)
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def encode_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def decode_base64_payload(stdout: bytes) -> Any:
    candidates = [
        line.strip()
        for line in stdout.splitlines()
        if len(line.strip()) >= 4 and BASE64_LINE.fullmatch(line.strip())
    ]
    if not candidates:
        raise RemoteProtocolError("远端没有返回可解析的数据")
    try:
        payload = base64.b64decode(candidates[-1], validate=True)
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteProtocolError("远端返回的数据格式无效") from exc


def _encoded_remote_path(remote_path: str) -> str:
    return base64.b64encode(remote_path.encode("utf-8")).decode("ascii")


def inventory_script(
    remote_root: str,
    extensions: Iterable[str],
    exclude_directories: Iterable[str],
    recursive: bool,
) -> str:
    extensions_ps = ",".join(ps_literal(item.casefold()) for item in extensions)
    excludes_ps = ",".join(ps_literal(item) for item in exclude_directories)
    return f"""
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$root = {ps_literal(remote_root)}
$extensions = @({extensions_ps})
$excludedDirectories = @({excludes_ps})
$recursive = {"$true" if recursive else "$false"}
$scanTime = [DateTime]::UtcNow
$records = New-Object System.Collections.Generic.List[object]

if (Test-Path -LiteralPath $root -PathType Container) {{
    $rootNormalized = $root.TrimEnd([char]92)
    $prefix = $rootNormalized + [char]92
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($root)
    while ($pending.Count -gt 0) {{
        $current = $pending.Pop()
        $files = Get-ChildItem -LiteralPath $current -File -Force -ErrorAction SilentlyContinue
        foreach ($file in $files) {{
            if ($extensions -notcontains $file.Extension.ToLowerInvariant()) {{ continue }}
            if (-not $file.FullName.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {{ continue }}
            $relative = $file.FullName.Substring($prefix.Length)
            [void]$records.Add([pscustomobject][ordered]@{{
                path = $file.FullName
                relative = $relative
                size = [Int64]$file.Length
                last_write_ticks = [Int64]$file.LastWriteTimeUtc.Ticks
                last_write_utc = $file.LastWriteTimeUtc.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
            }})
        }}
        if (-not $recursive) {{ continue }}
        $directories = Get-ChildItem -LiteralPath $current -Directory -Force -ErrorAction SilentlyContinue
        foreach ($directory in $directories) {{
            if ($excludedDirectories -contains $directory.Name) {{ continue }}
            if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {{ continue }}
            if (-not $directory.FullName.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {{ continue }}
            $pending.Push($directory.FullName)
        }}
    }}
    $payload = [pscustomobject][ordered]@{{
        exists = $true
        root = $root
        scanned_at_utc = $scanTime.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        files = $records.ToArray()
    }}
}} else {{
    $payload = [pscustomobject][ordered]@{{
        exists = $false
        root = $root
        scanned_at_utc = $scanTime.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        files = @()
    }}
}}
$json = $payload | ConvertTo-Json -Compress -Depth 6
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
"""


def stat_file_script(remote_path: str) -> str:
    encoded_path = _encoded_remote_path(remote_path)
    return f"""
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$scanTime = [DateTime]::UtcNow
if (Test-Path -LiteralPath $path -PathType Leaf) {{
    $file = Get-Item -LiteralPath $path -Force
    $payload = [pscustomobject][ordered]@{{
        exists = $true
        path = $file.FullName
        size = [Int64]$file.Length
        last_write_ticks = [Int64]$file.LastWriteTimeUtc.Ticks
        last_write_utc = $file.LastWriteTimeUtc.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        scanned_at_utc = $scanTime.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
    }}
}} else {{
    $payload = [pscustomobject][ordered]@{{
        exists = $false
        path = $path
        scanned_at_utc = $scanTime.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
    }}
}}
$json = $payload | ConvertTo-Json -Compress -Depth 4
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
"""


def stream_file_script(remote_path: str) -> str:
    encoded_path = _encoded_remote_path(remote_path)
    return f"""
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$source = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
$output = [Console]::OpenStandardOutput()
try {{
    $buffer = New-Object byte[] 1048576
    while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {{
        $output.Write($buffer, 0, $read)
    }}
    $output.Flush()
}} finally {{
    $source.Dispose()
    $output.Dispose()
}}
"""


def verified_stream_file_script(remote_path: str) -> str:
    """Stream one file and report its before/after metadata on stderr.

    stdout remains byte-for-byte file content. The fixed marker on stderr lets
    the caller ignore unrelated OpenSSH diagnostics while retaining the same
    source-change protection that previously required two extra SSH sessions.
    """
    encoded_path = _encoded_remote_path(remote_path)
    marker = STREAM_VERIFICATION_PREFIX.decode("ascii")
    return f"""
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$before = Get-Item -LiteralPath $path -Force
$beforeSize = [Int64]$before.Length
$beforeTicks = [Int64]$before.LastWriteTimeUtc.Ticks
$source = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
$output = [Console]::OpenStandardOutput()
try {{
    $buffer = New-Object byte[] 1048576
    while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {{
        $output.Write($buffer, 0, $read)
    }}
    $output.Flush()
}} finally {{
    $source.Dispose()
}}
$after = Get-Item -LiteralPath $path -Force
$payload = [pscustomobject][ordered]@{{
    before_size = $beforeSize
    before_ticks = $beforeTicks
    after_size = [Int64]$after.Length
    after_ticks = [Int64]$after.LastWriteTimeUtc.Ticks
}}
$json = $payload | ConvertTo-Json -Compress -Depth 2
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
[Console]::Error.WriteLine('{marker}' + $encoded)
"""


def live_preview_stream_file_script(remote_path: str) -> str:
    """Stream exactly the bytes present when an active file is opened.

    The writer may continue appending while the snapshot is read.  A fixed
    ``remaining`` counter prevents an active acquisition from turning the SSH
    stream into an unbounded download.  The source is opened with shared read
    access and is never modified or locked against the instrument.
    """
    encoded_path = _encoded_remote_path(remote_path)
    marker = LIVE_PREVIEW_STREAM_PREFIX.decode("ascii")
    return f"""
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$before = Get-Item -LiteralPath $path -Force
$beforeSize = [Int64]$before.Length
$beforeTicks = [Int64]$before.LastWriteTimeUtc.Ticks
$share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
$source = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
$output = [Console]::OpenStandardOutput()
$written = [Int64]0
try {{
    $buffer = New-Object byte[] 1048576
    $remaining = $beforeSize
    while ($remaining -gt 0) {{
        $requested = [int][Math]::Min([Int64]$buffer.Length, $remaining)
        $read = $source.Read($buffer, 0, $requested)
        if ($read -le 0) {{ break }}
        $output.Write($buffer, 0, $read)
        $written += $read
        $remaining -= $read
    }}
    $output.Flush()
}} finally {{
    $source.Dispose()
}}
$after = Get-Item -LiteralPath $path -Force
$payload = [pscustomobject][ordered]@{{
    before_size = $beforeSize
    before_ticks = $beforeTicks
    after_size = [Int64]$after.Length
    after_ticks = [Int64]$after.LastWriteTimeUtc.Ticks
    streamed_size = $written
    source_modified_utc = $before.LastWriteTimeUtc.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
}}
$json = $payload | ConvertTo-Json -Compress -Depth 2
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
[Console]::Error.WriteLine('{marker}' + $encoded)
"""


def decode_stream_verification(stderr: bytes) -> dict[str, Any]:
    """Read the final fixed verification record from mixed SSH stderr."""
    candidates = [
        line.strip()[len(STREAM_VERIFICATION_PREFIX) :].strip()
        for line in stderr.splitlines()
        if line.strip().startswith(STREAM_VERIFICATION_PREFIX)
    ]
    if not candidates:
        raise RemoteProtocolError("远端文件校验信息缺失")
    try:
        decoded = base64.b64decode(candidates[-1], validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteProtocolError("远端文件校验信息无效") from exc
    if not isinstance(payload, dict):
        raise RemoteProtocolError("远端文件校验信息无效")
    return payload


def decode_live_preview_verification(stderr: bytes) -> dict[str, Any]:
    """Read the fixed-size active-file snapshot metadata from SSH stderr."""
    candidates = [
        line.strip()[len(LIVE_PREVIEW_STREAM_PREFIX) :].strip()
        for line in stderr.splitlines()
        if line.strip().startswith(LIVE_PREVIEW_STREAM_PREFIX)
    ]
    if not candidates:
        raise RemoteProtocolError("活动文件快照校验信息缺失")
    try:
        decoded = base64.b64decode(candidates[-1], validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteProtocolError("活动文件快照校验信息无效") from exc
    if not isinstance(payload, dict):
        raise RemoteProtocolError("活动文件快照校验信息无效")
    return payload


def connectivity_script() -> str:
    """Return a fixed, read-only SSH authentication probe for Windows hosts."""
    return """
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$payload = [pscustomobject][ordered]@{
    ok = $true
    checked_at_utc = [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
}
$json = $payload | ConvertTo-Json -Compress -Depth 2
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
"""


def directory_probe_script(remote_path: str) -> str:
    """Return a fixed read-only directory existence/readability probe.

    The user-supplied path is data (base64 decoded on the remote side), never
    PowerShell source.  This deliberately does not enumerate children or open
    any file, so checking a proposed location cannot become a collection.
    """
    encoded_path = _encoded_remote_path(remote_path)
    return f"""
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$scanTime = [DateTime]::UtcNow
$exists = [bool](Test-Path -LiteralPath $path -PathType Container)
$payload = [pscustomobject][ordered]@{{
    exists = $exists
    checked_at_utc = $scanTime.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
}}
$json = $payload | ConvertTo-Json -Compress -Depth 2
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
"""


class SSHWindowsTransport:
    """Read-only OpenSSH transport for Windows PowerShell sources."""

    def __init__(self, *, known_hosts_file: Path | None = None) -> None:
        self.known_hosts_file = known_hosts_file

    def _ssh_args(self, machine: dict[str, Any]) -> list[str]:
        try:
            key_path = Path(str(machine["identity_file"])).expanduser()
            key_ready = key_path.is_file()
        except (KeyError, OSError, ValueError) as exc:
            raise CredentialConfigurationError(
                "SSH 登录密钥配置无效或不可读取"
            ) from exc
        if not key_ready:
            raise CredentialConfigurationError("SSH 登录密钥不存在或不可读取")
        try:
            ip = str(machine["ip"])
        except KeyError as exc:
            raise CollectionConfigurationError("实验电脑 SSH 地址配置无效") from exc
        try:
            ipaddress.ip_address(ip)
        except ValueError as exc:
            raise CollectionConfigurationError("实验电脑 SSH 地址配置无效") from exc
        user = str(machine.get("user") or "").strip()
        if not user or any(character in user for character in "\x00\r\n"):
            raise CollectionConfigurationError("实验电脑 SSH 用户配置无效")
        args = [
            "ssh",
            "-T",
            "-i",
            str(key_path),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=90",
            "-o",
            f"ControlPath=/tmp/start-stop-ssh-{os.getpid()}-%C",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
        configured_known_hosts = machine.get("known_hosts_file")
        try:
            known_hosts = (
                Path(str(configured_known_hosts)).expanduser()
                if configured_known_hosts
                else self.known_hosts_file
            )
        except (OSError, ValueError) as exc:
            raise CredentialConfigurationError(
                "SSH 主机身份校验文件配置无效"
            ) from exc
        if known_hosts is not None:
            try:
                known_hosts_ready = known_hosts.is_file()
            except (OSError, ValueError) as exc:
                raise CredentialConfigurationError(
                    "SSH 主机身份校验文件配置无效或不可读取"
                ) from exc
            if not known_hosts_ready:
                raise CredentialConfigurationError(
                    "SSH 主机身份校验文件不存在或不可读取"
                )
            args.extend(["-o", f"UserKnownHostsFile={known_hosts}"])
        args.extend(["-l", user, ip])
        return args

    def _powershell_args(self, machine: dict[str, Any], script: str) -> list[str]:
        return self._ssh_args(machine) + [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encode_powershell(script),
        ]

    def _run_payload(
        self,
        machine: dict[str, Any],
        script: str,
        *,
        timeout: int,
        error_type: type[PartialCollectionError] = RemoteRootError,
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                self._powershell_args(machine, script),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise error_type("实验电脑远端操作超时") from exc
        except OSError as exc:
            raise LocalProgramError("本机无法启动 SSH 客户端") from exc
        if result.returncode != 0:
            raise error_type("实验电脑远端读取失败")
        payload = decode_base64_payload(result.stdout)
        if not isinstance(payload, dict):
            raise RemoteProtocolError("远端返回的数据格式无效")
        return payload

    def check_connectivity(
        self,
        machine: dict[str, Any],
        *,
        timeout: int = DEFAULT_CONNECTIVITY_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Verify strict-host-key SSH authentication without reading remote data."""
        bounded_timeout = max(1, min(int(timeout), 30))
        started = time.monotonic()
        payload = self._run_payload(
            machine,
            connectivity_script(),
            timeout=bounded_timeout,
        )
        if payload.get("ok") is not True:
            raise RemoteProtocolError("远端连通性回应无效")
        return {
            "reachable": True,
            "message": "SSH 连接与密钥认证正常",
            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
            "checked_at_utc": str(payload.get("checked_at_utc") or iso_utc()),
        }

    def check_directory(
        self,
        machine: dict[str, Any],
        remote_path: str,
        *,
        timeout: int = DEFAULT_CONNECTIVITY_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Check one directory with ``Test-Path`` and no recursive inventory."""
        bounded_timeout = max(1, min(int(timeout), 30))
        started = time.monotonic()
        payload = self._run_payload(
            machine,
            directory_probe_script(remote_path),
            timeout=bounded_timeout,
        )
        exists = payload.get("exists") is True
        return {
            "reachable": exists,
            "message": "目录可访问" if exists else "未找到目录或当前账户无权访问",
            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
            "checked_at_utc": str(payload.get("checked_at_utc") or iso_utc()),
        }

    def inventory_root(
        self,
        machine: dict[str, Any],
        root: dict[str, Any],
        extensions: list[str],
    ) -> dict[str, Any]:
        payload = self._run_payload(
            machine,
            inventory_script(
                str(root["remote_path"]),
                extensions,
                root.get("exclude_directories", []),
                bool(root.get("recursive", True)),
            ),
            timeout=180,
        )
        if not payload.get("exists"):
            raise RemoteRootError("实验电脑上的数据目录不存在或不可读取")
        return payload

    def stat_file(
        self,
        machine: dict[str, Any],
        remote_path: str,
    ) -> dict[str, Any]:
        return self._run_payload(
            machine,
            stat_file_script(remote_path),
            timeout=60,
            error_type=RemoteFileError,
        )

    def fetch_file(
        self,
        machine: dict[str, Any],
        remote_path: str,
        destination: Path,
        expected_size: int,
    ) -> None:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise LocalFilesystemError("本地临时下载文件发生冲突")
        except FatalCollectionError:
            raise
        except OSError as exc:
            raise LocalFilesystemError("无法准备本地临时下载目录") from exc
        try:
            try:
                output_handle = destination.open("xb")
            except OSError as exc:
                raise LocalFilesystemError("无法创建本地临时下载文件") from exc
            with output_handle as output:
                try:
                    result = subprocess.run(
                        self._powershell_args(machine, stream_file_script(remote_path)),
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.PIPE,
                        timeout=3600,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RemoteFileError("远端文件读取超时") from exc
                except OSError as exc:
                    raise LocalProgramError("本机无法启动 SSH 客户端") from exc
            if result.returncode != 0:
                raise RemoteFileError("远端文件读取失败")
            try:
                actual_size = destination.stat().st_size
            except OSError as exc:
                raise LocalFilesystemError("无法校验本地临时下载文件") from exc
            if actual_size != expected_size:
                raise SourceChangedError("远端文件在下载期间发生变化")
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                # Preserve the primary, already classified failure.  The outer
                # batch cleanup will retry removal of the private directory.
                pass
            raise

    def fetch_file_verified(
        self,
        machine: dict[str, Any],
        remote_path: str,
        destination: Path,
        expected_size: int,
        expected_last_write_ticks: int,
    ) -> dict[str, int]:
        """Download and verify one stable remote version in one SSH session."""
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise LocalFilesystemError("本地临时下载文件发生冲突")
        except FatalCollectionError:
            raise
        except OSError as exc:
            raise LocalFilesystemError("无法准备本地临时下载目录") from exc
        try:
            try:
                output_handle = destination.open("xb")
            except OSError as exc:
                raise LocalFilesystemError("无法创建本地临时下载文件") from exc
            with output_handle as output:
                try:
                    result = subprocess.run(
                        self._powershell_args(
                            machine,
                            verified_stream_file_script(remote_path),
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.PIPE,
                        timeout=3600,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RemoteFileError("远端文件读取超时") from exc
                except OSError as exc:
                    raise LocalProgramError("本机无法启动 SSH 客户端") from exc
            if result.returncode != 0:
                raise RemoteFileError("远端文件读取失败")
            verification = decode_stream_verification(result.stderr)
            try:
                before_size = int(verification["before_size"])
                before_ticks = int(verification["before_ticks"])
                after_size = int(verification["after_size"])
                after_ticks = int(verification["after_ticks"])
                actual_size = destination.stat().st_size
            except (KeyError, TypeError, ValueError) as exc:
                raise RemoteProtocolError("远端文件校验信息无效") from exc
            except OSError as exc:
                raise LocalFilesystemError("无法校验本地临时下载文件") from exc
            expected = (int(expected_size), int(expected_last_write_ticks))
            if (
                (before_size, before_ticks) != expected
                or (after_size, after_ticks) != expected
                or actual_size != expected[0]
            ):
                raise SourceChangedError("远端文件在下载期间发生变化")
            return {
                "size": actual_size,
                "last_write_ticks": after_ticks,
            }
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def fetch_file_snapshot(
        self,
        machine: dict[str, Any],
        remote_path: str,
        destination: Path,
    ) -> dict[str, Any]:
        """Capture one bounded, read-only snapshot of a file still being written."""
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise LocalFilesystemError("本地活动预览临时文件发生冲突")
        except FatalCollectionError:
            raise
        except OSError as exc:
            raise LocalFilesystemError("无法准备活动预览临时目录") from exc
        try:
            try:
                output_handle = destination.open("xb")
            except OSError as exc:
                raise LocalFilesystemError("无法创建活动预览临时文件") from exc
            with output_handle as output:
                try:
                    result = subprocess.run(
                        self._powershell_args(
                            machine,
                            live_preview_stream_file_script(remote_path),
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.PIPE,
                        timeout=3600,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RemoteFileError("活动文件只读快照超时") from exc
                except OSError as exc:
                    raise LocalProgramError("本机无法启动 SSH 客户端") from exc
            if result.returncode != 0:
                raise RemoteFileError("活动文件只读快照失败")
            verification = decode_live_preview_verification(result.stderr)
            try:
                before_size = int(verification["before_size"])
                before_ticks = int(verification["before_ticks"])
                after_size = int(verification["after_size"])
                after_ticks = int(verification["after_ticks"])
                streamed_size = int(verification["streamed_size"])
                actual_size = destination.stat().st_size
            except (KeyError, TypeError, ValueError) as exc:
                raise RemoteProtocolError("活动文件快照校验信息无效") from exc
            except OSError as exc:
                raise LocalFilesystemError("无法校验活动预览临时文件") from exc
            if before_size < 0 or streamed_size != before_size or actual_size != before_size:
                raise SourceChangedError("活动文件在快照期间被截断，已跳过本轮预览")
            return {
                "size": actual_size,
                "last_write_ticks": before_ticks,
                "source_modified_utc": str(
                    verification.get("source_modified_utc") or ""
                ),
                "grew_during_snapshot": bool(
                    after_size > before_size or after_ticks != before_ticks
                ),
                "size_after": after_size,
            }
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def selected_machines(
    config: dict[str, Any], selectors: Iterable[str]
) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        raise CollectionConfigurationError("采集配置顶层格式无效")
    machines = config.get("machines")
    if not isinstance(machines, list) or not machines:
        raise CollectionConfigurationError("采集配置没有实验电脑")
    if any(not isinstance(machine, dict) for machine in machines):
        raise CollectionConfigurationError("实验电脑配置格式无效")
    wanted = {str(selector).casefold() for selector in selectors}
    if not wanted:
        return machines
    selected = [
        machine
        for machine in machines
        if wanted.intersection(
            {
                str(machine.get("id", "")).casefold(),
                str(machine.get("hostname", "")).casefold(),
                str(machine.get("ip", "")).casefold(),
            }
        )
    ]
    if not selected:
        raise CollectionConfigurationError("没有匹配到指定实验电脑")
    return selected


def make_planned_files(
    machine: dict[str, Any],
    root: dict[str, Any],
    inventory: dict[str, Any],
    settle_seconds: int,
) -> tuple[list[PlannedFile], list[dict[str, Any]]]:
    try:
        scanned_at = parse_utc(str(inventory["scanned_at_utc"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RemoteProtocolError("远端文件清单缺少有效扫描时间") from exc
    records = inventory.get("files", [])
    if not isinstance(records, list):
        raise RemoteProtocolError("远端文件清单格式无效")
    stable: list[PlannedFile] = []
    unsettled: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise RemoteProtocolError("远端文件记录格式无效")
        try:
            remote_path = str(record["path"])
            relative_path = str(record["relative"])
            size = int(record["size"])
            ticks = int(record["last_write_ticks"])
            modified = parse_utc(str(record["last_write_utc"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteProtocolError("远端文件元数据格式无效") from exc
        if not remote_path or "\x00" in remote_path:
            raise RemoteProtocolError("远端文件路径无效")
        path_key = remote_path.casefold()
        if path_key in seen_paths:
            raise RemoteProtocolError("远端文件清单包含重复项")
        seen_paths.add(path_key)
        if size < 0 or ticks < 0:
            raise RemoteProtocolError("远端文件元数据无效")
        if (scanned_at - modified).total_seconds() < settle_seconds:
            unsettled.append(record)
            continue
        stable.append(
            PlannedFile(
                machine=machine,
                root=root,
                remote_path=remote_path,
                relative_path=relative_path,
                size=size,
                last_write_ticks=ticks,
                last_write_utc=str(record["last_write_utc"]),
            )
        )
    stable.sort(key=lambda item: item.remote_path.casefold())
    return stable, unsettled


def _metadata_matches(item: PlannedFile, record: dict[str, Any]) -> bool:
    return bool(
        record.get("exists")
        and int(record.get("size", -1)) == item.size
        and int(record.get("last_write_ticks", -1)) == item.last_write_ticks
    )


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class StartStopCollector:
    """Coordinate incremental remote reads and database publication."""

    def __init__(
        self,
        database: Any,
        scratch_root: Path,
        *,
        transport: RemoteTransport | None = None,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
        reserved_free_bytes: int = RESERVED_FREE_BYTES,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.database = database
        self.scratch_root = Path(scratch_root)
        self.transport = transport or SSHWindowsTransport()
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.reserved_free_bytes = max(0, int(reserved_free_bytes))
        self.progress_callback = progress_callback

    @staticmethod
    def _friendly_text(value: Any, *, limit: int = 180) -> str:
        """Return compact display text without control characters."""
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
        return re.sub(r"\s+", " ", text).strip()[:limit]

    @classmethod
    def _file_basename(cls, remote_path: str) -> str:
        """Return only a Windows/POSIX basename for public progress state."""
        return cls._friendly_text(re.split(r"[\\/]", remote_path)[-1])

    def _emit_progress(
        self,
        *,
        phase: str,
        phase_label: str,
        phase_index: int,
        phase_count: int,
        percent: int | float | None,
        completed: int | None,
        total: int | None,
        unit: str,
        current_item: str = "",
        detail: str = "",
        machines: list[dict[str, Any]] | None = None,
        mode: str = "determinate",
    ) -> None:
        if self.progress_callback is None:
            return
        public_machines: list[dict[str, Any]] = []
        for machine in machines or []:
            public_machines.append(
                {
                    "machine_id": self._friendly_text(machine.get("machine_id"), limit=80),
                    "name": self._friendly_text(machine.get("name"), limit=120),
                    "status": self._friendly_text(machine.get("status"), limit=40),
                    "completed": max(0, int(machine.get("completed", 0))),
                    "total": max(0, int(machine.get("total", 0))),
                    "message": self._friendly_text(machine.get("message"), limit=220),
                }
            )
        payload = {
            "phase": self._friendly_text(phase, limit=64),
            "phase_label": self._friendly_text(phase_label, limit=80),
            "phase_index": max(1, int(phase_index)),
            "phase_count": max(1, int(phase_count)),
            "mode": "indeterminate" if mode == "indeterminate" else "determinate",
            "percent": (
                None
                if percent is None
                else max(0, min(100, int(round(float(percent)))))
            ),
            "completed": None if completed is None else max(0, int(completed)),
            "total": None if total is None else max(0, int(total)),
            "unit": unit if unit in {"files", "machines", "steps", "items", "bytes"} else "items",
            "current_item": self._friendly_text(current_item),
            "detail": self._friendly_text(detail, limit=260),
            "machines": public_machines,
            "updated_at_utc": iso_utc(),
        }
        try:
            self.progress_callback(payload)
        except OSError as exc:
            raise LocalFilesystemError("无法写入本地采集进度") from exc
        except Exception as exc:
            raise UnexpectedCollectionError("采集进度处理程序遇到意外错误") from exc

    def _retry(self, operation: Callable[[], T]) -> T:
        last_error: PartialCollectionError | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return operation()
            except SourceChangedError:
                raise
            except PartialCollectionError as exc:
                last_error = exc
                if attempt < self.retry_attempts and self.retry_delay_seconds:
                    time.sleep(self.retry_delay_seconds)
        assert last_error is not None
        raise last_error

    def _ensure_free_space(self, file_size: int) -> None:
        locations = [self.scratch_root]
        database_path = getattr(self.database, "path", None)
        if database_path is not None:
            locations.append(Path(database_path).parent)
        checked: set[int] = set()
        for location in locations:
            try:
                location.mkdir(parents=True, exist_ok=True)
                stat = location.stat()
            except OSError as exc:
                raise LocalFilesystemError(
                    "无法检查本地采集存储目录"
                ) from exc
            device_key = stat.st_dev
            if device_key in checked:
                continue
            checked.add(device_key)
            try:
                free = shutil.disk_usage(location).free
            except OSError as exc:
                raise LocalFilesystemError("无法读取本地磁盘剩余空间") from exc
            if file_size + self.reserved_free_bytes > free:
                raise LocalStorageError(
                    "本地存储空间不足，已停止采集以保护数据库"
                )

    @staticmethod
    def _needs_download(database: Any, item: PlannedFile) -> bool:
        try:
            decision = database.needs_download(
                str(item.machine["id"]),
                str(item.root["label"]),
                item.remote_path,
                item.size,
                item.last_write_ticks,
            )
        except Exception as exc:
            raise LocalDatabaseError("本地数据库无法检查文件版本") from exc
        if isinstance(decision, dict):
            return bool(decision.get("needs_download", decision.get("required", True)))
        return bool(decision)

    @staticmethod
    def _begin_batch(
        database: Any,
        *,
        run_id: str,
        batch_id: str,
        started_at_utc: str,
        settle_seconds: int,
        machine_count: int,
        collection_config_revision: int,
    ) -> Any:
        metadata = {
            "run_id": run_id,
            "started_at_utc": started_at_utc,
            "settle_seconds": settle_seconds,
            "collection_config_revision": collection_config_revision,
        }
        arguments = dict(
            machine_count=machine_count,
            metadata=metadata,
        )
        if batch_id:
            arguments["batch_id"] = batch_id
        try:
            result = database.begin_collection_batch(**arguments)
        except Exception as exc:
            raise LocalDatabaseError("本地数据库无法创建采集批次") from exc
        if isinstance(result, dict):
            return result.get("batch_id", result.get("id", run_id))
        return result if result is not None else run_id

    @staticmethod
    def _record_issue(
        database: Any,
        batch_id: Any,
        *,
        scope: str,
        severity: str,
        code: str,
        message: str,
        machine_id: str | None = None,
        root_label: str | None = None,
        remote_path: str | None = None,
    ) -> None:
        try:
            database.record_collection_issue(
                batch_id,
                severity=severity,
                code=code,
                message=message[:2000],
                machine_id=machine_id or "",
                root_label=root_label or "",
                remote_path=remote_path or "",
                detail={"scope": scope},
            )
        except Exception as exc:
            raise LocalDatabaseError("本地数据库无法记录采集问题") from exc

    @staticmethod
    def _ingest_staged_file(
        database: Any,
        staged_path: Path,
        metadata: dict[str, Any],
        batch_id: Any,
    ) -> Any:
        try:
            return database.ingest_staged_file(staged_path, metadata, batch_id)
        except Exception as exc:
            raise LocalDatabaseError("本地数据库写入文件失败") from exc

    @staticmethod
    def _finish_batch(
        database: Any,
        batch_id: Any,
        *,
        status: str,
        totals: dict[str, Any],
    ) -> Any:
        try:
            return database.finish_collection_batch(
                batch_id,
                status=status,
                totals=totals,
            )
        except Exception as exc:
            raise LocalDatabaseError("本地数据库无法完成采集批次") from exc

    @staticmethod
    def _cleanup_run_scratch(staged_path: Path, run_scratch: Path) -> None:
        try:
            staged_path.unlink(missing_ok=True)
            try:
                shutil.rmtree(run_scratch)
            except FileNotFoundError:
                pass
        except OSError as exc:
            raise LocalFilesystemError("无法清理本地采集临时目录") from exc

    def collect(
        self,
        config: dict[str, Any],
        *,
        selectors: Iterable[str] = (),
        settle_seconds_override: int | None = None,
        batch_id: str = "",
    ) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise CollectionConfigurationError("采集配置顶层格式无效")
        requested_batch_id = str(batch_id or "").strip()
        machines = selected_machines(config, selectors)
        for machine in machines:
            self._validate_machine(machine)
        machine_progress = [
            {
                "machine_id": str(machine["hostname"]),
                "name": str(machine.get("name") or machine["hostname"]),
                "status": "pending",
                "completed": 0,
                "total": 0,
                "message": "等待连接",
            }
            for machine in machines
        ]
        total_roots = sum(len(machine["roots"]) for machine in machines)
        first_machine_name = self._friendly_text(machines[0]["hostname"])
        self._emit_progress(
            phase="connecting_remote",
            phase_label="连接实验电脑",
            phase_index=1,
            phase_count=4,
            percent=0,
            completed=0,
            total=len(machines),
            unit="machines",
            current_item=first_machine_name,
            detail=f"正在准备连接 {first_machine_name}",
            machines=machine_progress,
        )
        extensions_raw = config.get("extensions")
        if not isinstance(extensions_raw, list) or not extensions_raw:
            raise CollectionConfigurationError("采集配置没有文件扩展名白名单")
        extensions = sorted(
            {
                str(extension).casefold()
                for extension in extensions_raw
                if str(extension).startswith(".") and "\x00" not in str(extension)
            }
        )
        if not extensions:
            raise CollectionConfigurationError("文件扩展名白名单无效")
        forbidden = FORBIDDEN_EXTENSIONS.intersection(extensions)
        if forbidden:
            raise CollectionConfigurationError(
                "采集配置包含禁止下载的文件类型：" + ", ".join(sorted(forbidden))
            )
        try:
            settle_seconds = (
                int(settle_seconds_override)
                if settle_seconds_override is not None
                else int(config.get("settle_seconds", DEFAULT_SETTLE_SECONDS))
            )
        except (TypeError, ValueError) as exc:
            raise CollectionConfigurationError("文件稳定等待时间配置无效") from exc
        if settle_seconds < 60:
            raise CollectionConfigurationError("文件稳定等待时间必须至少为 1 分钟")
        raw_config_revision = config.get("collection_config_revision", 0)
        if (
            isinstance(raw_config_revision, bool)
            or not isinstance(raw_config_revision, int)
            or raw_config_revision < 0
        ):
            raise CollectionConfigurationError("搜索位置配置修订号无效")
        collection_config_revision = int(raw_config_revision)

        configured_known_hosts = config.get("known_hosts_file")
        if configured_known_hosts and isinstance(self.transport, SSHWindowsTransport):
            self.transport.known_hosts_file = Path(str(configured_known_hosts)).expanduser()

        run_id = now_utc().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        started_at = iso_utc()
        try:
            self.scratch_root.mkdir(parents=True, exist_ok=True)
            run_scratch = self.scratch_root / f"collection-{run_id}"
            run_scratch.mkdir(mode=0o700)
        except OSError as exc:
            raise LocalFilesystemError("无法创建本地采集临时目录") from exc
        staged_path = run_scratch / "current-download.part"
        summary: dict[str, Any] = {
            "run_id": run_id,
            "mode": "collect",
            "started_at_utc": started_at,
            "settle_seconds": settle_seconds,
            "collection_config_revision": collection_config_revision,
            "failure_class": "none",
            "failure_code": "",
            "publishable": False,
            "machines": [],
            "totals": {
                "inventoried": 0,
                "stable": 0,
                "unsettled_skipped": 0,
                "already_collected": 0,
                "planned_files": 0,
                "planned_bytes": 0,
                "downloaded": 0,
                "ingested": 0,
                "unchanged_content": 0,
                "changed_during_collection": 0,
                "errors": 0,
                "roots_succeeded": 0,
                "roots_failed": 0,
            },
        }
        batch_id: Any = None
        batch_finished = False
        inventory_executor: concurrent.futures.ThreadPoolExecutor | None = None
        inventory_futures: dict[
            tuple[str, str, str], concurrent.futures.Future[dict[str, Any]]
        ] = {}
        processed_files = 0
        planned_files_seen = 0
        completed_roots = 0
        try:
            batch_id = self._begin_batch(
                self.database,
                run_id=run_id,
                batch_id=requested_batch_id,
                started_at_utc=started_at,
                settle_seconds=settle_seconds,
                machine_count=len(machines),
                collection_config_revision=collection_config_revision,
            )
            summary["batch_id"] = batch_id
            totals = summary["totals"]
            inventory_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(3, total_roots)),
                thread_name_prefix="start-stop-inventory",
            )
            for machine in machines:
                for root in machine["roots"]:
                    key = (
                        str(machine["id"]),
                        str(root["label"]),
                        str(root["remote_path"]),
                    )
                    inventory_futures[key] = inventory_executor.submit(
                        self._retry,
                        lambda machine=machine, root=root: self.transport.inventory_root(
                            machine,
                            root,
                            extensions,
                        ),
                    )
            for machine_index, machine in enumerate(machines):
                machine_state = machine_progress[machine_index]
                machine_state.update(status="connecting", message="正在连接")
                machine_roots_succeeded = 0
                machine_roots_failed = 0
                machine_summary: dict[str, Any] = {
                    "id": str(machine["id"]),
                    "hostname": str(machine["hostname"]),
                    "ip": str(machine["ip"]),
                    "roots": [],
                    "errors": [],
                    "deferred": [],
                }
                summary["machines"].append(machine_summary)
                for root in machine["roots"]:
                    source_name = self._friendly_text(
                        f"{machine['hostname']} · {root['label']}"
                    )
                    root_base_percent = (
                        100 * completed_roots / total_roots if total_roots else 0
                    )
                    machine_state.update(
                        status="connecting",
                        message=f"正在连接数据源 {self._friendly_text(root['label'])}",
                    )
                    self._emit_progress(
                        phase="connecting_remote",
                        phase_label="连接实验电脑",
                        phase_index=1,
                        phase_count=4,
                        percent=root_base_percent,
                        completed=machine_index,
                        total=len(machines),
                        unit="machines",
                        current_item=source_name,
                        detail=f"实验机 {self._friendly_text(machine['hostname'])}，数据源 {self._friendly_text(root['label'])}",
                        machines=machine_progress,
                    )
                    root_summary: dict[str, Any] = {
                        "label": str(root["label"]),
                        "remote_path": str(root["remote_path"]),
                    }
                    machine_summary["roots"].append(root_summary)
                    try:
                        machine_state.update(
                            status="scanning",
                            message=f"正在清点数据源 {self._friendly_text(root['label'])}",
                        )
                        self._emit_progress(
                            phase="scanning_remote",
                            phase_label="清点远程文件",
                            phase_index=2,
                            phase_count=4,
                            percent=min(99, root_base_percent + 2),
                            completed=completed_roots,
                            total=total_roots,
                            unit="items",
                            current_item=source_name,
                            detail=f"实验机 {self._friendly_text(machine['hostname'])}，数据源 {self._friendly_text(root['label'])}",
                            machines=machine_progress,
                        )
                        inventory = inventory_futures[
                            (
                                str(machine["id"]),
                                str(root["label"]),
                                str(root["remote_path"]),
                            )
                        ].result()
                        stable, unsettled = make_planned_files(
                            machine, root, inventory, settle_seconds
                        )
                        totals["inventoried"] += len(inventory.get("files", []))
                        totals["stable"] += len(stable)
                        totals["unsettled_skipped"] += len(unsettled)
                        planned: list[PlannedFile] = []
                        for scan_index, item in enumerate(stable):
                            filename = self._file_basename(item.remote_path)
                            machine_state.update(
                                status="scanning",
                                message=f"正在检查 {filename}",
                            )
                            self._emit_progress(
                                phase="scanning_remote",
                                phase_label="检查文件更新",
                                phase_index=2,
                                phase_count=4,
                                percent=min(
                                    99,
                                    root_base_percent
                                    + 2
                                    + 5 * (scan_index + 1) / max(1, len(stable)),
                                ),
                                completed=scan_index,
                                total=len(stable),
                                unit="files",
                                current_item=filename,
                                detail=f"实验机 {self._friendly_text(machine['hostname'])}，正在判断文件是否需要更新",
                                machines=machine_progress,
                            )
                            if self._needs_download(self.database, item):
                                planned.append(item)
                        already = len(stable) - len(planned)
                        planned_bytes = sum(item.size for item in planned)
                        planned_files_seen += len(planned)
                        machine_state["total"] = int(machine_state["total"]) + len(planned)
                        totals["roots_succeeded"] += 1
                        machine_roots_succeeded += 1
                        totals["already_collected"] += already
                        totals["planned_files"] += len(planned)
                        totals["planned_bytes"] += planned_bytes
                        root_summary.update(
                            {
                                "inventoried": len(inventory.get("files", [])),
                                "stable": len(stable),
                                "unsettled_skipped": len(unsettled),
                                "already_collected": already,
                                "planned_files": len(planned),
                                "planned_bytes": planned_bytes,
                            }
                        )
                        first_planned_name = (
                            self._file_basename(planned[0].remote_path)
                            if planned
                            else source_name
                        )
                        machine_state.update(
                            status="downloading" if planned else "scanning",
                            message=(
                                f"待处理 {len(planned)} 个文件"
                                if planned
                                else "没有需要下载的新文件"
                            ),
                        )
                        self._emit_progress(
                            phase="downloading_remote" if planned else "scanning_remote",
                            phase_label="下载新文件" if planned else "清点远程文件",
                            phase_index=3 if planned else 2,
                            phase_count=4,
                            percent=min(99, root_base_percent + 8),
                            completed=processed_files,
                            total=planned_files_seen,
                            unit="files",
                            current_item=first_planned_name,
                            detail=f"实验机 {self._friendly_text(machine['hostname'])}，本数据源待处理 {len(planned)} 个文件",
                            machines=machine_progress,
                        )
                    except PartialCollectionError as exc:
                        message = str(exc)
                        totals["roots_failed"] += 1
                        machine_roots_failed += 1
                        totals["errors"] += 1
                        root_summary["error"] = message
                        root_summary["error_code"] = exc.code
                        machine_summary["errors"].append(message)
                        self._record_issue(
                            self.database,
                            batch_id,
                            scope="root",
                            severity="error",
                            code=exc.code,
                            message=message,
                            machine_id=str(machine["id"]),
                            root_label=str(root.get("label", "")),
                        )
                        machine_state.update(
                            status="failed",
                            message=f"数据源 {self._friendly_text(root.get('label', ''))} 清点失败",
                        )
                        completed_roots += 1
                        self._emit_progress(
                            phase="scanning_remote",
                            phase_label="清点远程文件",
                            phase_index=2,
                            phase_count=4,
                            percent=min(99, 100 * completed_roots / total_roots),
                            completed=completed_roots,
                            total=total_roots,
                            unit="items",
                            current_item=source_name,
                            detail=f"实验机 {self._friendly_text(machine['hostname'])} 的数据源清点失败",
                            machines=machine_progress,
                        )
                        continue

                    root_downloaded = 0
                    root_ingested = 0
                    root_unchanged = 0
                    root_changed = 0
                    root_errors = 0
                    consecutive_errors = 0
                    for file_index, item in enumerate(planned):
                        filename = self._file_basename(item.remote_path)
                        within_root = file_index / max(1, len(planned))
                        file_span = 75 / (total_roots * max(1, len(planned)))
                        file_percent = min(
                            99,
                            max(
                                root_base_percent + 8,
                                100
                                * (completed_roots + 0.1 + 0.75 * within_root)
                                / total_roots,
                            ),
                        )
                        machine_state.update(
                            status="scanning",
                            message=f"正在校验 {filename}",
                        )
                        self._emit_progress(
                            phase="scanning_remote",
                            phase_label="校验待下载文件",
                            phase_index=3,
                            phase_count=4,
                            percent=file_percent,
                            completed=processed_files,
                            total=planned_files_seen,
                            unit="files",
                            current_item=filename,
                            detail=f"实验机 {self._friendly_text(machine['hostname'])}，数据源 {self._friendly_text(root['label'])}",
                            machines=machine_progress,
                        )
                        staged_path.unlink(missing_ok=True)
                        try:
                            self._ensure_free_space(item.size)
                            machine_state.update(
                                status="downloading",
                                message=f"正在下载 {filename}",
                            )
                            self._emit_progress(
                                phase="downloading_remote",
                                phase_label="下载新文件",
                                phase_index=3,
                                phase_count=4,
                                percent=min(99, file_percent + file_span * 0.25),
                                completed=processed_files,
                                total=planned_files_seen,
                                unit="files",
                                current_item=filename,
                                detail=f"实验机 {self._friendly_text(machine['hostname'])}，数据源 {self._friendly_text(root['label'])}",
                                machines=machine_progress,
                            )
                            verified_fetch = getattr(
                                self.transport,
                                "fetch_file_verified",
                                None,
                            )
                            if callable(verified_fetch):
                                self._retry(
                                    lambda item=item: verified_fetch(
                                        item.machine,
                                        item.remote_path,
                                        staged_path,
                                        item.size,
                                        item.last_write_ticks,
                                    )
                                )
                            else:
                                before = self._retry(
                                    lambda item=item: self.transport.stat_file(
                                        item.machine, item.remote_path
                                    )
                                )
                                if not _metadata_matches(item, before):
                                    raise SourceChangedError(
                                        "远端文件在下载前发生变化"
                                    )
                                self._retry(
                                    lambda item=item: self.transport.fetch_file(
                                        item.machine,
                                        item.remote_path,
                                        staged_path,
                                        item.size,
                                    )
                                )
                            try:
                                staged_stat = staged_path.stat()
                            except FileNotFoundError as exc:
                                raise RemoteFileError(
                                    "远端文件未完成下载"
                                ) from exc
                            except OSError as exc:
                                raise LocalFilesystemError(
                                    "无法校验本地临时下载文件"
                                ) from exc
                            if not stat.S_ISREG(staged_stat.st_mode):
                                raise LocalFilesystemError(
                                    "本地临时下载目标不是普通文件"
                                )
                            if staged_stat.st_size != item.size:
                                raise SourceChangedError(
                                    "远端文件下载字节数发生变化"
                                )
                            totals["downloaded"] += 1
                            root_downloaded += 1
                            if not callable(verified_fetch):
                                after = self._retry(
                                    lambda item=item: self.transport.stat_file(
                                        item.machine, item.remote_path
                                    )
                                )
                                if not _metadata_matches(item, after):
                                    raise SourceChangedError(
                                        "远端文件在下载后发生变化"
                                    )
                            machine_state.update(
                                status="storing",
                                message=f"正在入库 {filename}",
                            )
                            self._emit_progress(
                                phase="storing_database",
                                phase_label="写入数据库",
                                phase_index=3,
                                phase_count=4,
                                percent=min(99, file_percent + file_span * 0.75),
                                completed=processed_files,
                                total=planned_files_seen,
                                unit="files",
                                current_item=filename,
                                detail=f"实验机 {self._friendly_text(machine['hostname'])}，数据源 {self._friendly_text(root['label'])}",
                                machines=machine_progress,
                            )
                            ingest_result = self._ingest_staged_file(
                                self.database,
                                staged_path,
                                {
                                    "machine_id": str(machine["id"]),
                                    "hostname": str(machine["hostname"]),
                                    "ip": str(machine["ip"]),
                                    "root_label": str(root["label"]),
                                    "remote_root": str(root["remote_path"]),
                                    "remote_path": item.remote_path,
                                    "remote_relative_path": item.relative_path,
                                    "size": item.size,
                                    "last_write_ticks": item.last_write_ticks,
                                    "last_write_utc": item.last_write_utc,
                                    "collected_at_utc": iso_utc(),
                                },
                                batch_id,
                            )
                            status = (
                                str(ingest_result.get("status", "ingested"))
                                if isinstance(ingest_result, dict)
                                else "ingested"
                            )
                            totals["ingested"] += 1
                            root_ingested += 1
                            if status in {
                                "unchanged",
                                "unchanged_content",
                                "deduplicated",
                            } or (
                                isinstance(ingest_result, dict)
                                and ingest_result.get("changed") is False
                            ):
                                totals["unchanged_content"] += 1
                                root_unchanged += 1
                            consecutive_errors = 0
                        except SourceChangedError as exc:
                            message = str(exc)
                            totals["changed_during_collection"] += 1
                            root_changed += 1
                            machine_summary["deferred"].append(message)
                            self._record_issue(
                                self.database,
                                batch_id,
                                scope="file",
                                severity="warning",
                                code="source_changed",
                                message=message,
                                machine_id=str(machine["id"]),
                                root_label=str(root["label"]),
                                remote_path=item.remote_path,
                            )
                        except PartialCollectionError as exc:
                            message = str(exc)
                            totals["errors"] += 1
                            root_errors += 1
                            consecutive_errors += 1
                            machine_summary["errors"].append(message)
                            self._record_issue(
                                self.database,
                                batch_id,
                                scope="file",
                                severity="error",
                                code=exc.code,
                                message=message,
                                machine_id=str(machine["id"]),
                                root_label=str(root["label"]),
                                remote_path=item.remote_path,
                            )
                            if consecutive_errors >= 3:
                                break
                        finally:
                            processed_files += 1
                            machine_state["completed"] = int(machine_state["completed"]) + 1
                            staged_path.unlink(missing_ok=True)
                    root_summary.update(
                        {
                            "downloaded": root_downloaded,
                            "ingested": root_ingested,
                            "unchanged_content": root_unchanged,
                            "changed_during_collection": root_changed,
                            "errors": root_errors,
                        }
                    )
                    completed_roots += 1

                if machine_roots_succeeded == 0 and machine_roots_failed:
                    machine_status = "failed"
                    machine_message = f"全部 {machine_roots_failed} 个数据源清点失败"
                elif (
                    machine_roots_failed
                    or machine_summary["errors"]
                    or machine_summary["deferred"]
                ):
                    machine_status = "completed_with_warnings"
                    if machine_roots_failed:
                        machine_message = f"处理完成，{machine_roots_failed} 个数据源未完成"
                    elif machine_summary["errors"]:
                        machine_message = f"处理完成，{len(machine_summary['errors'])} 项文件错误"
                    else:
                        machine_message = f"处理完成，{len(machine_summary['deferred'])} 个文件已暂缓"
                else:
                    machine_status = "completed"
                    machine_message = "处理完成"
                machine_state.update(
                    status=machine_status,
                    message=machine_message,
                )

            publishable = bool(totals["roots_succeeded"])
            summary["publishable"] = publishable
            if totals["errors"]:
                status = (
                    "completed_with_warnings"
                    if publishable
                    else "failed"
                )
            elif totals["unsettled_skipped"] or totals["changed_during_collection"]:
                status = "completed_with_warnings"
            else:
                status = "completed"
            if not publishable:
                summary["failure_class"] = "fatal"
                summary["failure_code"] = "no_remote_roots_succeeded"
            elif totals["errors"]:
                summary["failure_class"] = "partial"
                summary["failure_code"] = "remote_partial_failure"
            elif totals["changed_during_collection"]:
                summary["failure_class"] = "partial"
                summary["failure_code"] = "source_changed"
            elif totals["unsettled_skipped"]:
                summary["failure_class"] = "partial"
                summary["failure_code"] = "source_not_settled"
            summary["status"] = status
            summary["finished_at_utc"] = iso_utc()
            finish_result = self._finish_batch(
                self.database,
                batch_id,
                status=status,
                totals=totals,
            )
            batch_finished = True
            if isinstance(finish_result, dict):
                for key in ("snapshot_id", "batch_id"):
                    if finish_result.get(key) is not None:
                        summary[key] = finish_result[key]
            self._emit_progress(
                phase="collection_complete",
                phase_label="实验电脑采集完成",
                phase_index=4,
                phase_count=4,
                percent=100,
                completed=processed_files,
                total=planned_files_seen,
                unit="files",
                detail=(
                    f"已下载 {totals['downloaded']} 个文件，"
                    f"入库 {totals['ingested']} 个文件"
                ),
                machines=machine_progress,
            )
            return summary
        except Exception as exc:
            fatal = self._fatal_error(exc)
            if batch_id is not None and not batch_finished:
                failed_at = iso_utc()
                summary["status"] = "failed"
                summary["finished_at_utc"] = failed_at
                summary["failure_class"] = "fatal"
                summary["failure_code"] = fatal.code
                summary["publishable"] = False
                summary["fatal_error"] = str(fatal)[:2000]
                try:
                    self._record_issue(
                        self.database,
                        batch_id,
                        scope="batch",
                        severity="error",
                        code=fatal.code,
                        message=str(fatal),
                    )
                    self._finish_batch(
                        self.database,
                        batch_id,
                        status="failed",
                        totals=summary["totals"],
                    )
                except Exception:
                    pass
            if fatal is exc:
                raise
            raise fatal from exc
        finally:
            if inventory_executor is not None:
                inventory_executor.shutdown(wait=True, cancel_futures=True)
            self._cleanup_run_scratch(staged_path, run_scratch)

    @staticmethod
    def _validate_machine(machine: dict[str, Any]) -> None:
        if not isinstance(machine, dict):
            raise CollectionConfigurationError("实验电脑配置格式无效")
        for key in ("id", "hostname", "ip", "user", "identity_file", "roots"):
            if key not in machine:
                raise CollectionConfigurationError(f"电脑配置缺少字段：{key}")
        if not isinstance(machine["roots"], list) or not machine["roots"]:
            raise CollectionConfigurationError("实验电脑没有配置数据来源")
        for root in machine["roots"]:
            if not isinstance(root, dict) or not root.get("label") or not root.get(
                "remote_path"
            ):
                raise CollectionConfigurationError("实验电脑的数据来源配置无效")

    @staticmethod
    def _fatal_error(exc: Exception) -> FatalCollectionError:
        if isinstance(exc, FatalCollectionError):
            return exc
        if isinstance(exc, PartialCollectionError):
            # Partial failures are handled at their root/file boundary.  If one
            # reaches here, continuing would violate the publishability gate.
            return UnexpectedCollectionError("采集错误超出预期处理范围，已停止本次任务")
        if isinstance(exc, OSError):
            return LocalFilesystemError("本地文件系统操作失败，已停止本次采集")
        return UnexpectedCollectionError("采集程序遇到意外错误，已停止本次任务")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 Windows 电化学工作站只读增量采集文件到 SQLite"
    )
    parser.add_argument("--config", type=Path, required=True, help="采集配置 JSON")
    parser.add_argument("--database", type=Path, required=True, help="持久 SQLite 数据库")
    parser.add_argument("--result-json", type=Path, required=True, help="本次采集结果 JSON")
    parser.add_argument("--scratch", type=Path, required=True, help="一次性下载临时目录")
    parser.add_argument(
        "--progress-json",
        type=Path,
        default=None,
        help="可选：原子更新的任务进度 JSON",
    )
    parser.add_argument(
        "--batch-id",
        default="",
        help="可选：由父服务预生成的采集批次编号",
    )
    parser.add_argument(
        "--machine",
        action="append",
        default=[],
        help="只处理指定电脑，可用 ID、主机名或 IP；可重复",
    )
    parser.add_argument(
        "--settle-minutes",
        type=int,
        default=None,
        help="覆盖文件稳定等待分钟数",
    )
    args = parser.parse_args(argv)
    if args.settle_minutes is not None and args.settle_minutes < 1:
        parser.error("--settle-minutes 必须至少为 1")
    if args.batch_id and not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", args.batch_id):
        parser.error("--batch-id 格式无效")
    return args


def _fatal_summary(error: FatalCollectionError) -> dict[str, Any]:
    timestamp = iso_utc()
    return {
        "run_id": None,
        "mode": "collect",
        "status": "failed",
        "started_at_utc": timestamp,
        "finished_at_utc": timestamp,
        "failure_class": "fatal",
        "failure_code": error.code,
        "publishable": False,
        "fatal_error": str(error)[:2000],
        "machines": [],
        "totals": {"errors": 1},
    }


def _main_fatal_error(exc: Exception) -> FatalCollectionError:
    if isinstance(exc, FatalCollectionError):
        return exc
    if isinstance(exc, json.JSONDecodeError):
        return CollectionConfigurationError("采集配置 JSON 格式无效")
    if isinstance(exc, OSError):
        return LocalFilesystemError("本地采集文件无法读取或写入")
    return UnexpectedCollectionError("采集程序遇到意外错误，已停止本次任务")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    progress_path = args.progress_json.resolve() if args.progress_json else None
    try:
        if progress_path is not None:
            try:
                progress_path.unlink(missing_ok=True)
            except OSError as exc:
                raise LocalFilesystemError("无法初始化采集进度文件") from exc
        try:
            with args.config.resolve().open("r", encoding="utf-8") as handle:
                config = json.load(handle)
        except json.JSONDecodeError as exc:
            raise CollectionConfigurationError("采集配置 JSON 格式无效") from exc
        except OSError as exc:
            raise CollectionConfigurationError("采集配置文件不存在或不可读取") from exc
        try:
            from .start_stop_database import StartStopDatabase
        except ImportError:  # pragma: no cover - direct script execution fallback
            package_root = str(Path(__file__).resolve().parent.parent)
            if package_root not in sys.path:
                sys.path.insert(0, package_root)
            from echem_platform.start_stop_database import StartStopDatabase

        try:
            database = StartStopDatabase(args.database.resolve())
        except Exception as exc:
            raise LocalDatabaseError("本地采集数据库无法打开") from exc
        collector = StartStopCollector(
            database,
            args.scratch.resolve(),
            progress_callback=(
                (lambda payload: _write_json_atomic(progress_path, payload))
                if progress_path is not None
                else None
            ),
        )
        summary = collector.collect(
            config,
            selectors=args.machine,
            settle_seconds_override=(
                args.settle_minutes * 60 if args.settle_minutes is not None else None
            ),
            batch_id=args.batch_id,
        )
        _write_json_atomic(args.result_json.resolve(), summary)
        totals = summary["totals"]
        print(
            "采集完成："
            f"清点 {totals['inventoried']}，下载 {totals['downloaded']}，"
            f"入库 {totals['ingested']}，暂缓 "
            f"{totals['unsettled_skipped'] + totals['changed_during_collection']}，"
            f"错误 {totals['errors']}。"
        )
        return 1 if totals["errors"] else 0
    except Exception as exc:
        fatal = _main_fatal_error(exc)
        summary = _fatal_summary(fatal)
        try:
            _write_json_atomic(args.result_json.resolve(), summary)
        except OSError:
            pass
        print(f"采集未完成：{fatal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
