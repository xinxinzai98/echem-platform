#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echem_platform.control import (  # noqa: E402
    ControlSafetyError,
    ProtocolValidationError,
    stage_c_profile_sha256,
    validate_stage_c_protocol,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查一个协议是否满足阶段 C 的 60 秒 OCP 范围。",
    )
    parser.add_argument("protocol", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.protocol.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("协议顶层必须是 JSON 对象。")
        normalized = validate_stage_c_protocol(payload)
        step = normalized.active_steps[0]
        report = {
            "eligible": True,
            "stage": "ocp_60s_preflight",
            "active_step_count": 1,
            "technique": "ocp",
            "duration_s": step.params["duration_s"],
            "profile_sha256": stage_c_profile_sha256(payload),
            "instrument_started": False,
            "files_written": False,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except FileNotFoundError:
        print("协议文件不存在。", file=sys.stderr)
        return 2
    except json.JSONDecodeError:
        print("协议文件不是有效 JSON。", file=sys.stderr)
        return 3
    except (ValueError, ProtocolValidationError, ControlSafetyError) as exc:
        print(str(exc), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
