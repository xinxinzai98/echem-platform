from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .start_stop_collection import CollectionError, SSHWindowsTransport, ps_literal


MONITOR_SCHEMA_VERSION = 1
DEFAULT_POLL_SECONDS = 30
DEFAULT_DISCOVERY_SECONDS = 300
DEFAULT_STALE_SECONDS = 180
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_RECENT_FILES_PER_ROOT = 12
MAX_QUICK_FILES_PER_MACHINE = 48
MAX_MATERIAL_ACTIVITIES_PER_MACHINE = 6
DISCOVERY_DEPTH = 4

_PROCESS_PATTERN = (
    r"(?i)(chi760e|chi760|chi700|csstudio6?|csanalysis|corrtest|cs310ma|"
    r"electrochem|gamry|autolab|nova|ivium|zahner|biologic)"
)
_CONSTANT_CURRENT = re.compile(
    r"(?:^|[-_\s])(?:恒流|恒)[-_\s]*(?P<current>\d+(?:\.\d+)?)\s*ma"
    r"[-_\s]*(?P<minutes>\d+(?:\.\d+)?)\s*min",
    re.IGNORECASE,
)
_PULSE = re.compile(
    r"(?:^|[-_\s])(?:脉冲|脉)[-_\s]*(?P<current>\d+(?:\.\d+)?)\s*ma"
    r"[-_\s]*(?P<on>\d+(?:\.\d+)?)\s*s"
    r"[-_\s]*(?P<off>\d+(?:\.\d+)?)\s*s"
    r"[-_\s]*(?P<minutes>\d+(?:\.\d+)?)\s*min",
    re.IGNORECASE,
)
_START_STOP_WORK_STEP = re.compile(
    r"(?:^|[-_\s])(?P<cathodic>\d+(?:\.\d+)?)[-_\s]+"
    r"(?P<recovery>\d+(?:\.\d+)?)[-_\s]+"
    r"(?P<minutes>\d+(?:\.\d+)?)\s*min\s*$",
    re.IGNORECASE,
)
_PROTOCOL_BOUNDARY = re.compile(
    r"[-_\s](?:恒流|恒|脉冲|脉|adt)(?:[-_\s]|$)",
    re.IGNORECASE,
)
_COM_NUMBER = re.compile(r"(?i)^COM(?P<number>\d+)$")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_text(value: dt.datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


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


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _safe_probe_error(error: Exception) -> str:
    detail = str(error)
    if "超时" in detail:
        return "SSH 读取超时"
    if "密钥" in detail or "known_hosts" in detail:
        return "SSH 密钥或主机指纹配置未就绪"
    if "地址" in detail:
        return "实验机地址配置无效"
    if "无法启动 SSH" in detail:
        return "服务器 SSH 客户端不可用"
    return "SSH 服务不可达或只读探测失败"


def workstation_signal_script() -> str:
    """Return the short process/serial metadata probe."""

    return rf"""
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$processPattern = '{_PROCESS_PATTERN}'

$processes = @(
  Get-Process -ErrorAction SilentlyContinue |
  Where-Object {{ ([string]$_.ProcessName) -match $processPattern }} |
  Sort-Object Id |
  ForEach-Object {{
    $started = $null
    $responding = $null
    try {{ $started = $_.StartTime.ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture) }} catch {{}}
    try {{ $responding = [bool]$_.Responding }} catch {{}}
    [pscustomobject][ordered]@{{
      process_name = [string]$_.ProcessName
      pid = [int]$_.Id
      responding = $responding
      started_at_utc = $started
    }}
  }}
)

$serialProbeOk = $true
$serialPorts = @()
try {{
  $serialPorts = @(
    Get-CimInstance -ClassName Win32_SerialPort -ErrorAction Stop |
    Sort-Object DeviceID |
    ForEach-Object {{
      [pscustomobject][ordered]@{{
        device_id = [string]$_.DeviceID
        name = [string]$_.Name
        status = [string]$_.Status
      }}
    }}
  )
}} catch {{
  $serialProbeOk = $false
  $serialPorts = @()
}}

$payload = [pscustomobject][ordered]@{{
  ok = $true
  computer_name = [Environment]::MachineName
  checked_at_utc = [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
  serial_probe_ok = $serialProbeOk
  serial_ports = $serialPorts
  processes = $processes
}}
$json = $payload | ConvertTo-Json -Compress -Depth 8
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
"""


def workstation_root_probe_script(
    *,
    root: dict[str, Any],
    extensions: list[str],
    candidates: list[dict[str, str]],
    discovery: bool,
) -> str:
    """Return one short metadata-only script for one configured search root."""

    label = str(root.get("label") or "")
    root_path = str(root.get("remote_path") or "")
    extension_literals = ",".join(
        ps_literal(value)
        for value in sorted(
            {
                str(item or "").strip().casefold()
                for item in extensions
                if str(item or "").strip().startswith(".")
            }
        )
    )
    excluded_literals = ",".join(
        ps_literal(str(item)) for item in root.get("exclude_directories", [])
    )
    header = rf"""$ProgressPreference='SilentlyContinue'
$InformationPreference='SilentlyContinue'
$WarningPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
$r={ps_literal(root_path)}
$l={ps_literal(label)}
$f=New-Object System.Collections.Generic.List[object]
$ok=[bool](Test-Path -LiteralPath $r -PathType Container)
$scan=$true
"""
    if discovery:
        body = rf"""$ext=@({extension_literals})
$exc=@({excluded_literals})
if($ok){{try{{$p=$r.TrimEnd([char]92)+[char]92
$all=@(Get-ChildItem -LiteralPath $r -File -Force -Recurse -Depth {DISCOVERY_DEPTH} -ErrorAction SilentlyContinue)
foreach($i in $all){{if($ext -notcontains $i.Extension.ToLowerInvariant()){{continue}};if(-not $i.FullName.StartsWith($p,[StringComparison]::OrdinalIgnoreCase)){{continue}};$rel=$i.FullName.Substring($p.Length);$skip=$false;foreach($part in $rel.Split([char]92)){{if($exc -contains $part){{$skip=$true;break}}}};if($skip){{continue}};[void]$f.Add([pscustomobject]@{{relative_path=$rel;file_name=[string]$i.Name;extension=$i.Extension.ToLowerInvariant();size_bytes=[Int64]$i.Length;last_write_ticks=[Int64]$i.LastWriteTimeUtc.Ticks;last_write_utc=$i.LastWriteTimeUtc.ToString('o',[Globalization.CultureInfo]::InvariantCulture)}})}}
$recent=@($f.ToArray()|Sort-Object last_write_ticks -Descending|Select-Object -First {MAX_RECENT_FILES_PER_ROOT})}}catch{{$scan=$false;$recent=@()}}}}else{{$recent=@()}}
"""
        mode = "discovery"
    else:
        candidate_values = [
            str(item.get("relative_path") or "")
            for item in candidates
            if str(item.get("root_label") or "") == label
        ][:6]
        # Six recent candidates per root are enough for two physical stations
        # and keep Windows OpenSSH's command line comfortably below 8191 bytes.
        candidate_source = ",".join(
            ps_literal(value) for value in candidate_values if value
        )
        body = rf"""$c=@({candidate_source})
if($ok){{try{{foreach($rel in $c){{if(-not $rel -or $rel.StartsWith([char]92)-or $rel.Contains('..')){{continue}};$i=Get-Item -LiteralPath (Join-Path $r $rel) -Force -ErrorAction SilentlyContinue;if($null -eq $i -or $i.PSIsContainer){{continue}};[void]$f.Add([pscustomobject]@{{relative_path=$rel;file_name=[string]$i.Name;extension=$i.Extension.ToLowerInvariant();size_bytes=[Int64]$i.Length;last_write_ticks=[Int64]$i.LastWriteTimeUtc.Ticks;last_write_utc=$i.LastWriteTimeUtc.ToString('o',[Globalization.CultureInfo]::InvariantCulture)}})}};$recent=@($f.ToArray())}}catch{{$scan=$false;$recent=@()}}}}else{{$recent=@()}}
"""
        mode = "quick"
    footer = rf"""$o=[pscustomobject]@{{ok=$true;checked_at_utc=[DateTime]::UtcNow.ToString('o',[Globalization.CultureInfo]::InvariantCulture);scan_mode='{mode}';root=[pscustomobject]@{{label=$l;exists=$ok;scan_ok=$scan;observed_files=@($recent).Count}};files=@($recent)}}
$j=$o|ConvertTo-Json -Compress -Depth 6
[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j)))
"""
    return header + body + footer


def workstation_probe_script(
    *,
    roots: list[dict[str, Any]],
    extensions: list[str],
    candidates: list[dict[str, str]],
    discovery: bool,
) -> str:
    """Backward-compatible one-root file metadata probe."""

    if len(roots) != 1:
        raise ValueError("工作站目录探测必须一次处理一个搜索位置")
    return workstation_root_probe_script(
        root=roots[0],
        extensions=extensions,
        candidates=candidates,
        discovery=discovery,
    )


def _normalized_relative_path(value: Any) -> str:
    raw = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/").strip("/")
    if not raw or "\x00" in raw:
        return ""
    parts = PurePosixPath(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    return PurePosixPath(*parts).as_posix()


def _path_key(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).replace("\\", "/").strip("/").casefold()


def infer_task(folder_name: str, file_name: str = "") -> dict[str, Any]:
    normalized = unicodedata.normalize("NFC", str(folder_name or "")).strip()
    labels: list[str] = []
    constant = _CONSTANT_CURRENT.search(normalized)
    pulse = _PULSE.search(normalized)
    combined = f"{normalized} {file_name}".casefold()
    explicit_start_stop = any(
        marker in combined for marker in ("qiting", "启停", "adt")
    )
    work_step = (
        _START_STOP_WORK_STEP.search(normalized) if explicit_start_stop else None
    )
    if constant:
        labels.append(
            f"恒流 {constant.group('current')} mA · {constant.group('minutes')} min"
        )
    if pulse:
        labels.append(
            "脉冲 "
            f"{pulse.group('current')} mA · {pulse.group('on')} s/{pulse.group('off')} s"
            f" · {pulse.group('minutes')} min"
        )
    work_step_payload: dict[str, Any] = {}
    if work_step:
        cathodic = float(work_step.group("cathodic"))
        recovery = float(work_step.group("recovery"))
        minutes = float(work_step.group("minutes"))
        cathodic_text = f"{cathodic:g}"
        recovery_text = f"{recovery:g}"
        minutes_text = f"{minutes:g}"
        labels.append(
            "启停 "
            f"−{cathodic_text} ↔ +{recovery_text} mA·cm⁻²"
            f" · {minutes_text} + {minutes_text} min"
        )
        work_step_payload = {
            "work_step_key": (
                f"start_stop|jc=-{cathodic_text}|jr={recovery_text}|"
                f"tc={minutes_text}min|tr={minutes_text}min"
            ),
            "work_step_label": labels[-1],
            "cathodic_current_ma_cm2": -cathodic,
            "recovery_current_ma_cm2": recovery,
            "cathodic_duration_s": minutes * 60.0,
            "recovery_duration_s": minutes * 60.0,
        }
    if "adt" in combined:
        test_type = "adt"
        test_label = "ADT 启停"
    elif "qiting" in combined or "启停" in combined:
        test_type = "start_stop"
        test_label = "启停测试"
    elif pulse:
        test_type = "pulse"
        test_label = "脉冲测试"
    elif constant:
        test_type = "constant_current"
        test_label = "恒流测试"
    else:
        test_type = "unknown"
        test_label = "电化学测试"
    if not labels:
        labels.append(test_label)
    boundary = _PROTOCOL_BOUNDARY.search(normalized)
    inferred_material = (
        normalized[: boundary.start()].rstrip("-_ ")
        if boundary
        else normalized[: work_step.start()].rstrip("-_ ")
        if work_step
        else normalized
    )
    return {
        "test_type": test_type,
        "test_label": test_label,
        "task_label": " → ".join(labels),
        "inferred_material_name": inferred_material or normalized or "未识别材料",
        "phase": "文件活动可见，当前仪器阶段未读取",
        "phase_identified": False,
        **work_step_payload,
    }


class WorkstationStateFile:
    def __init__(self, path: str | Path | None) -> None:
        raw = str(path or "").strip()
        self.path = Path(raw).expanduser().absolute() if raw else None

    def read(self) -> dict[str, Any] | None:
        path = self.path
        if path is None:
            return None
        try:
            metadata = path.stat()
            if not path.is_file() or path.is_symlink() or metadata.st_size > MAX_STATE_BYTES:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != MONITOR_SCHEMA_VERSION:
            return None
        if not isinstance(payload.get("machines"), list):
            return None
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        path = self.path
        if path is None:
            return
        data = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(data) > MAX_STATE_BYTES:
            raise ValueError("工作站监控缓存超过安全上限")
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


class WorkstationMonitor:
    """Poll three experiment PCs read-only and publish a bounded shared cache."""

    def __init__(
        self,
        state_file: str | Path | None,
        *,
        active_probe: bool = False,
        config_provider: Callable[[], dict[str, Any]] | None = None,
        material_provider: Callable[[], dict[str, Any]] | None = None,
        transport_factory: Any = SSHWindowsTransport,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        discovery_seconds: int = DEFAULT_DISCOVERY_SECONDS,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
    ) -> None:
        self.state_file = WorkstationStateFile(state_file)
        self.active_probe = bool(active_probe)
        self.config_provider = config_provider
        self.material_provider = material_provider
        self.transport_factory = transport_factory
        self.poll_seconds = max(10, min(int(poll_seconds), 600))
        self.discovery_seconds = max(
            self.poll_seconds,
            min(int(discovery_seconds), 3600),
        )
        self.stale_seconds = max(
            self.poll_seconds * 2,
            min(int(stale_seconds), 3600),
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_lock = threading.Lock()
        self._previous_files: dict[str, dict[str, Any]] = {}
        self._active_until: dict[str, dt.datetime] = {}
        self._candidates: dict[str, list[dict[str, str]]] = {}
        self._last_discovery_at: dt.datetime | None = None
        self._config_revision: Any = None

    def start(self) -> None:
        if not self.active_probe or self._thread is not None:
            return
        if not callable(self.config_provider):
            raise ValueError("工作站监控缺少实验机配置来源")
        self._thread = threading.Thread(
            target=self._run_loop,
            name="start-stop-workstation-monitor",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=min(5.0, float(self.poll_seconds)))
        self._thread = None

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            payload = copy.deepcopy(self._snapshot)
        if payload is None:
            payload = self.state_file.read()
        if payload is None:
            return {
                "schema_version": MONITOR_SCHEMA_VERSION,
                "status": "initializing" if self.active_probe else "unavailable",
                "message": (
                    "正在进行首次只读检查。"
                    if self.active_probe
                    else "工作站监控缓存尚未生成。"
                ),
                "generated_at_utc": "",
                "cache_age_seconds": None,
                "poll_seconds": self.poll_seconds,
                "discovery_seconds": self.discovery_seconds,
                "counts": {
                    "machines_total": 3,
                    "machines_reachable": 0,
                    "stations_total": 6,
                    "stations_online": 0,
                    "stations_running": 0,
                    "stations_idle": 0,
                    "stations_attention": 0,
                    "active_materials": 0,
                },
                "machines": [],
                "scope": self._scope(),
            }
        generated = _parse_utc(payload.get("generated_at_utc"))
        age = None
        if generated is not None:
            age = max(0, int((_utc_now() - generated).total_seconds()))
        payload["cache_age_seconds"] = age
        if age is not None and age > self.stale_seconds:
            payload["status"] = "stale"
            payload["message"] = "监控缓存已过期，服务器正在等待下一次只读检查。"
        return payload

    @staticmethod
    def _scope() -> dict[str, Any]:
        return {
            "monitoring_mode": "server_cached_read_only",
            "file_contents_read": False,
            "process_command_lines_read": False,
            "serial_ports_opened": False,
            "instrument_control_performed": False,
            "material_identification_basis": "active_or_recent_parent_folder",
            "station_material_mapping": "machine_level_only",
        }

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._probe_once()
            except Exception:
                self._publish_monitor_failure()
            elapsed = time.monotonic() - started
            self._stop.wait(max(1.0, self.poll_seconds - elapsed))

    def _publish_monitor_failure(self) -> None:
        with self._snapshot_lock:
            previous = copy.deepcopy(self._snapshot)
        if previous is None:
            previous = self.state_file.read()
        if previous is None:
            previous = {
                "schema_version": MONITOR_SCHEMA_VERSION,
                "machines": [],
                "counts": {
                    "machines_total": 3,
                    "machines_reachable": 0,
                    "stations_total": 6,
                    "stations_online": 0,
                    "stations_running": 0,
                    "stations_idle": 0,
                    "stations_attention": 0,
                    "active_materials": 0,
                },
                "scope": self._scope(),
            }
        previous.update(
            {
                "status": "partial",
                "message": "本轮只读检查未完成，继续显示上一次可用结果。",
                "last_attempt_at_utc": _utc_text(),
            }
        )
        with self._snapshot_lock:
            self._snapshot = previous

    def _material_map(self) -> dict[str, dict[str, Any]]:
        provider = self.material_provider
        if not callable(provider):
            return {}
        try:
            payload = provider()
        except Exception:
            return {}
        materials = payload.get("materials") if isinstance(payload, Mapping) else None
        if not isinstance(materials, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for raw in materials:
            if not isinstance(raw, Mapping):
                continue
            key = _path_key(raw.get("key"))
            if key:
                result[key] = {
                    "key": str(raw.get("key") or ""),
                    "plot_name": str(raw.get("plot_name") or raw.get("auto_name") or ""),
                    "auto_name": str(raw.get("auto_name") or ""),
                    "favorite": raw.get("favorite") is True,
                }
        return result

    def _probe_once(self) -> dict[str, Any]:
        if not callable(self.config_provider):
            raise ValueError("工作站监控配置不可用")
        config = self.config_provider()
        if not isinstance(config, dict):
            raise ValueError("工作站监控配置无效")
        machines = [dict(item) for item in config.get("machines", []) if isinstance(item, Mapping)]
        if not machines:
            raise ValueError("工作站监控没有实验机")
        revision = config.get("collection_config_revision")
        now = _utc_now()
        force_discovery = revision != self._config_revision
        if force_discovery:
            self._candidates.clear()
            self._previous_files.clear()
            self._active_until.clear()
            self._config_revision = revision
        discovery_due = bool(
            force_discovery
            or self._last_discovery_at is None
            or (now - self._last_discovery_at).total_seconds() >= self.discovery_seconds
        )
        known_hosts = config.get("known_hosts_file")
        material_map = self._material_map()

        def run(machine: dict[str, Any]) -> dict[str, Any]:
            machine_id = str(machine.get("id") or "")
            discovery = discovery_due or not self._candidates.get(machine_id)
            return self._probe_machine(
                machine,
                extensions=[str(item) for item in config.get("extensions", [])],
                known_hosts=known_hosts,
                discovery=discovery,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(3, len(machines)),
            thread_name_prefix="workstation-probe",
        ) as executor:
            futures = [executor.submit(run, machine) for machine in machines]
            raw_results = [future.result() for future in futures]
        if discovery_due:
            self._last_discovery_at = now
        public_machines = [
            self._public_machine(result, material_map=material_map, now=now)
            for result in raw_results
        ]
        counts = {
            "machines_total": len(public_machines),
            "machines_reachable": sum(1 for item in public_machines if item["reachable"]),
            "stations_total": sum(len(item["stations"]) for item in public_machines),
            "stations_online": sum(
                1
                for item in public_machines
                for station in item["stations"]
                if station["online"]
            ),
            "stations_running": sum(
                1
                for item in public_machines
                for station in item["stations"]
                if station["status"] in {"running", "running_attention"}
            ),
            "stations_idle": sum(
                1
                for item in public_machines
                for station in item["stations"]
                if station["status"] == "idle"
            ),
            "stations_attention": sum(
                1
                for item in public_machines
                for station in item["stations"]
                if station["status"] in {"attention", "offline", "running_attention"}
            ),
            "software_instances": sum(item["software_instances"] for item in public_machines),
            "serial_ports_online": sum(item["serial_ports_online"] for item in public_machines),
            "roots_total": sum(len(item["root_states"]) for item in public_machines),
            "roots_ok": sum(
                1
                for item in public_machines
                for root in item["root_states"]
                if root["exists"] and root["scan_ok"]
            ),
            "active_materials": sum(
                1
                for item in public_machines
                for activity in item["material_activities"]
                if activity["activity_status"] == "active"
            ),
            "unmatched_materials": sum(
                1
                for item in public_machines
                for activity in item["material_activities"]
                if activity["material_match"] == "unmatched"
            ),
            "unassigned_active_materials": sum(
                max(
                    0,
                    sum(
                        1
                        for activity in item["material_activities"]
                        if activity["activity_status"] == "active"
                    )
                    - len(item["stations"]),
                )
                for item in public_machines
            ),
        }
        counts["roots_failed"] = max(0, counts["roots_total"] - counts["roots_ok"])
        if (
            counts["machines_reachable"] == counts["machines_total"]
            and counts["roots_failed"] == 0
        ):
            status = "ready"
            message = "三台实验机的只读监控结果已更新。"
        elif counts["machines_reachable"]:
            status = "partial"
            message = "部分实验机不可达，其余只读监控结果已更新。"
        else:
            status = "unavailable"
            message = "三台实验机当前均不可达。"
        payload = {
            "schema_version": MONITOR_SCHEMA_VERSION,
            "status": status,
            "message": message,
            "generated_at_utc": _utc_text(now),
            "last_attempt_at_utc": _utc_text(now),
            "poll_seconds": self.poll_seconds,
            "discovery_seconds": self.discovery_seconds,
            "counts": counts,
            "machines": public_machines,
            "scope": self._scope(),
        }
        self.state_file.write(payload)
        with self._snapshot_lock:
            self._snapshot = copy.deepcopy(payload)
        return payload

    def _probe_machine(
        self,
        machine: dict[str, Any],
        *,
        extensions: list[str],
        known_hosts: Any,
        discovery: bool,
    ) -> dict[str, Any]:
        machine_id = str(machine.get("id") or "")
        public = {
            "id": machine_id,
            "name": str(machine.get("name") or machine.get("hostname") or machine_id),
            "hostname": str(machine.get("hostname") or ""),
            "ip": str(machine.get("ip") or ""),
        }
        roots = [
            {
                "label": str(root.get("label") or ""),
                "remote_path": str(root.get("remote_path") or ""),
                "exclude_directories": [
                    str(item) for item in root.get("exclude_directories", [])
                ],
            }
            for root in machine.get("roots", [])
            if isinstance(root, Mapping)
        ]
        try:
            transport = self.transport_factory(
                known_hosts_file=(Path(str(known_hosts)).expanduser() if known_hosts else None)
            )
            signal = transport._run_payload(
                machine,
                workstation_signal_script(),
                timeout=30,
            )
            if signal.get("ok") is not True:
                raise ValueError("工作站只读回应无效")
            root_states: list[dict[str, Any]] = []
            files: list[dict[str, Any]] = []
            for root in roots:
                try:
                    payload = transport._run_payload(
                        machine,
                        workstation_root_probe_script(
                            root=root,
                            extensions=extensions,
                            candidates=self._candidates.get(machine_id, []),
                            discovery=discovery,
                        ),
                        timeout=60 if discovery else 25,
                    )
                    if payload.get("ok") is not True:
                        raise ValueError("工作站目录回应无效")
                    state = payload.get("root")
                    if isinstance(state, Mapping):
                        root_states.append(dict(state))
                    for file_row in _as_rows(payload.get("files")):
                        file_row["root_label"] = str(root.get("label") or "")
                        files.append(file_row)
                except (CollectionError, OSError, ValueError):
                    root_states.append(
                        {
                            "label": str(root.get("label") or ""),
                            "exists": False,
                            "scan_ok": False,
                            "observed_files": 0,
                        }
                    )
            return {
                **public,
                "reachable": True,
                "checked_at_utc": str(signal.get("checked_at_utc") or _utc_text()),
                "scan_mode": "discovery" if discovery else "quick",
                "serial_probe_ok": signal.get("serial_probe_ok") is True,
                "serial_ports": _as_rows(signal.get("serial_ports")),
                "processes": _as_rows(signal.get("processes")),
                "roots": root_states,
                "files": files,
            }
        except (CollectionError, OSError, ValueError) as exc:
            return {
                **public,
                "reachable": False,
                "checked_at_utc": _utc_text(),
                "scan_mode": "discovery" if discovery else "quick",
                "message": _safe_probe_error(exc),
                "serial_probe_ok": False,
                "serial_ports": [],
                "processes": [],
                "roots": [],
                "files": [],
            }

    def _annotate_files(
        self,
        machine: Mapping[str, Any],
        *,
        now: dt.datetime,
    ) -> list[dict[str, Any]]:
        machine_id = str(machine.get("id") or "")
        annotated: list[dict[str, Any]] = []
        for raw in _as_rows(machine.get("files")):
            root_label = unicodedata.normalize("NFC", str(raw.get("root_label") or "")).strip()
            relative = _normalized_relative_path(raw.get("relative_path"))
            if not root_label or not relative:
                continue
            identity = f"{machine_id}/{root_label}/{relative}"
            identity_key = _path_key(identity)
            try:
                size_bytes = max(0, int(raw.get("size_bytes") or 0))
                ticks = max(0, int(raw.get("last_write_ticks") or 0))
            except (TypeError, ValueError):
                continue
            previous = self._previous_files.get(identity_key)
            changed = bool(
                previous is not None
                and (
                    size_bytes != previous.get("size_bytes")
                    or ticks != previous.get("last_write_ticks")
                )
            )
            if changed:
                self._active_until[identity_key] = now + dt.timedelta(
                    seconds=max(75, self.poll_seconds * 3)
                )
            active_until = self._active_until.get(identity_key)
            active = bool(active_until is not None and now <= active_until)
            last_write = str(raw.get("last_write_utc") or "")
            row = {
                "identity": identity,
                "identity_key": identity_key,
                "root_label": root_label,
                "relative_path": relative,
                "file_name": str(raw.get("file_name") or PurePosixPath(relative).name),
                "size_bytes": size_bytes,
                "last_write_ticks": ticks,
                "last_write_utc": last_write,
                "changed_since_previous_probe": changed,
                "active": active,
            }
            self._previous_files[identity_key] = {
                "size_bytes": size_bytes,
                "last_write_ticks": ticks,
                "last_write_utc": last_write,
            }
            annotated.append(row)
        annotated.sort(
            key=lambda item: (
                item["active"],
                _parse_utc(item["last_write_utc"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            ),
            reverse=True,
        )
        if machine.get("scan_mode") == "discovery":
            self._candidates[machine_id] = [
                {
                    "root_label": item["root_label"],
                    "relative_path": item["relative_path"],
                }
                for item in annotated[:MAX_QUICK_FILES_PER_MACHINE]
            ]
        return annotated

    @staticmethod
    def _material_activities(
        machine: Mapping[str, Any],
        files: list[dict[str, Any]],
        *,
        material_map: Mapping[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        machine_id = str(machine.get("id") or "")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in files:
            parent = PurePosixPath(item["relative_path"]).parent
            if str(parent) in {"", "."}:
                continue
            material_key = PurePosixPath(machine_id, item["root_label"], parent).as_posix()
            grouped.setdefault(_path_key(material_key), []).append(
                {**item, "material_key": material_key}
            )
        activities: list[dict[str, Any]] = []
        for normalized_key, rows in grouped.items():
            rows.sort(
                key=lambda item: _parse_utc(item["last_write_utc"])
                or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                reverse=True,
            )
            newest = rows[0]
            matched = material_map.get(normalized_key)
            folder_name = PurePosixPath(newest["material_key"]).name
            task = infer_task(folder_name, newest["file_name"])
            activities.append(
                {
                    "activity_status": "active" if any(row["active"] for row in rows) else "recent",
                    "material_match": "matched" if matched else "unmatched",
                    "material_key": (
                        str(matched.get("key") or newest["material_key"])
                        if matched
                        else newest["material_key"]
                    ),
                    "display_name": (
                        str(matched.get("plot_name") or matched.get("auto_name") or folder_name)
                        if matched
                        else task["inferred_material_name"]
                    ),
                    "folder_name": folder_name,
                    "favorite": bool(matched and matched.get("favorite")),
                    "root_label": newest["root_label"],
                    "relative_folder": PurePosixPath(newest["relative_path"]).parent.as_posix(),
                    "current_file": newest["file_name"],
                    "last_write_utc": newest["last_write_utc"],
                    "changed_since_previous_probe": any(
                        row["changed_since_previous_probe"] for row in rows
                    ),
                    "task": task,
                    "confidence": "high" if matched else "folder_inference",
                }
            )
        activities.sort(
            key=lambda item: (
                item["activity_status"] == "active",
                _parse_utc(item["last_write_utc"])
                or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            ),
            reverse=True,
        )
        return activities[:MAX_MATERIAL_ACTIVITIES_PER_MACHINE]

    @staticmethod
    def _port_sort_key(port: Mapping[str, Any]) -> tuple[int, str]:
        device_id = str(port.get("device_id") or "")
        match = _COM_NUMBER.fullmatch(device_id.strip())
        return (int(match.group("number")) if match else 10_000, device_id.casefold())

    @staticmethod
    def _station_process_rows(machine: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = _as_rows(machine.get("processes"))
        cs_studio = [
            row
            for row in rows
            if "csstudio" in str(row.get("process_name") or "").casefold()
        ]
        return cs_studio or rows

    @classmethod
    def _stations(
        cls,
        machine: Mapping[str, Any],
        *,
        activities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ports = sorted(_as_rows(machine.get("serial_ports")), key=cls._port_sort_key)
        preferred = [
            port
            for port in ports
            if "stmicro" in str(port.get("name") or "").casefold()
            or "virtual com" in str(port.get("name") or "").casefold()
        ]
        selected_ports = (preferred or ports)[:2]
        processes = sorted(
            cls._station_process_rows(machine),
            key=lambda item: int(item.get("pid") or 0),
        )[:2]
        stations: list[dict[str, Any]] = []
        active_activities = [
            item for item in activities if item["activity_status"] == "active"
        ]
        active_names = [item["display_name"] for item in active_activities]
        for index in range(2):
            port = selected_ports[index] if index < len(selected_ports) else None
            process = processes[index] if index < len(processes) else None
            assigned_activity = (
                active_activities[index] if index < len(active_activities) else None
            )
            port_ok = bool(
                port
                and str(port.get("status") or "OK").strip().casefold()
                not in {"error", "degraded", "unknown"}
            )
            process_ok = bool(process and process.get("responding") is not False)
            online = bool(machine.get("reachable") and port_ok and process_ok)
            if not machine.get("reachable"):
                status = "offline"
                label = "电脑或 SSH 不可达"
            elif assigned_activity and online:
                status = "running"
                label = "运行中"
            elif assigned_activity:
                status = "running_attention"
                label = "运行中，信号待检查"
            elif online:
                status = "idle"
                label = "空闲"
            else:
                status = "attention"
                label = "软件或串口信号不完整"
            stations.append(
                {
                    "station_id": f"{machine.get('id')}:station-{index + 1}",
                    "label": f"工作站 {chr(65 + index)}",
                    "status": status,
                    "status_label": label,
                    "online": online,
                    "serial_port": (
                        {
                            "device_id": str(port.get("device_id") or ""),
                            "name": str(port.get("name") or ""),
                            "status": str(port.get("status") or ""),
                        }
                        if port
                        else None
                    ),
                    "software": (
                        {
                            "process_name": str(process.get("process_name") or ""),
                            "pid": int(process.get("pid") or 0),
                            "responding": process.get("responding"),
                            "started_at_utc": str(process.get("started_at_utc") or ""),
                        }
                        if process
                        else None
                    ),
                    "material_assignment": "machine_level_only" if active_names else "none",
                    "assigned_activity": copy.deepcopy(assigned_activity),
                    "activity_assignment_basis": (
                        "machine_activity_order" if assigned_activity else "none"
                    ),
                    "physical_station_mapping_confirmed": False,
                    "detected_materials_on_machine": active_names,
                    "mapping_note": (
                        "任务按本机活动文件排序展示，尚未绑定到具体 COM 口。"
                        if assigned_activity
                        else "本机存在其他活动任务，但尚未绑定到具体 COM 口。"
                        if active_names
                        else "当前没有可用于材料识别的连续文件写入。"
                    ),
                }
            )
        return stations

    def _public_machine(
        self,
        machine: dict[str, Any],
        *,
        material_map: Mapping[str, dict[str, Any]],
        now: dt.datetime,
    ) -> dict[str, Any]:
        files = self._annotate_files(machine, now=now) if machine.get("reachable") else []
        activities = self._material_activities(
            machine,
            files,
            material_map=material_map,
        )
        stations = self._stations(machine, activities=activities)
        processes = self._station_process_rows(machine)
        ports = _as_rows(machine.get("serial_ports"))
        active = any(item["activity_status"] == "active" for item in activities)
        online_stations = sum(1 for station in stations if station["online"])
        root_states = _as_rows(machine.get("roots"))
        roots_ready = bool(root_states) and all(
            root.get("exists") is True and root.get("scan_ok") is True
            for root in root_states
        )
        if not machine.get("reachable"):
            status = "offline"
            status_label = str(machine.get("message") or "电脑或 SSH 不可达")
        elif active:
            status = "active"
            status_label = "检测到材料目录持续写入"
        elif not roots_ready:
            status = "attention"
            status_label = "部分搜索目录无法读取"
        elif online_stations == 2:
            status = "online"
            status_label = "两套软件与串口在线，未观察到写入"
        else:
            status = "attention"
            status_label = "部分软件或串口信号不完整"
        return {
            "id": str(machine.get("id") or ""),
            "name": str(machine.get("name") or ""),
            "hostname": str(machine.get("hostname") or ""),
            "ip": str(machine.get("ip") or ""),
            "reachable": machine.get("reachable") is True,
            "checked_at_utc": str(machine.get("checked_at_utc") or ""),
            "scan_mode": str(machine.get("scan_mode") or ""),
            "status": status,
            "status_label": status_label,
            "software_instances": len(processes),
            "serial_ports_online": len(ports),
            "serial_probe_ok": machine.get("serial_probe_ok") is True,
            "root_states": [
                {
                    "label": str(root.get("label") or ""),
                    "exists": root.get("exists") is True,
                    "scan_ok": root.get("scan_ok") is True,
                    "observed_files": max(0, int(root.get("observed_files") or 0)),
                }
                for root in _as_rows(machine.get("roots"))
            ],
            "material_activities": activities,
            "stations": stations,
            "assignment_note": (
                "材料由正在变化的子文件夹识别；材料与具体 COM 口的对应关系仍待确认。"
                if active
                else "未观察到文件连续变化时，仅显示最近材料，不判定实验正在运行。"
            ),
        }


__all__ = [
    "DEFAULT_DISCOVERY_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_STALE_SECONDS",
    "MONITOR_SCHEMA_VERSION",
    "WorkstationMonitor",
    "WorkstationStateFile",
    "infer_task",
    "workstation_probe_script",
    "workstation_root_probe_script",
    "workstation_signal_script",
]
