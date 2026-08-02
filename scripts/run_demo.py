#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED: dict[str, dict[str, Any]] = {
    "chi_cv_demo.txt": {
        "instrument": "CHI",
        "technique": "CV",
        "parse_status": "parsed",
        "point_count": 29,
        "x_name": "Potential/V",
        "y_name": "Current/A",
        "sha256": "04a8daf2e0d00aa5aa311367cc59769a4576d99142a2e8176ae80bb6a7ce67bc",
    },
    "chi_ocpt_demo.bin": {
        "instrument": "CHI",
        "technique": "OCP",
        "parse_status": "metadata_only",
        "point_count": 0,
        "x_name": "",
        "y_name": "",
        "sha256": "3fb4f483d0ddb0b95eaeaa29a7bafec844a7068d7bc6a14f1547a929d4d55157",
    },
    "corrtest_eis_demo.z60": {
        "instrument": "CorrTest",
        "technique": "EIS",
        "parse_status": "parsed",
        "point_count": 15,
        "x_name": "Zreal(ohm)",
        "y_name": "Zimag(ohm)",
        "sha256": "e3509b323ec12a4810e0bf2dc45ac64b677279b903aa5c994e5c47910d8e4534",
    },
    "corrtest_galstatic_demo.cor": {
        "instrument": "CorrTest",
        "technique": "CP/GCD",
        "parse_status": "parsed",
        "point_count": 11,
        "x_name": "T(s)",
        "y_name": "E(V)",
        "sha256": "70b30d74a76be2986bd6e5184508623cbe7b976f1d7d697430a47ffcaa6250d7",
    },
}


def load_app():
    specification = importlib.util.spec_from_file_location("echem_demo_app", ROOT / "app.py")
    if not specification or not specification.loader:
        raise RuntimeError("Unable to load app.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main() -> int:
    app = load_app()
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="echem-platform-demo-") as temporary:
        temporary_root = Path(temporary)
        config_path = temporary_root / "config.json"
        database_path = temporary_root / "state" / "demo.sqlite3"
        config_path.write_text(
            json.dumps(
                {
                    "bind": "127.0.0.1",
                    "port": 8787,
                    "scan_interval_seconds": 15,
                    "stable_age_seconds": 0,
                    "max_file_bytes": 52428800,
                    "max_points_per_curve": 2000,
                    "watch_roots": [str((ROOT / "demo_data").resolve())],
                    "extensions": [".txt", ".csv", ".cor", ".z60", ".bin", ".dat"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        runtime = app.build_runtime(config_path, database_path)
        scan = runtime.scanner.scan()
        counts = runtime.database.status_counts()
        records = sorted(runtime.database.list_runs(), key=lambda item: item["source_name"])

        summary = {
            "seen": scan.get("seen"),
            "imported": scan.get("imported"),
            "parsed": counts.get("parsed"),
            "metadata_only": counts.get("metadata_only"),
            "errors": scan.get("errors"),
        }
        expected_summary = {
            "seen": 4,
            "imported": 4,
            "parsed": 3,
            "metadata_only": 1,
            "errors": 0,
        }
        if summary != expected_summary:
            failures.append(f"summary mismatch: expected {expected_summary}, got {summary}")

        report_records: list[dict[str, Any]] = []
        actual_names = {record["source_name"] for record in records}
        if actual_names != set(EXPECTED):
            failures.append(
                f"record names mismatch: expected {sorted(EXPECTED)}, got {sorted(actual_names)}"
            )

        for record in records:
            name = record["source_name"]
            expected = EXPECTED.get(name)
            if expected is None:
                continue
            actual = {
                "instrument": record["instrument"],
                "technique": record["technique"],
                "parse_status": record["parse_status"],
                "point_count": record["point_count"],
                "x_name": record["x_name"],
                "y_name": record["y_name"],
                "sha256": digest(ROOT / "demo_data" / name),
            }
            for key, expected_value in expected.items():
                if actual[key] != expected_value:
                    failures.append(
                        f"{name} {key}: expected {expected_value!r}, got {actual[key]!r}"
                    )
            report_records.append({"file": name, **actual})

    report = {
        "ok": not failures,
        "app_version": app.APP_VERSION,
        "summary": summary,
        "records": report_records,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
