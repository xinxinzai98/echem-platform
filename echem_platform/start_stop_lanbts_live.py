"""Read-only snapshots of one unfinished BTS run; never part of the formal DB."""
from __future__ import annotations

import csv
import datetime as dt
import io
import itertools
import hashlib
import logging
import math
import ntpath
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .start_stop_collection import SSHWindowsTransport
from .start_stop_collection import RemoteFileError, RemoteRootError
from .start_stop_contracts import StartStopWorkspaceError
from .start_stop_lanbts import _run_id, lanbts_probe_script
from .start_stop_lanbts_import import (
    EXPECTED_COLUMNS, _decode_export_metadata, _profile_normalized_csv,
    classify_lanbts_stability, lanbts_record_export_script,
)
from .start_stop_live_preview import LivePreviewStateFile
from .start_stop_stability import _read_rows, _start_stop_cycles, _constant_summary, _downsample
from .start_stop_lanbts_paged import paged_export_script
from .start_stop_lanbts_live_cache import LiveRunCache

LOG = logging.getLogger(__name__)
INTERVAL_SECONDS = 300
MAX_CSV_BYTES = 128 * 1024 * 1024


def _absolute_timestamp(value):
    for pattern in ('%Y/%m/%d %H:%M:%S.%f', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return dt.datetime.strptime(value, pattern)
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def connect_records(content: bytes) -> tuple[list[dict], dict]:
    """Preserve acquisition order, joining step-local timers only at boundaries."""
    reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
    if tuple(reader.fieldnames or ()) != EXPECTED_COLUMNS:
        raise ValueError('蓝博快照数据表头不完整')
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPECTED_COLUMNS)
    writer.writeheader()
    previous_raw = previous_elapsed = None
    previous_step = None
    previous_absolute = ''
    offset = 0.0
    joined = duplicates = absolute_joins = 0
    seen = {}
    count = 0
    for row in reader:
        count += 1
        if count > 1_000_000 or None in row or any(value is None for value in row.values()):
            raise ValueError('蓝博快照记录超限或末行不完整')
        raw = float(row['time_s'])
        if not all(math.isfinite(float(row[key])) for key in ('time_s', 'potential_v', 'current_ma')) or raw < 0:
            raise ValueError('蓝博快照包含无效数值')
        step = (row['cycle_id'], row['step_id'])
        identity = (*step, row['record_id'])
        signature = tuple(row.values())
        if row['record_id']:
            if identity in seen:
                if seen[identity] != signature:
                    raise ValueError('相同记录编号内容冲突，请重新读取快照')
                duplicates += 1
                continue
            seen[identity] = signature
        if previous_raw is not None and raw < previous_raw - 1e-9:
            if step == previous_step:
                raise ValueError('同一工步内时间倒退，不能自动接续')
            offset = previous_elapsed
            before_abs, after_abs = _absolute_timestamp(previous_absolute), _absolute_timestamp(row['absolute_time'])
            if before_abs is not None and after_abs is not None and before_abs.tzinfo == after_abs.tzinfo and after_abs >= before_abs:
                offset = previous_elapsed + (after_abs - before_abs).total_seconds() - raw
                absolute_joins += 1
            joined += 1
        elapsed = offset + raw
        row['time_s'] = format(elapsed, '.17g')
        writer.writerow(row)
        previous_raw, previous_elapsed, previous_step = raw, elapsed, step
        previous_absolute = row['absolute_time']
    rows = _read_rows(output.getvalue().encode())
    return rows, {'source_records': count, 'complete_records': len(rows),
                  'duplicate_records_removed': duplicates, 'timer_resets_joined': joined,
                  'absolute_timestamp_joins': absolute_joins,
                  'time_policy': '连续原计时保留；工步回零优先按绝对时间接续，缺失时加上此前累计时间，不补造采样间隔'}


