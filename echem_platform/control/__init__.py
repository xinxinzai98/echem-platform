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
    "PROTOCOL_SCHEMA_VERSION",
    "CompiledMacro",
    "MacroInspection",
    "MacroValidationError",
    "NormalizedProtocol",
    "NormalizedStep",
    "ProtocolValidationError",
    "ValidationIssue",
    "compile_protocol",
    "inspect_macro",
    "normalize_protocol",
    "normalize_run_folder",
]
