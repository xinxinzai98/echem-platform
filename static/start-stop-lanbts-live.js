/* The same plot renderer/zoom controls as formal LANBTS stability analysis. */
(() => {
  const dialog = document.querySelector('#lanbtsLiveDialog');
  if (!dialog) return;
  const status = document.querySelector('#lanbtsLiveStatus');
  const capture = document.querySelector('#captureLanbtsLive');
  let run = null, timer = null, requestId = 0, controller = null;
  const metrics = {
    overview:['全程电压','V','time_h','potential_v'], current:['实测电流','mA','time_h','current_ma'],
    current_density:['实测电流密度','mA/cm²','time_h','current_density_ma_cm2'],
    stress_endpoint:['高负载段末端电压','V','cycle','stress_endpoint_v'],
    recovery_endpoint:['低负载段末端电压','V','cycle','recovery_endpoint_v'],
    negative_shift:['高负载电压负移','mV','cycle','negative_shift_mv'],
    minimum_time:['高负载段最低点位置','s','cycle','minimum_time_s'],
  };
  class LiveChart extends StartStopStabilityExplorer {
    buildExportControls() {}
    updateExportControls() {}
    renderList() {}
    loadChart() {
      if (!this.item) return;
      const metric = this.elements.metric.value;
      const [label, unit, xKey, field] = metrics[metric];
      const rows = xKey === 'time_h' ? this.item.overview : this.item.cycles;
      this.chartView = null;
      this.lastPayload = {analysis_mode:this.item.analysis_mode, metric,
        metric_spec:{label,unit,x_key:xKey,x_label:xKey === 'cycle'?'累计循环编号':'累计时间 / h',description:xKey === 'cycle' ? '已结束循环，阶段最后 1 s 中位数；当前末段不参与判断' : '同一次测试累计时间；不补偿、不换参照'},
        measurement_boundary:{message:this.item.measurement_boundary},
        series:[{series_id:this.item.run_id,display_name:this.item.display_name,summary:this.item.summary,
          points:rows.map(row=>({x:row[xKey],y:row[field],status:row.status}))}]};
      this.renderChart(this.lastPayload);
    }
    show(item) {
      this.item = item;
      this.mode = item.analysis_mode;
      this.series = [{series_id:item.run_id}];
      this.selected = new Set([item.run_id]);
      for (const option of this.elements.metric.options) option.disabled = item.analysis_mode === 'constant_current' && !['overview','current','current_density'].includes(option.value);
      if (this.elements.metric.selectedOptions[0]?.disabled) this.elements.metric.value='overview';
      this.loadChart();
    }
    clear() {
      this.item=this.lastPayload=this.geometry=null;
      this.elements.chart.replaceChildren();this.elements.legend.replaceChildren();this.elements.summary.replaceChildren();
      this.elements.empty.hidden=false;
    }
  }
  const chart = new LiveChart(document.querySelector('#lanbtsLiveExplorer'), 'start_stop');
  function schedule() {
    StartStopClient.clearVisibleTimeout(timer);
    if (dialog.open) timer=StartStopClient.visibleTimeout(refresh,5000);
  }
  async function refresh() {
    const id=++requestId, selected=run?.run_id;
    controller?.abort();controller=new AbortController();
    try {
      const payload=await StartStopClient.request('/api/start-stop/lanbts/live',{signal:controller.signal});
      if (id!==requestId || selected!==run?.run_id || !dialog.open) return;
      capture.hidden=!payload.can_capture;
      capture.disabled=payload.last_status==='running';
      const item=payload.preview?.items?.find(row=>row.run_id===selected);
      if (item) {
        const stale=Date.now()-Date.parse(item.captured_at_utc)>600000;
        const failed=payload.current_run_id===selected && payload.last_status==='failed';
        status.textContent=`${stale||failed?'缓存快照 · ':''}${new Date(item.captured_at_utc).toLocaleString('zh-CN')} · ${item.connection.complete_records.toLocaleString()} 条完整记录 · ${item.in_progress?`末尾循环 ${item.pending_cycle} 待确认闭合`:'测试已结束'}${failed?' · 本次刷新失败，保留上次结果':''}`;
        if (chart.item?.captured_at_utc!==item.captured_at_utc || chart.item?.run_id!==item.run_id) chart.show(item);
      } else {
        chart.clear();
        chart.elements.empty.textContent=payload.last_status==='running'?'正在读取并接续计算…':payload.can_capture?'尚无快照，请点击“读取快照并计算”。':'尚无本次测试快照，请在服务器本机开启实时预览或读取一次。';
        status.textContent=payload.message || '本次测试尚未生成快照';
      }
      if(payload.last_status==='running' && payload.current_run_id===selected) status.textContent+=' · 正在读取新快照';
    } catch(error) {
      if(id===requestId && !controller.signal.aborted) status.textContent=`读取失败：${error.message}；现有曲线仅为缓存。`;
    } finally { if(id===requestId) schedule(); }
  }
  globalThis.openLanbtsLive = channel => {
    requestId++; controller?.abort(); run=channel; chart.clear(); chart.highlighted.clear(); chart.onlyHighlights=false;
    chart.elements.metric.value='overview'; capture.hidden=true;
    document.querySelector('#lanbtsLiveTitle').textContent=`通道 ${channel.channel} · ${channel.display_name}`;
    document.querySelector('#lanbtsLiveMeta').textContent=channel.data_file;
    if(!dialog.open) dialog.showModal();
    refresh();
  };
  capture.addEventListener('click',async()=>{
    const selected=run?.run_id;capture.disabled=true;
    try {
      await StartStopClient.request('/api/start-stop/lanbts/live',{method:'POST',body:JSON.stringify({run_id:selected})});
      if(selected===run?.run_id) await refresh();
    } catch(error) {status.textContent=error.message;capture.disabled=false;}
  });
  document.querySelector('#closeLanbtsLive').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{requestId++;controller?.abort();StartStopClient.clearVisibleTimeout(timer);});
})();
