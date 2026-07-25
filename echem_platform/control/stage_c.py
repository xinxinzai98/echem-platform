from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from .macro_compiler import compile_protocol
from .models import ControlSafetyError, NormalizedProtocol
from .validation import normalize_protocol


STAGE_C_ID = "ocp_60s_preflight"
STAGE_C_DURATION_SECONDS = 60.0
ACTIVE_RUN_STATUSES = {"starting", "running", "stop_requested"}
REQUIRED_CONFIRMATIONS = (
    "electrodes_connected",
    "electrolyte_ready",
    "reference_confirmed",
    "gas_ventilation_ready",
    "no_other_chi_test",
    "operator_present",
    "chi_stop_available",
)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stage_c_profile_sha256(protocol: dict[str, Any]) -> str:
    """Hash only the executable OCP profile, excluding sample metadata and filenames."""
    normalized = validate_stage_c_protocol(protocol)
    step = normalized.active_steps[0]
    profile = {
        "stage": STAGE_C_ID,
        "technique": step.technique,
        "params": step.params,
    }
    return _sha256_bytes(_canonical_json(profile))


def validate_stage_c_protocol(protocol: dict[str, Any]) -> NormalizedProtocol:
    normalized = normalize_protocol(protocol)
    active = normalized.active_steps
    if len(active) != 1 or active[0].technique != "ocpt":
        raise ControlSafetyError(
            "stage_c_scope",
            "阶段 C 只允许一个已启用的 OCP 工步。",
            status=400,
        )
    duration = float(active[0].params["duration_s"])
    if abs(duration - STAGE_C_DURATION_SECONDS) > 1e-9:
        raise ControlSafetyError(
            "stage_c_duration",
            "阶段 C 只允许 60 秒 OCP。",
            status=400,
        )
    return normalized


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    image_name: str


@dataclass(frozen=True)
class ProcessObservation:
    alive: bool
    exit_code: int | None


class WindowsChiAdapter:
    """Minimal Windows process boundary; it never opens a serial port or uses a shell."""

    def __init__(self) -> None:
        self._handles: dict[int, subprocess.Popen[bytes]] = {}
        self.supported = os.name == "nt"

    def list_chi_processes(self) -> list[ProcessInfo]:
        if not self.supported:
            raise RuntimeError("CHI 进程探测只支持 Windows。")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [
                "tasklist.exe",
                "/FI",
                "IMAGENAME eq chi760e.exe",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=creation_flags,
        )
        if completed.returncode != 0:
            raise RuntimeError("无法读取 CHI 进程列表。")
        processes: list[ProcessInfo] = []
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) < 2 or row[0].casefold() != "chi760e.exe":
                continue
            try:
                pid = int(row[1].replace(",", ""))
            except ValueError:
                continue
            processes.append(ProcessInfo(pid=pid, image_name=row[0]))
        return processes

    def launch(self, executable: Path, working_directory: Path, macro_path: Path) -> int:
        if not self.supported:
            raise RuntimeError("CHI 启动器只支持 Windows。")
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            [str(executable), f"/runmacro:{macro_path}"],
            cwd=str(working_directory),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            creationflags=creation_flags,
        )
        self._handles[process.pid] = process
        return process.pid

    def poll(self, pid: int) -> ProcessObservation:
        handle = self._handles.get(pid)
        if handle is not None:
            exit_code = handle.poll()
            return ProcessObservation(alive=exit_code is None, exit_code=exit_code)
        alive = any(item.pid == pid for item in self.list_chi_processes())
        return ProcessObservation(alive=alive, exit_code=None)


def _check(check_id: str, label: str, passed: bool, detail: str = "") -> dict[str, str]:
    return {
        "id": check_id,
        "label": label,
        "status": "passed" if passed else "blocked",
        "detail": detail,
    }


