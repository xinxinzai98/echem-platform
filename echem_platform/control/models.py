from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CONTROL_COMPILER_VERSION = "0.3.0-dev.1"
PROTOCOL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
        }


class ProtocolValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = tuple(issues)
        message = "；".join(f"{issue.path}: {issue.message}" for issue in issues)
        super().__init__(message or "协议校验失败。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "protocol_validation_failed",
            "issues": [issue.to_dict() for issue in self.issues],
        }


class MacroValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {
            "error": "macro_validation_failed",
            "code": self.code,
            "message": str(self),
        }


@dataclass(frozen=True)
class NormalizedStep:
    id: str
    name: str
    technique: str
    enabled: bool
    save_basename: str
    params: dict[str, Any]
    expected_seconds: float | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "technique": self.technique,
            "enabled": self.enabled,
            "save_basename": self.save_basename,
            "params": dict(self.params),
            "expected_seconds": self.expected_seconds,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class NormalizedProtocol:
    schema_version: int
    name: str
    execution_mode: str
    metadata: dict[str, Any]
    steps: tuple[NormalizedStep, ...]

    @property
    def active_steps(self) -> tuple[NormalizedStep, ...]:
        return tuple(step for step in self.steps if step.enabled)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            warning
            for step in self.active_steps
            for warning in step.warnings
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "execution_mode": self.execution_mode,
            **dict(self.metadata),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class MacroCommand:
    name: str
    value: str | None
    line_number: int


@dataclass(frozen=True)
class MacroInspection:
    sha256: str
    body: str
    folder: str
    commands: tuple[MacroCommand, ...]
    techniques: tuple[str, ...]
    save_basenames: tuple[str, ...]
    run_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "folder": self.folder,
            "techniques": list(self.techniques),
            "save_basenames": list(self.save_basenames),
            "run_count": self.run_count,
            "command_count": len(self.commands),
        }


@dataclass(frozen=True)
class CompiledMacro:
    payload: bytes
    body: str
    macro_sha256: str
    protocol_sha256: str
    compiler_version: str
    output_folder: str
    normalized_protocol: NormalizedProtocol
    inspection: MacroInspection

    def report(self) -> dict[str, Any]:
        return {
            "compiler_version": self.compiler_version,
            "protocol_sha256": self.protocol_sha256,
            "macro_sha256": self.macro_sha256,
            "macro_bytes": len(self.payload),
            "output_folder": self.output_folder,
            "active_step_count": len(self.normalized_protocol.active_steps),
            "techniques": list(self.inspection.techniques),
            "save_basenames": list(self.inspection.save_basenames),
            "warnings": list(self.normalized_protocol.warnings),
            "instrument_started": False,
            "instrument_control_enabled": False,
        }
