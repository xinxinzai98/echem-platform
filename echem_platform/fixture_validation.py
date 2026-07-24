from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .parsers import PARSER_VERSION, ParsedCurve, parse_curve


SCHEMA_VERSION = 1
TOP_LEVEL_KEYS = {
    "schema_version",
    "max_file_bytes",
    "max_points_per_curve",
    "cases",
}
EXPECTED_KEYS = {
    "status",
    "instrument",
    "technique",
    "parser_id",
    "point_count",
    "min_points",
    "max_points",
    "x_name",
    "x_unit",
    "y_name",
    "y_unit",
    "sha256",
}
REQUIRED_EXPECTATIONS = {"status", "instrument", "technique", "parser_id"}
STRING_EXPECTATIONS = {
    "status",
    "instrument",
    "technique",
    "parser_id",
    "x_name",
    "x_unit",
    "y_name",
    "y_unit",
}
INTEGER_EXPECTATIONS = {"point_count", "min_points", "max_points"}
CASE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ManifestError(ValueError):
    """Raised when a private fixture manifest is unsafe or malformed."""


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded_integer(
    payload: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = payload.get(key, default)
    if not _plain_int(value) or not minimum <= value <= maximum:
        raise ManifestError(f"{key} 必须是 {minimum} 到 {maximum} 之间的整数。")
    return value


def _safe_case_path(base: Path, raw: Any) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError("每个样例的 file 必须是非空相对路径。")
    normalized = raw.strip().replace("\\", "/")
    if re.match(r"^[a-zA-Z]:/", normalized) or normalized.startswith(("/", "//")):
        raise ManifestError("样例 file 不允许使用绝对路径。")
    pure_path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in pure_path.parts):
        raise ManifestError("样例 file 不允许包含空段、`.` 或 `..`。")
    try:
        candidate = (base / Path(*pure_path.parts)).resolve()
    except OSError as exc:
        raise ManifestError("无法安全解析样例相对路径。") from exc
    if candidate == base or base not in candidate.parents:
        raise ManifestError("样例 file 必须位于清单所在目录内。")
    return candidate.suffix.lower(), candidate


