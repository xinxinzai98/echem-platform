"""Common start-stop errors, normalized scalar values and comparison rules."""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from typing import Any


class StartStopWorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


class _UnchangedRepositoryScan(Exception):
    def __init__(self, result: dict[str, Any], *, warning: bool) -> None:
        super().__init__("repository snapshot already published")
        self.result = dict(result)
        self.warning = bool(warning)


_DUPLICATE_PLOT_NAME_QUOTED = re.compile(
    r'绘图名称[“"](?P<name>[^”"\r\n]{1,180})[”"][^\r\n]{0,600}?重复'
)
_DUPLICATE_PLOT_NAME_PLAIN = re.compile(
    r"绘图名称重复[：:]\s*(?P<name>[^\r\n；;]{1,180})"
)


def duplicate_plot_name_notice(value: Any) -> str | None:
    """Convert a path-bearing analyzer collision into a safe UI notice."""
    if not isinstance(value, str):
        return None
    match = _DUPLICATE_PLOT_NAME_QUOTED.search(value)
    if match is None:
        match = _DUPLICATE_PLOT_NAME_PLAIN.search(value)
    if match is None:
        return None
    name = unicodedata.normalize("NFC", match.group("name"))
    name = " ".join(name.split()).strip()
    if not name or any(ord(character) < 32 for character in name):
        return None
    # A display name may contain slash-like punctuation. Use full-width
    # characters so the public notice can never be mistaken for a local path.
    name = name.replace("/", "／").replace("\\", "＼")[:120].rstrip()
    if not name:
        return None
    return (
        f"检测到重复的绘图名称：“{name}”。"
        "新数据已保留，请到“材料库”修改完整绘图名称后重试更新。"
    )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "是"}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _work_step_current_ma_cm2(value: Any) -> int | None:
    """Return a stable protocol current without splitting on acquisition noise.

    ADT exports report measured medians such as -299.344 and +29.954
    mA cm-2. Five-milliamp bins retain meaningful protocol differences while
    grouping those values with their nominal -300/+30 mA cm-2 work step.
    """
    parsed = _number(value)
    if parsed is None:
        return None
    quantized = int(round((parsed * 1000.0) / 5.0) * 5)
    return 0 if quantized == 0 else quantized


def _work_step_duration_s(value: Any) -> int | None:
    parsed = _number(value)
    if parsed is None or parsed < 0:
        return None
    return max(0, int(round(parsed)))


def _work_step_number_token(value: int | None) -> str:
    return "na" if value is None else str(value)


def _format_signed_work_step_current(value: int | None) -> str:
    if value is None:
        return "?"
    if value < 0:
        return f"−{abs(value)}"
    if value > 0:
        return f"+{value}"
    return "0"


def _work_step_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Build the scientific comparison key for one analyzed series."""
    test_type = str(row.get("test_type") or "unknown_start_stop").strip()
    test_label = str(row.get("test_type_label_zh") or "").strip()
    if not test_label:
        test_label = {
            "standard_start_stop": "标准启停",
            "variable_start_stop": "变工步启停",
            "adt_start_stop": "ADT 启停",
        }.get(test_type, "启停工步")
    cathodic_current = _work_step_current_ma_cm2(
        row.get("cathodic_current_median_a_cm2")
    )
    recovery_current = _work_step_current_ma_cm2(
        row.get("recovery_current_median_a_cm2")
    )
    cathodic_duration = _work_step_duration_s(
        row.get("median_cathodic_phase_duration_s")
    )
    recovery_duration = _work_step_duration_s(
        row.get("median_reverse_phase_duration_s")
    )
    key = "|".join(
        (
            test_type,
            f"jc={_work_step_number_token(cathodic_current)}",
            f"jr={_work_step_number_token(recovery_current)}",
            f"tc={_work_step_number_token(cathodic_duration)}",
            f"tr={_work_step_number_token(recovery_duration)}",
        )
    )
    current_label = (
        f"{_format_signed_work_step_current(cathodic_current)} ↔ "
        f"{_format_signed_work_step_current(recovery_current)} mA·cm⁻²"
    )
    if cathodic_duration is None or recovery_duration is None:
        duration_label = "阶段时长未识别"
    elif (
        cathodic_duration > 0
        and recovery_duration > 0
        and cathodic_duration % 60 == 0
        and recovery_duration % 60 == 0
    ):
        duration_label = (
            f"{cathodic_duration // 60} + {recovery_duration // 60} min"
        )
    else:
        duration_label = f"{cathodic_duration} + {recovery_duration} s"
    return {
        "work_step_key": key,
        "work_step_label": f"{test_label} · {current_label} · {duration_label}",
        "work_step_test_type": test_type,
        "work_step_test_type_label_zh": test_label,
        "work_step_cathodic_current_ma_cm2": cathodic_current,
        "work_step_recovery_current_ma_cm2": recovery_current,
        "work_step_cathodic_duration_s": cathodic_duration,
        "work_step_recovery_duration_s": recovery_duration,
    }
