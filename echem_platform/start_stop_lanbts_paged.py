"""Vendor-calibrated indexed BTS reader, with verified incremental prefixes."""
from __future__ import annotations

from .start_stop_lanbts_import import EXPECTED_COLUMNS, lanbts_record_export_script, META_PREFIX

READER_VERSION = 'lanbts-indexed/1'
CHUNK_SLOTS = 65536
PAGED_COLUMNS = (*EXPECTED_COLUMNS, 'source_slot', 'timestamp_ticks')

PAGED_CS = r'''
using System;
using System.Globalization;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;

public class StartStopPageResult {
    public int total_slots, start_slot, end_slot;
    public long total_status_records, exported_point_count;
    public string prefix_sha256, records_sha256, context_sha256;
    public bool reset_required;
}
public static class StartStopPageReader {
    static readonly CultureInfo C=CultureInfo.InvariantCulture;
    static readonly Dictionary<Type,Dictionary<string,PropertyInfo>> Properties=new Dictionary<Type,Dictionary<string,PropertyInfo>>();
    static object V(object o,string name) {
        Dictionary<string,PropertyInfo> p;
        if(!Properties.TryGetValue(o.GetType(),out p)) {
            p=new Dictionary<string,PropertyInfo>();
            foreach(var field in o.GetType().GetProperties()) p[field.Name]=field;
            Properties[o.GetType()]=p;
        }
        PropertyInfo getter;
        return p.TryGetValue(name,out getter)?getter.GetValue(o,null):null;
    }
    static string T(object o) {return Convert.ToString(o,C)??"";}
    static string Hex(byte[] b) { return BitConverter.ToString(b).Replace("-","").ToLowerInvariant(); }
    static string Q(string s) { return "\""+(s??"").Replace("\"","\"\"")+"\""; }
    static double LegacyRaw(float value,float factor) {
        // Match the legacy SDK's Single (G7) numeric string before converting
        // microvolts/microamps; do not substitute a guessed ADC calibration.
        return Double.Parse((value*factor).ToString("G7",C),C);
    }
    static string F(double v) {
        if(Double.IsNaN(v)||Double.IsInfinity(v)) throw new InvalidDataException("invalid_measurement");
        return v.ToString("R",C);
    }
    public static StartStopPageResult Export(object cache,StreamWriter writer,int start,int limit,int target,
        string expectedPrefix,string expectedContext,string salt,double area) {
        FieldInfo f=cache.GetType().GetField("_stream",BindingFlags.NonPublic|BindingFlags.Instance);
        if(f==null || ((Stream)f.GetValue(cache)).CanWrite) throw new InvalidDataException("reader_not_read_only");
        var d=V(cache,"TestData");
        string context=String.Join("|",new string[]{salt,T(V(d,"Key")),T(V(d,"DataVersion")),T(V(d,"BtsVersion")),
            T(V(d,"DeviceVersion")),T(V(d,"MaxU")),T(V(d,"MaxI")),T(V(d,"Units")),
            T(V(d,"UnitType")),((DateTime)V(d,"StartTime")).Ticks.ToString(C),T(V(d,"RecordTimestampRoundingOffsetRaw10Ms")),T(V(d,"LithiumState"))});
        string contextHash;
        using(var h=SHA256.Create()) contextHash=Hex(h.ComputeHash(Encoding.UTF8.GetBytes(context)));
        int total=(int)V(cache,"TotalCount");
        var result=new StartStopPageResult{total_slots=total,start_slot=start,context_sha256=contextHash};
        if(start<0 || start>total || (start>0 && contextHash!=expectedContext)) {result.reset_required=true;return result;}
        int end=Math.Min(total,Math.Min(start+limit,target>0?target:total));
        result.end_slot=end;
        long status=0,cycle=0,step=0;
        string mode="";
        int starts=0;
        MethodInfo getRecord=cache.GetType().GetMethod("GetRecord",new Type[]{typeof(int)});
        using(var prefix=SHA256.Create()) using(var full=SHA256.Create()) {
            for(int i=0;i<end;i++) {
                var r=getRecord.Invoke(cache,new object[]{i}); byte[] raw=(byte[])V(r,"Buffer");
                if(raw==null||raw.Length!=32) throw new InvalidDataException("unsupported_record_size");
                full.TransformBlock(raw,0,raw.Length,raw,0);
                if(i<start) prefix.TransformBlock(raw,0,raw.Length,raw,0);
                if(i==start && start>0) {
                    prefix.TransformFinalBlock(new byte[0],0,0);result.prefix_sha256=Hex(prefix.Hash);
                    if(result.prefix_sha256!=expectedPrefix) {result.reset_required=true;return result;}
                }
                int type=(int)V(r,"TypeCode");
                if(type==0) {if(++starts>1) throw new InvalidDataException("multiple_test_starts");}
                else if(type==1) cycle=Convert.ToInt64(V(r,"Loop"));
                else if(type==2) {step++; mode=T(V(r,"Mode"));}
                else if(type==3) {
                    status++;
                    object timestamp=V(r,"Timestamp"),span=V(r,"Span");
                    if(i<start) continue;
                    object voltage=V(r,"Voltage"),current=V(r,"Electricity"),temperature=V(r,"Temperature");
                    if(step==0 || timestamp==null || span==null || voltage==null || current==null)
                        throw new InvalidDataException("incomplete_status_record");
                    long ticks=((TimeSpan)span).Ticks;
                    if(ticks<0) throw new InvalidDataException("negative_record_time");
                    double seconds=new TimeSpan(ticks-ticks%1000000L).TotalSeconds;
                    // SDK exposes mV as an intermediate Single, then microvolts.
                    double uv=LegacyRaw((float)voltage*1000f,1000f), ua=LegacyRaw((float)current,1000f);
                    writer.WriteLine(String.Join(",",new string[]{F(seconds),F(uv/1000000.0),F(ua/1000.0),
                        Double.IsNaN(area)?"":F(ua/1000.0/area),cycle.ToString(C),step.ToString(C),Q(mode),
                        Q(((DateTime)timestamp).ToString("yyyy/MM/dd HH:mm:ss",C)),status.ToString(C),F(uv),F(ua),
                        temperature==null?"":F((float)temperature),i.ToString(C),((DateTime)timestamp).Ticks.ToString(C)}));
                    result.exported_point_count++;
                } else if(type!=4) throw new InvalidDataException("unknown_record_type");
            }
            if(end==start) {
                prefix.TransformFinalBlock(new byte[0],0,0);result.prefix_sha256=Hex(prefix.Hash);
                if(start>0 && result.prefix_sha256!=expectedPrefix) {result.reset_required=true;return result;}
            }
            full.TransformFinalBlock(new byte[0],0,0);result.records_sha256=Hex(full.Hash);
        }
        result.total_status_records=status;
        return result;
    }
}
'''


