from __future__ import annotations

import importlib.util
import inspect
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

# The analysis module creates its output tree at import time.  Keep that side
# effect in an isolated temporary directory so this test never touches either
# the real repository database or a published analysis generation.
_MODULE_TEMPORARY = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(_MODULE_TEMPORARY.cleanup)
_MODULE_ROOT = Path(_MODULE_TEMPORARY.name)
_ENVIRONMENT = {
    "START_STOP_PROJECT_ROOT": str(_MODULE_ROOT),
    "START_STOP_SOURCE_ROOT": str(_MODULE_ROOT / "source"),
    "START_STOP_OUTPUT_DIR": str(_MODULE_ROOT / "output"),
}
_PREVIOUS_ENVIRONMENT = {key: os.environ.get(key) for key in _ENVIRONMENT}
os.environ.update(_ENVIRONMENT)
try:
    SPEC = importlib.util.spec_from_file_location(
        "start_stop_scientific_golden_analysis",
        ROOT / "docker" / "start_stop_analysis" / "analyze_and_plot_start_stop.py",
    )
    assert SPEC and SPEC.loader
    ANALYSIS = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = ANALYSIS
    SPEC.loader.exec_module(ANALYSIS)
finally:
    for _key, _value in _PREVIOUS_ENVIRONMENT.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value


np = ANALYSIS.np
pd = ANALYSIS.pd


def make_record(
    *,
    relative_path: str = "lab/material/启停.txt",
    file_name_kind: str = "base",
    test_type: str = "standard_start_stop",
    mtime: float = 1.0,
) -> dict:
    label = {
        "standard_start_stop": "标准启停",
        "variable_start_stop": "变工步启停",
        "adt_start_stop": "ADT 启停",
    }[test_type]
    current_levels = (
        "[-0.3, 0.1]"
        if test_type == "variable_start_stop"
        else "[-0.3, 0.03]"
    )
    return {
        "absolute_path": relative_path,
        "relative_path": relative_path,
        "material_relative_path": str(Path(relative_path).parent),
        "file_name": Path(relative_path).name,
        "file_name_kind": file_name_kind,
        "source_program_type": (
            "adt_script_method" if test_type == "adt_start_stop" else "gal_square_wave"
        ),
        "test_type": test_type,
        "test_type_label_zh": label,
        "included_in_analysis": True,
        "_mtime_epoch": float(mtime),
        "sha256": "fixture-sha256",
        "time_decrease_count": 0,
        "duplicate_time_count": 0,
        "parse_error_rows": 0,
        "current_levels_a_cm2": current_levels,
        "cathodic_current_median_a_cm2": -0.3,
        "recovery_current_median_a_cm2": (
            0.1 if test_type == "variable_start_stop" else 0.03
        ),
    }


def make_series_spec(record: dict) -> dict:
    return {
        "series_id": "M01-main",
        "series_order": 1,
        "material_id": "M01",
        "material_display_name": "golden-material",
        "series_display_name": "golden-material",
        "material_relative_path": record["material_relative_path"],
        "workstation": "lab",
        "test_type": record["test_type"],
        "test_type_label_zh": record["test_type_label_zh"],
        "is_primary_series": True,
        "is_special_series": False,
        "special_file_name": "",
        "include_in_summary_atlas": True,
        "material_user_notes": "",
        "records": [record],
    }


def make_standard_cycle(
    minimum_time_s: float = 20.0,
    sample_interval_s: float = 0.1,
    phase_duration_s: float = 30.0,
) -> pd.DataFrame:
    cathodic_times = np.arange(0.0, phase_duration_s, sample_interval_s)
    closest = int(np.argmin(np.abs(cathodic_times - 15.0)))
    if 14.9 < minimum_time_s < 15.1:
        cathodic_times[closest] = minimum_time_s
        minimum_index = closest
    else:
        minimum_index = int(np.argmin(np.abs(cathodic_times - minimum_time_s)))
    reverse_times = np.arange(
        phase_duration_s,
        phase_duration_s * 2.0,
        sample_interval_s,
    )
    cathodic_potential = np.full(len(cathodic_times), -0.8)
    cathodic_potential[minimum_index] = -1.0
    return pd.DataFrame(
        {
            "potential_hghgo_v": np.r_[
                cathodic_potential,
                np.full(len(reverse_times), -0.2),
            ],
            "current_a_cm2": np.r_[
                np.full(len(cathodic_times), -0.3),
                np.full(len(reverse_times), 0.03),
            ],
            "time_s": np.r_[cathodic_times, reverse_times],
        }
    )