def _validate_expected(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError("每个样例的 expected 必须是 JSON 对象。")
    unknown = sorted(set(raw) - EXPECTED_KEYS)
    if unknown:
        raise ManifestError(f"expected 包含未知字段：{', '.join(unknown)}")
    missing = sorted(REQUIRED_EXPECTATIONS - set(raw))
    if missing:
        raise ManifestError(f"expected 缺少必要字段：{', '.join(missing)}")

    expected = dict(raw)
    for key in STRING_EXPECTATIONS & set(expected):
        if not isinstance(expected[key], str):
            raise ManifestError(f"expected.{key} 必须是字符串。")
    for key in REQUIRED_EXPECTATIONS:
        if not expected[key].strip():
            raise ManifestError(f"expected.{key} 不能为空。")
    if expected["status"] not in {"parsed", "metadata_only", "unparsed"}:
        raise ManifestError("expected.status 不是支持的解析状态。")
    for key in INTEGER_EXPECTATIONS & set(expected):
        if not _plain_int(expected[key]) or expected[key] < 0:
            raise ManifestError(f"expected.{key} 必须是非负整数。")
    if (
        "min_points" in expected
        and "max_points" in expected
        and expected["min_points"] > expected["max_points"]
    ):
        raise ManifestError("expected.min_points 不能大于 expected.max_points。")
    if (
        "point_count" in expected
        and "min_points" in expected
        and expected["point_count"] < expected["min_points"]
    ):
        raise ManifestError("expected.point_count 不能小于 expected.min_points。")
    if (
        "point_count" in expected
        and "max_points" in expected
        and expected["point_count"] > expected["max_points"]
    ):
        raise ManifestError("expected.point_count 不能大于 expected.max_points。")
    if "sha256" in expected:
        if not isinstance(expected["sha256"], str) or not SHA256_PATTERN.fullmatch(
            expected["sha256"]
        ):
            raise ManifestError("expected.sha256 必须是 64 位小写十六进制。")
    return expected


def load_fixture_manifest(path: Path) -> dict[str, Any]:
    """Load and validate a private fixture manifest without reading fixture contents."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ManifestError("找不到私有样例清单。") from exc
    except OSError as exc:
        raise ManifestError("无法读取私有样例清单。") from exc
    except UnicodeError as exc:
        raise ManifestError("私有样例清单必须使用 UTF-8 编码。") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"私有样例清单不是有效 JSON（第 {exc.lineno} 行）。") from exc
    if not isinstance(raw, dict):
        raise ManifestError("私有样例清单顶层必须是 JSON 对象。")
    unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
    if unknown:
        raise ManifestError(f"私有样例清单包含未知字段：{', '.join(unknown)}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"schema_version 必须是 {SCHEMA_VERSION}。")

    max_file_bytes = _bounded_integer(
        raw,
        "max_file_bytes",
        50 * 1024 * 1024,
        minimum=1024,
        maximum=1024 * 1024 * 1024,
    )
    max_points = _bounded_integer(
        raw,
        "max_points_per_curve",
        2000,
        minimum=100,
        maximum=1_000_000,
    )
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ManifestError("cases 必须是至少包含一个样例的数组。")

    base = path.resolve().parent
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            raise ManifestError(f"第 {index} 个样例必须是 JSON 对象。")
        unknown_case_keys = sorted(set(raw_case) - {"id", "file", "expected"})
        if unknown_case_keys:
            raise ManifestError(
                f"第 {index} 个样例包含未知字段：{', '.join(unknown_case_keys)}"
            )
        case_id = raw_case.get("id")
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise ManifestError(
                "样例 id 必须由小写字母、数字、点、下划线或连字符组成，最长 64 位。"
            )
        if case_id in seen_ids:
            raise ManifestError(f"样例 id 重复：{case_id}")
        seen_ids.add(case_id)
        suffix, fixture_path = _safe_case_path(base, raw_case.get("file"))
        cases.append(
            {
                "id": case_id,
                "suffix": suffix,
                "path": fixture_path,
                "expected": _validate_expected(raw_case.get("expected")),
            }
        )

    return {
        "max_file_bytes": max_file_bytes,
        "max_points_per_curve": max_points,
        "cases": cases,
    }


def _failed_case(
    case_id: str,
    suffix: str,
    message: str,
    *,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case_id": case_id,
        "passed": False,
        "file_type": suffix or "unknown",
        "errors": [message],
    }
    if size_bytes is not None:
        result["size_bytes"] = size_bytes
    return result


def _compare_curve(
    curve: ParsedCurve,
    expected: dict[str, Any],
    fingerprint: str,
) -> list[str]:
    errors: list[str] = []
    exact_fields = (
        "status",
        "instrument",
        "technique",
        "parser_id",
        "point_count",
        "x_name",
        "x_unit",
        "y_name",
        "y_unit",
    )
    for field in exact_fields:
        if field in expected and getattr(curve, field) != expected[field]:
            errors.append(f"{field} 不符合清单预期。")
    if "min_points" in expected and curve.point_count < expected["min_points"]:
        errors.append("point_count 小于清单下限。")
    if "max_points" in expected and curve.point_count > expected["max_points"]:
        errors.append("point_count 大于清单上限。")
    if "sha256" in expected and fingerprint != expected["sha256"]:
        errors.append("sha256 不符合清单预期。")
    return errors


def validate_fixture_manifest(path: Path) -> dict[str, Any]:
    """Validate private fixtures read-only and return a path-redacted report."""
    manifest = load_fixture_manifest(path)
    results: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        case_id = case["id"]
        suffix = case["suffix"]
        fixture_path = case["path"]
        try:
            before = fixture_path.stat()
        except OSError:
            results.append(_failed_case(case_id, suffix, "样例文件不存在或不可读取。"))
            continue
        if not fixture_path.is_file():
            results.append(_failed_case(case_id, suffix, "样例路径不是普通文件。"))
            continue
        if before.st_size > manifest["max_file_bytes"]:
            results.append(
                _failed_case(
                    case_id,
                    suffix,
                    "样例文件超过清单允许的大小。",
                    size_bytes=before.st_size,
                )
            )
            continue
        try:
            data = fixture_path.read_bytes()
            after = fixture_path.stat()
        except OSError:
            results.append(
                _failed_case(
                    case_id,
                    suffix,
                    "读取样例文件失败。",
                    size_bytes=before.st_size,
                )
            )
            continue
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            results.append(
                _failed_case(
                    case_id,
                    suffix,
                    "样例文件在读取期间发生变化。",
                    size_bytes=after.st_size,
                )
            )
            continue

        fingerprint = hashlib.sha256(data).hexdigest()
        try:
            curve = parse_curve(
                fixture_path,
                data,
                manifest["max_points_per_curve"],
            )
        except Exception:
            results.append(
                _failed_case(
                    case_id,
                    suffix,
                    "解析器无法安全处理该样例。",
                    size_bytes=after.st_size,
                )
            )
            continue
        try:
            final_data = fixture_path.read_bytes()
            final = fixture_path.stat()
        except OSError:
            results.append(
                _failed_case(
                    case_id,
                    suffix,
                    "解析后无法复核样例文件。",
                    size_bytes=after.st_size,
                )
            )
            continue
        source_unchanged = (
            before.st_size == final.st_size
            and before.st_mtime_ns == final.st_mtime_ns
            and hashlib.sha256(final_data).hexdigest() == fingerprint
        )
        errors = _compare_curve(curve, case["expected"], fingerprint)
        if not source_unchanged:
            errors.append("样例文件在解析验证期间发生变化。")
        results.append(
            {
                "case_id": case_id,
                "passed": not errors,
                "file_type": suffix or "unknown",
                "size_bytes": final.st_size,
                "sha256": fingerprint,
                "source_unchanged": source_unchanged,
                "status": curve.status,
                "instrument": curve.instrument,
                "technique": curve.technique,
                "parser_id": curve.parser_id,
                "point_count": curve.point_count,
                "errors": errors,
            }
        )

    passed_count = sum(result["passed"] for result in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "read_only": True,
        "privacy": {
            "absolute_paths_included": False,
            "file_names_included": False,
            "raw_content_included": False,
        },
        "passed": passed_count == len(results),
        "summary": {
            "total": len(results),
            "passed": passed_count,
            "failed": len(results) - passed_count,
        },
        "cases": results,
    }
