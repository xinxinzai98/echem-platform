import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from echem_platform.start_stop_database import StartStopDatabase
from echem_platform.start_stop_retention import artifact_retention_plan


class RetentionPreviewTests(unittest.TestCase):
    def test_preview_protects_current_pins_and_shared_source_blobs_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); db=StartStopDatabase(root/'repository.sqlite3')
            raw=root/'raw.txt'; raw.write_bytes(b'raw-source')
            db.ingest_staged_file(raw,{'machine_id':'a','root_label':'r','remote_path':'D:/a.txt',
                                      'remote_relative_path':'a.txt','size':10,'last_write_ticks':1,'is_candidate':True})
            snapshot=db.freeze_snapshot()
            with mock.patch('echem_platform.start_stop_database._utc_now',return_value='2026-08-01T00:00:00+00:00'):
                for index,kind in enumerate(('render','scan','render','scan'),1):
                    out=root/f'g{index}';out.mkdir()
                    (out/'old.txt').write_bytes(b'old-only' if index==1 else b'shared')
                    (out/'raw-copy.txt').write_bytes(b'raw-source')
                    db.publish_artifacts(out,snapshot['id'],0,kind)
            with db.session() as connection:
                before=connection.execute('SELECT COUNT(*) FROM content_blobs').fetchone()[0]
                connection.execute('PRAGMA query_only=ON')
                plan=artifact_retention_plan(connection,keep_render=1,keep_scan=1,keep_days=0,
                                             now=dt.datetime(2026,9,7,tzinfo=dt.timezone.utc))
                self.assertEqual(plan['candidate_count'],2)
                self.assertEqual(plan['candidate_exclusive_blob_bytes'],len(b'old-only'))
                self.assertFalse(plan['direct_delete_allowed'])
                self.assertTrue(plan['read_only'])
                pinned=artifact_retention_plan(connection,keep_render=1,keep_scan=1,keep_days=0,pinned_ids=[1])
                self.assertEqual(pinned['candidate_count'],1)
                self.assertEqual(pinned['candidate_exclusive_blob_bytes'],0)
                with self.assertRaisesRegex(ValueError,'不存在'):
                    artifact_retention_plan(connection,pinned_ids=[9999])
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM content_blobs').fetchone()[0],before)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM artifact_generations').fetchone()[0],4)