def make_variable_cycle(
    *,
    sample_interval_s: float = 0.1,
) -> pd.DataFrame:
    cathodic_times = np.arange(0.0, 30.0, sample_interval_s)
    recovery_times = np.arange(30.0, 40.0, sample_interval_s)
    return pd.DataFrame(
        {
            "potential_hghgo_v": np.r_[
                np.linspace(-0.75, -0.82, len(cathodic_times)),
                np.linspace(-0.25, -0.2, len(recovery_times)),
            ],
            "current_a_cm2": np.r_[
                np.full(len(cathodic_times), -0.3),
                np.full(len(recovery_times), 0.1),
            ],
            "time_s": np.r_[cathodic_times, recovery_times],
        }
    )


def analyze_one_frame(frame: pd.DataFrame, *, test_type: str) -> tuple:
    record = make_record(test_type=test_type)
    record["absolute_path"] = "fixture"
    if test_type == "adt_start_stop":
        record["cathodic_current_median_a_cm2"] = -0.2
        record["recovery_current_median_a_cm2"] = 0.02
    return ANALYSIS.analyze_all_series(
        [make_series_spec(record)],
        {"fixture": frame},
    )


class ReferenceElectrodeAndEndpointGoldenTests(unittest.TestCase):
    def test_web_analysis_does_not_export_pdf_unless_explicitly_requested(self):
        render_parameter = inspect.signature(
            ANALYSIS.render_from_config
        ).parameters["export_pdf"]
        water_parameter = inspect.signature(
            ANALYSIS.render_water_compensation_outputs
        ).parameters["export_pdf"]

        self.assertIs(render_parameter.default, False)
        self.assertIs(water_parameter.default, True)
        self.assertIs(
            inspect.signature(ANALYSIS.render_from_config)
            .parameters["export_static_figures"]
            .default,
            False,
        )

    def test_pdf_only_export_does_not_repeat_png_and_svg_rendering(self):
        class FakePdf:
            def __init__(self):
                self.calls = 0

            def savefig(self, *_args, **_kwargs):
                self.calls += 1

        with tempfile.TemporaryDirectory() as temporary_name:
            ANALYSIS.ensure_matplotlib()
            fig = ANALYSIS.plt.figure()
            pdf = FakePdf()
            ANALYSIS.configure_static_figure_export(False)
            with mock.patch.object(fig, "savefig") as static_save:
                ANALYSIS.save_figure(
                    fig,
                    Path(temporary_name) / "figure",
                    pdf,
                )

        static_save.assert_not_called()
        self.assertEqual(pdf.calls, 1)

    def test_raw_hg_hgo_potential_is_not_reference_converted(self):
        self.assertEqual(ANALYSIS.POTENTIAL_REFERENCE, "Hg/HgO")
        self.assertAlmostEqual(ANALYSIS.RAW_POTENTIAL_OFFSET_V, 0.0, places=12)

        cycles, *_ = analyze_one_frame(
            make_standard_cycle(minimum_time_s=20.0),
            test_type="standard_start_stop",
        )
        # The endpoint must remain exactly as measured vs Hg/HgO, with no
        # reference conversion or iR correction mixed in.
        self.assertAlmostEqual(
            float(cycles.iloc[0]["cathodic_last1s_median_raw_v"]),
            -0.8,
            places=12,
        )
        self.assertFalse(ANALYSIS.IR_CORRECTION_APPLIED)

    def test_overlay_style_capacity_preserves_two_styles_per_color(self):
        palette = ANALYSIS.OVERLAY_HIGH_CONTRAST_PALETTE
        styles = ANALYSIS.OVERLAY_LINE_STYLES

        self.assertEqual(len(palette), len(set(palette)))
        self.assertEqual(styles, ("-", "--"))
        self.assertGreaterEqual(len(palette) * len(styles), 64)

    def test_standard_endpoint_window_for_uniform_and_irregular_sampling(self):
        cases = {
            "0.1 s": (
                np.arange(0.0, 30.0, 0.1),
                np.arange(29.0, 30.0, 0.1),
                1.0,
            ),
            "1 s": (np.arange(0.0, 30.0, 1.0), np.array([29.0]), 1.0),
            "2 s": (np.arange(0.0, 30.0, 2.0), np.array([28.0]), 2.0),
            "irregular": (
                np.r_[np.arange(0.0, 28.0, 0.5), [28.3, 28.75, 29.1, 29.55, 29.9]],
                np.array([29.55, 29.9]),
                1.0,
            ),
        }
        for label, (elapsed, expected_points, expected_window) in cases.items():
            with self.subTest(label=label):
                interval = ANALYSIS.positive_median_step(elapsed)
                duration = float(elapsed[-1] + interval)
                mask, window = ANALYSIS.phase_endpoint_mask(
                    elapsed,
                    duration,
                    interval,
                )
                np.testing.assert_allclose(elapsed[mask], expected_points, atol=1e-12)
                self.assertAlmostEqual(window, expected_window, places=12)

    def test_standard_0_2_second_sampling_is_a_complete_cycle(self):
        frame = make_standard_cycle(
            minimum_time_s=20.0,
            sample_interval_s=0.2,
        )

        pairs, profile = ANALYSIS.build_complete_cycle_pairs(
            frame, "standard_start_stop"
        )
        cycles, *_ = analyze_one_frame(
            frame,
            test_type="standard_start_stop",
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(profile["incomplete_cycle_candidates"], 0)
        self.assertEqual(int(cycles.iloc[0]["cathodic_phase_point_count"]), 150)
        self.assertEqual(int(cycles.iloc[0]["cathodic_last1s_point_count"]), 5)
        self.assertLessEqual(
            int(cycles.iloc[0]["minimum_phase_point_count"]), 150
        )
        self.assertEqual(
            int(cycles.iloc[0]["minimum_endpoint_point_count"]), 5
        )

    def test_standard_five_and_ten_minute_work_steps_keep_their_duration(self):
        for phase_duration_s in (300.0, 600.0):
            with self.subTest(phase_duration_s=phase_duration_s):
                frame = make_standard_cycle(
                    minimum_time_s=20.0,
                    sample_interval_s=0.2,
                    phase_duration_s=phase_duration_s,
                )

                pairs, profile = ANALYSIS.build_complete_cycle_pairs(
                    frame, "standard_start_stop"
                )
                cycles, *_ = analyze_one_frame(
                    frame,
                    test_type="standard_start_stop",
                )

                self.assertEqual(len(pairs), 1)
                self.assertEqual(profile["incomplete_cycle_candidates"], 0)
                self.assertAlmostEqual(
                    float(cycles.iloc[0]["cathodic_phase_duration_s"]),
                    phase_duration_s,
                    places=9,
                )
                self.assertAlmostEqual(
                    float(cycles.iloc[0]["reverse_phase_duration_s"]),
                    phase_duration_s,
                    places=9,
                )

    def test_standard_ten_minute_work_step_passes_final_scientific_qa(self):
        frame = make_standard_cycle(
            minimum_time_s=20.0,
            sample_interval_s=0.2,
            phase_duration_s=600.0,
        )
        cycles, overview, _, series, segments, _ = analyze_one_frame(
            frame,
            test_type="standard_start_stop",
        )
        inventory = pd.DataFrame(
            [
                {
                    "included_in_analysis": True,
                    "sha256": "ten-minute-fixture",
                    "time_decrease_count": 0,
                    "parse_error_rows": 0,
                    "sample_interval_s": 0.2,
                }
            ]
        )
        materials = [
            {
                "material_display_name": "golden-material",
                "material_relative_path": "lab/material",
            }
        ]

        results = ANALYSIS.run_qa(
            inventory,
            materials,
            series,
            segments,
            cycles,
            overview,
        )

        self.assertIn(
            {
                "check": "标准启停各工步的阴极/恢复阶段时长均满足完整性要求",
                "passed": True,
            },
            results,
        )

    def test_variable_30_10_second_work_step_is_a_complete_separate_protocol(self):
        frame = make_variable_cycle(sample_interval_s=0.1)

        pairs, profile = ANALYSIS.build_complete_cycle_pairs(
            frame,
            "variable_start_stop",
        )
        cycles, *_ = analyze_one_frame(
            frame,
            test_type="variable_start_stop",
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(profile["incomplete_cycle_candidates"], 0)
        self.assertEqual(cycles.iloc[0]["test_type"], "variable_start_stop")
        self.assertAlmostEqual(
            float(cycles.iloc[0]["cathodic_phase_duration_s"]),
            30.0,
            places=9,
        )
        self.assertAlmostEqual(
            float(cycles.iloc[0]["reverse_phase_duration_s"]),
            10.0,
            places=9,
        )

    def test_short_phase_reports_material_instead_of_cycle_key_error(self):
        cathodic_times = np.arange(0.0, 28.0, 0.2)
        reverse_times = np.arange(28.0, 58.0, 0.2)
        frame = pd.DataFrame(
            {
                "potential_hghgo_v": np.r_[
                    np.full(len(cathodic_times), -0.8),
                    np.full(len(reverse_times), -0.2),
                ],
                "current_a_cm2": np.r_[
                    np.full(len(cathodic_times), -0.3),
                    np.full(len(reverse_times), 0.03),
                ],
                "time_s": np.r_[cathodic_times, reverse_times],
            }
        )

        pairs, profile = ANALYSIS.build_complete_cycle_pairs(
            frame, "standard_start_stop"
        )
        self.assertEqual(pairs, [])
        self.assertEqual(profile["incomplete_cycle_candidates"], 1)
        with self.assertRaisesRegex(
            RuntimeError,
            "golden-material.*未识别到.*完整启停循环",
        ):
            analyze_one_frame(frame, test_type="standard_start_stop")

    def test_updated_scope_keeps_only_explicitly_changed_materials(self):
        materials = [
            {"material_relative_path": "lab/material-a"},
            {"material_relative_path": "lab/material-b"},
        ]
        series = [
            {"material_relative_path": "lab/material-a", "series_id": "A"},
            {"material_relative_path": "lab/material-b", "series_id": "B"},
        ]

        scoped_materials, scoped_series = ANALYSIS.select_render_scope(
            materials,
            series,
            "updated",
            ["lab/material-b"],
        )

        self.assertEqual(
            [item["material_relative_path"] for item in scoped_materials],
            ["lab/material-b"],
        )
        self.assertEqual([item["series_id"] for item in scoped_series], ["B"])
        with self.assertRaisesRegex(RuntimeError, "没有新增或数据已更新"):
            ANALYSIS.select_render_scope(materials, series, "updated", [])

    def test_minimum_at_15_seconds_is_normal_and_strictly_before_is_abnormal(self):
        expected = {
            14.999: "abnormal",
            15.000: "normal",
            15.001: "normal",
        }
        for minimum_time_s, status in expected.items():
            with self.subTest(minimum_time_s=minimum_time_s):
                cycles, *_ = analyze_one_frame(
                    make_standard_cycle(minimum_time_s=minimum_time_s),
                    test_type="standard_start_stop",
                )
                row = cycles.iloc[0]
                self.assertAlmostEqual(
                    float(row["cathodic_phase_min_time_s"]),
                    minimum_time_s,
                    places=9,
                )
                self.assertEqual(row["cathodic_shift_status"], status)
                self.assertEqual(
                    bool(row["cathodic_phase_min_in_second_half"]),
                    minimum_time_s >= 15.0,
                )

    def test_tied_minimum_uses_earliest_occurrence_conservatively(self):
        frame = make_standard_cycle(minimum_time_s=14.999)
        later_index = int(
            frame.index[
                (frame["current_a_cm2"] < 0)
                & (frame["time_s"] > 14.999)
            ][0]
        )
        frame.loc[later_index, "time_s"] = 15.001
        frame.loc[later_index, "potential_hghgo_v"] = -1.0

        cycles, *_ = analyze_one_frame(frame, test_type="standard_start_stop")
        row = cycles.iloc[0]
        self.assertAlmostEqual(
            float(row["cathodic_phase_min_time_s"]),
            14.999,
            places=9,
        )
        self.assertEqual(row["cathodic_shift_status"], "abnormal")

    def test_adt_uses_the_last_available_cathodic_point(self):
        cathodic_times = np.arange(0.0, 32.0, 2.0)
        reverse_times = np.arange(32.0, 64.0, 2.0)
        cathodic_potential = np.linspace(-0.6, -1.2, len(cathodic_times))
        frame = pd.DataFrame(
            {
                "potential_hghgo_v": np.r_[
                    cathodic_potential,
                    np.linspace(-0.2, -0.1, len(reverse_times)),
                ],
                "current_a_cm2": np.r_[
                    np.full(len(cathodic_times), -0.2),
                    np.full(len(reverse_times), 0.02),
                ],
                "time_s": np.r_[cathodic_times, reverse_times],
            }
        )

        cycles, _, _, summary, *_ = analyze_one_frame(
            frame,
            test_type="adt_start_stop",
        )
        row = cycles.iloc[0]
        self.assertEqual(int(row["cathodic_last1s_point_count"]), 1)
        self.assertAlmostEqual(
            float(row["cathodic_last1s_median_raw_v"]),
            float(cathodic_potential[-1]),
            places=12,
        )
        self.assertEqual(summary.iloc[0]["endpoint_statistic"], "阶段末个可用采样点")


class SeriesContinuityGoldenTests(unittest.TestCase):
    def test_continuations_join_main_series_and_marked_files_stay_separate(self):
        paths = [
            ("lab/NiMoP/启停.txt", 1.0),
            ("lab/NiMoP/启停_2026_08_04_12_00_00.txt", 2.0),
            ("lab/NiMoP/启停2.txt", 3.0),
            ("lab/NiMoP/启停3.txt", 4.0),
        ]
        records = [
            make_record(
                relative_path=path,
                file_name_kind=ANALYSIS.classify_file_name(Path(path)),
                mtime=mtime,
            )
            for path, mtime in paths
        ]

        materials, series = ANALYSIS.build_series_specs(records)

        self.assertEqual(len(materials), 1)
        self.assertEqual(len(series), 2)
        self.assertEqual(
            [Path(row["relative_path"]).name for row in series[0]["records"]],
            ["启停.txt", "启停_2026_08_04_12_00_00.txt", "启停2.txt"],
        )
        self.assertTrue(series[0]["is_primary_series"])
        self.assertEqual([row["segment_index"] for row in series[0]["records"]], [1, 2, 3])
        self.assertTrue(series[1]["is_special_series"])
        self.assertEqual(series[1]["special_file_name"], "启停3.txt")
        self.assertIn("单独分析", series[1]["series_display_name"])

    def test_scoped_material_keeps_full_snapshot_material_identifier(self):
        record = make_record(relative_path="lab/Z/启停.txt")

        materials, series = ANALYSIS.build_series_specs(
            [record],
            ["lab/A", "lab/Z"],
        )

        self.assertEqual(materials[0]["material_id"], "M02")
        self.assertEqual(series[0]["series_id"], "M02-main")


class TrustedSnapshotProfilingTests(unittest.TestCase):
    def test_material_filter_uses_manifest_before_parse_and_skips_rehash(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            source = Path(temporary_name)
            payload = (
                "ID_GalSquareWave\nmetadata\nE(V)\ti(A/cm²)\tT(s)\n"
                "-0.8\t-0.3\t0\n-0.2\t0.03\t30\n"
            ).encode("utf-8")
            rows = []
            for material in ("A", "B"):
                relative = f"lab/{material}/启停.txt"
                path = source.joinpath(*Path(relative).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                rows.append(
                    {
                        "path": relative,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "candidate_kind": "gal_square_wave",
                    }
                )
            (source / ".start-stop-manifest.json").write_text(
                json.dumps({"files": rows}),
                encoding="utf-8",
            )

            with mock.patch.object(ANALYSIS, "SOURCE_ROOT", source), mock.patch.dict(
                os.environ,
                {"START_STOP_TRUST_SNAPSHOT_MANIFEST": "1"},
            ), mock.patch.object(
                ANALYSIS,
                "sha256_file",
                side_effect=AssertionError("trusted snapshot must not be rehashed"),
            ):
                records, cache = ANALYSIS.discover_and_profile(["lab/A"])

        self.assertEqual([row["material_relative_path"] for row in records], ["lab/A"])
        self.assertEqual(len(cache), 1)

    def test_explicit_variable_work_step_is_included_but_deposition_pulse_is_not(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            source = Path(temporary_name)
            payload = (
                "ID_GalSquareWave\nmetadata\nE(V)\ti(A/cm²)\tT(s)\n"
                "-0.80\t-0.30\t0\n-0.81\t-0.30\t1\n"
                "-0.20\t0.10\t2\n-0.19\t0.10\t3\n"
            ).encode("utf-8")
            rows = []
            for filename in ("启停.txt", "pulse.txt"):
                relative = f"lab/variable/{filename}"
                path = source.joinpath(*Path(relative).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                rows.append(
                    {
                        "path": relative,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "candidate_kind": "gal_square_wave",
                    }
                )
            (source / ".start-stop-manifest.json").write_text(
                json.dumps({"files": rows}),
                encoding="utf-8",
            )

            with mock.patch.object(ANALYSIS, "SOURCE_ROOT", source), mock.patch.dict(
                os.environ,
                {"START_STOP_TRUST_SNAPSHOT_MANIFEST": "1"},
            ):
                records, cache = ANALYSIS.discover_and_profile()

        by_name = {row["file_name"]: row for row in records}
        self.assertTrue(by_name["启停.txt"]["included_in_analysis"])
        self.assertEqual(
            by_name["启停.txt"]["test_type"],
            "variable_start_stop",
        )
        self.assertEqual(
            by_name["启停.txt"]["test_type_label_zh"],
            "变工步启停",
        )
        self.assertFalse(by_name["pulse.txt"]["included_in_analysis"])
        self.assertEqual(
            by_name["pulse.txt"]["candidate_class"],
            "excluded_nonstandard_square_wave",
        )
        self.assertEqual(set(cache), {str(source / "lab" / "variable" / "启停.txt")})


class IncludedFileCountGoldenTests(unittest.TestCase):
    def test_total_standard_and_adt_counts_are_conserved(self):
        inventory = pd.DataFrame(
            [
                {
                    "included_in_analysis": True,
                    "test_type": "standard_start_stop",
                },
                {
                    "included_in_analysis": True,
                    "test_type": "standard_start_stop",
                },
                {
                    "included_in_analysis": True,
                    "test_type": "adt_start_stop",
                },
                {
                    "included_in_analysis": True,
                    "test_type": "variable_start_stop",
                },
                {
                    "included_in_analysis": False,
                    "test_type": "",
                },
            ]
        )

        counts = ANALYSIS.included_file_counts(inventory)

        self.assertEqual(
            counts,
            {
                "included_files": 4,
                "included_standard_files": 2,
                "included_variable_files": 1,
                "included_adt_files": 1,
            },
        )
        self.assertEqual(
            counts["included_files"],
            counts["included_standard_files"]
            + counts["included_variable_files"]
            + counts["included_adt_files"],
        )

    def test_included_unknown_protocol_breaks_the_count_invariant(self):
        inventory = pd.DataFrame(
            [
                {
                    "included_in_analysis": True,
                    "test_type": "standard_start_stop",
                },
                {
                    "included_in_analysis": True,
                    "test_type": "unexpected_protocol",
                },
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "协议计数不守恒"):
            ANALYSIS.included_file_counts(inventory)


def make_water_compensation_fixture() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows = []
    for series_order, (series_id, raw_slope) in enumerate(
        (("reference", -0.006), ("overcompensated", 0.003)),
        start=1,
    ):
        for cycle, time_h in enumerate(np.linspace(0.0, 12.0, 120), start=1):
            rows.append(
                {
                    "series_id": series_id,
                    "series_order": series_order,
                    "cycle": cycle,
                    "cathodic_endpoint_time_h": time_h,
                    "reverse_endpoint_time_h": time_h,
                    "cathodic_current_median_a_cm2": -0.3,
                    "recovery_current_median_a_cm2": 0.03,
                    "cathodic_last1s_median_raw_v": -0.5 + raw_slope * time_h,
                    "reverse_last1s_median_raw_v": 0.2,
                }
            )
    cycles = pd.DataFrame(rows)
    series = pd.DataFrame(
        [
            {
                "series_id": "reference",
                "material_relative_path": ANALYSIS.DEFAULT_WATER_COMP_REFERENCE_KEY,
                "material_display_name": "可重命名参照显示名",
                "test_type": "standard_start_stop",
                "cathodic_current_median_a_cm2": -0.3,
                "include_in_summary_atlas": True,
            },
            {
                "series_id": "overcompensated",
                "material_relative_path": "lab/positive-drift-control",
                "material_display_name": "positive-drift-control",
                "test_type": "standard_start_stop",
                "cathodic_current_median_a_cm2": -0.3,
                "include_in_summary_atlas": True,
            },
        ]
    )
    overview = pd.DataFrame(
        {
            "series_id": ["reference", "reference", "overcompensated", "overcompensated"],
            "continuous_time_h": [0.0, 12.0, 0.0, 12.0],
            "current_a_cm2": [-0.3, -0.3, -0.3, -0.3],
            "potential_raw_v": [-0.5, -0.572, -0.5, -0.464],
        }
    )
    representative = pd.DataFrame(
        {
            "series_id": ["reference", "reference", "overcompensated", "overcompensated"],
            "continuous_time_s": [0.0, 43200.0, 0.0, 43200.0],
            "current_a_cm2": [-0.3, -0.3, -0.3, -0.3],
            "potential_raw_v": [-0.5, -0.572, -0.5, -0.464],
        }
    )
    return cycles, overview, representative, series


class WaterCompensationGoldenTests(unittest.TestCase):
    def test_known_reference_slope_produces_known_resistance_drift(self):
        cycles, overview, representative, series = make_water_compensation_fixture()
        model = ANALYSIS.fit_water_compensation_model(cycles, series)

        self.assertEqual(
            model["reference_material_key"],
            ANALYSIS.DEFAULT_WATER_COMP_REFERENCE_KEY,
        )
        self.assertEqual(
            model["reference_material_display_name"],
            "可重命名参照显示名",
        )
        self.assertEqual(model["reference_series_id"], "reference")
        self.assertAlmostEqual(model["reference_fit_slope_v_per_h"], -0.006, places=12)
        self.assertAlmostEqual(
            model["area_specific_resistance_drift_ohm_cm2_per_h"],
            0.02,
            places=12,
        )
        compensated, *_ = ANALYSIS.apply_water_compensation(
            cycles,
            overview,
            representative,
            series,
            model,
        )
        reference_tail = compensated[
            compensated["series_id"] == "reference"
        ].sort_values("cycle")
        corrected = reference_tail[
            "cathodic_last1s_median_water_compensated_v"
        ].to_numpy(dtype=float)
        np.testing.assert_allclose(corrected, -0.5, atol=1e-12)

    def test_overcompensation_is_flagged_and_never_silently_clipped(self):
        cycles, overview, representative, series = make_water_compensation_fixture()
        model = ANALYSIS.fit_water_compensation_model(cycles, series)
        compensated_cycles, compensated_overview, _, compensated_series = (
            ANALYSIS.apply_water_compensation(
                cycles,
                overview,
                representative,
                series,
                model,
            )
        )

        validation = ANALYSIS.validate_water_compensation(
            cycles,
            compensated_cycles,
            overview,
            compensated_overview,
            compensated_series,
            model,
        )

        flagged = validation["selected_series_with_negative_corrected_shift"]
        flagged_by_name = {
            row["material_display_name"]: row
            for row in flagged
        }
        self.assertIn("positive-drift-control", flagged_by_name)
        self.assertLess(
            float(
                flagged_by_name["positive-drift-control"][
                    "water_comp_cathodic_negative_shift_first10_to_last10_mv"
                ]
            ),
            0.0,
        )
        victim = compensated_cycles[
            compensated_cycles["series_id"] == "overcompensated"
        ]
        self.assertGreater(
            float(
                victim["cathodic_last1s_median_water_compensated_v"].iloc[-1]
                - victim["cathodic_last1s_median_water_compensated_v"].iloc[0]
            ),
            0.0,
        )

    def test_reference_roundoff_is_not_reported_as_overcompensation(self):
        cycles, overview, representative, series = make_water_compensation_fixture()
        model = ANALYSIS.fit_water_compensation_model(cycles, series)
        compensated_cycles, compensated_overview, _, compensated_series = (
            ANALYSIS.apply_water_compensation(
                cycles,
                overview,
                representative,
                series,
                model,
            )
        )
        validation = ANALYSIS.validate_water_compensation(
            cycles,
            compensated_cycles,
            overview,
            compensated_overview,
            compensated_series,
            model,
        )

        flagged_by_name = {
            row["material_display_name"]: float(
                row["water_comp_cathodic_negative_shift_first10_to_last10_mv"]
            )
            for row in validation["selected_series_with_negative_corrected_shift"]
        }
        reference_name = "可重命名参照显示名"
        self.assertNotIn(reference_name, flagged_by_name)
        self.assertEqual(
            validation["overcompensation_tolerance_mv"],
            ANALYSIS.WATER_COMP_OVERCOMPENSATION_TOLERANCE_MV,
        )

    def test_wrong_or_ambiguous_reference_is_rejected(self):
        cycles, _, _, series = make_water_compensation_fixture()
        with self.subTest("missing reference"):
            missing = series.copy()
            missing.loc[
                missing["series_id"] == "reference",
                "material_relative_path",
            ] = "wrong/reference-key"
            with self.assertRaisesRegex(RuntimeError, "必须且只能匹配 1 条序列"):
                ANALYSIS.fit_water_compensation_model(cycles, missing)

        with self.subTest("ambiguous reference"):
            ambiguous = series.copy()
            ambiguous.loc[:, "material_relative_path"] = (
                ANALYSIS.DEFAULT_WATER_COMP_REFERENCE_KEY
            )
            with self.assertRaisesRegex(RuntimeError, "必须且只能匹配 1 条序列"):
                ANALYSIS.fit_water_compensation_model(cycles, ambiguous)

        with self.subTest("renamed display remains the same reference"):
            renamed = series.copy()
            renamed.loc[
                renamed["series_id"] == "reference",
                "material_display_name",
            ] = "用户任意改名后的参照"
            model = ANALYSIS.fit_water_compensation_model(cycles, renamed)
            self.assertEqual(
                model["reference_material_display_name"],
                "用户任意改名后的参照",
            )

        with self.subTest("controlled key override"):
            overridden = series.copy()
            overridden.loc[
                overridden["series_id"] == "reference",
                "material_relative_path",
            ] = "controlled/reference-key"
            with mock.patch.object(
                ANALYSIS,
                "WATER_COMP_REFERENCE_KEY",
                "controlled/reference-key",
            ):
                model = ANALYSIS.fit_water_compensation_model(cycles, overridden)
            self.assertEqual(
                model["reference_material_key"],
                "controlled/reference-key",
            )

        with self.subTest("non-negative reference slope"):
            invalid_cycles = cycles.copy()
            mask = invalid_cycles["series_id"] == "reference"
            invalid_cycles.loc[mask, "cathodic_last1s_median_raw_v"] = (
                -0.5 + 0.001 * invalid_cycles.loc[mask, "cathodic_endpoint_time_h"]
            )
            with self.assertRaisesRegex(RuntimeError, "负斜率"):
                ANALYSIS.fit_water_compensation_model(invalid_cycles, series)


if __name__ == "__main__":
    unittest.main()
