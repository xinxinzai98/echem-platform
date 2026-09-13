import csv,io,tempfile,unittest
from pathlib import Path
from echem_platform.start_stop_lanbts_live_cache import LiveRunCache
from echem_platform.start_stop_lanbts_paged import PAGED_COLUMNS
from echem_platform.start_stop_lanbts_live import analyze_snapshot
from echem_platform.start_stop_stability import _constant_summary,_read_rows
from tests.test_start_stop_stability import start_stop_rows,constant_rows,csv_bytes


def page_bytes(rows, offset=0):
    out=io.StringIO();writer=csv.DictWriter(out,fieldnames=PAGED_COLUMNS);writer.writeheader()
    for i,row in enumerate(rows,offset):
        writer.writerow({**row,'source_slot':i,'timestamp_ticks':int(float(row['time_s'])*10_000_000)+630000000000000000})
    return out.getvalue().encode()


def metadata(start,end,total):
    return {'capture_kind':'indexed_read_only_snapshot','start_slot':start,'end_slot':end,'total_slots':total,
        'exported_point_count':end-start,'total_status_records':end,'prefix_sha256':f'{start:064x}',
        'records_sha256':f'{end:064x}','context_sha256':'c'*64,'snapshot_sha256':'a'*64,
        'captured_source_size':1000,'process':[]}


class LiveCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cache=LiveRunCache(self.root/'cache.sqlite3')
        self.channel={'channel':2,'run_id':'fixture-run','display_name':'完整材料名','data_file':'fixture.bts',
                      'status':'discharging','configuration':{}}

    def tearDown(self): self.tmp.cleanup()

    def add(self,rows,start,end,total,meta=None):
        file=self.root/'page.csv';file.write_bytes(page_bytes(rows[start:end],start))
        return self.cache.ingest(file,meta or metadata(start,end,total))

    def test_split_mid_phase_matches_full_scientific_calculation(self):
        rows=start_stop_rows();n=len(rows)
        from tests.test_start_stop_lanbts_import import process
        meta=metadata(0,25,n);meta['process']=process(-300,30)
        self.add(rows,0,25,n,meta);self.assertEqual(self.cache.item(self.channel)['summary']['complete_cycles'],0)
        self.add(rows,25,57,n);self.add(rows,57,n,n)
        actual=self.cache.item(self.channel)
        oracle=analyze_snapshot(csv_bytes(rows),{'capture_kind':'live_read_only_snapshot','downsample_stride':1,
            'record_count':n,'exported_point_count':n,'snapshot_sha256':'a'*64,'captured_source_size':1000},self.channel,
            {'analysis_mode':'start_stop','protocol_current_levels_ma':[-300,30],'protocol':{}})
        self.assertEqual(actual['summary'],oracle['summary'])
        self.assertEqual(actual['cycles'],oracle['cycles'])
        self.assertEqual(actual['overview'],oracle['overview'])

    def test_unchanged_capture_and_restart_never_duplicate_records(self):
        rows=start_stop_rows();n=len(rows);self.add(rows,0,n,n)
        reopened=LiveRunCache(self.cache.path)
        self.assertEqual(reopened.state()['record_count'],n)
        self.add(rows,n,n,n)
        self.assertEqual(self.cache.item(self.channel)['connection']['complete_records'],n)

    def test_bad_prefix_or_context_preserves_committed_cache(self):
        rows=start_stop_rows();self.add(rows,0,40,80);before=self.cache.state()
        for key in ('prefix_sha256','context_sha256'):
            meta=metadata(40,80,80);meta[key]='d'*64
            with self.assertRaises(ValueError):self.add(rows,40,80,80,meta)
            self.assertEqual(before,self.cache.state())

    def test_truncated_page_rolls_back_all_inserted_records(self):
        rows=start_stop_rows();self.add(rows,0,40,80)
        file=self.root/'truncated.csv';file.write_bytes(page_bytes(rows[40:60],40))
        with self.assertRaises(ValueError):self.cache.ingest(file,metadata(40,80,80))
        self.assertEqual(self.cache.state()['record_count'],40)
        with self.cache.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0],40)

    def test_finished_transition_closes_last_cycle_without_new_rows(self):
        rows=start_stop_rows();self.add(rows,0,80,80)
        before=self.cache.item(self.channel);after=self.cache.item(dict(self.channel,status='completed'))
        self.assertEqual(before['summary']['complete_cycles'],1)
        self.assertEqual(after['summary']['complete_cycles'],2)
        self.assertEqual(before['cycles'][0],after['cycles'][0])

    def test_constant_summary_matches_existing_method(self):
        rows=constant_rows();n=len(rows);self.add(rows,0,20,n);self.add(rows,20,n,n)
        item=self.cache.item(self.channel);oracle=_constant_summary(_read_rows(csv_bytes(rows)))
        self.assertEqual(item['analysis_mode'],'constant_current')
        for key in ('duration_h','start_voltage_median_v','end_voltage_median_v','voltage_change_mv','linear_drift_mv_per_h','current_median_ma'):
            self.assertAlmostEqual(item['summary'][key],oracle[key],places=9)

    def test_sparse_source_slots_are_not_missing_measurement_records(self):
        rows=start_stop_rows();out=io.StringIO();writer=csv.DictWriter(out,fieldnames=PAGED_COLUMNS);writer.writeheader()
        for i,row in enumerate(rows):writer.writerow({**row,'source_slot':i+4,'timestamp_ticks':630000000000000000+i*10_000_000})
        file=self.root/'sparse.csv';file.write_text(out.getvalue())
        meta=metadata(0,84,84);meta.update(exported_point_count=80,total_status_records=80)
        self.cache.ingest(file,meta)
        self.assertEqual(self.cache.state()['record_count'],80)

    def test_verified_reset_replaces_derived_rows_instead_of_appending(self):
        rows=start_stop_rows();self.add(rows,0,80,80)
        file=self.root/'reset.csv';file.write_bytes(page_bytes(rows[:40]))
        meta=metadata(0,40,40);meta['context_sha256']='d'*64
        self.cache.ingest(file,meta,reset=True)
        self.assertEqual(self.cache.state()['record_count'],40)
        with self.cache.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0],40)

    def test_no_measurement_yet_is_an_explicit_empty_result(self):
        file=self.root/'empty.csv';file.write_bytes(page_bytes([]))
        self.cache.ingest(file,metadata(0,0,0))
        item=self.cache.item(self.channel)
        self.assertEqual(item['connection']['complete_records'],0)
        self.assertEqual(item['overview'],[])
