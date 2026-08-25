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
        with self.assertRaisesRegex(CvEisAnalysisError, "禁止再次"):
            analyze_pair(
                parse_cv_text(corrtest_cv(ir_applied=True)),
                parse_eis_text(corrtest_eis()),
            )

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