def _closed_live_cycles(rows, classification, completed):
    """LANBTS cycle_id can advance at charge start: pair actual ordered steps."""
    levels = classification.get('protocol_current_levels_ma') or []
    if len(levels) < 2:
        measured=sorted(row['current_ma'] for row in rows)
        levels=[measured[max(0,int(len(measured)*.05)-1)], measured[min(len(measured)-1,int(len(measured)*.95))]]
    levels = sorted(value for value in levels if isinstance(value, (float, int)) and math.isfinite(value))
    if len(levels) < 2 or levels[0] == levels[-1]:
        return [], None
    stress = levels[0] if levels[0] < 0 else max(levels, key=abs)
    recovery = max(levels, key=lambda value: (value != stress, value))
    steps = [list(group) for _, group in itertools.groupby(rows, key=lambda row:(row['cycle_id'], row['step_id']))]
    pending, result, number = None, [], 0
    for index, step in enumerate(steps):
        current = sorted(row['current_ma'] for row in step)[len(step)//2]
        if abs(current-stress) <= abs(current-recovery):
            pending = step
            continue
        if pending is None:
            continue
        number += 1
        if index == len(steps)-1 and not completed:
            return result, number
        canonical = [dict(row, cycle_id=number) for row in (*pending, *step)]
        # The pair is closed by the next SDK step or the instrument's completed
        # state above; preserve that evidence when passing only this pair.
        computed = _start_stop_cycles(canonical, {'metadata':classification}, last_cycle_closed=True)
        if computed:
            result.append(computed[0])
        pending = None
    return result, number+1 if pending is not None else None


def analyze_snapshot(content: bytes, metadata: dict, channel: dict, classification: dict) -> dict:
    if metadata.get('capture_kind') != 'live_read_only_snapshot' or metadata.get('downsample_stride') != 1:
        raise ValueError('不是完整只读快照')
    rows, connection = connect_records(content)
    if connection['source_records'] != metadata.get('exported_point_count') or metadata.get('record_count') != metadata.get('exported_point_count'):
        raise ValueError('快照行数与提取计数不一致')
    mode = classification['analysis_mode']
    completed = channel.get('status') == 'completed'
    cycles, pending_cycle = _closed_live_cycles(rows, classification, completed) if mode == 'start_stop' else ([],None)
    if cycles:
        baseline=cycles[0]['stress_endpoint_v']
        for cycle in cycles:
            cycle['negative_shift_mv']=(baseline-cycle['stress_endpoint_v'])*1000.0
    summary = ({'complete_cycles': len(cycles),
                'normal_cycles': sum(r['status'] == 'normal' for r in cycles),
                'abnormal_cycles': sum(r['status'] == 'abnormal' for r in cycles)}
               if mode == 'start_stop' else _constant_summary(rows))
    return {
        'run_id': channel['run_id'], 'channel': channel['channel'],
        'display_name': channel['display_name'], 'source_file': channel['data_file'],
        'captured_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'snapshot_sha256': str(metadata['snapshot_sha256']),
        'source_size_bytes': metadata['captured_source_size'],
        'analysis_mode': mode, 'protocol': classification['protocol'],
        'electrode_area_cm2': channel.get('configuration', {}).get('electrode_area_cm2'),
        'connection': connection, 'summary': summary,
        'in_progress': not completed, 'pending_cycle': pending_cycle,
        'latest': {key: rows[-1][key] for key in ('time_s', 'potential_v', 'current_ma', 'cycle_id', 'step_id')},
        'overview': [{key: row[key] for key in ('time_h', 'potential_v', 'current_ma', 'current_density_ma_cm2')}
                     for row in _downsample(rows, 2000)],
        'cycles': _downsample(cycles, 2000),
        'measurement_boundary': '仅本次 BTS 已落盘记录；按高负载→恢复工步顺序配对，不直接拼用仪器循环编号；不跨文件；未结束循环不计末端与异常；电压参照待确认',
    }


class LanbtsLivePreview:
    def __init__(self, monitor, state_path, scratch_dir, *, database=None,
                 transport_factory=SSHWindowsTransport):
        self.monitor, self.database = monitor, database
        self.state = LivePreviewStateFile(state_path)
        self.scratch_dir = Path(scratch_dir)
        self.transport_factory = transport_factory
        self.active = bool(database and monitor.active_probe)
        self._lock = threading.RLock()
        self._busy = False
        self._stop = threading.Event()
        self._thread = None
        self._worker = None
        self._attempts = {}
        if self.active and self.state.snapshot().get('last_status')=='running':
            self._write(last_status='interrupted',message='服务已重启；下一次抓取会从已验证的记录位置继续。')

    def snapshot(self):
        with self._lock:
            payload = self.state.snapshot()
        current = {row.get('run_id') for row in self.monitor.snapshot().get('channels', [])}
        payload['preview']['items'] = [item for item in payload['preview']['items'] if item.get('run_id') in current]
        payload['can_capture'] = self.active
        return payload

    def _write(self, **updates):
        with self._lock:
            payload = self.state.snapshot()
            payload.update(updates)
            payload['available'] = True
            self.state.write(payload)

    def request(self, run_id):
        if not self.active:
            raise StartStopWorkspaceError('只读入口仅可查看服务器已生成的测试中快照。', 403)
        current = self.monitor.snapshot().get('channels', [])
        channel = next((row for row in current if row.get('run_id') == run_id), None)
        if not isinstance(run_id, str) or not run_id or channel is None:
            raise StartStopWorkspaceError('该通道测试已切换，请刷新状态后重试。', 409)
        with self._lock:
            if self._busy or time.monotonic() - self._attempts.get(run_id, -math.inf) < 15:
                raise StartStopWorkspaceError('快照正在读取或刚刚更新，请稍后重试。', 409)
            self._busy = True
            self._attempts[run_id] = time.monotonic()
            self._write(last_status='running', current_run_id=run_id,
                        message=f'正在只读抓取通道 {channel["channel"]}，接续并计算完整记录…')
            self._worker = threading.Thread(target=self._capture, args=(dict(channel),), daemon=True, name='lanbts-live-capture')
            self._worker.start()
        return self.snapshot()

    def _capture(self, channel):
        try:
            config = self.monitor.machine_config
            transport = self.transport_factory(known_hosts_file=Path(config['known_hosts_file']).expanduser(), multiplex=False)
            raw = self._retry_read(lambda: transport.run_stdin_payload(config, lanbts_probe_script(config), timeout=30))
            match = next((row for row in raw.get('channels', []) if
                _run_id(config['id'], int(row['channel']), str(row.get('data_path', '')),
                        str(row.get('test_start_local', ''))) == channel['run_id']), None)
            if not match:
                raise ValueError('测试已切换，旧快照请求已取消')
            source = ntpath.normpath(str(match['data_path']))
            root = ntpath.normpath(config['data_root'])
            if ntpath.commonpath([source, root]).casefold() != root.casefold() or not source.lower().endswith('.bts'):
                raise ValueError('测试文件不在已配置的蓝博数据目录')
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.scratch_dir).free < 256 * 1024 * 1024:
                raise ValueError('预览临时空间不足')
            key=hashlib.sha256(channel['run_id'].encode()).hexdigest()
            cache=LiveRunCache(self.scratch_dir/'lanbts-live-records'/f'{key}.sqlite3')
            state=cache.state()
            target=state.get('target_slots',0) if state.get('end_slot',0)<state.get('target_slots',0) else 0
            reset=False;resets=0;newly_read=0
            with tempfile.TemporaryDirectory(prefix='lanbts-live-', dir=self.scratch_dir) as directory:
                for page in range(512):
                    if self._stop.is_set():raise RuntimeError('Live reader is stopping')
                    path=Path(directory)/f'page-{page}.csv'
                    start=0 if reset else state.get('end_slot',0)
                    script=paged_export_script(config,data_path=source,start_slot=start,
                        prefix_sha='' if reset else state.get('records_sha256',''),
                        context_sha='' if reset else state.get('context_sha256',''),target_slots=target)
                    meta=_decode_export_metadata(self._retry_read(lambda: transport.stream_stdin_script(config,script,path,timeout=90)))
                    if meta.get('reset_required'):
                        resets+=1
                        if resets>1:raise ValueError('Source changed repeatedly during capture')
                        reset=True;target=0;newly_read=0;path.unlink();continue
                    if path.stat().st_size>16*1024*1024:raise ValueError('Record page exceeds limit')
                    if not target:target=int(meta['total_slots'])
                    meta['target_slots']=target
                    state=cache.ingest(path,meta,reset=reset);reset=False;path.unlink()
                    newly_read+=int(meta['exported_point_count'])
                    self._write(last_status='running',current_run_id=channel['run_id'],
                        message=f'通道 {channel["channel"]}：已接续读取 {state["end_slot"]:,} / {target:,} 个记录位置，正在计算…',
                        progress={'completed':state['end_slot'],'total':target,'new_records':newly_read})
                    if state['end_slot']>=target:break
                    if state['end_slot']<=start:raise ValueError('Record cursor did not advance')
                else:raise ValueError('Too many record pages')
            item=cache.item(channel,newly_read=newly_read)
            current = {row.get('run_id') for row in self.monitor.snapshot().get('channels', [])}
            if channel['run_id'] not in current:
                raise ValueError('测试已切换，旧结果不会替代新测试')
            with self._lock:
                items = [old for old in self.snapshot()['preview']['items'] if old['run_id'] != channel['run_id']]
                items.append(item)
                self._write(last_status='completed', message='快照已接续并计算，不写入正式材料数据库。',
                            preview={'items': items[-8:], 'errors': []})
        except Exception:
            LOG.exception('LANBTS live snapshot failed for channel %s', channel.get('channel'))
            self._write(last_status='failed', message='本次快照读取或计算失败；可能文件正在改写、记录未落盘或超过预览上限。保留上次快照，请稍后重试。')
        finally:
            with self._lock:
                self._busy = False

    def start(self):
        if self.active and self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name='lanbts-live-scheduler')
            self._thread.start()

    def _retry_read(self, read):
        for attempt in range(3):
            try:return read()
            except (RemoteFileError, RemoteRootError):
                if attempt==2 or self._stop.wait(1):raise

    def _loop(self):
        while not self._stop.wait(5):
            try:
                enabled = bool(self.database.get_live_preview_config().get('enabled'))
                if self.snapshot().get('enabled') != enabled:
                    self._write(enabled=enabled)
                if not enabled or self._busy:
                    continue
                channels = self.monitor.snapshot().get('channels', [])
                previews = {item['run_id']: item for item in self.snapshot()['preview']['items']}
                for channel in sorted(channels,key=lambda row:(self._attempts.get(row.get('run_id'),-math.inf),row.get('data_size_bytes',0))):
                    run_id = channel.get('run_id')
                    needs_final_snapshot = channel.get('status') == 'completed' and previews.get(run_id, {}).get('in_progress')
                    if run_id and (channel.get('status') in {'charging', 'discharging'} or needs_final_snapshot) and time.monotonic() - self._attempts.get(run_id, -math.inf) >= INTERVAL_SECONDS:
                        self.request(run_id)
                        break
                current = {row.get('run_id') for row in channels}
                self._attempts = {key: value for key, value in self._attempts.items() if key in current}
            except Exception:
                LOG.exception('LANBTS live scheduler failed')

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
