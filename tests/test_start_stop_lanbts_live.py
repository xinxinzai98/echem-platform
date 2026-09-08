import copy
import io
import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from echem_platform.start_stop_lanbts_import import EXPECTED_COLUMNS, lanbts_record_export_script
from echem_platform.start_stop_lanbts_live import analyze_snapshot, connect_records, LanbtsLivePreview
from tests.test_start_stop_stability import start_stop_rows, csv_bytes


class LanbtsLiveTests(unittest.TestCase):
    def setUp(self):
        self.channel={'run_id':'run-a','channel':2,'display_name':'完整材料名称',
                      'data_file':'test.bts','status':'discharging','configuration':{}}
        self.classification={'analysis_mode':'start_stop','protocol_current_levels_ma':[-300,30], 'protocol':{}}

    def metadata(self, count):
        return {'capture_kind':'live_read_only_snapshot','downsample_stride':1,'record_count':count,
                'exported_point_count':count,'snapshot_sha256':'a'*64,'captured_source_size':1000}

    def test_open_tail_with_both_phases_is_not_a_completed_cycle(self):
        rows=start_stop_rows()
        live=analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)),self.channel,self.classification)
        completed=analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)),dict(self.channel,status='completed'),self.classification)
        self.assertEqual(live['summary']['complete_cycles'],1)
        self.assertEqual(completed['summary']['complete_cycles'],2)
        self.assertEqual(live['pending_cycle'],2)
        self.assertEqual(live['connection']['complete_records'],len(rows))
        self.assertEqual(live['cycles'][0],completed['cycles'][0])

    def test_refresh_replaces_same_run_and_does_not_double_count(self):
        rows=start_stop_rows()
        first=analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)),self.channel,self.classification)
        second=analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)),self.channel,self.classification)
        self.assertEqual(first['cycles'],second['cycles'])
        self.assertEqual(first['overview'],second['overview'])

    def test_sdk_counter_advancing_at_charge_start_cannot_mispair_steps(self):
        rows=start_stop_rows()
        expected=analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)),self.channel,self.classification)
        for row in rows:
            row['cycle_id']={1:1,2:2,3:2,4:3}[row['step_id']]
        actual=analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)),self.channel,self.classification)
        self.assertEqual(actual['cycles'],expected['cycles'])
        self.assertEqual(actual['pending_cycle'],2)
        finished=analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)),dict(self.channel,status='completed'),self.classification)
        self.assertEqual(finished['summary']['complete_cycles'],2)

    def test_step_timer_reset_is_joined_but_intra_step_reversal_is_rejected(self):
        rows=start_stop_rows()
        original,_=connect_records(csv_bytes(rows))
        offsets={}
        for row in rows:
            key=(row['cycle_id'],row['step_id'])
            offsets.setdefault(key,row['time_s'])
            row['time_s']-=offsets[key]
        connected,info=connect_records(csv_bytes(rows))
        self.assertGreater(info['timer_resets_joined'],0)
        self.assertTrue(all(a['time_s']<=b['time_s'] for a,b in zip(connected,connected[1:])))
        self.assertEqual([r['potential_v'] for r in original],[r['potential_v'] for r in connected])
        rows[3]['time_s']=-1
        with self.assertRaises(ValueError): connect_records(csv_bytes(rows))

    def test_row_count_mismatch_rejects_truncated_csv(self):
        rows=start_stop_rows()
        with self.assertRaisesRegex(ValueError,'行数'):
            analyze_snapshot(csv_bytes(rows),self.metadata(len(rows)+1),self.channel,self.classification)

    def test_duplicate_ids_are_deduplicated_only_when_content_identical(self):
        rows=start_stop_rows()
        rows.insert(1,copy.deepcopy(rows[0]))
        _,info=connect_records(csv_bytes(rows))
        self.assertEqual(info['duplicate_records_removed'],1)
        rows[1]['potential_v']-=.2
        with self.assertRaisesRegex(ValueError,'冲突'): connect_records(csv_bytes(rows))

    def test_cache_hides_previous_run_when_channel_starts_new_test(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=SimpleNamespace(active_probe=False,snapshot=lambda:{'channels':[self.channel]})
            preview=LanbtsLivePreview(monitor,Path(directory)/'cache.json',directory)
            preview._write(preview={'items':[{'run_id':'old','channel':2},{'run_id':'run-a','channel':2}]})
            self.assertEqual([row['run_id'] for row in preview.snapshot()['preview']['items']],['run-a'])
            self.assertFalse(preview.snapshot()['can_capture'])
            with self.assertRaises(Exception) as caught: preview.request('run-a')
            self.assertEqual(caught.exception.status,403)

    def test_remote_sdk_reads_verified_copy_not_the_active_source(self):
        config={'installation_root':r'D:\LANBTS','system_root':r'D:\LANBTS\LANBTSSystem'}
        script=lanbts_record_export_script(config,data_path=r'D:\LANBTS\Data\test.bts',
            expected_size=0,expected_ticks=0,area_cm2=None,live_snapshot=True)
        self.assertIn('[IO.FileAccess]::Read,[IO.FileShare]::ReadWrite',script)
        self.assertLess(script.index('$dataPath=$snapshotPath'),script.index('$reader.GetCycleData($dataPath)'))
        self.assertIn('$snapshotSha -eq $verifiedSha',script)
        self.assertIn('capture_kind=\'live_read_only_snapshot\'',script)
        self.assertIn('[IO.File]::Delete($snapshotPath)',script)
        self.assertNotIn('[IO.File]::Delete($sourceDataPath)',script)

    def test_unicode_source_paths_are_ascii_encoded_for_windows_stdin(self):
        import base64
        path=r'D:\LANBTS\Data\产线_启停.bts'
        script=lanbts_record_export_script({'installation_root':r'D:\LANBTS','system_root':r'D:\LANBTS\LANBTSSystem'},
            data_path=path,expected_size=0,expected_ticks=0,area_cm2=None,live_snapshot=True)
        self.assertNotIn(path,script)
        self.assertIn(base64.b64encode(path.encode()).decode(),script)

    def test_watchdog_is_spawned_only_after_ssh_input_is_consumed(self):
        from echem_platform.start_stop_collection import stdin_powershell_loader_script
        script=stdin_powershell_loader_script()
        self.assertLess(script.index('[Console]::In.ReadToEnd()'),script.index('Start-Process powershell.exe'))
        framed=stdin_powershell_loader_script(input_length=12000)
        self.assertNotIn('ReadToEnd',framed)
        self.assertIn('$expected=12000',framed)
        self.assertIn('truncated_script_frame',framed)
        self.assertIn('FromBase64String',framed)

    def test_lan_reader_cannot_trigger_capture_or_read_request_body(self):
        from tests.test_start_stop_lan_auth import HandlerHarness, VALID_AUTHORIZATION
        status,_,_=HandlerHarness().request('POST','/api/start-stop/lanbts/live',
            authorization=VALID_AUTHORIZATION,exploding_body=True)
        self.assertEqual(status,403)

    def test_manager_refresh_replaces_cache_and_failure_preserves_last_good(self):
        import base64
        from echem_platform.start_stop_lanbts import _run_id
        rows=start_stop_rows()
        config={'id':'machine','data_root':r'D:\LANBTS\Data','installation_root':r'D:\LANBTS',
            'system_root':r'D:\LANBTS\LANBTSSystem','known_hosts_file':'fixture-hosts','device_id':'123','box_id':'1'}
        raw={'channel':2,'data_path':r'D:\LANBTS\Data\test.bts','test_start_local':'2026-09-08'}
        self.channel['run_id']=_run_id('machine',2,raw['data_path'],raw['test_start_local'])
        metadata=self.metadata(len(rows)); metadata['process']=[]
        class Transport:
            fail=False
            def __init__(self,**kwargs): pass
            def run_stdin_payload(self,*args,**kwargs): return {'ok':True,'channels':[raw]}
            def stream_stdin_script(self,config,script,path,**kwargs):
                if self.fail: raise RuntimeError('fixture transport failure')
                path.write_bytes(csv_bytes(rows))
                return b'__START_STOP_LANBTS_META__'+base64.b64encode(__import__('json').dumps(metadata).encode())
        with tempfile.TemporaryDirectory() as directory:
            monitor=SimpleNamespace(active_probe=True,machine_config=config,snapshot=lambda:{'channels':[self.channel]})
            manager=LanbtsLivePreview(monitor,Path(directory)/'cache.json',directory,database=object(),transport_factory=Transport)
            manager._capture(self.channel)
            manager._capture(self.channel)
            before=manager.snapshot()['preview']['items']
            self.assertEqual(len(before),1)
            Transport.fail=True
            with self.assertLogs('echem_platform.start_stop_lanbts_live',level='ERROR'):
                manager._capture(self.channel)
            self.assertEqual(manager.snapshot()['preview']['items'],before)
            self.assertEqual(manager.snapshot()['last_status'],'failed')
