"""Offline-only CHI protocol validation and macro compilation.

This package intentionally contains no launcher, subprocess, serial-port, or
network control implementation.
"""

from .macro_compiler import (
    CHI_MACRO_HEADER,
    compile_protocol,
    inspect_macro,
    normalize_run_folder,
)
from .dry_run import (
    DRY_RUN_STAGE,
    build_dry_run,
    default_dry_run_draft,
    dry_run_capabilities,
)
from .models import (
    CONTROL_COMPILER_VERSION,
    PROTOCOL_SCHEMA_VERSION,
    CompiledMacro,
    MacroInspection,
    MacroValidationError,
    NormalizedProtocol,
    NormalizedStep,
    ProtocolValidationError,
    ValidationIssue,
)
from .validation import normalize_protocol

__all__ = [
    "CHI_MACRO_HEADER",
    "CONTROL_COMPILER_VERSION",
    "DRY_RUN_STAGE",
    "PROTOCOL_SCHEMA_VERSION",
    "CompiledMacro",
    "MacroInspection",
    "MacroValidationError",
    "NormalizedProtocol",
    "NormalizedStep",
    "ProtocolValidationError",
    "ValidationIssue",
    "build_dry_run",
    "compile_protocol",
    "default_dry_run_draft",
    "dry_run_capabilities",
    "inspect_macro",
    "normalize_protocol",
    "normalize_run_folder",
]
