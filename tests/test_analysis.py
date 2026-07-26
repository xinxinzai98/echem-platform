from __future__ import annotations

import hashlib
import importlib.util
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from echem_platform.analysis import (
    AnalysisValidationError,
    calculate_cv_overpotential,
    calculate_eis_resistance,
)
from echem_platform.parsers import parse_curve, parse_numeric_table


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "echem_app_analysis_tests",
    ROOT / "app.py",
)
assert SPEC and SPEC.loader
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def parsed_table(name: str, lines: list[str]):
    payload = "\n".join(lines).encode("utf-8")
    table = parse_numeric_table(Path(name), payload)
    if table.status != "parsed":
        raise AssertionError(f"fixture did not parse: {table.error}")
    return table


def eis_table(
    rows: list[tuple[float, float, float]],
    *,
    resistance_header: str = "ohm",
):
    return parsed_table(
        "fixture_eis.txt",
        [
            "Method: EIS",
            f"Freq/Hz,Z'/{resistance_header},Z\"/{resistance_header}",
            *[f"{frequency},{real},{imaginary}" for frequency, real, imaginary in rows],
        ],
    )


def cv_table(
    rows: list[tuple[float, float]],
    *,
    potential_header: str = "Potential/V",
    current_header: str = "Current/A",
):
    return parsed_table(
        "fixture_cv.txt",
        [
            "Technique: Cyclic Voltammetry",
            f"{potential_header},{current_header}",
            *[f"{potential},{current}" for potential, current in rows],
        ],
    )


def cv_parameters(
    reaction: str,
    *,
    area_cm2: float = 1.0,
    compensation_percent: float = 85.0,
    scan_branch: str = "auto",
    online_compensation_status: str = "not_compensated",
):
    return {
        "solution": "1 M KOH",
        "ph": 14.0,
        "reaction": reaction,
        "reference_electrode": "Hg/HgO",
        "reference_offset_v": 0.098,
        "compensation_percent": compensation_percent,
        "solution_resistance_ohm": 2.0,
        "area_cm2": area_cm2,
        "target_current_density_ma_cm2": 10.0,
        "scan_branch": scan_branch,
        "online_compensation_status": online_compensation_status,
    }


class FullResolutionTableTests(unittest.TestCase):
    def test_numeric_table_and_analysis_keep_all_rows_while_curve_is_decimated(self):
        rows = [
            (100_000.0 - index * 100.0, 1.0 + index * 0.01, -0.5)
            for index in range(101)
        ]
        lines = [
            "Method: EIS",
            "Freq/Hz,Z'/ohm,Z\"/ohm",
            *[f"{frequency},{real},{imaginary}" for frequency, real, imaginary in rows],
        ]
        payload = "\n".join(lines).encode("utf-8")

        table = parse_numeric_table(Path("full_eis.txt"), payload)
        curve = parse_curve(Path("full_eis.txt"), payload, 7)
        analysis = calculate_eis_resistance(table)

        self.assertEqual(table.row_count, 101)
        self.assertEqual(len(table.rows), 101)
        self.assertEqual(curve.point_count, 101)
        self.assertEqual(len(curve.points), 7)
        self.assertEqual(analysis["result"]["point_count"], 101)


