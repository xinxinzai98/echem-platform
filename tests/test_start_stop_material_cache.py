import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from tests.test_start_stop_scientific_golden import ANALYSIS, make_record, make_series_spec, make_standard_cycle, make_water_compensation_fixture
from material_result_cache import MaterialResultCache, verified_profile_records, prune_computed_cache
from water_result_cache import compensate_with_cache


class MaterialComputationCacheTests(unittest.TestCase):
    def fixture(self, key="A"):
        frame = make_standard_cycle()
        record = make_record(relative_path=f"lab/{key}/启停.txt")
        record["absolute_path"] = key
        record["sha256"] = hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest()
        spec = make_series_spec(record)
        return frame, record, spec

    def test_exact_numeric_roundtrip_and_unchanged_series_never_recalculates(self):
        frame, record, spec = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            original = ANALYSIS.analyze_all_series([spec], {"A":frame})
            first, stats = ANALYSIS.analyze_with_material_cache([spec], {"A":frame}, temporary)
            self.assertEqual(stats["materials_analyzed"], 1)
            for expected, actual in zip(original, first):
                pd.testing.assert_frame_equal(expected.reset_index(drop=True), actual.reset_index(drop=True), check_exact=True)
            with mock.patch.object(ANALYSIS,"analyze_all_series",side_effect=AssertionError("must reuse")), mock.patch.object(ANALYSIS,"read_square_wave_table",side_effect=AssertionError("must not parse")):
                second, stats = ANALYSIS.analyze_with_material_cache([spec], {}, temporary)
            self.assertEqual(stats["materials_analyzed"], 0)
            self.assertEqual(stats["materials_skipped_unchanged"], 1)
            for expected, actual in zip(original, second):
                pd.testing.assert_frame_equal(expected.reset_index(drop=True), actual.reset_index(drop=True), check_exact=True)

    def test_renaming_or_id_remapping_preserves_calculations(self):
        frame, record, spec = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            ANALYSIS.analyze_with_material_cache([spec], {"A":frame}, temporary)
            renamed = copy.deepcopy(spec)
            renamed.update(series_id="M99-main",material_id="M99",series_order=99,series_display_name="完整新名称",material_display_name="完整新名称")
            with mock.patch.object(ANALYSIS,"analyze_all_series",side_effect=AssertionError("label-only change")):
                result, stats = ANALYSIS.analyze_with_material_cache([renamed], {}, temporary)
            self.assertEqual(stats["materials_analyzed"],0)
            self.assertEqual(result[3].iloc[0]["series_id"],"M99-main")
            self.assertEqual(result[3].iloc[0]["series_display_name"],"完整新名称")

    def test_changed_source_recomputes_only_that_material_and_retains_others(self):
        fa, ra, a = self.fixture("A")
        fb, rb, b = self.fixture("B")
        b.update(series_id="M02-main",material_id="M02",series_order=2)
        with tempfile.TemporaryDirectory() as temporary:
            ANALYSIS.analyze_with_material_cache([a,b], {"A":fa,"B":fb}, temporary)
            changed = copy.deepcopy(b)
            changed["records"][0]["sha256"] = "f"*64
            with mock.patch.object(ANALYSIS,"analyze_all_series",wraps=ANALYSIS.analyze_all_series) as compute:
                result, stats = ANALYSIS.analyze_with_material_cache([a,changed], {"B":fb}, temporary)
            self.assertEqual(compute.call_count,1)
            self.assertEqual(stats["materials_analyzed"],1)
            self.assertEqual(stats["materials_skipped_unchanged"],1)
            self.assertEqual(set(result[3]["series_id"]),{"M01-main","M02-main"})

    def test_corrupt_cache_is_a_miss_and_profiles_require_exact_source_binding(self):
        frame, record, spec = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            cache=MaterialResultCache(root,"version-1")
            cache.store(spec,ANALYSIS.analyze_all_series([spec],{"A":frame}))
            next(root.glob("*.zip")).write_bytes(b"corrupt")
            self.assertIsNone(cache.load(spec))
            source=root/"source";source.mkdir();(source/"a.txt").write_bytes(b"abc")
            snapshot={"files":[{"relative_path":"a.txt","sha256":"a"*64,"file_size_bytes":3}]}
            trusted=[{"path":"a.txt","sha256":"a"*64,"size_bytes":3,"candidate_kind":"gal_square_wave"}]
            self.assertIsNotNone(verified_profile_records(source,snapshot,trusted))
            trusted[0]["sha256"]="b"*64
            self.assertIsNone(verified_profile_records(source,snapshot,trusted))

    def test_cache_limits_are_optional_and_do_not_touch_other_files(self):
        frame, record, spec = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = MaterialResultCache(root, "v1")
            frames = ANALYSIS.analyze_all_series([spec], {"A": frame})
            with mock.patch("material_result_cache.MAX_ENTRY_DISK_BYTES", 1):
                self.assertFalse(cache.store(spec, frames))
            self.assertEqual(list(root.iterdir()), [])
            self.assertTrue(cache.store(spec, frames))
            (root / "original.txt").write_text("preserve")
            (root / "arbitrary").mkdir()
            (root / "arbitrary" / ("a" * 64 + ".zip")).write_bytes(b"preserve")
            prune_computed_cache(root, 0)
            self.assertEqual(list(root.glob("*.zip")), [])
            self.assertEqual((root / "original.txt").read_text(), "preserve")
            self.assertTrue((root / "arbitrary" / ("a" * 64 + ".zip")).is_file())

    def test_real_cli_prepare_full_then_incremental_preserves_both_materials(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            env = {**os.environ, "START_STOP_PROJECT_ROOT":str(root),
                   "START_STOP_SOURCE_ROOT":str(source), "START_STOP_OUTPUT_DIR":str(output),
                   "START_STOP_COMPUTED_CACHE_DIR":str(root/"computed"),
                   "START_STOP_WORKBOOK_BUILDER":str(project/"docker/start_stop_analysis/material_config_workbook.py"),
                   "START_STOP_TRUST_SNAPSHOT_MANIFEST":"1"}
            def write_sources(offset):
                manifest=[]
                for index, name in enumerate(("A","B")):
                    frame=make_standard_cycle()
                    frame["potential_hghgo_v"] += index*0.01 + (offset if name=="B" else 0)
                    relative=f"lab/{name}/启停.txt"; file=source/relative;file.parent.mkdir(parents=True,exist_ok=True)
                    text="CSStudioFile,ID_GalSquareWave,fixture\nE(V)\ti(A/cm²)\tT(s)\n"
                    text+=frame[["potential_hghgo_v","current_a_cm2","time_s"]].to_csv(sep="\t",index=False,header=False)
                    file.write_text(text)
                    manifest.append({"path":relative,"size_bytes":file.stat().st_size,
                                     "sha256":hashlib.sha256(file.read_bytes()).hexdigest(),"candidate_kind":"gal_square_wave"})
                (source/".start-stop-manifest.json").write_text(json.dumps({"files":manifest}))
            def run(*args):
                result=subprocess.run([sys.executable,str(project/"docker/start_stop_analysis/analyze_and_plot_start_stop.py"),*args],
                                      env=env,capture_output=True,text=True,timeout=90)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                return json.loads(result.stdout)
            def config():
                snapshot=json.loads((output/"material_config_snapshot.json").read_text())
                payload={"dataset_fingerprint":snapshot["dataset_fingerprint"],"materials":[{
                    "key":row["key"],"fingerprint":row["fingerprint"],"plot_name":row["auto_name"],
                    "include_in_summary_atlas":True,"notes":""} for row in snapshot["materials"]]}
                path=root/"config.json";path.write_text(json.dumps(payload));return str(path)
            write_sources(0)
            run("--prepare-config")
            first=run("--render","--data-mode","raw","--material-config-json",config())
            self.assertEqual(first["materials_analyzed"],2)
            before=pd.read_csv(output/"cycle_summary_raw.csv")
            write_sources(0.002)
            run("--prepare-config")
            second=run("--render","--data-mode","raw","--material-scope","updated",
                       "--updated-material-key","lab/B","--material-config-json",config())
            self.assertEqual(second["materials_analyzed"],1)
            self.assertEqual(second["materials_skipped_unchanged"],1)
            self.assertEqual(second["analysis_series"],2)
            after=pd.read_csv(output/"cycle_summary_raw.csv")
            pd.testing.assert_frame_equal(before[before.series_id=="M01-main"].reset_index(drop=True),
                                          after[after.series_id=="M01-main"].reset_index(drop=True),check_exact=True)
            csv_before=hashlib.sha256((output/"cycle_summary_raw.csv").read_bytes()).hexdigest()
            caches_before={file.name:file.stat().st_mtime_ns for file in (root/"computed").glob("*.zip")}
            exported=run("--export-existing","--data-mode","raw")
            self.assertTrue(exported["analysis_reused"])
            self.assertEqual(exported["materials_analyzed"],0)
            self.assertEqual(hashlib.sha256((output/"cycle_summary_raw.csv").read_bytes()).hexdigest(),csv_before)
            self.assertEqual({file.name:file.stat().st_mtime_ns for file in (root/"computed").glob("*.zip")},caches_before)
            pdf=output/"启停数据_原始电位_全量图集.pdf"
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))


