"""Incremental, disposable SQLite cache for a single BTS run's measurements."""
from __future__ import annotations
import csv
import datetime as dt
import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .start_stop_lanbts import LanbtsMonitor
from .start_stop_lanbts_import import classify_lanbts_stability
from .start_stop_lanbts_paged import PAGED_COLUMNS
from .start_stop_stability import _downsample


class LiveRunCache:
    def __init__(self, path: Path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                  source_slot INTEGER PRIMARY KEY, record_id INTEGER NOT NULL UNIQUE,
                  time_s REAL NOT NULL, potential_v REAL NOT NULL, current_ma REAL NOT NULL,
                  cycle_id INTEGER NOT NULL,step_id INTEGER NOT NULL,step_name TEXT NOT NULL,
                  absolute_time TEXT NOT NULL,timestamp_ticks INTEGER NOT NULL,
                  voltage_raw_uv REAL NOT NULL,current_raw_ua REAL NOT NULL,temperature_c REAL);
                CREATE INDEX IF NOT EXISTS record_time ON records(time_s);
                CREATE INDEX IF NOT EXISTS record_step ON records(step_id,time_s);
                CREATE INDEX IF NOT EXISTS record_current ON records(current_ma);
                CREATE TABLE IF NOT EXISTS phases(
                  step_id INTEGER PRIMARY KEY,start_s REAL,end_s REAL,current_ma REAL,
                  endpoint_v REAL,minimum_time_s REAL,point_count INTEGER);
            ''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:
            db.close()

    def state(self):
        with self.connect() as db:
            row=db.execute('SELECT payload FROM meta WHERE id=1').fetchone()
            return json.loads(row[0]) if row else {'end_slot':0,'record_count':0}

    @staticmethod
    def _median(db, column, where='', args=(), *, upper=False):
        assert column in {'potential_v','current_ma'}
        clause=' WHERE '+where if where else ''
        count=db.execute('SELECT COUNT(*) FROM records'+clause,args).fetchone()[0]
        if not count:return None
        size=1 if count%2 or upper else 2
        offset=count//2 if upper else (count-1)//2
        values=db.execute(f'SELECT {column} FROM records'+clause+f' ORDER BY {column} LIMIT ? OFFSET ?',(*args,size,offset)).fetchall()
        return sum(r[0] for r in values)/len(values)

    def ingest(self, csv_path: Path, metadata: dict, *, reset=False):
        if metadata.get('capture_kind')!='indexed_read_only_snapshot' or metadata.get('reset_required'):
            raise ValueError('Not a verified indexed snapshot')
        start,end,total=(int(metadata[k]) for k in ('start_slot','end_slot','total_slots'))
        if not 0<=start<=end<=total:raise ValueError('Invalid snapshot range')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT payload FROM meta WHERE id=1').fetchone()
            state=json.loads(previous[0]) if previous else {'end_slot':0,'record_count':0}
            if reset:
                if start!=0:raise ValueError('Reset must start at zero')
                db.execute('DELETE FROM records');db.execute('DELETE FROM phases')
                state={'end_slot':0,'record_count':0}
            if start!=state['end_slot']:raise ValueError('Snapshot cursor changed')
            if start and (metadata['prefix_sha256']!=state['records_sha256'] or metadata['context_sha256']!=state['context_sha256']):
                raise ValueError('Verified prefix or calibration context changed')
            count=state['record_count'];changed=set();received=0;last_slot=start-1
            previous_time=state.get('last_source_time');last_time=state.get('last_time',0)
            offset=state.get('time_offset',0);previous_step=state.get('last_step');ticks_before=state.get('last_ticks')
            joined=state.get('timer_resets_joined',0)
            with Path(csv_path).open(encoding='utf-8-sig',newline='') as file:
                reader=csv.DictReader(file)
                if tuple(reader.fieldnames or ())!=PAGED_COLUMNS:raise ValueError('Unexpected indexed CSV schema')
                for row in reader:
                    if None in row or any(value is None for value in row.values()):raise ValueError('Truncated record')
                    slot=int(row['source_slot']);rid=int(row['record_id']);step=int(row['step_id']);cycle=int(row['cycle_id'])
                    raw=float(row['time_s']);v=float(row['potential_v']);current=float(row['current_ma']);ticks=int(row['timestamp_ticks'])
                    uv=float(row['voltage_raw_uv']);ua=float(row['current_raw_ua'])
                    if not all(math.isfinite(x) for x in (raw,v,current,uv,ua)) or raw<0:raise ValueError('Invalid numeric record')
                    if not last_slot<slot<end or slot<start or rid!=count+1 or step<=0:raise ValueError('Missing, duplicated or out-of-order record')
                    if previous_time is not None and raw<previous_time-1e-9:
                        if step==previous_step:raise ValueError('Time reversal inside a step')
                        offset=last_time
                        if ticks_before is not None and ticks>=ticks_before:
                            offset=last_time+(ticks-ticks_before)/10_000_000-raw
                        joined+=1
                    time_s=offset+raw
                    if previous_time is not None and time_s<last_time-1e-9:raise ValueError('Joined time reversed')
                    temperature=float(row['temperature_c']) if row['temperature_c'] else None
                    if temperature is not None and not math.isfinite(temperature):raise ValueError('Invalid temperature')
                    db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (slot,rid,time_s,v,current,cycle,step,LanbtsMonitor._state_label(row['step_name']),row['absolute_time'],ticks,uv,ua,temperature))
                    received+=1;count+=1;last_slot=slot;changed.add(step)
                    previous_time,last_time,previous_step,ticks_before=raw,time_s,step,ticks
            if received!=int(metadata['exported_point_count']) or count!=int(metadata['total_status_records']):
                raise ValueError('Snapshot record count mismatch')
            for step in changed:
                timing=db.execute('SELECT MIN(time_s),MAX(time_s),COUNT(*) FROM records WHERE step_id=?',(step,)).fetchone()
                minimum=db.execute('SELECT time_s FROM records WHERE step_id=? ORDER BY potential_v,source_slot LIMIT 1',(step,)).fetchone()[0]
                endpoint=self._median(db,'potential_v','step_id=? AND time_s>=?',(step,timing[1]-1-1e-9))
                current=self._median(db,'current_ma','step_id=?',(step,),upper=True)
                db.execute('INSERT OR REPLACE INTO phases VALUES(?,?,?,?,?,?,?)',
                           (step,timing[0],timing[1],current,endpoint,minimum-timing[0],timing[2]))
            state.update(metadata)
            state.update(record_count=count,last_source_time=previous_time,last_time=last_time,
                         time_offset=offset,last_step=previous_step,last_ticks=ticks_before,timer_resets_joined=joined)
            state.setdefault('target_slots',total)
            db.execute('INSERT OR REPLACE INTO meta VALUES(1,?)',(json.dumps(state,ensure_ascii=False),))
        return state

    @staticmethod
    def _profile(db):
        n=db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        def q(fraction):
            if not n:return 0.0
            position=(n-1)*fraction; low=int(position)
            rows=db.execute('SELECT current_ma FROM records ORDER BY current_ma LIMIT 2 OFFSET ?',(low,)).fetchall()
            return rows[0][0] if len(rows)==1 else rows[0][0]*(1-position+low)+rows[1][0]*(position-low)
        p05,p95=q(.05),q(.95);span=p95-p05;low=p05+.25*span;high=p05+.75*span
        low_count=high_count=transitions=0
        if span>0:
            low_count,high_count=db.execute('SELECT SUM(current_ma<=?),SUM(current_ma>=?) FROM records',(low,high)).fetchone()
            transitions=db.execute('''WITH selected AS (
                SELECT record_id,CASE WHEN current_ma<=? THEN 0 ELSE 1 END label FROM records WHERE current_ma<=? OR current_ma>=?
              ), changes AS (SELECT label,LAG(label) OVER(ORDER BY record_id) previous FROM selected)
              SELECT COALESCE(SUM(label!=previous),0) FROM changes''',(low,low,high)).fetchone()[0]
        return {'valid_point_count':n,'invalid_point_count':0,'time_decrease_count':0,
            'current_p05_ma':p05,'current_p95_ma':p95,'current_span_ma':span,
            'current_fluctuation_threshold_ma':max(5,.05*max(abs(p05),abs(p95),1)),
            'current_low_fraction':low_count/n if n else 0,'current_high_fraction':high_count/n if n else 0,
            'current_level_transition_count':transitions}

    def item(self, channel, *, newly_read=0):
        state=self.state();completed=channel.get('status')=='completed'
        with self.connect() as db:
            profile=self._profile(db)
            classification=classify_lanbts_stability(process_rows=state.get('process',[]),measured_profile=profile)
            mode=classification['analysis_mode'];cycles=[];pending_cycle=None
            if mode=='start_stop':
                levels=classification['protocol_current_levels_ma'] or [profile['current_p05_ma'],profile['current_p95_ma']]
                stress=levels[0] if levels[0]<0 else max(levels,key=abs)
                recovery=max(levels,key=lambda x:(x!=stress,x))
                phases=db.execute('SELECT * FROM phases ORDER BY step_id').fetchall();pending=None;number=0
                for index,phase in enumerate(phases):
                    if abs(phase['current_ma']-stress)<=abs(phase['current_ma']-recovery):pending=phase;continue
                    if pending is None:continue
                    number+=1
                    if index==len(phases)-1 and not completed:pending_cycle=number;break
                    cycles.append({'cycle':number,'time_h':phase['end_s']/3600,'stress_endpoint_v':pending['endpoint_v'],
                        'recovery_endpoint_v':phase['endpoint_v'],'minimum_time_s':pending['minimum_time_s'],
                        'status':'normal' if pending['minimum_time_s']>=15 else 'abnormal'})
                    pending=None
                if pending is not None and pending_cycle is None:pending_cycle=number+1
                if cycles:
                    baseline=cycles[0]['stress_endpoint_v']
                    for cycle in cycles:cycle['negative_shift_mv']=(baseline-cycle['stress_endpoint_v'])*1000
                summary={'complete_cycles':len(cycles),'normal_cycles':sum(r['status']=='normal' for r in cycles),
                         'abnormal_cycles':sum(r['status']=='abnormal' for r in cycles)}
            else:
                summary=self._constant(db)
            count=state['record_count'];limit=min(count,2000)
            ids=sorted({round(i*(count-1)/(limit-1))+1 for i in range(limit)}) if limit>1 else ([1] if limit else [])
            rows=db.execute('SELECT * FROM records WHERE record_id IN ('+','.join('?' for _ in ids)+') ORDER BY record_id',ids).fetchall() if ids else []
            area=channel.get('configuration',{}).get('electrode_area_cm2')
            overview=[{'time_h':r['time_s']/3600,'potential_v':r['potential_v'],'current_ma':r['current_ma'],
                       'current_density_ma_cm2':r['current_ma']/area if area else None} for r in rows]
            last=dict(db.execute('SELECT * FROM records ORDER BY record_id DESC LIMIT 1').fetchone()) if count else {}
        return {'run_id':channel['run_id'],'channel':channel['channel'],'display_name':channel['display_name'],
            'source_file':channel['data_file'],'captured_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'snapshot_sha256':state.get('snapshot_sha256',''),'records_sha256':state.get('records_sha256',''),
            'source_size_bytes':state.get('captured_source_size',0),'analysis_mode':mode,'protocol':classification['protocol'],
            'electrode_area_cm2':area,'connection':{'complete_records':count,'source_records':count,
                'record_slots':state['end_slot'],'pending_slots':max(0,state['total_slots']-state['end_slot']),
                'new_records_read':newly_read,'timer_resets_joined':state.get('timer_resets_joined',0),
                'duplicate_records_removed':0,'time_policy':'原计时连续保留；工步回零使用原始时间戳接续'},
            'summary':summary,'in_progress':not completed,'pending_cycle':pending_cycle,
            'latest':{k:last.get(k) for k in ('time_s','potential_v','current_ma','cycle_id','step_id')},
            'overview':overview,'cycles':_downsample(cycles,2000),
            'measurement_boundary':'同一次测试的完整已读取记录；只转换新增记录，旧记录验证摘要后复用；按实际工步配对；未闭合循环不计末端与异常；电压参照待确认'}

    def _constant(self,db):
        timing=db.execute('SELECT MIN(time_s),MAX(time_s),COUNT(*) FROM records').fetchone()
        if not timing[2]:return {'duration_h':0,'linear_drift_mv_per_h':None,'voltage_change_mv':None,'current_median_ma':None}
        start,end=timing[0],timing[1];duration=end-start;settle=min(300,duration*.05);threshold=start+settle
        n,x,y,first=db.execute('SELECT COUNT(*),AVG(time_s/3600.0),AVG(potential_v),MIN(time_s) FROM records WHERE time_s>=?',(threshold,)).fetchone()
        if n<2:
            threshold=start
            n,x,y,first=db.execute('SELECT COUNT(*),AVG(time_s/3600.0),AVG(potential_v),MIN(time_s) FROM records').fetchone()
        xy,xx=db.execute('SELECT SUM((time_s/3600.0-?)*(potential_v-?)),SUM((time_s/3600.0-?)*(time_s/3600.0-?)) FROM records WHERE time_s>=?',(x,y,x,x,threshold)).fetchone()
        window=min(300,max(60,duration*.02))
        begin=self._median(db,'potential_v','time_s>=? AND time_s<=?',(threshold,first+window))
        finish=self._median(db,'potential_v','time_s>=?',(max(threshold,end-window),))
        p05=db.execute('SELECT current_ma FROM records WHERE time_s>=? ORDER BY current_ma LIMIT 1 OFFSET ?',
                       (threshold,max(0,int(n*.05)-1))).fetchone()[0]
        p95=db.execute('SELECT current_ma FROM records WHERE time_s>=? ORDER BY current_ma LIMIT 1 OFFSET ?',
                       (threshold,min(n-1,int(n*.95)))).fetchone()[0]
        return {'duration_h':duration/3600,'settling_excluded_s':settle,'endpoint_window_s':window,
            'start_voltage_median_v':begin,'end_voltage_median_v':finish,'voltage_change_mv':(finish-begin)*1000,
            'linear_drift_mv_per_h':xy/xx*1000 if xx else None,
            'current_median_ma':self._median(db,'current_ma','time_s>=?',(threshold,)),
            'current_p05_ma':p05,'current_p95_ma':p95}
