import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_cv_eis import CvEisAnalysisError, CvEisRepositoryAnalyzer, SourceRecord, _pair_score, analyze_pair, parse_cv_text, parse_eis_text
from echem_platform.start_stop_cv_eis_review import read_reviews, select_eis
from echem_platform.start_stop_database import StartStopDatabase
from tests.test_start_stop_cv_eis import corrtest_cv, corrtest_eis


class CvEisReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = StartStopDatabase(self.root / "repository.sqlite3")
        self.analyzer = CvEisRepositoryAnalyzer(self.db)

    def ingest(self, name, content, directory="sample", timestamp="2026-09-01T00:00:00Z"):
        file = self.root / name
        file.write_bytes(content)
        return self.db.ingest_staged_file(file, {
            "machine_id": "lab", "root_label": "data", "remote_path": f"D:\\{directory}\\{name}",
            "repository_path": f"lab/data/{directory}/{name}", "size": len(content),
            "last_write_ticks": 123, "last_write_utc": timestamp, "is_candidate": False,
        })

    def test_pre_post_never_auto_pairs_and_raw_cv_remains_visible(self):
        cv = self.ingest("CV-pre.txt", corrtest_cv())
        eis = self.ingest("EIS-post.txt", corrtest_eis())
        record = self.analyzer.catalog()["materials"][0]
        self.assertEqual(record["status"], "pairing_required")
        self.assertIsNone(record["eis"])
        self.assertEqual(record["overpotentials"], [])
        self.assertTrue(self.analyzer.curve(record["analysis_id"])["points"])
        with self.assertRaisesRegex(CvEisAnalysisError, "阶段相容"):
            self.analyzer.review({"expected_revision": 0, "cv_source_version_id": cv["version_id"],
                                  "eis_source_version_id": eis["version_id"], "instrument_ir_applied": False})

    def test_unknown_ir_confirmation_is_version_bound_audited_and_conflict_checked(self):
        unknown = corrtest_cv().decode().splitlines()
        unknown[0] = "CSStudioFile,ID_CV,invalid"
        content = "\n".join(unknown).encode()
        cv = self.ingest("her-cv.txt", content)
        eis = self.ingest("her-eis.txt", corrtest_eis())
        record = self.analyzer.catalog()["materials"][0]
        self.assertEqual(record["status"], "ir_confirmation_required")
        self.assertEqual(record["overpotentials"], [])
        self.assertTrue(all(p["ir90_e_rhe_v"] is None for p in self.analyzer.curve(record["analysis_id"])["points"]))
        payload = {"expected_revision": 0, "cv_source_version_id": cv["version_id"],
                   "eis_source_version_id": eis["version_id"], "instrument_ir_applied": False}
        saved = self.analyzer.review(payload)
        self.assertGreater(saved["revision"], 0)
        record = self.analyzer.catalog()["materials"][0]
        self.assertEqual(record["status"], "ready")
        self.assertTrue(record["overpotentials"])
        self.assertEqual(record["pairing_status"], "reviewed")
        self.assertEqual(record["review"]["cv_sha256"], hashlib.sha256(content).hexdigest())
        with self.assertRaises(CvEisAnalysisError) as conflict:
            self.analyzer.review(payload)
        self.assertEqual(conflict.exception.status, 409)
        with self.db.session() as connection:
            self.assertEqual(read_reviews(connection)["revision"], saved["revision"])
            original = connection.execute("SELECT content FROM content_blobs WHERE sha256=?", (cv["sha256"],)).fetchone()[0]
            self.assertEqual(original, content)

    def test_ambiguous_pair_requires_selection_and_cannot_use_other_material(self):
        cv = self.ingest("cv.txt", corrtest_cv())
        eis1 = self.ingest("a-eis.txt", corrtest_eis())
        self.ingest("b-eis.txt", corrtest_eis(real_offset=1))
        other = self.ingest("eis.txt", corrtest_eis(), directory="other")
        record = self.analyzer.catalog()["materials"][0]
        self.assertEqual(record["status"], "pairing_required")
        payload = {"expected_revision": 0, "cv_source_version_id": cv["version_id"],
                   "eis_source_version_id": other["version_id"], "instrument_ir_applied": False}
        with self.assertRaises(CvEisAnalysisError):
            self.analyzer.review(payload)
        payload["eis_source_version_id"] = eis1["version_id"]
        self.analyzer.review(payload)
        self.assertEqual(self.analyzer.catalog()["materials"][0]["status"], "ready")

    def test_missing_or_old_time_needs_explicit_eis_review(self):
        cv = SourceRecord(1, 1, "lab/sample/her-cv.txt", 1, "a"*64, "2026-09-01T00:00:00Z", "cv", "her", 100)
        for time in ("", "2026-09-04T00:00:00Z"):
            eis = SourceRecord(2, 2, "lab/sample/her-eis.txt", 1, "b"*64, time, "eis", "her", 100)
            chosen, state, _ = select_eis(cv, [eis], _pair_score)
            self.assertIsNone(chosen)
            self.assertEqual(state, "pairing_required")
            self.assertEqual(select_eis(cv, [eis], _pair_score, reviewed_id=2)[1], "reviewed")

    def test_ir_three_states_never_treat_unknown_as_false(self):
        cv = parse_cv_text(corrtest_cv())
        eis = parse_eis_text(corrtest_eis())
        for value in (None, False, True):
            result = analyze_pair(dataclasses.replace(cv, instrument_ir_applied=value), eis)
            self.assertEqual(result["ir_correction_available"], value is False)
            self.assertEqual(bool(result["overpotentials"]), value is False)
            self.assertTrue(result["points"])

    def test_cannot_override_instrument_metadata(self):
        cv = self.ingest("her-cv.txt", corrtest_cv(ir_applied=True))
        eis = self.ingest("her-eis.txt", corrtest_eis())
        with self.assertRaisesRegex(CvEisAnalysisError, "不能.*覆盖"):
            self.analyzer.review({"expected_revision": 0, "cv_source_version_id": cv["version_id"],
                                  "eis_source_version_id": eis["version_id"], "instrument_ir_applied": False})

    def test_review_http_route_is_local_only_and_returns_actionable_conflicts(self):
        from tests.test_docker_start_stop_scope import FakeWorkspace, LiveServer
        cv = self.ingest("her-cv.txt", corrtest_cv())
        eis = self.ingest("her-eis.txt", corrtest_eis())
        payload = {"expected_revision": 0, "cv_source_version_id": cv["version_id"],
                   "eis_source_version_id": eis["version_id"], "instrument_ir_applied": False}
        workspace = FakeWorkspace(self.root)
        with LiveServer(workspace, cv_eis_analyzer=self.analyzer) as server:
            status, response, _ = server.request("PUT", "/api/start-stop/cv-eis/review", body=payload,
                                               origin="http://127.0.0.1:18787", content_type="application/json")
            self.assertEqual(status, 200)
            self.assertGreater(response["revision"], 0)
            status, response, _ = server.request("PUT", "/api/start-stop/cv-eis/review", body=payload,
                                               origin="http://127.0.0.1:18787", content_type="application/json")
            self.assertEqual(status, 409)
        with LiveServer(workspace, cv_eis_analyzer=self.analyzer, lan_read_only=True,
                        public_host="192.168.110.225") as server:
            status, _, _ = server.request("PUT", "/api/start-stop/cv-eis/review", body=b"not-json",
                                          host="192.168.110.225:18788")
            self.assertEqual(status, 403)