def build_stage_c_preflight(
    config: dict[str, Any],
    *,
    adapter: Any,
    active_run_count: int,
    run_root_path: Path | None = None,
    global_lock_run_id: str = "",
) -> dict[str, Any]:
    """Collect read-only readiness evidence. This function never creates a directory."""
    checks: list[dict[str, str]] = []
    enabled = bool(config.get("instrument_control_enabled"))
    checks.append(
        _check(
            "control_enabled",
            "本机私有配置已显式启用阶段 C 控制。",
            enabled and config.get("control_stage") == "ocp_60s",
        )
    )
    checks.append(
        _check(
            "loopback_only",
            "网页服务仅监听本机回环地址。",
            config.get("bind") in {"127.0.0.1", "::1", "localhost"},
        )
    )
    checks.append(
        _check(
            "adapter_supported",
            "当前系统支持 Windows CHI 进程探测。",
            bool(getattr(adapter, "supported", False)),
        )
    )

    executable_raw = str(config.get("chi_executable", "")).strip()
    working_directory_raw = str(config.get("chi_working_directory", "")).strip()
    control_root_raw = str(config.get("control_root", "")).strip()
    run_root_raw = str(config.get("run_root", "")).strip()
    executable = Path(executable_raw) if executable_raw else None
    working_directory = (
        Path(working_directory_raw) if working_directory_raw else None
    )
    control_root = Path(control_root_raw) if control_root_raw else None
    native_run_root = run_root_path or (Path(run_root_raw) if run_root_raw else None)

    executable_exists = executable is not None and executable.is_file()
    checks.append(
        _check(
            "chi_executable",
            "CHI760E 可执行文件存在。",
            executable_exists,
            str(executable) if executable is not None else "未配置",
        )
    )
    checks.append(
        _check(
            "chi_working_directory",
            "CHI760E 工作目录存在。",
            working_directory is not None and working_directory.is_dir(),
            str(working_directory) if working_directory is not None else "未配置",
        )
    )
    checks.append(
        _check(
            "control_root",
            "控制程序根目录存在。",
            control_root is not None and control_root.is_dir(),
            str(control_root) if control_root is not None else "未配置",
        )
    )
    run_parent = native_run_root.parent if native_run_root is not None else None
    checks.append(
        _check(
            "run_root_parent",
            "运行目录的上级目录存在。",
            run_parent is not None and run_parent.is_dir(),
            str(run_parent) if run_parent is not None else "未配置",
        )
    )

    expected_executable_hash = str(config.get("chi_executable_sha256", "")).lower()
    actual_executable_hash = ""
    if executable_exists and executable is not None:
        try:
            actual_executable_hash = _sha256_file(executable)
        except OSError:
            actual_executable_hash = ""
    checks.append(
        _check(
            "chi_executable_hash",
            "CHI760E 可执行文件与本机锁定的 SHA-256 一致。",
            bool(expected_executable_hash)
            and actual_executable_hash == expected_executable_hash,
            actual_executable_hash or "不可读取",
        )
    )

    configured_profile = str(config.get("stage_c_ocp_profile_sha256", "")).lower()
    checks.append(
        _check(
            "ocp_profile_pinned",
            "已锁定经过复核的 60 秒 OCP 参数指纹。",
            len(configured_profile) == 64,
            configured_profile or "未配置",
        )
    )

    processes: list[ProcessInfo] = []
    process_error = ""
    if getattr(adapter, "supported", False):
        try:
            processes = list(adapter.list_chi_processes())
        except Exception as exc:
            process_error = str(exc)
    checks.append(
        _check(
            "single_instance_clear",
            "启动前不存在任何 CHI760E 实例。",
            not process_error and len(processes) == 0,
            process_error or f"当前实例数：{len(processes)}",
        )
    )
    checks.append(
        _check(
            "no_active_platform_run",
            "平台中不存在正在启动或运行的任务。",
            active_run_count == 0,
            f"当前活动任务数：{active_run_count}",
        )
    )
    checks.append(
        _check(
            "global_lock_clear",
            "跨进程仪器运行锁为空。",
            not global_lock_run_id,
            global_lock_run_id or "未占用",
        )
    )

    blocked = [item["id"] for item in checks if item["status"] != "passed"]
    return {
        "stage": STAGE_C_ID,
        "status": "ready" if not blocked else "blocked",
        "ready": not blocked,
        "checks": checks,
        "blocked_checks": blocked,
        "chi_process_count": len(processes),
        "chi_pids": [item.pid for item in processes],
        "serial_access": False,
        "network_control": False,
        "writes_performed": False,
        "instrument_started": False,
    }