class EISResistanceTests(unittest.TestCase):
    def test_high_and_low_frequency_intercepts_are_interpolated_without_fitting(self):
        table = eis_table(
            [
                (100_000.0, 1.0, 1.0),
                (10_000.0, 3.0, -1.0),
                (1_000.0, 7.0, -1.0),
                (100.0, 9.0, 1.0),
            ]
        )

        analysis = calculate_eis_resistance(table)
        result = analysis["result"]

        self.assertAlmostEqual(result["solution_resistance"], 2.0)
        self.assertAlmostEqual(result["low_frequency_intercept"], 8.0)
        self.assertAlmostEqual(result["apparent_polarization_resistance"], 6.0)
        self.assertEqual(
            [crossing["kind"] for crossing in result["crossings"]],
            ["high_frequency", "low_frequency"],
        )
        self.assertFalse(result["extrapolated"])
        self.assertTrue(any("不是等效电路拟合" in item for item in analysis["warnings"]))
        self.assertEqual(
            result["quality"],
            {
                "level": "screening",
                "label": "仅筛查",
                "reasons": [
                    "当前结果来自实轴零交叉插值，不是等效电路拟合，仅可作为筛查值。"
                ],
            },
        )

    def test_no_zero_crossing_returns_no_resistance_and_never_extrapolates(self):
        table = eis_table(
            [
                (100_000.0, 1.0, -0.1),
                (10_000.0, 2.0, -0.4),
                (1_000.0, 4.0, -0.8),
            ]
        )

        analysis = calculate_eis_resistance(table)
        result = analysis["result"]

        self.assertIsNone(result["solution_resistance"])
        self.assertIsNone(result["low_frequency_intercept"])
        self.assertIsNone(result["apparent_polarization_resistance"])
        self.assertEqual(result["crossings"], [])
        self.assertFalse(result["extrapolated"])
        self.assertTrue(any("未外推溶液电阻" in item for item in analysis["warnings"]))
        self.assertEqual(result["quality"]["level"], "not_calculable")
        self.assertEqual(result["quality"]["label"], "不可计算")
        self.assertTrue(result["quality"]["reasons"])

    def test_resistance_units_distinguish_ohm_and_area_normalized_ohm(self):
        cases = (
            ("ohm", "Ω"),
            ("Ohm.cm²", "Ω·cm²"),
        )
        for header_unit, expected in cases:
            with self.subTest(header_unit=header_unit):
                table = eis_table(
                    [
                        (100_000.0, 1.0, 1.0),
                        (10_000.0, 3.0, -1.0),
                    ],
                    resistance_header=header_unit,
                )
                result = calculate_eis_resistance(table)["result"]
                self.assertEqual(result["resistance_unit"], expected)
                self.assertAlmostEqual(result["solution_resistance"], 2.0)

    def test_resistance_prefixes_are_converted_and_negative_intercepts_are_blocked(self):
        kilo = calculate_eis_resistance(
            eis_table(
                [
                    (100_000.0, 0.001, 0.001),
                    (10_000.0, 0.003, -0.001),
                ],
                resistance_header="kOhm",
            )
        )
        self.assertEqual(kilo["result"]["resistance_unit"], "Ω")
        self.assertAlmostEqual(kilo["result"]["solution_resistance"], 2.0)

        negative = calculate_eis_resistance(
            eis_table(
                [
                    (100_000.0, -3.0, 1.0),
                    (10_000.0, -1.0, -1.0),
                ]
            )
        )
        self.assertIsNone(negative["result"]["solution_resistance"])
        self.assertEqual(
            negative["result"]["crossings"][0]["kind"],
            "high_frequency_invalid",
        )
        self.assertTrue(any("负值" in item for item in negative["warnings"]))


