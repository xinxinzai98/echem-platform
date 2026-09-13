from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from .start_stop_collection import SSHWindowsTransport, ps_literal


LANBTS_SCHEMA_VERSION = 1
LANBTS_CHANNELS = tuple(range(1, 9))
LANBTS_ACTIVE_STATUSES = frozenset({"charging", "discharging"})
DEFAULT_POLL_SECONDS = 30
DEFAULT_STALE_SECONDS = 180
MAX_STATE_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 512 * 1024
MAX_RUN_CONFIGS = 4096
_RUN_ID = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ROOT = re.compile(r"^[A-Za-z]:\\")
_STEP_CURRENT = re.compile(
    r"恒流(?P<mode>充电|放电)\s*:(?P<current>\d+(?:\.\d+)?)\s*mA",
    re.IGNORECASE,
)
_DATA_FILE = re.compile(
    r"^\d+_(?P<cell>[^_]+)_(?P<project>.+)_COM\d+_\d+_"
    r"(?P<channel>[1-8])_(?P<started>\d{14})\.bts$",
    re.IGNORECASE,
)


class LanbtsConfigConflict(ValueError):
    """Raised when a channel configuration revision or run binding is stale."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_text(value: dt.datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _parse_utc(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_text(value: Any, *, maximum: int, label: str) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    if len(text) > maximum or any(ord(character) < 32 for character in text):
        raise ValueError(f"{label}无效或过长")
    return text


def _atomic_json_write(path: Path, payload: Mapping[str, Any], *, maximum: int) -> None:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(data) > maximum:
        raise ValueError("蓝博状态或配置超过安全上限")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class LanbtsStateFile:
    def __init__(self, path: str | Path | None) -> None:
        raw = str(path or "").strip()
        self.path = Path(raw).expanduser().absolute() if raw else None

    def read(self) -> dict[str, Any] | None:
        path = self.path
        if path is None:
            return None
        try:
            metadata = path.stat()
            if (
                not path.is_file()
                or path.is_symlink()
                or metadata.st_size > MAX_STATE_BYTES
            ):
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != LANBTS_SCHEMA_VERSION:
            return None
        channels = payload.get("channels")
        if not isinstance(channels, list) or len(channels) != len(LANBTS_CHANNELS):
            return None
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        if self.path is not None:
            _atomic_json_write(self.path, payload, maximum=MAX_STATE_BYTES)


class LanbtsRunConfigStore:
    """Persist material and area by immutable run id, not by physical channel."""

    def __init__(self, path: str | Path | None) -> None:
        raw = str(path or "").strip()
        self.path = Path(raw).expanduser().absolute() if raw else None
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": LANBTS_SCHEMA_VERSION,
            "revision": 0,
            "updated_at_utc": "",
            "runs": {},
        }

    def read(self) -> dict[str, Any]:
        path = self.path
        if path is None:
            return self._empty()
        try:
            metadata = path.stat()
            if (
                not path.is_file()
                or path.is_symlink()
                or metadata.st_size > MAX_CONFIG_BYTES
            ):
                return self._empty()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._empty()
        if not isinstance(payload, dict):
            return self._empty()
        runs = payload.get("runs")
        if (
            payload.get("schema_version") != LANBTS_SCHEMA_VERSION
            or not isinstance(runs, dict)
        ):
            return self._empty()
        clean_runs: dict[str, dict[str, Any]] = {}
        for run_id, item in runs.items():
            if not _RUN_ID.fullmatch(str(run_id)) or not isinstance(item, Mapping):
                continue
            channel = _integer(item.get("channel"))
            if channel not in LANBTS_CHANNELS:
                continue
            area = _finite(item.get("electrode_area_cm2"))
            clean_runs[str(run_id)] = {
                "channel": channel,
                "source_file": str(item.get("source_file") or "")[:220],
                "material_name": str(item.get("material_name") or "")[:120],
                "electrode_area_cm2": (
                    area if area is not None and 0 < area <= 10_000 else None
                ),
                "notes": str(item.get("notes") or "")[:300],
                "updated_at_utc": str(item.get("updated_at_utc") or "")[:40],
            }
        return {
            "schema_version": LANBTS_SCHEMA_VERSION,
            "revision": max(0, _integer(payload.get("revision"))),
            "updated_at_utc": str(payload.get("updated_at_utc") or "")[:40],
            "runs": clean_runs,
        }

    def save(
        self,
        *,
        expected_revision: Any,
        channels: Any,
        current_runs: Mapping[int, Mapping[str, str]],
    ) -> dict[str, Any]:
        if self.path is None:
            raise ValueError("蓝博材料与面积配置文件不可用")
        if not isinstance(channels, list) or len(channels) != len(LANBTS_CHANNELS):
            raise ValueError("蓝博配置必须完整包含 1–8 通道")
        normalized: dict[int, dict[str, Any]] = {}
        for raw in channels:
            if not isinstance(raw, Mapping):
                raise ValueError("蓝博通道配置行无效")
            unknown = sorted(
                set(raw)
                - {
                    "channel",
                    "run_id",
                    "material_name",
                    "electrode_area_cm2",
                    "notes",
                }
            )
            if unknown:
                raise ValueError("蓝博通道配置包含未知字段：" + ", ".join(unknown))
            channel = _integer(raw.get("channel"), -1)
            if channel not in LANBTS_CHANNELS or channel in normalized:
                raise ValueError("蓝博通道编号缺失或重复")
            run_id = str(raw.get("run_id") or "").strip().casefold()
            if run_id and not _RUN_ID.fullmatch(run_id):
                raise ValueError("蓝博运行标识无效")
            material_name = _safe_text(
                raw.get("material_name"), maximum=120, label="材料名称"
            )
            notes = _safe_text(raw.get("notes"), maximum=300, label="备注")
            raw_area = raw.get("electrode_area_cm2")
            area = None if raw_area in (None, "") else _finite(raw_area)
            if area is not None and not (0 < area <= 10_000):
                raise ValueError("电极面积必须大于 0 且不超过 10000 cm²")
            normalized[channel] = {
                "run_id": run_id,
                "material_name": material_name,
                "electrode_area_cm2": area,
                "notes": notes,
            }
        if set(normalized) != set(LANBTS_CHANNELS):
            raise ValueError("蓝博配置必须完整包含 1–8 通道")

        with self._lock:
            current = self.read()
            if _integer(expected_revision, -1) != int(current["revision"]):
                raise LanbtsConfigConflict("配置已被其他页面更新，请刷新后重试。")
            runs = dict(current["runs"])
            now = _utc_text()
            for channel in LANBTS_CHANNELS:
                row = normalized[channel]
                current_run = current_runs.get(channel, {})
                expected_run_id = str(current_run.get("run_id") or "")
                if row["run_id"] != expected_run_id:
                    raise LanbtsConfigConflict(
                        f"通道 {channel} 已切换测试，请刷新页面后重新填写。"
                    )
                if not expected_run_id:
                    continue
                if not (
                    row["material_name"]
                    or row["electrode_area_cm2"] is not None
                    or row["notes"]
                ):
                    runs.pop(expected_run_id, None)
                    continue
                runs[expected_run_id] = {
                    "channel": channel,
                    "source_file": str(current_run.get("source_file") or "")[:220],
                    "material_name": row["material_name"],
                    "electrode_area_cm2": row["electrode_area_cm2"],
                    "notes": row["notes"],
                    "updated_at_utc": now,
                }
            if len(runs) > MAX_RUN_CONFIGS:
                ordered = sorted(
                    runs.items(),
                    key=lambda item: str(item[1].get("updated_at_utc") or ""),
                    reverse=True,
                )[:MAX_RUN_CONFIGS]
                runs = dict(ordered)
            saved = {
                "schema_version": LANBTS_SCHEMA_VERSION,
                "revision": int(current["revision"]) + 1,
                "updated_at_utc": now,
                "runs": runs,
            }
            _atomic_json_write(self.path, saved, maximum=MAX_CONFIG_BYTES)
            return saved


def load_lanbts_machine_config(path: str | Path | None) -> dict[str, Any] | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    source = Path(raw).expanduser().resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("蓝博固定监控配置无效")
    required = {
        "id",
        "name",
        "hostname",
        "ip",
        "user",
        "identity_file",
        "known_hosts_file",
        "installation_root",
        "data_root",
        "system_root",
        "device_id",
        "box_id",
    }
    if set(payload) != required | {"version"}:
        raise ValueError("蓝博固定监控配置字段不完整")
    result = {key: str(payload.get(key) or "").strip() for key in required}
    for key in ("id", "name", "hostname", "user", "device_id", "box_id"):
        if not result[key] or any(ord(character) < 32 for character in result[key]):
            raise ValueError("蓝博固定监控配置包含无效文本")
    try:
        import ipaddress

        ipaddress.ip_address(result["ip"])
    except ValueError as exc:
        raise ValueError("蓝博监控地址无效") from exc
    for key in ("installation_root", "data_root", "system_root"):
        if not _WINDOWS_ROOT.match(result[key]) or ".." in PureWindowsPath(
            result[key]
        ).parts:
            raise ValueError("蓝博 Windows 目录配置无效")
    if not result["device_id"].isdigit() or not result["box_id"].isdigit():
        raise ValueError("蓝博设备或箱号配置无效")
    return {"version": 1, **result}


def lanbts_probe_script(config: Mapping[str, Any]) -> str:
    """Return the fixed LANBTS read-only status/protocol probe.

    The only remote writes are two support-file copies under the SSH user's
    Windows temporary directory. Instrument data, plans, configuration and
    control processes are never modified.
    """

    install = ps_literal(str(config["installation_root"]))
    data_root = ps_literal(str(config["data_root"]))
    system_root = ps_literal(str(config["system_root"]))
    device_id = ps_literal(str(config["device_id"]))
    box_id = ps_literal(str(config["box_id"]))
    return rf"""
