#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from echem_platform.control import (
    MacroValidationError,
    ProtocolValidationError,
    compile_protocol,
)


MAX_PROTOCOL_BYTES = 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "离线校验 CHI 协议并编译宏；不会启动 CHI 软件、访问串口或连接仪器。"
        )
    )
    parser.add_argument("protocol", type=Path, help="协议 JSON 文件")
    parser.add_argument(
        "--output-folder",
        required=True,
        help="宏内的 Windows 新运行目录，例如 D:/EchemPlatform/Runs/RUN-001",
    )
    parser.add_argument(
        "--allowed-run-root",
        required=True,
        help="允许的 Windows 运行根目录",
    )
    parser.add_argument(
        "--macro-output",
        type=Path,
        help="可选：在本机独占创建 .mcr 文件；已存在时拒绝覆盖",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="输出紧凑 JSON",
    )
    return parser.parse_args()


def _load_protocol(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_PROTOCOL_BYTES:
            raise ValueError("协议 JSON 超过 1 MiB。")
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError("找不到协议 JSON。") from exc
    except UnicodeError as exc:
        raise ValueError("协议 JSON 必须使用 UTF-8。") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"协议 JSON 格式错误（第 {exc.lineno} 行）。") from exc
    except OSError as exc:
        raise ValueError("无法读取协议 JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("协议 JSON 顶层必须是对象。")
    return payload


def _write_macro_exclusive(path: Path, payload: bytes) -> None:
    if path.suffix.lower() != ".mcr":
        raise ValueError("宏输出文件必须使用 .mcr 扩展名。")
    if not path.parent.is_dir():
        raise ValueError("宏输出目录必须已经存在。")
    created = False
    try:
        with path.open("xb") as handle:
            created = True
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ValueError("宏输出文件已存在；离线编译器拒绝覆盖。") from None
    except OSError as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise ValueError("无法安全写入宏输出文件。") from exc
    try:
        if path.read_bytes() != payload:
            path.unlink()
            raise ValueError("宏写入后的二进制复核失败。")
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise ValueError("无法复核已写入的宏文件。") from exc


def _print_json(payload: dict, *, compact: bool, stream=None) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if compact else 2,
        ),
        file=stream or sys.stdout,
    )


def main() -> int:
    args = parse_args()
    try:
        protocol = _load_protocol(args.protocol)
        compiled = compile_protocol(
            protocol,
            output_folder=args.output_folder,
            allowed_run_root=args.allowed_run_root,
        )
        if args.macro_output is not None:
            _write_macro_exclusive(args.macro_output, compiled.payload)
        report = compiled.report()
        report["macro_written"] = args.macro_output is not None
        if args.macro_output is not None:
            report["macro_output_name"] = args.macro_output.name
        _print_json(report, compact=args.compact)
        return 0
    except ProtocolValidationError as exc:
        _print_json(exc.to_dict(), compact=args.compact, stream=sys.stderr)
        return 2
    except MacroValidationError as exc:
        _print_json(exc.to_dict(), compact=args.compact, stream=sys.stderr)
        return 3
    except ValueError as exc:
        _print_json(
            {
                "error": "offline_compile_failed",
                "message": str(exc),
            },
            compact=args.compact,
            stream=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