class CVOverpotentialTests(unittest.TestCase):
    def test_her_and_oer_apply_rhe_and_ir_correction_with_opposite_current_signs(self):
        her = calculate_cv_overpotential(
            cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
            cv_parameters("HER"),
        )["result"]["target_point"]
        oer = calculate_cv_overpotential(
            cv_table([(0.4, 0.0), (0.5, 0.02)]),
            cv_parameters("OER"),
        )["result"]["target_point"]
        assert her is not None and oer is not None

        expected_her_rhe = -0.95 + 0.098 + 0.05916 * 14.0
        expected_oer_rhe = 0.45 + 0.098 + 0.05916 * 14.0
        self.assertAlmostEqual(her["potential_rhe_v"], expected_her_rhe)
        self.assertAlmostEqual(oer["potential_rhe_v"], expected_oer_rhe)
        self.assertAlmostEqual(her["applied_ir_drop_v"], -0.017)
        self.assertAlmostEqual(oer["applied_ir_drop_v"], 0.017)
        self.assertAlmostEqual(
            her["compensated_potential_v"],
            expected_her_rhe + 0.017,
        )
        self.assertAlmostEqual(
            oer["compensated_potential_v"],
            expected_oer_rhe - 0.017,
        )
        self.assertAlmostEqual(
            her["overpotential_mv"],
            abs(expected_her_rhe + 0.017) * 1_000.0,
        )
        self.assertAlmostEqual(
            her["signed_overpotential_mv"],
            (expected_her_rhe + 0.017) * 1_000.0,
        )
        self.assertAlmostEqual(
            oer["overpotential_mv"],
            abs(expected_oer_rhe - 0.017 - 1.229) * 1_000.0,
        )
        self.assertAlmostEqual(
            oer["signed_overpotential_mv"],
            (expected_oer_rhe - 0.017 - 1.229) * 1_000.0,
        )

    def test_current_and_potential_units_use_area_consistently(self):
        cases = (
            ("Potential/V", "Current/A", [(-1.0, -0.04), (-0.9, 0.0)]),
            ("Potential/mV", "Current/mA", [(-1_000.0, -40.0), (-900.0, 0.0)]),
            (
                "Potential/V",
                "CurrentDensity/A/cm²",
                [(-1.0, -0.02), (-0.9, 0.0)],
            ),
            (
                "Potential/V",
                "CurrentDensity/mA/cm²",
                [(-1.0, -20.0), (-0.9, 0.0)],
            ),
            (
                "Potential/V",
                "CurrentDensity/A/m²",
                [(-1.0, -200.0), (-0.9, 0.0)],
            ),
            (
                "Potential/V",
                "Current Density / mA cm⁻²",
                [(-1.0, -20.0), (-0.9, 0.0)],
            ),
            (
                "Potential/V",
                "Current Density / mA cm−2",
                [(-1.0, -20.0), (-0.9, 0.0)],
            ),
            (
                "Potential/V",
                "Current Density / A m⁻²",
                [(-1.0, -200.0), (-0.9, 0.0)],
            ),
        )
        parameters = cv_parameters("HER", area_cm2=2.0, compensation_percent=0.0)
        for potential_header, current_header, rows in cases:
            with self.subTest(
                potential_header=potential_header,
                current_header=current_header,
            ):
                result = calculate_cv_overpotential(
                    cv_table(
                        rows,
                        potential_header=potential_header,
                        current_header=current_header,
                    ),
                    parameters,
                )["result"]["target_point"]
                assert result is not None
                self.assertAlmostEqual(result["measured_potential_v"], -0.95)
                self.assertAlmostEqual(result["current_a"], -0.02)
                self.assertAlmostEqual(result["interpolated_current_a"], -0.02)
                self.assertAlmostEqual(result["current_density_ma_cm2"], -10.0)

    def test_rhe_input_is_not_shifted_by_ph_and_rejects_a_nonzero_offset(self):
        parameters = cv_parameters("HER", compensation_percent=0.0)
        parameters.update(
            {
                "reference_electrode": "RHE",
                "reference_offset_v": 0.0,
                "ph": 14.0,
            }
        )
        analysis = calculate_cv_overpotential(
            cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
            parameters,
        )
        target = analysis["result"]["target_point"]
        assert target is not None
        self.assertAlmostEqual(target["measured_potential_v"], -0.95)
        self.assertAlmostEqual(target["potential_rhe_v"], -0.95)
        self.assertEqual(
            analysis["parameters"]["reference_conversion_mode"],
            "already_rhe",
        )

        parameters["reference_offset_v"] = 0.1
        with self.assertRaises(AnalysisValidationError) as caught:
            calculate_cv_overpotential(
                cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
                parameters,
            )
        self.assertEqual(caught.exception.code, "invalid_reference_conversion")

    def test_forward_and_reverse_branches_produce_distinct_intersections(self):
        table = cv_table(
            [
                (-1.0, -0.020),
                (-0.9, 0.000),
                (-0.8, 0.010),
                (-0.9, -0.005),
                (-1.0, -0.030),
            ]
        )
        forward_parameters = cv_parameters(
            "HER",
            compensation_percent=0.0,
            scan_branch="forward",
        )
        forward_parameters.update(
            {
                "reference_electrode": "RHE",
                "reference_offset_v": 0.0,
            }
        )
        reverse_parameters = dict(forward_parameters)
        reverse_parameters["scan_branch"] = "reverse"
        forward = calculate_cv_overpotential(
            table,
            forward_parameters,
        )["result"]
        reverse = calculate_cv_overpotential(
            table,
            reverse_parameters,
        )["result"]
        assert forward["target_point"] is not None
        assert reverse["target_point"] is not None

        self.assertEqual(forward["selected_branch"]["id"], "segment_1")
        self.assertEqual(reverse["selected_branch"]["id"], "segment_2")
        self.assertEqual(
            [option["value"] for option in forward["branch_options"]],
            ["segment_1", "segment_2"],
        )
        self.assertEqual(
            [option["label"] for option in forward["branch_options"]],
            [
                "段1：-1→-0.8 V（电位递增）",
                "段2：-0.8→-1 V（电位递减）",
            ],
        )
        self.assertEqual(
            calculate_cv_overpotential(table, forward_parameters)["parameters"][
                "scan_branch"
            ],
            "segment_1",
        )
        self.assertEqual(
            calculate_cv_overpotential(table, reverse_parameters)["parameters"][
                "scan_branch"
            ],
            "segment_2",
        )
        self.assertAlmostEqual(
            forward["target_point"]["measured_potential_v"],
            -0.95,
        )
        self.assertAlmostEqual(
            reverse["target_point"]["measured_potential_v"],
            -0.92,
        )
        with self.assertRaises(AnalysisValidationError) as caught:
            auto_parameters = dict(forward_parameters)
            auto_parameters["scan_branch"] = "auto"
            calculate_cv_overpotential(table, auto_parameters)
        self.assertEqual(caught.exception.code, "branch_required")
        self.assertEqual(len(caught.exception.details["branch_options"]), 2)

    def test_reaction_direction_mismatch_is_rejected_before_reporting_magnitude(self):
        cases = (
            ("HER", cv_table([(0.4, -0.02), (0.5, 0.0)])),
            ("OER", cv_table([(0.4, 0.0), (0.5, 0.02)])),
        )
        for reaction, table in cases:
            with self.subTest(reaction=reaction):
                parameters = cv_parameters(reaction, compensation_percent=0.0)
                parameters.update(
                    {
                        "reference_electrode": "RHE",
                        "reference_offset_v": 0.0,
                    }
                )
                with self.assertRaises(AnalysisValidationError) as caught:
                    calculate_cv_overpotential(table, parameters)
                self.assertEqual(
                    caught.exception.code,
                    "reaction_direction_mismatch",
                )
                self.assertEqual(caught.exception.field, "reaction")
                self.assertIn(
                    "signed_overpotential_mv",
                    caught.exception.details,
                )

    def test_target_not_reached_returns_no_target_point_and_does_not_extrapolate(self):
        analysis = calculate_cv_overpotential(
            cv_table([(-1.0, -0.001), (-0.9, -0.002)]),
            cv_parameters("HER", compensation_percent=0.0),
        )

        self.assertIsNone(analysis["result"]["target_point"])
        self.assertTrue(any("未外推过电位" in item for item in analysis["warnings"]))
        self.assertEqual(
            analysis["result"]["quality"]["level"],
            "not_calculable",
        )

    def test_cv_quality_distinguishes_quantitative_and_screening_results(self):
        quantitative_parameters = cv_parameters(
            "HER",
            compensation_percent=0.0,
        )
        quantitative_parameters.update(
            {
                "reference_electrode": "RHE",
                "reference_offset_v": 0.0,
            }
        )
        quantitative = calculate_cv_overpotential(
            cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
            quantitative_parameters,
        )
        self.assertEqual(
            quantitative["result"]["quality"]["level"],
            "quantitative",
        )

        multiple_crossings = calculate_cv_overpotential(
            cv_table(
                [
                    (-1.1, -0.005),
                    (-1.0, -0.020),
                    (-0.9, -0.005),
                    (-0.8, -0.020),
                ]
            ),
            quantitative_parameters,
        )
        self.assertEqual(
            multiple_crossings["result"]["quality"]["level"],
            "screening",
        )
        self.assertTrue(
            any(
                "多个目标电流交点" in reason
                for reason in multiple_crossings["result"]["quality"]["reasons"]
            )
        )

    def test_online_compensation_blocks_nonzero_second_compensation(self):
        with self.assertRaises(AnalysisValidationError) as caught:
            calculate_cv_overpotential(
                cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
                cv_parameters(
                    "HER",
                    compensation_percent=85.0,
                    online_compensation_status="already_compensated",
                ),
            )
        self.assertEqual(caught.exception.code, "double_compensation")
        self.assertEqual(caught.exception.field, "compensation_percent")

        with self.assertRaises(AnalysisValidationError) as caught:
            calculate_cv_overpotential(
                cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
                cv_parameters(
                    "HER",
                    compensation_percent=85.0,
                    online_compensation_status="unknown",
                ),
            )
        self.assertEqual(caught.exception.code, "compensation_status_unknown")
        self.assertEqual(caught.exception.field, "compensation_percent")

        allowed = calculate_cv_overpotential(
            cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
            cv_parameters(
                "HER",
                compensation_percent=0.0,
                online_compensation_status="already_compensated",
            ),
        )
        self.assertIsNotNone(allowed["result"]["target_point"])
        self.assertEqual(
            allowed["result"]["quality"]["level"],
            "screening",
        )

        unknown_without_offline_compensation = calculate_cv_overpotential(
            cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
            cv_parameters(
                "HER",
                compensation_percent=0.0,
                online_compensation_status="unknown",
            ),
        )
        self.assertIsNotNone(
            unknown_without_offline_compensation["result"]["target_point"]
        )
        self.assertTrue(
            any(
                "本次未应用离线 iR 补偿" in warning
                for warning in unknown_without_offline_compensation["warnings"]
            )
        )
        self.assertEqual(
            unknown_without_offline_compensation["result"]["quality"]["level"],
            "screening",
        )

    def test_zero_offline_compensation_allows_null_rs_without_recording_a_fake_zero(self):
        base = cv_parameters(
            "HER",
            compensation_percent=0.0,
            online_compensation_status="not_compensated",
        )
        base["solution_resistance_ohm"] = None

        not_compensated = calculate_cv_overpotential(
            cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
            base,
        )

        self.assertIsNone(
            not_compensated["parameters"]["solution_resistance_ohm"]
        )
        self.assertEqual(
            not_compensated["result"]["target_point"]["applied_ir_drop_v"],
            0.0,
        )
        self.assertEqual(
            not_compensated["result"]["quality"]["level"],
            "quantitative",
        )

        for status in ("unknown", "already_compensated"):
            with self.subTest(status=status):
                parameters = dict(base)
                parameters["online_compensation_status"] = status
                analysis = calculate_cv_overpotential(
                    cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
                    parameters,
                )
                self.assertIsNone(
                    analysis["parameters"]["solution_resistance_ohm"]
                )
                self.assertEqual(
                    analysis["result"]["quality"]["level"],
                    "screening",
                )

    def test_nonzero_offline_compensation_requires_positive_rs(self):
        for invalid_rs, expected_code in (
            (None, "missing_parameter"),
            ("", "missing_parameter"),
            (0.0, "parameter_out_of_range"),
        ):
            with self.subTest(solution_resistance_ohm=invalid_rs):
                parameters = cv_parameters(
                    "HER",
                    compensation_percent=85.0,
                    online_compensation_status="not_compensated",
                )
                parameters["solution_resistance_ohm"] = invalid_rs
                with self.assertRaises(AnalysisValidationError) as caught:
                    calculate_cv_overpotential(
                        cv_table([(-1.0, -0.02), (-0.9, 0.0)]),
                        parameters,
                    )
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(
                    caught.exception.field,
                    "solution_resistance_ohm",
                )


class AnalysisPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.database = APP.Database(self.root / "state" / "analysis.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def make_runtime(self, source: Path, *, max_points: int = 2):
        scanner = APP.Scanner(
            database=self.database,
            roots=[self.source_root],
            extensions=[source.suffix.lower()],
            max_file_bytes=1024 * 1024,
            max_points=max_points,
            stable_age_seconds=0,
        )
        result = scanner.scan()
        self.assertEqual(result["imported"], 1)
        runtime = APP.RuntimeState(
            self.database,
            scanner,
            APP.load_config(ROOT / "config.json"),
        )
        run = self.database.list_runs()[0]
        return runtime, run

    def test_analysis_records_are_append_only_and_database_triggers_block_changes(self):
        source = self.source_root / "record_cv.txt"
        source.write_text(
            "\n".join(
                [
                    "Technique: Cyclic Voltammetry",
                    "Potential/V,Current/A",
                    "-1.0,-0.02",
                    "-0.9,0.0",
                ]
            ),
            encoding="utf-8",
        )
        runtime, run = self.make_runtime(source)
        saved = runtime.calculate_analysis(
            run["id"],
            "cv_overpotential",
            cv_parameters("HER"),
            persist=True,
        )
        original = self.database.list_analyses(run["id"])
        assert original is not None
        self.assertEqual(len(original), 1)
        self.assertEqual(original[0], saved)
        self.assertEqual(saved["parameters"]["scan_branch"], "segment_1")

        with self.assertRaises(sqlite3.DatabaseError):
            with self.database.session() as connection:
                connection.execute(
                    "UPDATE analysis_records SET algorithm_version = ? WHERE id = ?",
                    ("tampered", saved["id"]),
                )
        with self.assertRaises(sqlite3.DatabaseError):
            with self.database.session() as connection:
                connection.execute(
                    "DELETE FROM analysis_records WHERE id = ?",
                    (saved["id"],),
                )

        after = self.database.list_analyses(run["id"])
        self.assertEqual(after, original)

    def test_runtime_rereads_full_source_checks_sha_and_never_modifies_source(self):
        source = self.source_root / "runtime_eis.txt"
        payload = "\n".join(
            [
                "Method: EIS",
                "Freq/Hz,Z'/ohm,Z\"/ohm",
                "100000,1,1",
                "10000,3,-1",
                "1000,7,-1",
                "100,9,1",
            ]
        ).encode("utf-8")
        source.write_bytes(payload)
        before = source.stat()
        runtime, run = self.make_runtime(source, max_points=2)
        cached = self.database.get_run(run["id"])
        assert cached is not None
        self.assertEqual(len(cached["points"]), 2)

        preview = runtime.calculate_analysis(
            run["id"],
            "eis_resistance",
            {},
            persist=False,
        )
        after = source.stat()

        self.assertEqual(preview["source_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertNotIn("source_path", preview)
        self.assertEqual(preview["result"]["point_count"], 4)
        self.assertAlmostEqual(preview["result"]["solution_resistance"], 2.0)
        self.assertAlmostEqual(preview["result"]["low_frequency_intercept"], 8.0)
        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

        source.write_bytes(payload.replace(b"100000,1,1", b"100000,2,1"))
        with self.assertRaises(APP.AnalysisRequestError) as caught:
            runtime.calculate_analysis(
                run["id"],
                "eis_resistance",
                {},
                persist=False,
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("重新扫描", str(caught.exception))
        self.assertEqual(self.database.list_analyses(run["id"]), [])

    def test_runtime_refuses_to_persist_not_calculable_preview(self):
        source = self.source_root / "target_not_reached_cv.txt"
        source.write_text(
            "\n".join(
                [
                    "Technique: Cyclic Voltammetry",
                    "Potential/V,Current/A",
                    "-1.0,-0.001",
                    "-0.9,-0.002",
                ]
            ),
            encoding="utf-8",
        )
        runtime, run = self.make_runtime(source)
        parameters = cv_parameters("HER", compensation_percent=0.0)
        audit_before = self.database.recent_audit(200)

        preview = runtime.calculate_analysis(
            run["id"],
            "cv_overpotential",
            parameters,
            persist=False,
        )
        self.assertEqual(
            preview["result"]["quality"]["level"],
            "not_calculable",
        )

        with self.assertRaises(AnalysisValidationError) as caught:
            runtime.calculate_analysis(
                run["id"],
                "cv_overpotential",
                parameters,
                persist=True,
            )
        self.assertEqual(caught.exception.code, "analysis_not_calculable")
        self.assertEqual(caught.exception.field, "quality")
        self.assertEqual(
            caught.exception.details["quality"]["level"],
            "not_calculable",
        )
        self.assertEqual(self.database.list_analyses(run["id"]), [])
        self.assertEqual(self.database.recent_audit(200), audit_before)

    def test_runtime_refuses_to_persist_not_calculable_eis_result(self):
        source = self.source_root / "no_intercept_eis.txt"
        source.write_text(
            "\n".join(
                [
                    "Method: EIS",
                    "Freq/Hz,Z'/ohm,Z\"/ohm",
                    "100000,1,-0.1",
                    "10000,2,-0.4",
                    "1000,4,-0.8",
                ]
            ),
            encoding="utf-8",
        )
        runtime, run = self.make_runtime(source)
        audit_before = self.database.recent_audit(200)

        preview = runtime.calculate_analysis(
            run["id"],
            "eis_resistance",
            {},
            persist=False,
        )
        self.assertEqual(
            preview["result"]["quality"]["level"],
            "not_calculable",
        )
        with self.assertRaises(AnalysisValidationError) as caught:
            runtime.calculate_analysis(
                run["id"],
                "eis_resistance",
                {},
                persist=True,
            )

        self.assertEqual(caught.exception.code, "analysis_not_calculable")
        self.assertEqual(self.database.list_analyses(run["id"]), [])
        self.assertEqual(self.database.recent_audit(200), audit_before)

    def test_runtime_allows_screening_result_to_be_persisted(self):
        source = self.source_root / "screening_eis.txt"
        source.write_text(
            "\n".join(
                [
                    "Method: EIS",
                    "Freq/Hz,Z'/ohm,Z\"/ohm",
                    "100000,1,1",
                    "10000,3,-1",
                ]
            ),
            encoding="utf-8",
        )
        runtime, run = self.make_runtime(source)

        saved = runtime.calculate_analysis(
            run["id"],
            "eis_resistance",
            {},
            persist=True,
        )

        self.assertEqual(saved["result"]["quality"]["level"], "screening")
        records = self.database.list_analyses(run["id"])
        assert records is not None
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"]["quality"]["level"], "screening")


if __name__ == "__main__":
    unittest.main()
