import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop import StartStopWorkspace
from echem_platform.start_stop_database import StartStopDatabase, ReadOnlyStartStopSourceDatabase
from echem_platform.start_stop_runtime_status import RUNTIME_STATUS_NAME, RuntimeSafetyPublisher, read_runtime_safety
from start_stop_service import _sanitize_safety_status


class SharedRuntimeStateTests(unittest.TestCase):
    def test_readonly_material_preferences_and_provenance_use_real_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            db=StartStopDatabase(root/'repository.sqlite3')
            db.save_start_stop_config(dataset_fingerprint='fixture',expected_revision=0,materials=[{
                'key':'sample','plot_name':'完整材料名','favorite':True,'include_in_summary_atlas':True,'notes':'备注','fingerprint':'f'
            }])
            reader=ReadOnlyStartStopSourceDatabase(db.path)
            self.assertEqual(reader.get_start_stop_config(),db.get_start_stop_config())
            self.assertEqual(reader.current_analysis_run(),db.current_analysis_run())
            self.assertEqual(reader.repository_status()['config'],db.repository_status()['config'])
            current=root/'published'/'current'; current.mkdir(parents=True)
            (current/'material_config_snapshot.json').write_text(json.dumps({'dataset_fingerprint':'fixture','materials':[{'key':'sample','auto_name':'sample','fingerprint':'f'}]}))
            ephemeral=StartStopDatabase(root/'lan-temporary.sqlite3')
            workspace=StartStopWorkspace(ephemeral,current,source_database=reader,repository_mode=True)
            materials=workspace.materials()
            self.assertTrue(materials['materials'][0]['favorite'])
            self.assertEqual(materials['materials'][0]['plot_name'],'完整材料名')
            self.assertEqual(materials['revision'],1)
            self.assertEqual(materials['updated_utc'],db.get_start_stop_config()['updated_utc'])

    def test_projection_is_sanitized_atomic_and_expires_without_zero_backup_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            current=root/'current'; current.mkdir()
            provider=lambda:_sanitize_safety_status({
                'storage':{'free_bytes':1000000,'path':'/private/database'},
                'backup':{'configured':True,'available':True,'backup_count':3,'secret':'hidden'},
                'provenance':{'state':'sealed'},
            },provenance={'state':'sealed'})
            publisher=RuntimeSafetyPublisher(root/RUNTIME_STATUS_NAME,provider)
            publisher.publish()
            serialized=(root/RUNTIME_STATUS_NAME).read_text()
            self.assertNotIn('/private',serialized)
            self.assertNotIn('hidden',serialized)
            self.assertEqual(read_runtime_safety(root/RUNTIME_STATUS_NAME)['backup']['backup_count'],3)
            self.assertIsNone(read_runtime_safety(root/RUNTIME_STATUS_NAME,now=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=125)))
            self.assertEqual(list(root.glob('*.tmp')),[])
            db=StartStopDatabase(root/'real.sqlite3')
            workspace=StartStopWorkspace(StartStopDatabase(root/'local.sqlite3'),current,source_database=ReadOnlyStartStopSourceDatabase(db.path),repository_mode=True)
            self.assertEqual(workspace._safety_status()['backup']['backup_count'],3)
            (root/RUNTIME_STATUS_NAME).unlink()
            self.assertEqual(workspace._safety_status()['backup'],{})

    def test_malformed_projection_is_unknown_not_an_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/RUNTIME_STATUS_NAME
            for value in ([], None, {"updated_utc": []}, {"updated_utc": 12}):
                path.write_text(json.dumps(value))
                self.assertIsNone(read_runtime_safety(path))
