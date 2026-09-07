"""Isolated audit-remediation browser fixture; never connects to instruments."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from echem_platform.start_stop_database import StartStopDatabase
from tests.test_start_stop_cv_eis import corrtest_cv, corrtest_eis
from echem_platform.start_stop_cv_eis import CvEisRepositoryAnalyzer, ReadOnlyCvEisDatabase
from start_stop_service import main, build_parser, build_workspace, create_handler, StartStopHTTPServer


def seed(root: Path) -> None:
    database = StartStopDatabase(root / "repository.sqlite3")
    current = root / "published" / "current"
    current.mkdir(parents=True, exist_ok=True)
    cv = corrtest_cv().decode().splitlines()
    cv[0] = "CSStudioFile,ID_CV,invalid"
    files = [("her-cv.txt", "\n".join(cv).encode()),
             ("her-eis-a.txt", corrtest_eis()), ("her-eis-b.txt", corrtest_eis(real_offset=0.4))]
    for name, content in files:
        file = root / name
        file.write_bytes(content)
        database.ingest_staged_file(file, {
            "machine_id":"audit", "root_label":"fixture", "remote_path":f"D:\\fixture\\{name}",
            "repository_path":f"audit/fixture/仅用于验证的材料/{name}", "size":len(content),
            "last_write_ticks":1, "last_write_utc":"2026-09-07T00:00:00Z", "is_candidate":False,
        })
    (current / "material_config_snapshot.json").write_text(json.dumps({
        "dataset_fingerprint":"audit-only", "generated_at":"2026-09-07T00:00:00+00:00",
        "materials":[{"key":"audit/fixture/仅用于验证的材料", "auto_name":"仅用于验证的材料", "fingerprint":"audit-only"}],
    }, ensure_ascii=False))


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=18795)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--full", action="store_true", help="seed synthetic workstation and LANBTS analysis fixtures")
    parser.add_argument("--monitors", action="store_true", help="use synthetic monitor state; never probe hardware")
    args=parser.parse_args()
    root=args.root or Path(tempfile.mkdtemp(prefix="start-stop-audit-ui-"))
    if not (root / "repository.sqlite3").exists():
        if args.read_only:
            parser.error("start the fixture management service first")
        seed(root)
        if args.full:
            from audit_fixture_data import seed_analysis
            seed_analysis(root)
    os.environ["START_STOP_SERVICE_VERSION"]="0.7.0-audit-preview"
    os.environ["START_STOP_WORKBOOK_BUILDER"]=str(ROOT/"docker/start_stop_analysis/material_config_workbook.py")
    print(f"FIXTURE_ROOT={root}", flush=True)
    command=["--database",str(root/("lan.sqlite3" if args.read_only else "repository.sqlite3")),
             "--analysis-dir",str(root/"published/current"),"--scratch-dir",str(root/"scratch"),
             "--analysis-script",str(ROOT/"docker/start_stop_analysis/analyze_and_plot_start_stop.py"),
             "--bind","127.0.0.1","--port",str(args.port)]
    if args.read_only:
        command += ["--lan-read-only","--lan-no-auth","--source-database",str(root/"repository.sqlite3"),
                    "--cv-eis-database",str(root/"repository.sqlite3")]
        # Exercise the production read-only handler over loopback test data;
        # the production CLI's RFC1918 deployment-address policy is unchanged.
        workspace = build_workspace(build_parser().parse_args(command))
        monitor_args = {}
        if args.monitors:
            from audit_monitor_fixtures import fixtures
            stations, lanbts, connectivity, preview = fixtures()
            monitor_args = {"workstation_monitor":stations,"lanbts_monitor":lanbts,
                            "connectivity_monitor":connectivity,"live_preview_snapshot_provider":preview}
        handler = create_handler(workspace, lan_read_only=True, lan_no_auth=True,
                                 public_host="127.0.0.1", public_port=args.port,
                                 cv_eis_analyzer=CvEisRepositoryAnalyzer(ReadOnlyCvEisDatabase(root/"repository.sqlite3")), **monitor_args)
        with StartStopHTTPServer(("127.0.0.1",args.port),handler) as server:
            print(f"READONLY_FIXTURE=http://127.0.0.1:{args.port}",flush=True)
            server.serve_forever()
        raise SystemExit(0)
    raise SystemExit(main(command))
