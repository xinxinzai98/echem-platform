from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_database import (
    ReadOnlyStartStopSourceDatabase,
    StartStopDatabase,
)


class StabilityDatabaseTests(unittest.TestCase):
    def test_current_normalized_sources_preserve_parent_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "start-stop.sqlite3"
            database = StartStopDatabase(database_path)
            raw = root / "raw.bts"
            raw.write_bytes(b"raw-bts")
            raw_result = database.ingest_staged_file(
                raw,
                {
                    "machine_id": "lanbts",
                    "root_label": "data",
                    "remote_path": "D:\\LANBTS\\Data\\sample.bts",
                    "repository_path": "lanbts/data/sample.bts",
                    "size": raw.stat().st_size,
                    "last_write_ticks": 123,
                    "is_candidate": False,
                },
            )
            normalized = root / "normalized.csv"
            normalized.write_text(
                "time_s,potential_v,current_ma\n0,-1.5,-500\n",
                encoding="utf-8",
            )
            normalized_result = database.ingest_staged_file(
                normalized,
                {
                    "machine_id": "lanbts",
                    "root_label": "data",
                    "remote_path": "D:\\LANBTS\\Data\\sample.bts::normalized-stability-v1",
                    "repository_path": "lanbts/data/sample.bts.normalized.csv",
                    "size": normalized.stat().st_size,
                    "last_write_ticks": 123,
                    "is_candidate": True,
                    "candidate_kind": "lanbts_constant_current",
                    "analysis_mode": "constant_current",
                    "parent_source_version_id": raw_result["version_id"],
                    "parent_sha256": raw_result["sha256"],
                    "source_file": "sample.bts",
                    "material_name": "NiMo 恒流",
                },
            )

            current = database.current_source_version(
                "lanbts",
                "data",
                "D:\\LANBTS\\Data\\sample.bts::normalized-stability-v1",
            )
            sources = database.stability_sources()
            readonly_sources = ReadOnlyStartStopSourceDatabase(
                database_path
            ).stability_sources()

        self.assertIsNotNone(current)
        self.assertEqual(current["id"], normalized_result["version_id"])
        self.assertEqual(current["candidate_kind"], "lanbts_constant_current")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["metadata"]["material_name"], "NiMo 恒流")
        self.assertEqual(sources[0]["metadata"]["parent_sha256"], raw_result["sha256"])
        self.assertEqual(readonly_sources, sources)


if __name__ == "__main__":
    unittest.main()
