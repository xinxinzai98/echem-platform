"""CHI protocol compilation plus the opt-in, loopback-only Stage C safety gate."""

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
    ControlSafetyError,
    MacroInspection,
    MacroValidationError,
    NormalizedProtocol,
    NormalizedStep,
    ProtocolValidationError,
    ValidationIssue,
)
from .stage_c import (
    REQUIRED_CONFIRMATIONS,
    STAGE_C_DURATION_SECONDS,
    STAGE_C_ID,
    ProcessInfo,
    ProcessObservation,
    StageCManager,
    WindowsChiAdapter,
    build_stage_c_preflight,
    stage_c_profile_sha256,
    validate_stage_c_protocol,
)
from .validation import normalize_protocol

__all__ = [
    "CHI_MACRO_HEADER",
    "CONTROL_COMPILER_VERSION",
    "DRY_RUN_STAGE",
    "PROTOCOL_SCHEMA_VERSION",
    "CompiledMacro",
    "ControlSafetyError",
    "MacroInspection",
    "MacroValidationError",
    "NormalizedProtocol",
    "NormalizedStep",
    "ProtocolValidationError",
    "ProcessInfo",
    "ProcessObservation",
    "REQUIRED_CONFIRMATIONS",
    "STAGE_C_DURATION_SECONDS",
    "STAGE_C_ID",
    "StageCManager",
    "ValidationIssue",
    "build_dry_run",
    "build_stage_c_preflight",
    "compile_protocol",
    "default_dry_run_draft",
    "dry_run_capabilities",
    "inspect_macro",
    "normalize_protocol",
    "normalize_run_folder",
    "stage_c_profile_sha256",
    "validate_stage_c_protocol",
    "WindowsChiAdapter",
]
