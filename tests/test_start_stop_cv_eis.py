from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_cv_eis import (
    CvEisAnalysisError,
    CvEisRepositoryAnalyzer,
    ReadOnlyCvEisDatabase,
    SourceRecord,
    _pair_score,
    _source_kind_and_priority,
    analyze_pair,
    extract_high_frequency_rs,
    parse_cv_text,
    parse_eis_text,
    return_scan_indices,
)


def corrtest_header(*, ir_applied: bool = False, area: float = 1.0) -> str:
    payload = (
        "CORRW ASCII\n"
        f"ExpParmas:DevParam=IsIR={'True' if ir_applied else 'False'}&|&IrXS=1"
        f"&-&CellParam=Area={area}&|&Temp=25\n"
        f"Surface Area: {area}\n"
    ).encode("utf-8")
    return base64.b64encode(gzip.compress(payload)).decode("ascii")


def corrtest_cv(*, ir_applied: bool = False) -> bytes:
    potential = [-0.85 - index * 0.005 for index in range(61)]
    potential += [-1.15 + index * 0.005 for index in range(1, 61)]
    lines = [
        f"CSStudioFile,ID_CV,{corrtest_header(ir_applied=ir_applied)}",
        "E(V)\ti(A/cm²)\tT(s)",
    ]
    for index, value in enumerate(potential):
        current = min(0.0, value + 0.9268)
        lines.append(f"{value:.6f}\t{current:.8f}\t{index * 0.1:.2f}")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def corrtest_eis(*, crossing: bool = True, real_offset: float = 0.0) -> bytes:
    imaginary = (0.10, 0.02, -0.02, -0.10) if crossing else (-0.10, -0.08, -0.05, -0.03)
    lines = [
        "CSStudioFile,ID_EISVSFRQ,invalid",
        "Freq(Hz)\tZ'(Ohm.cm²)\tZ''(Ohm.cm²)",
    ]
    for frequency, real, imag in zip((10000, 2000, 1000, 500), (1.0, 1.1, 1.2, 1.3), imaginary):
        real += real_offset
        lines.append(f"{frequency}\t{real}\t{imag}")
    # Minimum parser length is eight points; lower-frequency rows do not affect
    # the highest-frequency-first crossing.
    for frequency, real, imag in ((400, 1.4, -0.2), (300, 1.5, -0.3), (200, 1.6, -0.4), (100, 1.7, -0.5)):
        real += real_offset
        lines.append(f"{frequency}\t{real}\t{imag}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def chi_cv() -> bytes:
    potential = [-0.85 - index * 0.005 for index in range(61)]
    potential += [-1.15 + index * 0.005 for index in range(1, 61)]
    lines = [
        "August 20, 2026",
        "Cyclic Voltammetry",
        "Instrument Model: CHI760E",
        "Potential/V, Current/A",
        "",
    ]
    for value in potential:
        lines.append(f"{value:.6f}, {min(0.0, value + 0.9268):.8f}")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def chi_eis() -> bytes:
    lines = [
        "August 20, 2026",
        "A.C. Impedance",
        "Instrument Model: CHI760E",
        "Freq/Hz, Z'/ohm, Z\"/ohm, Z/ohm, Phase/deg",
        "",
    ]
    for frequency, real, imag in (
        (10000, 1.0, 0.10),
        (2000, 1.1, 0.02),
        (1000, 1.2, -0.02),
        (500, 1.3, -0.10),
        (400, 1.4, -0.2),
        (300, 1.5, -0.3),
        (200, 1.6, -0.4),
        (100, 1.7, -0.5),
    ):
        magnitude = (real**2 + imag**2) ** 0.5
        lines.append(f"{frequency}, {real}, {imag}, {magnitude}, 0")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


class StartStopCvEisTests(unittest.TestCase):
    def cv_from_scans(self, *scans):
        lines = [f"CSStudioFile,ID_CV,{corrtest_header()}", "E(V)\ti(A/cm²)\tT(s)"]
        for potential, gain in scans:
            for value in potential:
                lines.append(f"{value:.8f}\t{gain * min(0.0, value + 0.9268):.8f}\t{len(lines)}")
        return parse_cv_text(("\n".join(lines) + "\n").encode())

    def test_last_local_turn_is_selected_when_cycles_reach_different_minima(self):
        first = [-0.85 - i * 0.005 for i in range(61)]
        first += [-1.15 + i * 0.005 for i in range(1, 61)]
        for second_minimum in (-1.14, -1.15, -1.16):
            with self.subTest(second_minimum=second_minimum):
                count = round((-0.85 - second_minimum) / 0.005)
                second = [-0.85 - i * 0.005 for i in range(count + 1)]
                second += [second_minimum + i * 0.005 for i in range(1, count + 1)]
                cv = self.cv_from_scans((first, 1), (second, 2))
                start, end = return_scan_indices(cv)
                self.assertEqual((start, end), (len(first) + count, len(first) + len(second)))
                analysis = analyze_pair(cv, parse_eis_text(corrtest_eis()))
                eta100 = next(row for row in analysis["overpotentials"] if row["target_ma_cm2"] == 100)
                self.assertAlmostEqual(eta100["raw_eta_mv"], 50.0, places=6)

    def test_complete_branch_ends_before_following_incomplete_scan(self):
        first = [-0.85 - i * 0.005 for i in range(61)]
        first += [-1.15 + i * 0.005 for i in range(1, 61)]
        outward = [-0.85 - i * 0.005 for i in range(1, 59)]
        partial_return = [-1.14 + i * 0.005 for i in range(1, 31)]
        for trailing in (outward, outward + partial_return):
            with self.subTest(trailing_points=len(trailing)):
                cv = self.cv_from_scans((first, 1), (trailing, 2))
                self.assertEqual(return_scan_indices(cv), (60, len(first)))
                result = analyze_pair(cv, parse_eis_text(corrtest_eis()))
                self.assertEqual(result["return_scan_points"], 61)
                self.assertEqual(result["return_scan_omitted_tail_points"], len(trailing))

    def test_unclosed_single_return_is_rejected_even_with_many_points_and_50mv_excursion(self):
        outward = [-0.85 - i * 0.005 for i in range(61)]
        incomplete = outward + [-1.15 + i * 0.005 for i in range(1, 41)]
        with self.assertRaisesRegex(CvEisAnalysisError, "完整阴极回扫"):
            analyze_pair(self.cv_from_scans((incomplete, 1)), parse_eis_text(corrtest_eis()))

    def test_catalog_explains_selection_before_an_incomplete_trailing_scan(self):
        from echem_platform.start_stop_database import StartStopDatabase

        lines = corrtest_cv().decode().splitlines()
        for index in range(1, 41):
            value = -0.85 - index * 0.005
            lines.append(f"{value:.6f}\t{2 * min(0., value + 0.9268):.8f}\t{20 + index * .1}")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StartStopDatabase(root / "repository.sqlite3")
            for name, content in (("her-cv.txt", "\n".join(lines).encode()), ("her-eis.txt", corrtest_eis())):
                source = root / name
                source.write_bytes(content)
                database.ingest_staged_file(source, {
                    "machine_id": "lab", "root_label": "data", "remote_path": f"D:\\sample\\{name}",
                    "repository_path": f"lab/data/sample/{name}", "size": len(content),
                    "last_write_ticks": 123, "last_write_utc": "2026-09-01T00:00:00Z", "is_candidate": False,
                })
            analyzer = CvEisRepositoryAnalyzer(database)
            record = analyzer.catalog()["materials"][0]
            self.assertEqual(record["status"], "ready")
            self.assertEqual(record["return_scan_points"], 61)
            self.assertTrue(any("40 个记录点未纳入" in warning for warning in record["warnings"]))
            self.assertEqual(
                analyzer.curve(record["analysis_id"])["rules"]["scan"],
                "last_complete_cathodic_return_branch",
            )

    def test_return_endpoint_can_omit_one_measured_increment_but_not_a_partial_tail(self):
        outward = [-0.85 - i * 0.005 for i in range(61)]
        complete = outward + [-1.15 + i * 0.005 for i in range(1, 60)]
        self.assertEqual(return_scan_indices(self.cv_from_scans((complete, 1))), (60, len(complete)))
        with self.assertRaisesRegex(CvEisAnalysisError, "完整阴极回扫"):
            return_scan_indices(self.cv_from_scans((complete[:-2], 1)))

    def test_plateau_and_small_direction_noise_preserve_the_original_return_samples(self):
        outward = [-0.85 - i * 0.005 for i in range(61)]
        outward[20] = outward[19] + 0.001  # isolated reversal during cathodic scan
        plateau = [-1.15, -1.149, -1.15, -1.15]
        returning = [-1.15 + i * 0.005 for i in range(1, 61)]
        returning[20] = returning[19] - 0.001  # isolated reversal during return
        potential = outward + plateau + returning
        cv = self.cv_from_scans((potential, 1))
        expected_start = len(outward) + len(plateau) - 1
        self.assertEqual(return_scan_indices(cv), (expected_start, len(potential)))
        result = analyze_pair(cv, None)
        self.assertEqual(result["return_scan_points"], len(potential) - expected_start)
        self.assertEqual(
            [point["raw_e_rhe_v"] for point in result["points"]],
            [value + 0.9268 for value in cv.potential_v[expected_start:]],
        )

    def test_initial_anodic_scan_does_not_count_as_a_cathodic_return(self):
        anodic = [-1.0 + i * 0.005 for i in range(61)]
        with self.assertRaisesRegex(CvEisAnalysisError, "完整阴极回扫"):
            return_scan_indices(self.cv_from_scans((anodic, 1)))
        cathodic = [-0.7 - i * 0.005 for i in range(1, 81)]
        returning = [-1.1 + i * 0.005 for i in range(1, 81)]
        cv = self.cv_from_scans((anodic + cathodic + returning, 1))
        self.assertEqual(return_scan_indices(cv), (len(anodic) + len(cathodic) - 1, len(cv.potential_v)))

    def test_corrtest_pair_uses_return_scan_high_frequency_rs_and_90_percent_ir(self):
        cv = parse_cv_text(corrtest_cv())
        eis = parse_eis_text(corrtest_eis())
        rs = extract_high_frequency_rs(eis)
        analysis = analyze_pair(cv, eis)

        self.assertEqual(cv.current_basis, "density")
        self.assertEqual(cv.area_cm2, 1.0)
        self.assertFalse(cv.instrument_ir_applied)
        self.assertAlmostEqual(rs["rs"], 1.15, places=9)
        eta10 = next(row for row in analysis["overpotentials"] if row["target_ma_cm2"] == 10)
        self.assertAlmostEqual(eta10["raw_eta_mv"], 10.0, places=5)
        self.assertAlmostEqual(eta10["ir90_eta_mv"], -0.35, places=5)
        self.assertLessEqual(len(analysis["points"]), 2400)

    def test_chi_pair_keeps_absolute_current_and_does_not_claim_density_overpotential(self):
        analysis = analyze_pair(parse_cv_text(chi_cv()), parse_eis_text(chi_eis()))

        self.assertEqual(analysis["current_basis"], "absolute")
        self.assertEqual(analysis["current_unit"], "mA")
        self.assertEqual(analysis["overpotentials"], [])
        self.assertEqual(analysis["rs"]["unit"], "Ω")

    def test_missing_high_frequency_zero_crossing_is_rejected(self):
        with self.assertRaisesRegex(CvEisAnalysisError, "没有可确认"):
            extract_high_frequency_rs(parse_eis_text(corrtest_eis(crossing=False)))

    def test_instrument_compensated_cv_is_not_compensated_twice(self):
        result = analyze_pair(parse_cv_text(corrtest_cv(ir_applied=True)), parse_eis_text(corrtest_eis()))
        self.assertFalse(result["ir_correction_available"])
        self.assertEqual(result["overpotentials"], [])
        self.assertTrue(result["points"])
        self.assertTrue(all(point["ir90_e_rhe_v"] is None for point in result["points"]))

    def test_pairing_prefers_matching_file_signature_inside_one_directory(self):
        def source(identifier: int, name: str, kind: str) -> SourceRecord:
            return SourceRecord(
                source_version_id=identifier,
                blob_id=identifier,
                repository_path=f"machine/root/sample/{name}",
                size_bytes=100,
                sha256="a" * 64,
                source_modified_utc="2026-08-20T00:00:00Z",
                kind=kind,
                stage="main",
                priority=80,
            )

        cv = source(1, "16.1#-Fe30-CV.txt", "cv")
        matching = source(2, "16.1#-Fe30-EIS.txt", "eis")
        other = source(3, "16.1#-Fe40-EIS.txt", "eis")
        self.assertGreater(_pair_score(cv, matching), _pair_score(cv, other))

    def test_scan_rate_and_copy_files_are_not_formal_performance_cv(self):
        self.assertEqual(_source_kind_and_priority("sample/cv1.txt"), ("", 0))
        self.assertEqual(_source_kind_and_priority("sample/her-cv_copy.txt"), ("", 0))
        self.assertEqual(_source_kind_and_priority("sample/her-cv.txt")[0], "cv")
        self.assertEqual(_source_kind_and_priority("sample/her-eis.txt")[0], "eis")

    def test_repository_catalog_reads_verified_blobs_and_pairs_matching_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "repository.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE content_blobs(
                    id INTEGER PRIMARY KEY, sha256 TEXT, size_bytes INTEGER, content BLOB
                );
                CREATE TABLE sources(
                    id INTEGER PRIMARY KEY, machine_id TEXT, root_label TEXT,
                    remote_path TEXT, created_utc TEXT
                );
                CREATE TABLE source_versions(
                    id INTEGER PRIMARY KEY, source_id INTEGER, blob_id INTEGER,
                    repository_path TEXT, size_bytes INTEGER,
                    source_modified_utc TEXT
                );
                CREATE TABLE source_selections(
                    id INTEGER PRIMARY KEY, source_id INTEGER, source_version_id INTEGER
                );
                CREATE TABLE source_current(
                    source_id INTEGER PRIMARY KEY, selection_id INTEGER
                );
                """
            )
            fixtures = (
                ("16.1#-Fe30-CV.txt", corrtest_cv()),
                ("16.1#-Fe30-EIS.txt", corrtest_eis(real_offset=0.0)),
                ("16.1#-Fe40-CV.txt", corrtest_cv()),
                ("16.1#-Fe40-EIS.txt", corrtest_eis(real_offset=1.0)),
            )
            for index, (name, payload) in enumerate(fixtures, start=1):
                connection.execute(
                    "INSERT INTO content_blobs VALUES(?,?,?,?)",
                    (index, hashlib.sha256(payload).hexdigest(), len(payload), payload),
                )
                connection.execute(
                    "INSERT INTO sources VALUES(?,?,?,?,?)",
                    (index, "machine", "root", f"D:\\sample\\{name}", "2026-08-20T00:00:00Z"),
                )
                connection.execute(
                    "INSERT INTO source_versions VALUES(?,?,?,?,?,?)",
                    (
                        index,
                        index,
                        index,
                        f"machine/root/sample/{name}",
                        len(payload),
                        f"2026-08-20T00:0{index}:00Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO source_selections VALUES(?,?,?)",
                    (index, index, index),
                )
                connection.execute(
                    "INSERT INTO source_current VALUES(?,?)",
                    (index, index),
                )
            connection.commit()
            connection.close()

            class Database:
                @contextlib.contextmanager
                def session(self):
                    session = sqlite3.connect(path)
                    session.row_factory = sqlite3.Row
                    try:
                        yield session
                    finally:
                        session.close()

            catalog = CvEisRepositoryAnalyzer(Database()).catalog()

        self.assertEqual(catalog["counts"]["ready"], 2)
        by_cv = {item["cv"]["name"]: item for item in catalog["materials"]}
        self.assertEqual(by_cv["16.1#-Fe30-CV.txt"]["eis"]["name"], "16.1#-Fe30-EIS.txt")
        self.assertEqual(by_cv["16.1#-Fe40-CV.txt"]["eis"]["name"], "16.1#-Fe40-EIS.txt")
        self.assertAlmostEqual(by_cv["16.1#-Fe30-CV.txt"]["rs"]["rs"], 1.15)
        self.assertAlmostEqual(by_cv["16.1#-Fe40-CV.txt"]["rs"]["rs"], 2.15)
        self.assertNotIn("D:\\", json.dumps(catalog, ensure_ascii=False))

    def test_read_only_database_adapter_enforces_query_only_connections(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "readonly.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE samples(value INTEGER)")
            connection.execute("INSERT INTO samples VALUES(7)")
            connection.commit()
            connection.close()

            database = ReadOnlyCvEisDatabase(path)
            with database.session() as session:
                self.assertEqual(session.execute("SELECT value FROM samples").fetchone()[0], 7)
                with self.assertRaises(sqlite3.OperationalError):
                    session.execute("INSERT INTO samples VALUES(8)")


if __name__ == "__main__":
    unittest.main()
