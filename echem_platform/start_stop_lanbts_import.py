from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

from .start_stop_collection import (
    SSHWindowsTransport,
    PlannedFile,
    iso_utc,
    make_planned_files,
    ps_literal,
)
from .start_stop_database import StartStopDatabase
from .start_stop_lanbts import (
    LanbtsRunConfigStore,
    _material_hint,
    _protocol,
    load_lanbts_machine_config,
)


EXTRACTOR_VERSION = "lanbts-normalized-records/2-full"
CLASSIFIER_VERSION = "lanbts-stability-classifier/1"
DERIVED_SOURCE_SUFFIX = "::normalized-stability-v1"
LANBTS_MACHINE_ID = "04_蓝博八通道_AGHID-P_192.168.110.144"
LANBTS_ROOT_LABEL = "D盘_LANBTS_Data"
DEFAULT_SETTLE_SECONDS = 300
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
REPEATED_TIMEOUT_THRESHOLD = 2
REPEATED_TIMEOUT_BACKOFF_SECONDS = 24 * 60 * 60
META_PREFIX = b"__START_STOP_LANBTS_META__"
EXPECTED_COLUMNS = (
    "time_s",
    "potential_v",
    "current_ma",
    "current_density_ma_cm2",
    "cycle_id",
    "step_id",
    "step_name",
    "absolute_time",
    "record_id",
    "voltage_raw_uv",
    "current_raw_ua",
    "temperature_c",
)


def _json_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _extract_timeout_seconds(size_bytes: int) -> int:
    size_mib = max(1, math.ceil(max(0, int(size_bytes)) / (1024 * 1024)))
    return max(90, min(1800, 30 + size_mib * 15))


