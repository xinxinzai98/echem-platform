#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from echem_platform.fixture_validation import ManifestError, validate_fixture_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读验证私有 CHI / CorrTest 样例，不输出路径、文件名或原始数据。"
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "private_fixtures" / "manifest.local.json",
        help="私有样例清单路径",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="输出紧凑 JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = validate_fixture_manifest(args.manifest)
    except ManifestError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": False,
                    "manifest_error": str(exc),
                },
                ensure_ascii=False,
                indent=None if args.compact else 2,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=None if args.compact else 2,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