class StageCManager:
    def __init__(
        self,
        database: Any,
        config: dict[str, Any],
        *,
        adapter: Any | None = None,
        run_root_path: Path | None = None,
        output_importer: Callable[[Path, Path], dict[str, Any]] | None = None,
        now: Callable[[], dt.datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.config = config
        self.adapter = adapter or WindowsChiAdapter()
        configured_run_root = str(config.get("run_root", "")).strip()
        self.run_root_path = (
            run_root_path
            if run_root_path is not None
            else (Path(configured_run_root) if configured_run_root else None)
        )
        self.output_importer = output_importer
        self.now = now
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self.owner_token = secrets.token_hex(16)
        self._file_observations: dict[tuple[str, str], tuple[int, int, float]] = {}
        self._exit_seen: dict[str, float] = {}
        self._invalidate_stale_arms()

    def _invalidate_stale_arms(self) -> None:
        for run in self.database.list_automation_runs(limit=200):
            if run["status"] == "armed":
                self.database.update_automation_run(
                    run["run_id"],
                    {
                        "status": "prepared",
                        "arm_token_sha256": "",
                        "arm_expires_utc": "",
                        "failure_reason": "",
                    },
                )
                self._event(
                    run["run_id"],
                    "arm_invalidated_restart",
                    "warning",
                    {"message": "平台重启后一次性确认已失效。"},
                )

    def capabilities(self) -> dict[str, Any]:
        enabled = bool(self.config.get("instrument_control_enabled"))
        return {
            "stage": STAGE_C_ID,
            "schema_version": 1,
            "instrument_control_enabled": enabled,
            "instrument_started": False,
            "launch_available": enabled,
            "serial_access": False,
            "network_control": False,
            "allowed_scope": {
                "techniques": ["ocp"],
                "active_steps": 1,
                "duration_s": 60,
            },
            "required_confirmations": list(REQUIRED_CONFIRMATIONS),
            "arm_token_ttl_seconds": self.config["arm_token_ttl_seconds"],
            "preflight_available": True,
        }

    def preflight(self) -> dict[str, Any]:
        global_lock = self.database.automation_control_lock()
        return build_stage_c_preflight(
            self.config,
            adapter=self.adapter,
            active_run_count=self.database.active_automation_run_count(),
            run_root_path=self.run_root_path,
            global_lock_run_id=global_lock["run_id"] if global_lock else "",
        )

    def _require_enabled(self) -> None:
        if not self.config.get("instrument_control_enabled"):
            raise ControlSafetyError(
                "control_disabled",
                "阶段 C 仪器控制未在本机私有配置中启用。",
                status=404,
            )

    def _require_ready(self) -> None:
        preflight = self.preflight()
        if not preflight["ready"]:
            raise ControlSafetyError(
                "preflight_blocked",
                "阶段 C 预检未通过：" + ", ".join(preflight["blocked_checks"]),
            )

    def _new_run_id(self) -> str:
        stamp = self.now().strftime("%Y%m%d-%H%M%S")
        return f"RUN-{stamp}-{secrets.token_hex(3).upper()}"

    def _event(
        self,
        run_id: str,
        event_type: str,
        severity: str,
        detail: dict[str, Any],
    ) -> None:
        event = self.database.append_automation_event(
            run_id,
            event_type,
            severity,
            detail,
        )
        run = self.database.get_automation_run(run_id)
        if not run:
            return
        events_path = Path(run["run_directory"]) / "events.jsonl"
        line = _canonical_json(event) + b"\n"
        with events_path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def create_run(self, protocol: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._require_enabled()
            self._require_ready()
            normalized = validate_stage_c_protocol(protocol)
            profile_hash = stage_c_profile_sha256(protocol)
            if profile_hash != self.config["stage_c_ocp_profile_sha256"]:
                raise ControlSafetyError(
                    "ocp_profile_mismatch",
                    "当前 OCP 参数与本机锁定的已复核参数指纹不一致。",
                    status=400,
                )

            run_id = self._new_run_id()
            date_folder = self.now().strftime("%Y%m%d")
            windows_folder = (
                PureWindowsPath(str(self.config["run_root"]))
                / date_folder
                / run_id
            ).as_posix()
            compiled = compile_protocol(
                protocol,
                output_folder=windows_folder,
                allowed_run_root=str(self.config["run_root"]),
            )

            if self.run_root_path is None:
                raise ControlSafetyError(
                    "run_root_missing",
                    "阶段 C 运行目录未配置。",
                )
            date_root = self.run_root_path / date_folder
            date_root.mkdir(parents=True, exist_ok=True)
            run_directory = date_root / run_id
            preparing = date_root / f".preparing-{run_id}"
            preparing.mkdir(exist_ok=False)

            protocol_bytes = json.dumps(
                protocol,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8") + b"\n"
            normalized_bytes = json.dumps(
                normalized.to_dict(),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8") + b"\n"
            snapshot_hashes = {
                "protocol.json": _sha256_bytes(protocol_bytes),
                "protocol.normalized.json": _sha256_bytes(normalized_bytes),
                "protocol.mcr": compiled.macro_sha256,
            }
            manifest = {
                "schema_version": 1,
                "stage": STAGE_C_ID,
                "run_id": run_id,
                "created_utc": _iso(self.now()),
                "output_folder": windows_folder,
                "protocol_sha256": compiled.protocol_sha256,
                "profile_sha256": profile_hash,
                "macro_sha256": compiled.macro_sha256,
                "chi_executable_sha256": self.config["chi_executable_sha256"],
                "snapshot_file_sha256": snapshot_hashes,
                "expected_outputs": [
                    f"{normalized.active_steps[0].save_basename}.bin",
                    f"{normalized.active_steps[0].save_basename}.txt",
                ],
                "instrument_started": False,
            }
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8") + b"\n"
            files = {
                "protocol.json": protocol_bytes,
                "protocol.normalized.json": normalized_bytes,
                "protocol.mcr": compiled.payload,
                "manifest.json": manifest_bytes,
                "events.jsonl": b"",
            }
            for name, payload in files.items():
                with (preparing / name).open("xb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(preparing, run_directory)

            step = normalized.active_steps[0]
            record = {
                "run_id": run_id,
                "protocol_sha256": compiled.protocol_sha256,
                "profile_sha256": profile_hash,
                "macro_sha256": compiled.macro_sha256,
                "snapshot_manifest_sha256": _sha256_bytes(manifest_bytes),
                "status": "prepared",
                "current_step_index": 0,
                "created_utc": _iso(self.now()),
                "output_root": windows_folder,
                "run_directory": str(run_directory),
                "macro_path": str(run_directory / "protocol.mcr"),
                "completion_confirmed": False,
                "failure_reason": "",
            }
            self.database.create_automation_run(
                record,
                [
                    {
                        "step_index": 0,
                        "step_id": step.id,
                        "name": step.name,
                        "technique": step.technique,
                        "params": step.params,
                        "save_basename": step.save_basename,
                        "expected_seconds": step.expected_seconds,
                        "status": "prepared",
                    }
                ],
            )
            self._event(
                run_id,
                "snapshot_prepared",
                "info",
                {
                    "protocol_sha256": compiled.protocol_sha256,
                    "profile_sha256": profile_hash,
                    "macro_sha256": compiled.macro_sha256,
                },
            )
            return self.database.get_automation_run(run_id)

    def _verify_snapshot(self, run: dict[str, Any]) -> None:
        run_directory = Path(run["run_directory"])
        manifest_path = run_directory / "manifest.json"
        if (
            not manifest_path.is_file()
            or _sha256_file(manifest_path)
            != run.get("snapshot_manifest_sha256")
        ):
            raise ControlSafetyError(
                "snapshot_manifest",
                "运行快照清单完整性校验失败。",
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlSafetyError(
                "snapshot_manifest",
                "运行快照清单缺失或损坏。",
            ) from exc
        if (
            manifest.get("protocol_sha256") != run["protocol_sha256"]
            or manifest.get("macro_sha256") != run["macro_sha256"]
        ):
            raise ControlSafetyError(
                "snapshot_manifest",
                "运行快照清单与数据库哈希不一致。",
            )
        checks = manifest.get("snapshot_file_sha256")
        if not isinstance(checks, dict):
            raise ControlSafetyError(
                "snapshot_manifest",
                "运行快照清单缺少文件哈希。",
            )
        if checks.get("protocol.mcr") != run["macro_sha256"]:
            raise ControlSafetyError(
                "snapshot_manifest",
                "运行快照宏哈希与数据库记录不一致。",
            )
        for name, expected in checks.items():
            path = run_directory / name
            if (
                not isinstance(name, str)
                or not isinstance(expected, str)
                or Path(name).name != name
                or not path.is_file()
                or _sha256_file(path) != expected
            ):
                raise ControlSafetyError(
                    "snapshot_integrity",
                    f"运行快照完整性校验失败：{name}",
                )
        try:
            normalized_payload = json.loads(
                (run_directory / "protocol.normalized.json").read_text(encoding="utf-8")
            )
            normalized_hash = _sha256_bytes(_canonical_json(normalized_payload))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlSafetyError(
                "snapshot_protocol",
                "规范化协议快照无法读取。",
            ) from exc
        if normalized_hash != run["protocol_sha256"]:
            raise ControlSafetyError(
                "snapshot_protocol",
                "规范化协议快照与数据库参数哈希不一致。",
            )
        if manifest.get("profile_sha256") != run["profile_sha256"]:
            raise ControlSafetyError(
                "snapshot_profile",
                "运行快照参数指纹与数据库记录不一致。",
            )
        executable = Path(self.config["chi_executable"])
        if (
            not executable.is_file()
            or _sha256_file(executable) != self.config["chi_executable_sha256"]
        ):
            raise ControlSafetyError(
                "chi_executable_changed",
                "CHI760E 可执行文件已变化，拒绝启动。",
            )

    def arm(
        self,
        run_id: str,
        confirmations: dict[str, Any],
        typed_confirmation: str,
    ) -> dict[str, Any]:
        with self.lock:
            self._require_enabled()
            run = self.database.get_automation_run(run_id, include_secret=True)
            if not run:
                raise ControlSafetyError("run_not_found", "运行快照不存在。", status=404)
            if run["status"] not in {"prepared", "armed"}:
                raise ControlSafetyError("run_state", "当前运行状态不能重新确认。")
            if set(confirmations) != set(REQUIRED_CONFIRMATIONS) or not all(
                confirmations.get(key) is True for key in REQUIRED_CONFIRMATIONS
            ):
                raise ControlSafetyError(
                    "confirmations_incomplete",
                    "现场安全确认项不完整。",
                    status=400,
                )
            if typed_confirmation != run_id:
                raise ControlSafetyError(
                    "typed_confirmation",
                    "手工输入的运行编号不匹配。",
                    status=400,
                )
            self._require_ready()
            self._verify_snapshot(run)
            token = secrets.token_urlsafe(32)
            expires = self.now() + dt.timedelta(
                seconds=self.config["arm_token_ttl_seconds"]
            )
            self.database.update_automation_run(
                run_id,
                {
                    "status": "armed",
                    "arm_token_sha256": _sha256_bytes(token.encode("ascii")),
                    "arm_expires_utc": _iso(expires),
                    "confirmations_json": json.dumps(
                        confirmations,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "failure_reason": "",
                },
            )
            self._event(
                run_id,
                "run_armed",
                "warning",
                {"expires_utc": _iso(expires)},
            )
            return {
                "run": self.database.get_automation_run(run_id),
                "arm_token": token,
                "expires_utc": _iso(expires),
            }

    def start(self, run_id: str, arm_token: str) -> dict[str, Any]:
        with self.lock:
            self._require_enabled()
            run = self.database.get_automation_run(run_id, include_secret=True)
            if not run:
                raise ControlSafetyError("run_not_found", "运行快照不存在。", status=404)
            if run["status"] != "armed":
                raise ControlSafetyError("run_state", "运行快照尚未完成一次性现场确认。")
            token_hash = _sha256_bytes(str(arm_token).encode("utf-8"))
            expires_raw = run.get("arm_expires_utc") or ""
            try:
                expires = dt.datetime.fromisoformat(expires_raw)
            except ValueError as exc:
                raise ControlSafetyError("arm_expired", "一次性确认已失效。") from exc
            if (
                not secrets.compare_digest(token_hash, run.get("arm_token_sha256") or "")
                or self.now() >= expires
            ):
                raise ControlSafetyError("arm_expired", "一次性确认无效或已过期。")

            self.database.update_automation_run(
                run_id,
                {
                    "status": "prepared",
                    "arm_token_sha256": "",
                    "arm_expires_utc": "",
                },
            )
            lock_acquired = False
            try:
                self._require_ready()
                self._verify_snapshot(run)
                step = run["steps"][0]
                run_directory = Path(run["run_directory"])
                for suffix in (".bin", ".txt"):
                    if (run_directory / f"{step['save_basename']}{suffix}").exists():
                        raise ControlSafetyError(
                            "output_collision",
                            "目标输出文件已存在，拒绝启动。",
                        )
                lock_acquired = self.database.acquire_automation_control_lock(
                    run_id,
                    self.owner_token,
                )
                if not lock_acquired:
                    raise ControlSafetyError(
                        "global_lock_busy",
                        "另一个平台进程已持有仪器运行锁。",
                    )
                processes = list(self.adapter.list_chi_processes())
                if processes:
                    raise ControlSafetyError(
                        "chi_started_during_preflight",
                        "预检后出现新的 CHI760E 实例，拒绝启动。",
                    )
            except ControlSafetyError as exc:
                if lock_acquired:
                    self.database.release_automation_control_lock(run_id)
                self._event(
                    run_id,
                    "start_blocked",
                    "warning",
                    {"code": exc.code, "message": str(exc)},
                )
                raise
            except Exception as exc:
                if lock_acquired:
                    self.database.release_automation_control_lock(run_id)
                self._event(
                    run_id,
                    "start_blocked",
                    "error",
                    {"code": "process_probe_failed", "message": str(exc)},
                )
                raise ControlSafetyError(
                    "process_probe_failed",
                    "启动前最后一次 CHI 进程检查失败。",
                ) from exc

            self.database.update_automation_run(
                run_id,
                {
                    "status": "starting",
                    "started_utc": _iso(self.now()),
                    "failure_reason": "",
                },
            )
            self._event(run_id, "launch_requested", "warning", {})
            try:
                pid = self.adapter.launch(
                    Path(self.config["chi_executable"]),
                    Path(self.config["chi_working_directory"]),
                    Path(run["macro_path"]),
                )
            except Exception as exc:
                self.database.release_automation_control_lock(run_id)
                self.database.update_automation_run(
                    run_id,
                    {
                        "status": "failed",
                        "failure_reason": "launcher_error",
                        "completed_utc": _iso(self.now()),
                    },
                )
                self._event(
                    run_id,
                    "launch_failed",
                    "error",
                    {"message": str(exc)},
                )
                raise ControlSafetyError(
                    "launcher_error",
                    "CHI760E 启动失败。",
                ) from exc
            self.database.update_automation_run(
                run_id,
                {
                    "status": "running",
                    "chi_pid": int(pid),
                },
            )
            self.database.update_automation_step(
                run_id,
                0,
                {"status": "running", "started_utc": _iso(self.now())},
            )
            self._event(run_id, "chi_started", "warning", {"chi_pid": int(pid)})
            return self.database.get_automation_run(run_id)

    def _outputs_stable(
        self,
        run_id: str,
        paths: tuple[Path, Path],
    ) -> bool:
        now_value = self.monotonic()
        all_stable = True
        for path in paths:
            if not path.is_file():
                all_stable = False
                continue
            stat = path.stat()
            if stat.st_size <= 0:
                all_stable = False
                continue
            key = (run_id, str(path))
            previous = self._file_observations.get(key)
            current = (stat.st_size, stat.st_mtime_ns)
            if previous is None or previous[:2] != current:
                self._file_observations[key] = (*current, now_value)
                all_stable = False
                continue
            if now_value - previous[2] < self.config["stable_age_seconds"]:
                all_stable = False
        return all_stable

    def _complete_from_outputs(
        self,
        run: dict[str, Any],
        binary_path: Path,
        text_path: Path,
        exit_code: int,
    ) -> None:
        before = {
            str(path): {
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256": _sha256_file(path),
            }
            for path in (binary_path, text_path)
        }
        imported = {
            "binary_run_id": None,
            "text_run_id": None,
            "parse_status": "not_imported",
        }
        if self.output_importer is not None:
            imported.update(self.output_importer(binary_path, text_path))
        after = {
            str(path): {
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256": _sha256_file(path),
            }
            for path in (binary_path, text_path)
        }
        source_unchanged = before == after
        parse_status = str(imported.get("parse_status") or "not_imported")
        completion_confirmed = (
            exit_code == 0 and source_unchanged and parse_status == "parsed"
        )
        now_iso = _iso(self.now())
        self.database.update_automation_step(
            run["run_id"],
            0,
            {
                "status": "completed",
                "completed_utc": now_iso,
                "binary_run_id": imported.get("binary_run_id"),
                "text_run_id": imported.get("text_run_id"),
                "data_status": parse_status,
                "binary_sha256": before[str(binary_path)]["sha256"],
                "text_sha256": before[str(text_path)]["sha256"],
                "source_unchanged": source_unchanged,
            },
        )
        self.database.update_automation_run(
            run["run_id"],
            {
                "status": "completed",
                "chi_exit_code": exit_code,
                "completed_utc": now_iso,
                "completion_confirmed": completion_confirmed,
                "failure_reason": "" if completion_confirmed else "data_review_required",
            },
        )
        self._event(
            run["run_id"],
            "run_completed",
            "info" if completion_confirmed else "warning",
            {
                "chi_exit_code": exit_code,
                "parse_status": parse_status,
                "source_unchanged": source_unchanged,
                "completion_confirmed": completion_confirmed,
            },
        )
        self.database.release_automation_control_lock(run["run_id"])

    def poll_all(self) -> None:
        with self.lock:
            for run in self.database.list_active_automation_runs():
                pid = int(run.get("chi_pid") or 0)
                if pid <= 0:
                    continue
                try:
                    observation = self.adapter.poll(pid)
                except Exception as exc:
                    self._event(
                        run["run_id"],
                        "process_probe_failed",
                        "error",
                        {"message": str(exc)},
                    )
                    continue
                if observation.alive:
                    continue

                exit_seen = self._exit_seen.setdefault(
                    run["run_id"],
                    self.monotonic(),
                )
                step = run["steps"][0]
                run_directory = Path(run["run_directory"])
                binary_path = run_directory / f"{step['save_basename']}.bin"
                text_path = run_directory / f"{step['save_basename']}.txt"
                stable = self._outputs_stable(
                    run["run_id"],
                    (binary_path, text_path),
                )
                if observation.exit_code not in {None, 0}:
                    self.database.update_automation_run(
                        run["run_id"],
                        {
                            "status": "failed",
                            "chi_exit_code": observation.exit_code,
                            "completed_utc": _iso(self.now()),
                            "failure_reason": "chi_nonzero_exit",
                        },
                    )
                    self._event(
                        run["run_id"],
                        "chi_nonzero_exit",
                        "error",
                        {"chi_exit_code": observation.exit_code},
                    )
                    self.database.release_automation_control_lock(run["run_id"])
                    continue
                if stable and observation.exit_code == 0:
                    try:
                        self._complete_from_outputs(
                            run,
                            binary_path,
                            text_path,
                            observation.exit_code,
                        )
                    except Exception as exc:
                        self.database.update_automation_run(
                            run["run_id"],
                            {
                                "status": "needs_review",
                                "chi_exit_code": observation.exit_code,
                                "completed_utc": _iso(self.now()),
                                "failure_reason": "output_import_failed",
                            },
                        )
                        self._event(
                            run["run_id"],
                            "output_import_failed",
                            "error",
                            {"message": str(exc)},
                        )
                        self.database.release_automation_control_lock(
                            run["run_id"]
                        )
                    continue
                elapsed = self.monotonic() - exit_seen
                if elapsed < self.config["completion_grace_seconds"]:
                    continue
                failure_reason = (
                    "exit_code_unavailable_after_restart"
                    if observation.exit_code is None
                    else "output_files_missing_or_unstable"
                )
                self.database.update_automation_run(
                    run["run_id"],
                    {
                        "status": "needs_review"
                        if observation.exit_code is None
                        else "failed",
                        "completed_utc": _iso(self.now()),
                        "failure_reason": failure_reason,
                    },
                )
                self._event(
                    run["run_id"],
                    failure_reason,
                    "warning" if observation.exit_code is None else "error",
                    {},
                )
                self.database.release_automation_control_lock(run["run_id"])

    def request_stop(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            self._require_enabled()
            run = self.database.get_automation_run(run_id)
            if not run:
                raise ControlSafetyError("run_not_found", "运行快照不存在。", status=404)
            if run["status"] not in {"starting", "running", "stop_requested"}:
                raise ControlSafetyError("run_state", "当前运行不接受停止请求。")
            self.database.update_automation_run(
                run_id,
                {"status": "stop_requested"},
            )
            self._event(
                run_id,
                "stop_requested",
                "warning",
                {
                    "message": "请在 CHI 软件中人工停止，并现场确认 celloff。",
                    "process_killed": False,
                },
            )
            return self.database.get_automation_run(run_id)