def _utc_datetime(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _repeated_timeout_retry_after(
    raw_info: Mapping[str, Any],
    issues: Iterable[Mapping[str, Any]],
    *,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    source_created = _utc_datetime(raw_info.get("created_utc"))
    matching: list[dt.datetime] = []
    for issue in issues:
        if "数据解析超时" not in str(issue.get("message") or ""):
            continue
        created = _utc_datetime(issue.get("created_utc"))
        if created is None or (source_created is not None and created < source_created):
            continue
        matching.append(created)
    if len(matching) < REPEATED_TIMEOUT_THRESHOLD:
        return None
    retry_after = max(matching) + dt.timedelta(
        seconds=REPEATED_TIMEOUT_BACKOFF_SECONDS
    )
    current = now or dt.datetime.now(dt.timezone.utc)
    return retry_after if current < retry_after else None


def _write_progress(path: Path | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _repository_path(machine_id: str, root_label: str, relative: str) -> str:
    parts = PureWindowsPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("蓝博相对文件路径无效")
    return PurePosixPath(machine_id, root_label, *parts).as_posix()


def _derived_repository_path(
    machine_id: str,
    root_label: str,
    relative: str,
) -> str:
    source = PureWindowsPath(relative)
    filename = source.name + ".normalized.csv"
    return PurePosixPath(
        machine_id,
        root_label,
        "_标准化稳定性数据",
        *source.parent.parts,
        filename,
    ).as_posix()


def _decode_export_metadata(stderr: bytes) -> dict[str, Any]:
    candidates = [
        line[len(META_PREFIX) :].strip()
        for line in stderr.splitlines()
        if line.startswith(META_PREFIX)
    ]
    if not candidates:
        raise ValueError("蓝博记录解析未返回校验信息")
    try:
        payload = json.loads(base64.b64decode(candidates[-1], validate=True))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("蓝博记录解析校验信息无效") from exc
    if not isinstance(payload, dict):
        raise ValueError("蓝博记录解析校验信息无效")
    return payload


def _area_literal(area_cm2: float | None) -> str:
    if area_cm2 is None:
        return "$null"
    if not math.isfinite(area_cm2) or not 0 < area_cm2 <= 10_000:
        raise ValueError("蓝博电极面积无效")
    return format(area_cm2, ".17g")


def lanbts_record_export_script(
    config: Mapping[str, Any],
    *,
    data_path: str,
    expected_size: int,
    expected_ticks: int,
    area_cm2: float | None,
) -> str:
    """Build the fixed remote read-only BTS-to-CSV extraction script."""

    install = ps_literal(str(config["installation_root"]))
    system_root = ps_literal(str(config["system_root"]))
    path = ps_literal(str(data_path))
    area = _area_literal(area_cm2)
    return rf"""
$ProgressPreference='SilentlyContinue'
$InformationPreference='SilentlyContinue'
$WarningPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$install={install}
$systemRoot={system_root}
$dataPath={path}
$expectedSize=[int64]{int(expected_size)}
$expectedTicks=[int64]{int(expected_ticks)}
$areaCm2={area}
$readMutex=New-Object Threading.Mutex($false,'Local\StartStopLanbtsReadOnlyV1')
$lockAcquired=$false
try{{$lockAcquired=$readMutex.WaitOne(30000)}}
catch [Threading.AbandonedMutexException]{{$lockAcquired=$true}}
if(-not $lockAcquired){{throw 'lanbts_read_mutex_timeout'}}
$probeRoot=Join-Path $env:LOCALAPPDATA 'Temp\start-stop-lanbts-import-v1'
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
$before=Get-Item -LiteralPath $dataPath -ErrorAction Stop
if([int64]$before.Length -ne $expectedSize -or [int64]$before.LastWriteTimeUtc.Ticks -ne $expectedTicks){{
  throw 'source_changed_before_extract'
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
[void][Reflection.Assembly]::LoadFrom((Join-Path $install 'lib\ReadDataFile.dll'))
$reader=New-Object ReadDataFile.ReadBtsFile
$watch=[Diagnostics.Stopwatch]::StartNew()
$cycles=@($reader.GetCycleData($dataPath))
[int64]$stepCount=0
$process=@(
  $reader.GetProcessData($dataPath) |
  Select-Object stepnum,stepname,finishconditon1,finishconditon2,jump,savecondition
)
$culture=[Globalization.CultureInfo]::InvariantCulture
$exportPath=Join-Path $probeRoot ('records-'+$PID+'.csv')
if([IO.File]::Exists($exportPath)){{[IO.File]::Delete($exportPath)}}
function Parse-Number([object]$value){{
  [double]$parsed=0
  if([double]::TryParse([string]$value,[Globalization.NumberStyles]::Float,$culture,[ref]$parsed)){{return $parsed}}
  if([double]::TryParse([string]$value,[ref]$parsed)){{return $parsed}}
  return [double]::NaN
}}
function Format-Number([double]$value){{
  if([double]::IsNaN($value) -or [double]::IsInfinity($value)){{return ''}}
  return $value.ToString('R',$culture)
}}
function Csv-Text([object]$value){{
  $text=[string]$value
  return '"'+$text.Replace('"','""')+'"'
}}
function Parse-Time([object]$value){{
  try{{return [TimeSpan]::Parse([string]$value,$culture).TotalSeconds}}
  catch{{return [double]::NaN}}
}}
$encoding=New-Object Text.UTF8Encoding($false)
$writer=New-Object IO.StreamWriter($exportPath,$false,$encoding,65536)
try{{
  $writer.WriteLine('{','.join(EXPECTED_COLUMNS)}')
  [int64]$exported=0
  foreach($cycle in $cycles){{
    $cycleId=[int]$cycle.cycleid
    $steps=@($reader.GetStepData($dataPath,$cycleId))
    $stepCount+=$steps.Count
    foreach($step in $steps){{
      $stepId=[int]$step.stepid
      foreach($row in $reader.GetRecordData($dataPath,$cycleId,$stepId)){{
    $timeS=Parse-Time $row.recordtime
    $voltageRaw=Parse-Number $row.voltage
    $currentRaw=Parse-Number $row.current
    $temperature=Parse-Number $row.temperature
    $voltageV=$voltageRaw/1000000.0
    $currentMa=$currentRaw/1000.0
    $density=if($null -ne $areaCm2){{$currentMa/[double]$areaCm2}}else{{[double]::NaN}}
    $values=@(
      (Format-Number $timeS),
      (Format-Number $voltageV),
      (Format-Number $currentMa),
      (Format-Number $density),
      (Csv-Text $row.cycleid),
      (Csv-Text $row.stepid),
      (Csv-Text $row.stepname),
      (Csv-Text $row.absolutetime),
      (Csv-Text $row.id),
      (Format-Number $voltageRaw),
      (Format-Number $currentRaw),
      (Format-Number $temperature)
    )
    $writer.WriteLine([string]::Join(',',$values))
    $exported++
      }}
    }}
  }}
  $writer.Flush()
}}finally{{
  $writer.Dispose()
}}
$csvStream=[IO.File]::OpenRead($exportPath)
try{{$csvStream.CopyTo([Console]::OpenStandardOutput(),65536)}}finally{{$csvStream.Dispose()}}
[Console]::Out.Flush()
[IO.File]::Delete($exportPath)
$watch.Stop()
$after=Get-Item -LiteralPath $dataPath -ErrorAction Stop
if([int64]$after.Length -ne $expectedSize -or
   [int64]$after.LastWriteTimeUtc.Ticks -ne $expectedTicks){{
  throw 'source_changed_after_extract'
}}
$meta=[pscustomobject][ordered]@{{
  ok=$true
  source_size=[int64]$after.Length
  source_ticks=[int64]$after.LastWriteTimeUtc.Ticks
  source_modified_utc=$after.LastWriteTimeUtc.ToString('o',$culture)
  record_count=[int64]$exported
  cycle_count=[int64]$cycles.Count
  step_count=[int64]$stepCount
  exported_point_count=[int64]$exported
  downsample_stride=1
  sampling_policy='full_records'
  elapsed_ms=[int64]$watch.ElapsedMilliseconds
  process=@($process)
}}
$json=$meta|ConvertTo-Json -Compress -Depth 8
$encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
[Console]::Error.WriteLine('{META_PREFIX.decode("ascii")}'+$encoded)
[Console]::Error.Flush()
[Environment]::Exit(0)
"""


def _quantile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return math.nan
    position = max(0.0, min(1.0, fraction)) * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _profile_normalized_csv(path: Path) -> dict[str, Any]:
    currents: list[float] = []
    valid_rows = 0
    invalid_rows = 0
    previous_time = -math.inf
    time_decrease_count = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != EXPECTED_COLUMNS:
            raise ValueError("蓝博标准化 CSV 表头无效")
        for row in reader:
            try:
                time_s = float(row["time_s"])
                potential_v = float(row["potential_v"])
                current_ma = float(row["current_ma"])
            except (TypeError, ValueError):
                invalid_rows += 1
                continue
            if not all(math.isfinite(value) for value in (time_s, potential_v, current_ma)):
                invalid_rows += 1
                continue
            if time_s < previous_time - 1e-9:
                time_decrease_count += 1
            previous_time = time_s
            currents.append(current_ma)
            valid_rows += 1
    if valid_rows < 2:
        raise ValueError("蓝博标准化数据有效点不足")
    ordered = sorted(currents)
    p05 = _quantile(ordered, 0.05)
    p95 = _quantile(ordered, 0.95)
    span = p95 - p05
    maximum_abs = max(abs(p05), abs(p95), 1.0)
    threshold = max(5.0, 0.05 * maximum_abs)
    low_limit = p05 + 0.25 * span
    high_limit = p95 - 0.25 * span
    labels: list[int] = []
    low_count = 0
    high_count = 0
    if span > 0:
        for value in currents:
            if value <= low_limit:
                label = 0
                low_count += 1
            elif value >= high_limit:
                label = 1
                high_count += 1
            else:
                continue
            if not labels or labels[-1] != label:
                labels.append(label)
    return {
        "valid_point_count": valid_rows,
        "invalid_point_count": invalid_rows,
        "time_decrease_count": time_decrease_count,
        "current_p05_ma": p05,
        "current_p95_ma": p95,
        "current_span_ma": span,
        "current_fluctuation_threshold_ma": threshold,
        "current_low_fraction": low_count / valid_rows,
        "current_high_fraction": high_count / valid_rows,
        "current_level_transition_count": max(0, len(labels) - 1),
    }


def classify_lanbts_stability(
    *,
    process_rows: Iterable[Mapping[str, Any]],
    measured_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify current fluctuation using protocol first and measurements second."""

    protocol = _protocol(list(process_rows))
    protocol_currents = sorted(
        {
            float(step["current_ma"])
            for step in protocol.get("steps", [])
            if isinstance(step, Mapping)
            and isinstance(step.get("current_ma"), (int, float))
            and math.isfinite(float(step["current_ma"]))
        }
    )
    protocol_span = (
        protocol_currents[-1] - protocol_currents[0]
        if len(protocol_currents) >= 2
        else 0.0
    )
    protocol_maximum = max((abs(value) for value in protocol_currents), default=1.0)
    protocol_threshold = max(5.0, 0.05 * protocol_maximum)
    measured_span = float(measured_profile["current_span_ma"])
    measured_threshold = float(
        measured_profile["current_fluctuation_threshold_ma"]
    )
    transitions = int(measured_profile["current_level_transition_count"])
    low_fraction = float(measured_profile["current_low_fraction"])
    high_fraction = float(measured_profile["current_high_fraction"])

    if len(protocol_currents) >= 2 and protocol_span >= protocol_threshold:
        mode = "start_stop"
        reason = (
            f"BTS 内嵌工步包含 {len(protocol_currents)} 个显著不同电流档，"
            f"跨度 {protocol_span:.3g} mA。"
        )
        method = "embedded_protocol"
    elif (
        measured_span >= measured_threshold
        and transitions >= 2
        and low_fraction >= 0.03
        and high_fraction >= 0.03
    ):
        mode = "start_stop"
        reason = (
            f"实测电流 P5–P95 跨度 {measured_span:.3g} mA，"
            f"识别到 {transitions} 次持续电流档切换。"
        )
        method = "measured_current_fluctuation"
    else:
        mode = "constant_current"
        reason = (
            "未识别到两个持续且显著不同的电流档；"
            f"实测 P5–P95 跨度 {measured_span:.3g} mA。"
        )
        method = "single_level_or_no_sustained_transition"
    return {
        "analysis_mode": mode,
        "candidate_kind": (
            "lanbts_start_stop"
            if mode == "start_stop"
            else "lanbts_constant_current"
        ),
        "classification_method": method,
        "classification_reason": reason,
        "classifier_version": CLASSIFIER_VERSION,
        "protocol": protocol,
        "protocol_current_levels_ma": protocol_currents,
        "protocol_current_span_ma": protocol_span,
    }


def _run_config_by_file(
    store: LanbtsRunConfigStore | None,
) -> dict[str, dict[str, Any]]:
    if store is None:
        return {}
    rows = store.read().get("runs", {})
    result: dict[str, dict[str, Any]] = {}
    for item in rows.values():
        if not isinstance(item, Mapping):
            continue
        source_file = str(item.get("source_file") or "").casefold()
        if not source_file:
            continue
        previous = result.get(source_file)
        if previous is None or str(item.get("updated_at_utc") or "") >= str(
            previous.get("updated_at_utc") or ""
        ):
            result[source_file] = dict(item)
    return result


class LanbtsStabilityImporter:
    def __init__(
        self,
        database: StartStopDatabase,
        machine_config: Mapping[str, Any],
        *,
        channel_config_store: LanbtsRunConfigStore | None = None,
        transport: SSHWindowsTransport | None = None,
    ) -> None:
        self.database = database
        self.machine = dict(machine_config)
        self.channel_config_store = channel_config_store
        known_hosts = self.machine.get("known_hosts_file")
        self.transport = transport or SSHWindowsTransport(
            known_hosts_file=(Path(str(known_hosts)).expanduser() if known_hosts else None)
        )

    @property
    def root(self) -> dict[str, Any]:
        return {
            "label": LANBTS_ROOT_LABEL,
            "remote_path": str(self.machine["data_root"]),
            "exclude_directories": [],
            "recursive": True,
        }

    @staticmethod
    def _ensure_space(path: Path, source_size: int) -> None:
        usage = shutil.disk_usage(path.parent)
        required = max(MIN_FREE_BYTES, int(source_size) * 3)
        if usage.free < required:
            raise OSError("本地空间不足，无法导入蓝博原始与标准化数据")

    def _ingest_raw(
        self,
        item: PlannedFile,
        staged: Path,
        *,
        batch_id: str,
    ) -> dict[str, Any]:
        self._ensure_space(staged, item.size)
        self.transport.fetch_file_verified(
            self.machine,
            item.remote_path,
            staged,
            item.size,
            item.last_write_ticks,
        )
        return self.database.ingest_staged_file(
            staged,
            {
                "machine_id": str(self.machine["id"]),
                "hostname": str(self.machine["hostname"]),
                "ip": str(self.machine["ip"]),
                "root_label": LANBTS_ROOT_LABEL,
                "remote_root": str(self.machine["data_root"]),
                "remote_path": item.remote_path,
                "remote_relative_path": item.relative_path,
                "repository_path": _repository_path(
                    str(self.machine["id"]),
                    LANBTS_ROOT_LABEL,
                    item.relative_path,
                ),
                "size": item.size,
                "last_write_ticks": item.last_write_ticks,
                "last_write_utc": item.last_write_utc,
                "collected_at_utc": iso_utc(),
                "source_format": "lanbts_bts",
                "is_candidate": False,
            },
            batch_id,
        )

    def run(
        self,
        scratch_dir: str | Path,
        *,
        settle_seconds: int = DEFAULT_SETTLE_SECONDS,
        progress_path: str | Path | None = None,
    ) -> dict[str, Any]:
        scratch = Path(scratch_dir).resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        if scratch.is_symlink() or not scratch.is_dir():
            raise ValueError("蓝博导入临时目录无效")
        inventory = self.transport.inventory_root(
            self.machine,
            self.root,
            [".bts"],
        )
        stable, unsettled = make_planned_files(
            self.machine,
            self.root,
            inventory,
            max(1, int(settle_seconds)),
        )
        progress_target = (
            Path(progress_path).resolve() if progress_path is not None else None
        )
        machine_progress = {
            "machine_id": str(self.machine["id"]),
            "name": "蓝博八通道",
            "status": "scanning",
            "completed": 0,
            "total": len(stable),
            "message": "正在核对稳定 BTS 文件",
        }

        def emit_progress(
            completed: int,
            *,
            current_item: str = "",
            detail: str = "",
            status: str = "importing",
        ) -> None:
            machine_progress.update(
                status=status,
                completed=max(0, min(int(completed), len(stable))),
                message=detail[:200],
            )
            _write_progress(
                progress_target,
                {
                    "phase": "importing_lanbts",
                    "phase_label": "导入蓝博稳定性数据",
                    "phase_index": 5,
                    "phase_count": 7,
                    "mode": "determinate",
                    "percent": (
                        100.0 * max(0, min(int(completed), len(stable))) / len(stable)
                        if stable
                        else 100
                    ),
                    "completed": max(0, min(int(completed), len(stable))),
                    "total": len(stable),
                    "unit": "files",
                    "current_item": PureWindowsPath(current_item).name,
                    "detail": detail[:240],
                    "machines": [dict(machine_progress)],
                },
            )

        emit_progress(0, detail="正在检查蓝博稳定文件是否已入库")
        batch_id = self.database.begin_collection_batch(
            machine_count=1,
            metadata={
                "source": "lanbts_stability_import",
                "extractor_version": EXTRACTOR_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
            },
            batch_id=f"lanbts-{uuid.uuid4().hex}",
        )
        totals = {
            "inventoried": len(inventory.get("files", [])),
            "stable": len(stable),
            "unsettled_skipped": len(unsettled),
            "raw_downloaded": 0,
            "raw_ingested": 0,
            "derived_ingested": 0,
            "derived_reused": 0,
            "start_stop": 0,
            "constant_current": 0,
            "known_failure_skipped": 0,
            "errors": 0,
        }
        errors: list[dict[str, str]] = []
        raw_stage = scratch / "lanbts-raw.part"
        normalized_stage = scratch / "lanbts-normalized.csv"
        config_by_file = _run_config_by_file(self.channel_config_store)
        try:
            for item_index, item in enumerate(stable, start=1):
                raw_stage.unlink(missing_ok=True)
                normalized_stage.unlink(missing_ok=True)
                emit_progress(
                    item_index - 1,
                    current_item=item.remote_path,
                    detail="正在保存原始 BTS 并核对标准化版本",
                )
                try:
                    if item.size <= 0 or item.size > MAX_SOURCE_BYTES:
                        raise ValueError("蓝博 BTS 文件大小超出导入上限")
                    raw_needed = self.database.needs_download(
                        str(self.machine["id"]),
                        LANBTS_ROOT_LABEL,
                        item.remote_path,
                        item.size,
                        item.last_write_ticks,
                    )
                    if raw_needed:
                        self._ingest_raw(item, raw_stage, batch_id=batch_id)
                        totals["raw_downloaded"] += 1
                        totals["raw_ingested"] += 1
                    raw_info = self.database.current_source_version(
                        str(self.machine["id"]),
                        LANBTS_ROOT_LABEL,
                        item.remote_path,
                    )
                    if raw_info is None:
                        raise RuntimeError("蓝博原始 BTS 文件未能入库")
                    filename = PureWindowsPath(item.remote_path).name
                    run_config = config_by_file.get(filename.casefold(), {})
                    area_raw = run_config.get("electrode_area_cm2")
                    area = (
                        float(area_raw)
                        if isinstance(area_raw, (int, float))
                        and math.isfinite(float(area_raw))
                        and float(area_raw) > 0
                        else None
                    )
                    config_signature = _json_hash(
                        {
                            "material_name": str(run_config.get("material_name") or ""),
                            "electrode_area_cm2": area,
                            "notes": str(run_config.get("notes") or ""),
                        }
                    )
                    derived_remote = item.remote_path + DERIVED_SOURCE_SUFFIX
                    existing = self.database.current_source_version(
                        str(self.machine["id"]),
                        LANBTS_ROOT_LABEL,
                        derived_remote,
                    )
                    existing_meta = (
                        existing.get("metadata", {})
                        if isinstance(existing, Mapping)
                        else {}
                    )
                    derivation_current = bool(
                        existing
                        and existing_meta.get("parent_sha256") == raw_info["sha256"]
                        and existing_meta.get("extractor_version") == EXTRACTOR_VERSION
                        and existing_meta.get("classifier_version") == CLASSIFIER_VERSION
                        and existing_meta.get("run_config_signature") == config_signature
                    )
                    if derivation_current:
                        totals["derived_reused"] += 1
                        mode = str(existing_meta.get("analysis_mode") or "")
                        if mode in {"start_stop", "constant_current"}:
                            totals[mode] += 1
                        emit_progress(
                            item_index,
                            current_item=item.remote_path,
                            detail="原始与标准化版本均未变化，已复用",
                        )
                        continue
                    issue_reader = getattr(
                        self.database, "collection_issues_for_source", None
                    )
                    retry_after = None
                    if callable(issue_reader):
                        retry_after = _repeated_timeout_retry_after(
                            raw_info,
                            issue_reader(
                                str(self.machine["id"]),
                                LANBTS_ROOT_LABEL,
                                item.remote_path,
                                code="lanbts_stability_import_failed",
                                limit=20,
                            ),
                        )
                    if retry_after is not None:
                        totals["known_failure_skipped"] += 1
                        emit_progress(
                            item_index,
                            current_item=item.remote_path,
                            detail=(
                                "同一原始版本已连续解析超时，暂缓重试至 "
                                + retry_after.astimezone(dt.timezone.utc).isoformat(
                                    timespec="minutes"
                                )
                            ),
                            status="deferred",
                        )
                        continue
                    if existing and existing.get("is_candidate"):
                        self.database.mark_candidate(
                            int(existing["id"]),
                            False,
                            reason="父 BTS 或解析配置已更新，等待重新标准化",
                        )
                    script = lanbts_record_export_script(
                        self.machine,
                        data_path=item.remote_path,
                        expected_size=item.size,
                        expected_ticks=item.last_write_ticks,
                        area_cm2=area,
                    )
                    stderr = self.transport.stream_stdin_script(
                        self.machine,
                        script,
                        normalized_stage,
                        timeout=_extract_timeout_seconds(item.size),
                    )
                    export_meta = _decode_export_metadata(stderr)
                    if (
                        export_meta.get("ok") is not True
                        or int(export_meta.get("source_size", -1)) != item.size
                        or int(export_meta.get("source_ticks", -1))
                        != item.last_write_ticks
                    ):
                        raise RuntimeError("蓝博解析前后文件版本不一致")
                    if (
                        int(export_meta.get("downsample_stride", 1)) != 1
                        or int(export_meta.get("record_count", -1)) != int(export_meta.get("exported_point_count", -2))
                    ):
                        raise RuntimeError("蓝博标准化必须保留全部记录，抽样数据不能作为正式分析来源")
                    measured = _profile_normalized_csv(normalized_stage)
                    if measured["valid_point_count"] + measured["invalid_point_count"] != int(export_meta["exported_point_count"]):
                        raise RuntimeError("蓝博导出行数与远端记录计数不一致，拒绝不完整数据")
                    if measured["time_decrease_count"] > 0:
                        raise RuntimeError("蓝博标准化记录时间发生倒退")
                    classification = classify_lanbts_stability(
                        process_rows=export_meta.get("process", []),
                        measured_profile=measured,
                    )
                    material_name = str(run_config.get("material_name") or "").strip()
                    if not material_name:
                        material_name = _material_hint(filename) or PureWindowsPath(filename).stem
                    metadata = {
                        "machine_id": str(self.machine["id"]),
                        "hostname": str(self.machine["hostname"]),
                        "ip": str(self.machine["ip"]),
                        "root_label": LANBTS_ROOT_LABEL,
                        "remote_root": str(self.machine["data_root"]),
                        "remote_path": derived_remote,
                        "remote_relative_path": item.relative_path + ".normalized.csv",
                        "repository_path": _derived_repository_path(
                            str(self.machine["id"]),
                            LANBTS_ROOT_LABEL,
                            item.relative_path,
                        ),
                        "size": normalized_stage.stat().st_size,
                        "last_write_ticks": item.last_write_ticks,
                        "last_write_utc": item.last_write_utc,
                        "collected_at_utc": iso_utc(),
                        "is_candidate": True,
                        "_force_metadata_version": True,
                        "candidate_kind": classification["candidate_kind"],
                        "source_format": "lanbts_normalized_csv",
                        "extractor_version": EXTRACTOR_VERSION,
                        "classifier_version": CLASSIFIER_VERSION,
                        "parent_source_version_id": int(raw_info["id"]),
                        "parent_repository_path": str(raw_info["repository_path"]),
                        "parent_sha256": str(raw_info["sha256"]),
                        "parent_size_bytes": int(raw_info["size_bytes"]),
                        "source_file": filename,
                        "channel": int(
                            re.search(r"_([1-8])_\d{14}\.bts$", filename, re.I).group(1)
                        ) if re.search(r"_([1-8])_\d{14}\.bts$", filename, re.I) else None,
                        "material_name": material_name[:120],
                        "electrode_area_cm2": area,
                        "notes": str(run_config.get("notes") or "")[:300],
                        "run_config_signature": config_signature,
                        "voltage_reference": "unconfirmed",
                        **classification,
                        **measured,
                        "record_count": int(export_meta.get("record_count", 0)),
                        "cycle_count": int(export_meta.get("cycle_count", 0)),
                        "step_count": int(export_meta.get("step_count", 0)),
                        "exported_point_count": int(
                            export_meta.get("exported_point_count", 0)
                        ),
                        "downsample_stride": int(
                            export_meta.get("downsample_stride", 1)
                        ),
                        "sampling_policy": "full_records",
                        "extract_elapsed_ms": int(export_meta.get("elapsed_ms", 0)),
                    }
                    self.database.ingest_staged_file(
                        normalized_stage,
                        metadata,
                        batch_id,
                    )
                    totals["derived_ingested"] += 1
                    totals[classification["analysis_mode"]] += 1
                    emit_progress(
                        item_index,
                        current_item=item.remote_path,
                        detail=(
                            "已归入启停分析"
                            if classification["analysis_mode"] == "start_stop"
                            else "已归入恒流分析"
                        ),
                    )
                except Exception as exc:
                    totals["errors"] += 1
                    error = {
                        "file": PureWindowsPath(item.remote_path).name,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                    errors.append(error)
                    self.database.record_collection_issue(
                        batch_id,
                        severity="error",
                        code="lanbts_stability_import_failed",
                        message=error["message"],
                        machine_id=str(self.machine["id"]),
                        root_label=LANBTS_ROOT_LABEL,
                        remote_path=item.remote_path,
                        detail={"file": error["file"]},
                    )
                    emit_progress(
                        item_index,
                        current_item=item.remote_path,
                        detail="当前文件导入失败，已记录并继续",
                        status="failed",
                    )
            status = "partial" if totals["errors"] else "completed"
            self.database.finish_collection_batch(batch_id, status=status, totals=totals)
            emit_progress(
                len(stable),
                detail="蓝博稳定性数据导入完成",
                status=status,
            )
        except BaseException:
            try:
                self.database.finish_collection_batch(
                    batch_id,
                    status="failed",
                    totals=totals,
                )
            except Exception:
                pass
            raise
        finally:
            raw_stage.unlink(missing_ok=True)
            normalized_stage.unlink(missing_ok=True)
        return {
            "batch_id": batch_id,
            "status": "partial" if totals["errors"] else "completed",
            "totals": totals,
            "errors": errors,
            "extractor_version": EXTRACTOR_VERSION,
            "classifier_version": CLASSIFIER_VERSION,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="蓝博 BTS 稳定性数据只读导入")
    parser.add_argument("--config", required=True)
    parser.add_argument("--channel-config", default="")
    parser.add_argument("--database", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--progress-json", default="")
    parser.add_argument("--settle-seconds", type=int, default=DEFAULT_SETTLE_SECONDS)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    result_path = Path(args.result_json).resolve()
    try:
        config = load_lanbts_machine_config(args.config)
        if config is None:
            raise ValueError("蓝博固定配置不可用")
        channel_store = (
            LanbtsRunConfigStore(args.channel_config)
            if str(args.channel_config or "").strip()
            else None
        )
        importer = LanbtsStabilityImporter(
            StartStopDatabase(args.database),
            config,
            channel_config_store=channel_store,
        )
        result = importer.run(
            args.scratch,
            settle_seconds=max(1, int(args.settle_seconds)),
            progress_path=(
                Path(args.progress_json).resolve()
                if str(args.progress_json or "").strip()
                else None
            ),
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["status"] == "partial" else 0
    except Exception as exc:
        failure = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(failure, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLASSIFIER_VERSION",
    "EXTRACTOR_VERSION",
    "LanbtsStabilityImporter",
    "classify_lanbts_stability",
    "lanbts_record_export_script",
]