$ProgressPreference='SilentlyContinue'
$InformationPreference='SilentlyContinue'
$WarningPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$install={install}
$dataRoot={data_root}
$systemRoot={system_root}
$deviceId={device_id}
$boxId={box_id}
$readMutex=New-Object Threading.Mutex($false,'Local\StartStopLanbtsReadOnlyV1')
$lockAcquired=$false
try{{$lockAcquired=$readMutex.WaitOne(5000)}}
catch [Threading.AbandonedMutexException]{{$lockAcquired=$true}}
if(-not $lockAcquired){{throw 'lanbts_read_mutex_timeout'}}
$probeRoot=Join-Path $env:LOCALAPPDATA 'Temp\start-stop-lanbts-readonly-v1'
$probeSystem=Join-Path $probeRoot 'LANBTSSystem'
$coreSource=Join-Path $install 'lib\LanBts.Core.dll'
$coreTarget=Join-Path $probeRoot 'LanBts.Core.dll'
$configSource=Join-Path $systemRoot 'Config.ini'
$configTarget=Join-Path $probeSystem 'Config.ini'
if(-not (Test-Path -LiteralPath $probeSystem -PathType Container)){{
  New-Item -ItemType Directory -Path $probeSystem -Force | Out-Null
}}
if(-not (Test-Path -LiteralPath $coreTarget -PathType Leaf) -or
   (Get-Item -LiteralPath $coreTarget).Length -ne (Get-Item -LiteralPath $coreSource).Length){{
  Copy-Item -LiteralPath $coreSource -Destination $coreTarget -Force
}}
if(-not (Test-Path -LiteralPath $configTarget -PathType Leaf) -or
   (Get-Item -LiteralPath $configTarget).Length -ne (Get-Item -LiteralPath $configSource).Length){{
  Copy-Item -LiteralPath $configSource -Destination $configTarget -Force
}}
Set-Location $probeRoot
foreach($name in @(
  'log4net.dll','Felix.Common.dll','Microsoft.Practices.ServiceLocation.dll',
  'Microsoft.Practices.Unity.dll',
  'Microsoft.Practices.Unity.RegistrationByConvention.dll',
  'LanBts.Resource.Globalization.dll'
)){{
  [void][Reflection.Assembly]::LoadFrom((Join-Path $install ('lib\'+$name)))
}}
[void][Reflection.Assembly]::LoadFrom($coreTarget)
foreach($name in @('Felix.Driver.dll','Felix.Utility.dll','LanBts.Extensions.dll')){{
  [void][Reflection.Assembly]::LoadFrom((Join-Path $install ('lib\'+$name)))
}}
[void][Reflection.Assembly]::LoadFrom((Join-Path $install 'LanbtsDDA.exe'))
$software=Get-Item -LiteralPath (Join-Path $install 'LANBTS.exe')
$process=@(Get-Process -Name LANBTS -ErrorAction SilentlyContinue)
$statusPattern='^'+[regex]::Escape($deviceId)+'_'+[regex]::Escape($boxId)+'_(?<channel>[1-8])\.lin$'
$channels=New-Object System.Collections.Generic.List[object]
$activePaths=@{{}}
$statusSnapshotRoot=Join-Path $probeRoot 'status-snapshots'
if(-not (Test-Path -LiteralPath $statusSnapshotRoot -PathType Container)){{
  New-Item -ItemType Directory -Path $statusSnapshotRoot -Force | Out-Null
}}
foreach($line in @(Get-ChildItem -LiteralPath $systemRoot -File -Filter ($deviceId+'_'+$boxId+'_*.lin') -ErrorAction SilentlyContinue)){{
  $match=[regex]::Match($line.Name,$statusPattern)
  if(-not $match.Success){{continue}}
  $snapshotPath=Join-Path $statusSnapshotRoot ([Guid]::NewGuid().ToString('N')+'.lin')
  try{{
    $before=Get-Item -LiteralPath $line.FullName -ErrorAction Stop
    $source=$null
    $target=$null
    try{{
      $share=[IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
      $source=[IO.File]::Open($line.FullName,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share)
      $target=[IO.File]::Open($snapshotPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
      $source.CopyTo($target)
      $target.Flush()
    }}finally{{
      if($null -ne $target){{$target.Dispose()}}
      if($null -ne $source){{$source.Dispose()}}
    }}
    $after=Get-Item -LiteralPath $line.FullName -ErrorAction Stop
    if($before.Length -ne $after.Length -or $before.LastWriteTimeUtc.Ticks -ne $after.LastWriteTimeUtc.Ticks){{
      throw 'status_changed_during_snapshot'
    }}
    $status=[LanBts.Core.Services.BatteryExtensions]::LoadBatteryStatusData($snapshotPath)
    $dataPath=[string]$status.TestDataFilePath
    if($dataPath){{$activePaths[$dataPath.ToLowerInvariant()]=$true}}
    $dataFile=$null
    if($dataPath){{$dataFile=Get-Item -LiteralPath $dataPath -ErrorAction SilentlyContinue}}
    [void]$channels.Add([pscustomobject][ordered]@{{
      channel=[int]$match.Groups['channel'].Value
      state=[string]$status.State
      last_state=[string]$status.LastState
      voltage_v=[double]$status.Voltage
      current_ma=[double]$status.Electricity
      target_ma=[double]$status.PrimaryParam
      step_no=[int]$status.StepNo
      loop_count=[int64]$status.ProcessLoopCount
      elapsed_s=[double]$status.TotalTimeSpan.TotalSeconds
      step_elapsed_s=[double]$status.StepSpan.TotalSeconds
      test_start_local=$status.TestStartTime.ToString('o',[Globalization.CultureInfo]::InvariantCulture)
      data_timestamp_local=$status.DataTimestamp.ToString('o',[Globalization.CultureInfo]::InvariantCulture)
      data_path=$dataPath
      data_file=if($dataPath){{[IO.Path]::GetFileName($dataPath)}}else{{''}}
      data_size_bytes=if($null -ne $dataFile){{[int64]$dataFile.Length}}else{{0}}
      data_last_write_utc=if($null -ne $dataFile){{$dataFile.LastWriteTimeUtc.ToString('o',[Globalization.CultureInfo]::InvariantCulture)}}else{{''}}
      plan_file=if($status.TestProcessFilePath){{[IO.Path]::GetFileName([string]$status.TestProcessFilePath)}}else{{''}}
      processes=@()
    }})
  }}catch{{
    [void]$channels.Add([pscustomobject][ordered]@{{
      channel=[int]$match.Groups['channel'].Value
      error='status_decode_failed'
    }})
  }}finally{{
    if([IO.File]::Exists($snapshotPath)){{[IO.File]::Delete($snapshotPath)}}
  }}
}}
$files=@(Get-ChildItem -LiteralPath $dataRoot -Recurse -File -Filter *.bts -ErrorAction SilentlyContinue)
$activeCount=0
foreach($file in $files){{if($activePaths.ContainsKey($file.FullName.ToLowerInvariant())){{$activeCount++}}}}
$payload=[pscustomobject][ordered]@{{
  ok=$true
  checked_at_utc=[DateTime]::UtcNow.ToString('o',[Globalization.CultureInfo]::InvariantCulture)
  computer_name=[Environment]::MachineName
  software_running=($process.Count -gt 0)
  software_version=[string]$software.VersionInfo.FileVersion
  channels=@($channels.ToArray())
  files=[pscustomobject][ordered]@{{
    total=$files.Count
    total_bytes=[int64](($files|Measure-Object Length -Sum).Sum)
    active=$activeCount
    inactive=[math]::Max(0,$files.Count-$activeCount)
  }}
}}
$json=$payload|ConvertTo-Json -Compress -Depth 9
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
[Console]::Out.Flush()
[Environment]::Exit(0)
"""


def _duration_seconds(value: Any) -> float | None:
    text = str(value or "")
    match = re.search(r"(?:≥|>=)\s*(?P<duration>\d+(?:\.\d+)?(?::\d+){1,2})", text)
    if not match:
        return None
    token = match.group("duration")
    days = 0
    if "." in token.split(":", 1)[0]:
        day_text, token = token.split(".", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    parts = token.split(":")
    try:
        if len(parts) == 2:
            hours = 0
            minutes, seconds = (float(part) for part in parts)
        elif len(parts) == 3:
            hours, minutes, seconds = (float(part) for part in parts)
        else:
            return None
    except ValueError:
        return None
    return days * 86400.0 + hours * 3600.0 + minutes * 60.0 + seconds


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "时长未识别"
    if seconds >= 86400:
        days = int(seconds // 86400)
        remainder = seconds - days * 86400
        hours = int(remainder // 3600)
        minutes = int((remainder % 3600) // 60)
        parts = [f"{days} d"]
        if hours:
            parts.append(f"{hours} h")
        if minutes:
            parts.append(f"{minutes} min")
        return " ".join(parts)
    if seconds >= 3600 and math.isclose(seconds % 3600, 0.0, abs_tol=0.1):
        return f"{seconds / 3600:g} h"
    if seconds >= 60 and math.isclose(seconds % 60, 0.0, abs_tol=0.1):
        return f"{seconds / 60:g} min"
    return f"{seconds:g} s"


def _protocol(definitions: Any) -> dict[str, Any]:
    rows = [dict(item) for item in definitions if isinstance(item, Mapping)] if isinstance(definitions, list) else []
    constant_steps: list[dict[str, Any]] = []
    for raw in rows:
        step_name = str(raw.get("stepname") or "")
        match = _STEP_CURRENT.search(step_name)
        if not match:
            continue
        magnitude = float(match.group("current"))
        mode = "charge" if match.group("mode") == "充电" else "discharge"
        signed_current = magnitude if mode == "charge" else -magnitude
        duration = _duration_seconds(raw.get("finishconditon1"))
        constant_steps.append(
            {
                "order": len(constant_steps) + 1,
                "mode": mode,
                "mode_label": "恒流充电" if mode == "charge" else "恒流放电",
                "current_ma": signed_current,
                "duration_s": duration,
                "duration_label": _format_duration(duration),
            }
        )
    charge = [item for item in constant_steps if item["mode"] == "charge"]
    discharge = [item for item in constant_steps if item["mode"] == "discharge"]
    if charge and discharge:
        kind = "bipolar"
        category = "反向启停"
        if len(constant_steps) > 2:
            precondition = constant_steps[:-2]
            cycle = constant_steps[-2:]
            prefix = " → ".join(
                f"{item['current_ma']:+g} mA {item['duration_label']}"
                for item in precondition
            )
            suffix = " ↔ ".join(
                f"{item['current_ma']:+g} mA {item['duration_label']}"
                for item in cycle
            )
            label = f"预处理 {prefix}；循环 {suffix}"
        else:
            label = " ↔ ".join(
                f"{item['current_ma']:+g} mA {item['duration_label']}"
                for item in constant_steps
            )
    elif len(discharge) > 1:
        kind = "cathodic_multilevel"
        category = "同向负载循环"
        label = " ↔ ".join(
            f"{item['current_ma']:+g} mA {item['duration_label']}"
            for item in discharge
        )
    elif len(discharge) == 1:
        kind = "constant_current"
        category = "恒流长时运行"
        item = discharge[0]
        label = f"{item['current_ma']:+g} mA · {item['duration_label']}"
    else:
        kind = "unknown"
        category = "方案待识别"
        label = "未读取到恒流工步"
    key = "|".join(
        f"{item['mode']}:{item['current_ma']:g}:{item['duration_s']}"
        for item in constant_steps
    )
    return {
        "kind": kind,
        "category": category,
        "label": label.replace("+", "+").replace("-", "−"),
        "key": hashlib.sha256(key.encode("utf-8")).hexdigest() if key else "",
        "steps": constant_steps,
        "source": "bts_embedded_process",
    }


def _material_hint(data_file: str) -> str:
    match = _DATA_FILE.fullmatch(data_file)
    if not match:
        return PureWindowsPath(data_file).stem[:120]
    cell = match.group("cell")
    project = match.group("project")
    return f"{cell} · {project}"[:120]


def _run_id(machine_id: str, channel: int, data_path: str, started: str) -> str:
    identity = "\0".join((machine_id, str(channel), data_path, started))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class LanbtsMonitor:
    """Poll one LANBTS eight-channel controller and publish a safe cache."""

    def __init__(
        self,
        state_file: str | Path | None,
        *,
        active_probe: bool = False,
        machine_config: Mapping[str, Any] | None = None,
        config_store: LanbtsRunConfigStore | None = None,
        transport_factory: Any = SSHWindowsTransport,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
    ) -> None:
        self.state_file = LanbtsStateFile(state_file)
        self.active_probe = bool(active_probe)
        self.machine_config = dict(machine_config or {})
        self.config_store = config_store
        self.transport_factory = transport_factory
        self.poll_seconds = max(15, min(int(poll_seconds), 600))
        self.stale_seconds = max(
            self.poll_seconds * 2,
            min(int(stale_seconds), 3600),
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_lock = threading.RLock()

    def start(self) -> None:
        if not self.active_probe or self._thread is not None:
            return
        if not self.machine_config:
            raise ValueError("蓝博八通道监控缺少固定设备配置")
        self._thread = threading.Thread(
            target=self._run_loop,
            name="start-stop-lanbts-monitor",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=min(5.0, float(self.poll_seconds)))
        self._thread = None

    @staticmethod
    def _scope() -> dict[str, Any]:
        return {
            "monitoring_mode": "server_cached_read_only",
            "instrument_control_performed": False,
            "instrument_data_modified": False,
            "raw_records_read_during_poll": False,
            "status_files_read": True,
            "bts_embedded_process_read": False,
            "protocol_source": "cached_embedded_process_or_stable_import",
            "support_cache_location": "windows_user_temp",
            "voltage_reference": "unconfirmed",
        }

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": LANBTS_SCHEMA_VERSION,
            "status": "initializing" if self.active_probe else "unavailable",
            "message": (
                "正在进行首次蓝博八通道只读检查。"
                if self.active_probe
                else "蓝博八通道监控缓存尚未生成。"
            ),
            "generated_at_utc": "",
            "cache_age_seconds": None,
            "poll_seconds": self.poll_seconds,
            "device": {
                "name": "蓝博八通道",
                "hostname": "",
                "ip": "",
                "software_version": "",
                "software_running": False,
            },
            "counts": {
                "channels_total": 8,
                "channels_running": 0,
                "channels_charging": 0,
                "channels_discharging": 0,
                "channels_completed": 0,
                "channels_idle": 8,
                "channels_attention": 0,
                "channels_configured": 0,
                "channels_area_missing": 0,
            },
            "files": {"total": 0, "total_bytes": 0, "active": 0, "inactive": 0},
            "configuration": {"revision": 0, "updated_at_utc": ""},
            "channels": [self._idle_channel(channel) for channel in LANBTS_CHANNELS],
            "scope": self._scope(),
        }

    @staticmethod
    def _idle_channel(channel: int) -> dict[str, Any]:
        return {
            "channel": channel,
            "status": "idle",
            "status_label": "空置",
            "run_id": "",
            "data_file": "",
            "material_hint": "",
            "display_name": f"通道 {channel} · 等待新测试",
            "state": "",
            "state_label": "空置",
            "voltage_v": None,
            "voltage_label": "LANBTS 测得电压（参照未确认）",
            "current_ma": None,
            "target_ma": None,
            "current_density_ma_cm2": None,
            "loop_count": 0,
            "step_no": 0,
            "elapsed_s": 0.0,
            "step_elapsed_s": 0.0,
            "test_start_local": "",
            "data_timestamp_local": "",
            "protocol": _protocol([]),
            "configuration": {
                "material_name": "",
                "electrode_area_cm2": None,
                "notes": "",
                "complete": False,
            },
            "warnings": [],
        }

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            payload = copy.deepcopy(self._snapshot)
        if payload is None:
            payload = self.state_file.read()
        if payload is None:
            return self._empty()
        generated = _parse_utc(payload.get("generated_at_utc"))
        age = None
        if generated is not None:
            age = max(0, int((_utc_now() - generated).total_seconds()))
        payload["cache_age_seconds"] = age
        if age is not None and age > self.stale_seconds:
            payload["status"] = "stale"
            payload["message"] = "蓝博监控缓存已过期，等待下一次只读检查。"
        return payload

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._probe_once()
            except Exception:
                self._publish_failure()
            elapsed = time.monotonic() - started
            self._stop.wait(max(1.0, self.poll_seconds - elapsed))

    def _publish_failure(self) -> None:
        with self._snapshot_lock:
            previous = copy.deepcopy(self._snapshot)
        if previous is None:
            previous = self.state_file.read()
        if previous is None:
            previous = self._empty()
        previous.update(
            {
                "status": "unavailable",
                "message": "蓝博仪器当前不可达，继续显示上一次可用结果。",
                "last_attempt_at_utc": _utc_text(),
            }
        )
        with self._snapshot_lock:
            self._snapshot = previous

    @staticmethod
    def _state_label(value: str) -> str:
        return {
            "ConstElectricityCharge": "恒流充电",
            "ConstElectricityDischarge": "恒流放电",
            "Pause": "暂停",
            "Stop": "测试完成",
            "Stopped": "测试完成",
            "Finish": "测试完成",
            "Finished": "测试完成",
            "Complete": "测试完成",
            "Completed": "测试完成",
        }.get(value, value or "状态未识别")

    @classmethod
    def _channel_status(cls, value: str) -> tuple[str, str]:
        """Normalize the instrument state without conflating active phases."""

        raw = str(value or "").strip()
        normalized = raw.casefold()
        terminal = any(
            token in normalized for token in ("finish", "complete", "stopped")
        ) or normalized == "stop"
        if terminal:
            return "completed", "测试完成"
        if "discharge" in normalized:
            return "discharging", "放电"
        if "charge" in normalized:
            return "charging", "充电"
        return "attention", cls._state_label(raw)

    def _normalize_channel(
        self,
        raw: Mapping[str, Any],
        *,
        machine_id: str,
    ) -> dict[str, Any]:
        channel = _integer(raw.get("channel"), -1)
        if channel not in LANBTS_CHANNELS:
            raise ValueError("蓝博回应包含无效通道")
        if raw.get("error"):
            row = self._idle_channel(channel)
            row.update(
                {
                    "status": "attention",
                    "status_label": "读取失败",
                    "warnings": ["当前通道状态文件无法解码"],
                }
            )
            return row
        data_file = PureWindowsPath(str(raw.get("data_file") or "")).name[:220]
        data_path = str(raw.get("data_path") or "")
        started = str(raw.get("test_start_local") or "")[:40]
        run_id = _run_id(machine_id, channel, data_path, started)
        state = str(raw.get("state") or "")[:80]
        status, status_label = self._channel_status(state)
        current = _finite(raw.get("current_ma"))
        voltage = _finite(raw.get("voltage_v"))
        target = _finite(raw.get("target_ma"))
        protocol = _protocol(raw.get("processes"))
        warnings = ["电压参照尚未确认，不能标为 Hg/HgO"]
        if protocol["kind"] == "cathodic_multilevel":
            warnings.append("同向负载循环不能与电流符号反转启停混合比较")
        if status == "attention":
            warnings.append(f"当前仪器状态未归类：{self._state_label(state)}")
        return {
            "channel": channel,
            "status": status,
            "status_label": status_label,
            "run_id": run_id,
            "data_file": data_file,
            "material_hint": _material_hint(data_file),
            "display_name": _material_hint(data_file) or f"通道 {channel} 待命名",
            "state": state,
            "state_label": self._state_label(state),
            "voltage_v": voltage,
            "voltage_label": "LANBTS 测得电压（参照未确认）",
            "current_ma": current,
            "target_ma": target,
            "current_density_ma_cm2": None,
            "loop_count": max(0, _integer(raw.get("loop_count"))),
            "step_no": max(0, _integer(raw.get("step_no"))),
            "elapsed_s": max(0.0, _finite(raw.get("elapsed_s"), 0.0) or 0.0),
            "step_elapsed_s": max(
                0.0,
                _finite(raw.get("step_elapsed_s"), 0.0) or 0.0,
            ),
            "test_start_local": started,
            "data_timestamp_local": str(raw.get("data_timestamp_local") or "")[:40],
            "data_size_bytes": max(0, _integer(raw.get("data_size_bytes"))),
            "data_last_write_utc": str(raw.get("data_last_write_utc") or "")[:40],
            "plan_file": PureWindowsPath(str(raw.get("plan_file") or "")).name[:180],
            "protocol": protocol,
            "configuration": {
                "material_name": "",
                "electrode_area_cm2": None,
                "notes": "",
                "complete": False,
            },
            "warnings": warnings,
        }

    def _apply_configuration(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config_store.read() if self.config_store is not None else None
        if config is None:
            config = {
                "revision": _integer(payload.get("configuration", {}).get("revision")),
                "updated_at_utc": str(
                    payload.get("configuration", {}).get("updated_at_utc") or ""
                ),
                "runs": {},
            }
        runs = config.get("runs") if isinstance(config.get("runs"), dict) else {}
        configured = 0
        area_missing = 0
        for channel in payload["channels"]:
            channel["warnings"] = [
                warning
                for warning in channel.get("warnings", [])
                if warning != "未填写电极面积，暂不计算电流密度"
            ]
            run_id = str(channel.get("run_id") or "")
            item = runs.get(run_id) if run_id else None
            if not isinstance(item, Mapping):
                item = {}
            material_name = str(item.get("material_name") or "")[:120]
            area = _finite(item.get("electrode_area_cm2"))
            if area is not None and not (0 < area <= 10_000):
                area = None
            notes = str(item.get("notes") or "")[:300]
            complete = bool(material_name and area is not None)
            channel["configuration"] = {
                "material_name": material_name,
                "electrode_area_cm2": area,
                "notes": notes,
                "complete": complete,
            }
            if material_name:
                channel["display_name"] = material_name
            active = channel["status"] in LANBTS_ACTIVE_STATUSES
            if active or channel["status"] == "completed":
                if area is None:
                    if active:
                        area_missing += 1
                    if active and "未填写电极面积，暂不计算电流密度" not in channel["warnings"]:
                        channel["warnings"].append(
                            "未填写电极面积，暂不计算电流密度"
                        )
                else:
                    current = _finite(channel.get("current_ma"))
                    channel["current_density_ma_cm2"] = (
                        current / area if current is not None else None
                    )
                if complete:
                    configured += 1
        payload["configuration"] = {
            "revision": max(0, _integer(config.get("revision"))),
            "updated_at_utc": str(config.get("updated_at_utc") or "")[:40],
        }
        payload["counts"]["channels_configured"] = configured
        payload["counts"]["channels_area_missing"] = area_missing
        return payload

    def _probe_once(self) -> dict[str, Any]:
        if not self.machine_config:
            raise ValueError("蓝博固定监控配置不可用")
        known_hosts = self.machine_config.get("known_hosts_file")
        transport = self.transport_factory(
            known_hosts_file=(Path(str(known_hosts)).expanduser() if known_hosts else None)
        )
        script = lanbts_probe_script(self.machine_config)
        stdin_runner = getattr(transport, "run_stdin_payload", None)
        raw = (
            stdin_runner(self.machine_config, script, timeout=20)
            if callable(stdin_runner)
            else transport._run_payload(
                self.machine_config,
                script,
                timeout=20,
            )
        )
        if raw.get("ok") is not True:
            raise ValueError("蓝博只读回应无效")
        observed: dict[int, dict[str, Any]] = {}
        with self._snapshot_lock:
            previous = copy.deepcopy(self._snapshot)
        if previous is None:
            previous = self.state_file.read()
        previous_by_run = {
            str(item.get("run_id") or ""): item
            for item in (
                previous.get("channels", [])
                if isinstance(previous, Mapping)
                else []
            )
            if isinstance(item, Mapping) and str(item.get("run_id") or "")
        }
        machine_id = str(self.machine_config.get("id") or "lanbts")
        for item in raw.get("channels", []):
            if not isinstance(item, Mapping):
                continue
            channel = self._normalize_channel(item, machine_id=machine_id)
            if channel.get("protocol", {}).get("kind") == "unknown":
                cached = previous_by_run.get(str(channel.get("run_id") or ""))
                cached_protocol = (
                    cached.get("protocol")
                    if isinstance(cached, Mapping)
                    else None
                )
                if (
                    isinstance(cached_protocol, Mapping)
                    and cached_protocol.get("kind") != "unknown"
                ):
                    channel["protocol"] = copy.deepcopy(cached_protocol)
            observed[channel["channel"]] = channel
        channels = [
            observed.get(channel, self._idle_channel(channel))
            for channel in LANBTS_CHANNELS
        ]
        charging = sum(1 for item in channels if item["status"] == "charging")
        discharging = sum(1 for item in channels if item["status"] == "discharging")
        completed = sum(1 for item in channels if item["status"] == "completed")
        idle = sum(1 for item in channels if item["status"] == "idle")
        running = charging + discharging
        attention = sum(1 for item in channels if item["status"] == "attention")
        files = raw.get("files") if isinstance(raw.get("files"), Mapping) else {}
        payload = {
            "schema_version": LANBTS_SCHEMA_VERSION,
            "status": "ready" if raw.get("software_running") else "partial",
            "message": (
                "蓝博八通道只读状态已更新。"
                if raw.get("software_running")
                else "蓝博软件未运行，显示最近可读取状态。"
            ),
            "generated_at_utc": str(raw.get("checked_at_utc") or _utc_text()),
            "last_attempt_at_utc": _utc_text(),
            "poll_seconds": self.poll_seconds,
            "device": {
                "name": str(self.machine_config.get("name") or "蓝博八通道")[:80],
                "hostname": str(raw.get("computer_name") or self.machine_config.get("hostname") or "")[:80],
                "ip": str(self.machine_config.get("ip") or "")[:64],
                "software_version": str(raw.get("software_version") or "")[:40],
                "software_running": raw.get("software_running") is True,
            },
            "counts": {
                "channels_total": 8,
                "channels_running": running,
                "channels_charging": charging,
                "channels_discharging": discharging,
                "channels_completed": completed,
                "channels_idle": idle,
                "channels_attention": attention,
                "channels_configured": 0,
                "channels_area_missing": 0,
            },
            "files": {
                "total": max(0, _integer(files.get("total"))),
                "total_bytes": max(0, _integer(files.get("total_bytes"))),
                "active": max(0, _integer(files.get("active"))),
                "inactive": max(0, _integer(files.get("inactive"))),
            },
            "configuration": {"revision": 0, "updated_at_utc": ""},
            "channels": channels,
            "scope": self._scope(),
        }
        payload = self._apply_configuration(payload)
        self.state_file.write(payload)
        with self._snapshot_lock:
            self._snapshot = copy.deepcopy(payload)
        return payload

    def save_config(self, *, expected_revision: Any, channels: Any) -> dict[str, Any]:
        if not self.active_probe or self.config_store is None:
            raise ValueError("蓝博材料与面积配置仅可在服务器本机修改")
        with self._snapshot_lock:
            current = copy.deepcopy(self._snapshot)
        if current is None:
            current = self.state_file.read()
        if current is None:
            raise LanbtsConfigConflict("蓝博状态尚未生成，请稍后刷新。")
        current_runs = {
            int(item["channel"]): {
                "run_id": str(item.get("run_id") or ""),
                "source_file": str(item.get("data_file") or ""),
            }
            for item in current.get("channels", [])
            if isinstance(item, Mapping)
            and _integer(item.get("channel"), -1) in LANBTS_CHANNELS
        }
        self.config_store.save(
            expected_revision=expected_revision,
            channels=channels,
            current_runs=current_runs,
        )
        updated = self._apply_configuration(current)
        self.state_file.write(updated)
        with self._snapshot_lock:
            self._snapshot = copy.deepcopy(updated)
        return {
            "revision": updated["configuration"]["revision"],
            "updated_at_utc": updated["configuration"]["updated_at_utc"],
            "channels": [
                {
                    "channel": item["channel"],
                    "run_id": item["run_id"],
                    **item["configuration"],
                }
                for item in updated["channels"]
            ],
        }


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "LanbtsConfigConflict",
    "LanbtsMonitor",
    "LanbtsRunConfigStore",
    "LanbtsStateFile",
    "lanbts_probe_script",
    "load_lanbts_machine_config",
]
