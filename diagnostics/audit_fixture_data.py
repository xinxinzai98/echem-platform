"""Synthetic browser fixtures generated through the real repository workflow."""
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pandas as pd

from echem_platform.start_stop import StartStopWorkspace
from echem_platform.start_stop_database import StartStopDatabase
from tests.test_start_stop_scientific_golden import make_standard_cycle
from tests.test_start_stop_stability import FakeDatabase, csv_bytes, start_stop_rows, constant_rows


def seed_analysis(root: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    script = project / "docker/start_stop_analysis/analyze_and_plot_start_stop.py"
    database = StartStopDatabase(root / "repository.sqlite3")
    workspace = StartStopWorkspace(database, root / "published/current", repository_mode=True,
                                   analysis_script=script, scratch_dir=root / "fixture-scratch",
                                   python_executable=sys.executable, timeout_seconds=120)
    uploads = []
    for index in range(22):
        cycles = []
        for cycle in range(8):
            duration = 30 if index < 18 else 300
            frame = make_standard_cycle(sample_interval_s=1, phase_duration_s=duration)
            frame["time_s"] += cycle * duration * 2
            frame["potential_hghgo_v"] -= index * 0.01 + frame["time_s"] * 0.006 / 3600
            cycles.append(frame)
        frame = pd.concat(cycles, ignore_index=True)
        content = "CSStudioFile,ID_GalSquareWave,fixture\nE(V)\ti(A/cm²)\tT(s)\n"
        content += frame[["potential_hghgo_v", "current_a_cm2", "time_s"]].to_csv(sep="\t", index=False, header=False)
        path = root / f"sample-{index}.txt"
        path.write_text(content)
        material = f"审计样例-{index+1:02d}-NiMoP-{'恒流' if index % 2 else '脉冲'}-完整材料名称"
        uploaded = workspace.upload_file(path, filename="启停.txt", relative_path=f"{material}/启停.txt",
                                         group="隔离验证", last_modified=1788755830000 + index * 60000,
                                         size_bytes=path.stat().st_size)
        uploads.append(uploaded["upload_id"])

    def run(action, **kwargs):
        workspace.start_job(action, **kwargs)
        deadline = time.monotonic() + 120
        while workspace._active_job_id and time.monotonic() < deadline:
            time.sleep(0.02)
        job = workspace._public_job()
        if job["status"] != "completed":
            raise RuntimeError(json.dumps(job, ensure_ascii=False))

    with mock.patch.dict(os.environ, {"START_STOP_PROJECT_ROOT": str(root),
        "START_STOP_WORKBOOK_BUILDER": str(script.parent / "material_config_workbook.py")}):
        run("prepare_upload", upload_ids=uploads)
        material_payload = workspace.materials()
        rows = [{key: row[key] for key in ("key", "plot_name", "include_in_summary_atlas", "favorite", "notes")}
                for row in material_payload["materials"]]
        rows[0]["favorite"] = True
        rows[0]["notes"] = "审计合成样例，不是实验结果。"
        workspace.save_materials(dataset_fingerprint=material_payload["dataset_fingerprint"], expected_revision=0, materials=rows)
        run("render", render_data_mode="raw")

    fake = FakeDatabase()
    for index in range(6):
        mode = "start_stop" if index < 4 else "constant_current"
        rows = start_stop_rows() if mode == "start_stop" else constant_rows()
        for row in rows:
            row["potential_v"] -= index * 0.02
            row["voltage_raw_uv"] = row["potential_v"] * 1_000_000
        raw = root / f"lanbts-fixture-{index}.bts"
        raw.write_bytes(f"Synthetic placeholder only, channel {index+1}; never parsed as BTS".encode())
        raw_result = database.ingest_staged_file(raw, {
            "machine_id":"audit-lanbts", "root_label":"fixture", "remote_path":str(raw.name),
            "repository_path":f"audit-lanbts/fixture/{raw.name}", "size":raw.stat().st_size,
            "last_write_ticks":index+1, "is_candidate":False,
        })
        normalized = root / f"lanbts-fixture-{index}.csv"
        normalized.write_bytes(csv_bytes(rows))
        metadata = fake.rows[0 if mode == "start_stop" else 1]["metadata"].copy()
        metadata.update(material_name=f"审计样例-蓝博{index+1}-NiMo-{'启停' if mode=='start_stop' else '恒流'}",
                        record_count=len(rows), exported_point_count=len(rows), source_file=raw.name,
                        parent_source_version_id=raw_result["version_id"], parent_sha256=raw_result["sha256"],
                        parent_repository_path=f"audit-lanbts/fixture/{raw.name}", channel=index+1)
        if index == 3:
            metadata["protocol"] = {"kind":"bipolar", "category":"反向启停", "label":"独立工步样例", "key":"other-protocol"}
        database.ingest_staged_file(normalized, {
            "machine_id":"audit-lanbts", "root_label":"fixture", "remote_path":raw.name+"::normalized",
            "repository_path":f"audit-lanbts/fixture/{normalized.name}", "size":normalized.stat().st_size,
            "last_write_ticks":index+1, "is_candidate":True, "candidate_kind":f"lanbts_{mode}",
            **metadata,
        })