def paged_export_script(config, *, data_path, start_slot=0, prefix_sha='', context_sha='', target_slots=0, limit=CHUNK_SLOTS):
    import base64
    import ntpath
    import hashlib
    config={**config, 'installation_root':ntpath.normpath(config['installation_root']),
            'system_root':ntpath.normpath(config['system_root'])}
    data_path=ntpath.normpath(data_path)
    for digest in (prefix_sha, context_sha):
        if digest and (len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid prefix digest')
    if not 0<=int(start_slot)<=100_000_000 or not 0<=int(target_slots)<=100_000_000 or not 1<=int(limit)<=CHUNK_SLOTS:
        raise ValueError('Invalid record range')
    base=lanbts_record_export_script(config,data_path=data_path,expected_size=0,expected_ticks=0,area_cm2=None,live_snapshot=True)
    first=base.index('$reader=New-Object ReadDataFile.ReadBtsFile')
    last=base.index('\n}finally{\n  if($reader')
    encoded=base64.b64encode(PAGED_CS.encode()).decode()
    body=rf'''
$reader=New-Object ReadDataFile.ReadBtsFile
$process=@($reader.GetProcessData($dataPath) | Select-Object stepnum,stepname,finishconditon1,finishconditon2,jump,savecondition)
$cache=New-Object LanBts.DDA.Adapter.RecordCache -ArgumentList $dataPath
try{{
  # The data stream is read-only and the file is our immutable copy; release
  # the shared setup mutex before doing numerical work, so status reads run.
  $readMutex.ReleaseMutex();$lockAcquired=$false
  $code=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
  Add-Type -TypeDefinition $code -ReferencedAssemblies @('System.dll','System.Core.dll')
  $salt='{READER_VERSION}:{hashlib.sha256(PAGED_CS.encode()).hexdigest()}'+(Get-FileHash $coreTarget -Algorithm SHA256).Hash+(Get-FileHash (Join-Path $install 'LanbtsDDA.exe') -Algorithm SHA256).Hash
  $exportPath=Join-Path $liveDirectory 'page.csv'
  $writer=New-Object IO.StreamWriter($exportPath,$false,(New-Object Text.UTF8Encoding($false)))
  $watch=[Diagnostics.Stopwatch]::StartNew()
  try{{
    $writer.WriteLine('{','.join(PAGED_COLUMNS)}')
    $result=[StartStopPageReader]::Export($cache,$writer,{int(start_slot)},{int(limit)},{int(target_slots)},'{prefix_sha}','{context_sha}',$salt,[double]::NaN)
    $writer.Flush()
  }}finally{{$writer.Dispose()}}
  $out=[IO.File]::OpenRead($exportPath)
  try{{$out.CopyTo([Console]::OpenStandardOutput())}}finally{{$out.Dispose()}}
  [IO.File]::Delete($exportPath)
  $payload=@{{capture_kind='indexed_read_only_snapshot';reader_version='{READER_VERSION}';snapshot_sha256=$snapshotSha;
    captured_source_size=$captureSize;captured_source_ticks=$captureTicks;captured_source_modified_utc=$captureModified;
    total_slots=$result.total_slots;start_slot=$result.start_slot;end_slot=$result.end_slot;exported_point_count=$result.exported_point_count;
    total_status_records=$result.total_status_records;prefix_sha256=$result.prefix_sha256;records_sha256=$result.records_sha256;
    context_sha256=$result.context_sha256;reset_required=$result.reset_required;elapsed_ms=$watch.ElapsedMilliseconds;process=@($process)}}
  $json=$payload|ConvertTo-Json -Compress -Depth 8
  [Console]::Error.WriteLine('{META_PREFIX.decode()}'+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
}}finally{{$cache.Dispose();if($exportPath -and [IO.File]::Exists($exportPath)){{[IO.File]::Delete($exportPath)}}}}
'''
    return base[:first]+body+base[last:]