class WaterComputationCacheTests(unittest.TestCase):
    def fixture(self):
        frames = make_water_compensation_fixture()
        specs = []
        for index, row in frames[3].iterrows():
            record = make_record(relative_path=row["material_relative_path"] + "/启停.txt")
            record["sha256"] = hashlib.sha256(row["series_id"].encode()).hexdigest()
            spec = make_series_spec(record)
            spec.update(row.to_dict())
            spec["series_order"] = index + 1
            specs.append(spec)
        return frames, specs

    def test_exact_water_results_and_warm_cache_never_fits_or_compensates(self):
        frames, specs = self.fixture()
        expected_model = ANALYSIS.fit_water_compensation_model(frames[0], frames[3])
        expected = ANALYSIS.apply_water_compensation(*frames, expected_model)
        with tempfile.TemporaryDirectory() as temporary:
            model, first, stats = compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
            self.assertFalse(stats["reference_model_reused"])
            for a, b in zip(expected, first):
                pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True), check_exact=True)
            with mock.patch.object(ANALYSIS, "fit_water_compensation_model", side_effect=AssertionError("fit reused")), mock.patch.object(ANALYSIS, "apply_water_compensation", side_effect=AssertionError("result reused")):
                next_model, second, stats = compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
            self.assertTrue(stats["reference_model_reused"])
            self.assertEqual(stats["series_cache_hits"], 2)
            self.assertEqual(model, next_model)
            for a, b in zip(first, second):
                pd.testing.assert_frame_equal(a, b, check_exact=True)

    def test_nonreference_update_retains_model_and_other_material(self):
        frames, specs = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
            specs[1]["records"][0]["sha256"] = "b" * 64
            with mock.patch.object(ANALYSIS, "fit_water_compensation_model", side_effect=AssertionError("unchanged reference")), mock.patch.object(ANALYSIS, "apply_water_compensation", wraps=ANALYSIS.apply_water_compensation) as apply:
                _, _, stats = compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
            self.assertEqual(apply.call_count, 1)
            self.assertEqual(stats["series_cache_hits"], 1)

    def test_reference_update_invalidates_all_compensation_but_not_raw(self):
        frames, specs = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            model, _, _ = compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
            specs[0]["records"][0]["sha256"] = "a" * 64
            with mock.patch.object(ANALYSIS, "fit_water_compensation_model", wraps=ANALYSIS.fit_water_compensation_model) as fit, mock.patch.object(ANALYSIS, "apply_water_compensation", wraps=ANALYSIS.apply_water_compensation) as apply:
                new_model, _, stats = compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
            self.assertEqual(fit.call_count, 1)
            self.assertEqual(apply.call_count, 2)
            self.assertNotEqual(model["reference_source_version"], new_model["reference_source_version"])
            self.assertEqual(stats["series_cache_hits"], 0)

    def test_algorithm_version_and_corrupt_model_are_misses(self):
        frames, specs = self.fixture()
        with tempfile.TemporaryDirectory() as temporary:
            compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
            next((Path(temporary)/"water").glob("*.json")).write_text("{}");
            with mock.patch.object(ANALYSIS, "fit_water_compensation_model", wraps=ANALYSIS.fit_water_compensation_model) as fit:
                compensate_with_cache(ANALYSIS, specs, frames, temporary, "v1")
                _, _, stats = compensate_with_cache(ANALYSIS, specs, frames, temporary, "v2")
            self.assertEqual(fit.call_count, 2)
            self.assertEqual(stats["series_cache_hits"], 0)
