const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/start-stop-stability.js'), 'utf8')
  .replace(/initializeStabilityAnalysis\(\);\s*$/, 'globalThis.Explorer = StabilityExplorer;');
const pending = [];
const context = {URLSearchParams, AbortController, DOMException, setTimeout, clearTimeout, fetch: (url, options) => new Promise(resolve => {
  // Deliberately ignore abort: stale responses must never change the chart.
  pending.push({url, options, resolve});
})};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root, 'static/start-stop-client.js'), 'utf8'), context);
vm.runInContext(source, context);
const rendered = [];
const emptyNode = () => ({replaceChildren() {this.cleared = true;}});
const explorer = Object.create(context.Explorer.prototype);
Object.assign(explorer, {selected:new Set(['A']),series:[{series_id:'A'}],mode:'start_stop',loading:false,
  elements:{metric:{value:'stress_endpoint'},empty:{},chart:emptyNode(),legend:emptyNode(),summary:emptyNode()},
  renderChart: payload => rendered.push(payload)});
function respond(index, id) {
  pending[index].resolve({ok:true,headers:{get:()=> 'application/json'},json:async()=>({series:[{series_id:id}]})});
}
(async()=>{
  const a=explorer.loadChart();
  explorer.selected=new Set(['B']); explorer.elements.metric.value='current';
  const b=explorer.loadChart();
  explorer.selected=new Set(['C']); explorer.elements.metric.value='overview';
  const c=explorer.loadChart();
  assert.equal(pending.length,3);
  respond(2,'C'); await c;
  respond(0,'A'); await a;
  respond(1,'B'); await b;
  assert.deepEqual(rendered.map(x=>x.series[0].series_id),['C']);
  assert.equal(explorer.loading,false);
  assert.match(pending[2].url,/metric=overview/);
  explorer.selected=new Set(['A']); const old=explorer.loadChart();
  explorer.selected.clear(); await explorer.loadChart();
  respond(3,'A'); await old;
  assert.equal(explorer.lastPayload,null);
  assert.equal(rendered.length,1);
  assert.equal(explorer.elements.legend.cleared,true);

  const main = fs.readFileSync(path.join(root,'static/start-stop.js'),'utf8');
  const selection = main.slice(main.indexOf('function selectIncludedSeriesForActiveWorkStep('),main.indexOf('\nfunction seriesStyle('));
  const state={selectedSeries:new Set(),highlightedSeries:new Set(),activeWorkStepKey:'same',
    materials:Array.from({length:40},(_,i)=>({key:'k'+i,include_in_summary_atlas:true,favorite:i===0,latest_source_modified_at:new Date(2026,0,i+1).toISOString()})),
    series:Array.from({length:40},(_,i)=>({series_id:'s'+i,material_relative_path:'k'+i,work_step_key:'same'}))};
  const selectContext={state,DEFAULT_CHART_SELECTION:16,MAX_CHART_SELECTION:64,enforceSelectionLimit(){}};
  vm.createContext(selectContext); vm.runInContext(selection,selectContext);
  selectContext.selectIncludedSeriesForActiveWorkStep();
  assert.equal(state.selectedSeries.size,16);
  assert.equal([...state.selectedSeries][0],'s0');
  assert.equal([...state.selectedSeries][1],'s39');
  selectContext.selectIncludedSeriesForActiveWorkStep(64);
  assert.equal(state.selectedSeries.size,40);
  const statusFunction=main.slice(main.indexOf('function updateStatusView()'),main.indexOf('\nfunction materialForSeries('));
  const statusContext={state:{status:{available:true,analysis_ready:true,export_ready:false}},
    elements:{sidebarStartStopState:{}},document:{body:{classList:{toggle(){}}}},
    applyDeploymentProfile(){},isLanReadOnly:()=>false,setNotice(message=''){statusContext.notice=message;}};
  vm.createContext(statusContext); vm.runInContext(statusFunction,statusContext);
  statusContext.updateStatusView();
  assert.equal(statusContext.elements.sidebarStartStopState.textContent,'平台分析已就绪');
  assert.equal(statusContext.notice,'');
  statusContext.state.status.configuration_stale=true;
  statusContext.state.status.analysis_ready=false;
  statusContext.isLanReadOnly=()=>true;
  statusContext.updateStatusView();
  assert.match(statusContext.notice,/材料配置已修改/);
  const rawPending = [];
  const rawState = {chartRequestId:0, selected:['A'], activeWorkStepKey:'same', livePreviewItems:new Map(),
    formalSeries:[], metric:'cathodic', xAxis:'cycle', mode:'raw'};
  const rawContext = {state:rawState, AbortController, URLSearchParams,
    elements:{chartTooltip:{},chartEmpty:{}}, resetChartZoom(){}, activeSeriesIds:()=>rawState.selected,
    updateHighlightExportControls(){}, renderChart(){}, isAnomalyMode:()=>false,
    emptyLiveChartPayload:()=>({series:[],work_step_key:'same'}),mergeLiveComparisonChart:payload=>payload,
    request:(url,options)=>new Promise(resolve=>rawPending.push({url,options,resolve}))};
  vm.createContext(rawContext);
  vm.runInContext(main.slice(main.indexOf('async function loadChart()'),main.indexOf('\nfunction chartSeriesToShow()')), rawContext);
  const rawA=rawContext.loadChart(); rawState.selected=['B'];
  const rawB=rawContext.loadChart();
  assert.equal(rawPending[0].options.signal.aborted,true);
  rawPending[1].resolve({series:[{series_id:'B'}],work_step_key:'same'}); await rawB;
  rawPending[0].resolve({series:[{series_id:'A'}],work_step_key:'same'}); await rawA;
  assert.equal(rawState.chartData.series[0].series_id,'B');
  const rawOld=rawContext.loadChart(); rawState.selected=[];
  await rawContext.loadChart();
  assert.equal(rawState.loadingChart,false);
  assert.equal(rawState.chartData,null);
  assert.equal(rawPending[2].options.signal.aborted,true);
  rawPending[2].resolve({series:[{series_id:'B'}],work_step_key:'same'}); await rawOld;
  assert.equal(rawState.chartData,null);
  const configSource=fs.readFileSync(path.join(root,'static/start-stop-config.js'),'utf8');
  const jobContext={configState:{status:{},jobStartError:''},
    configElements:{renderDataMode:{value:'raw'},renderMaterialScope:{value:'updated'}},
    configCapabilities:()=>({canRender:true}),
    configRequest:async()=>{throw new Error('空间不足，请稍后重试');},
    setConfigNotice(message){jobContext.notice=message;},
    loadConfigWorkspace:async()=>{jobContext.notice=jobContext.configState.jobStartError || '旧任务已完成';}};
  vm.createContext(jobContext);
  vm.runInContext(configSource.slice(configSource.indexOf('async function startSavedConfigJob('),
    configSource.indexOf('\nfunction defaultUploadGroupName(')),jobContext);
  await jobContext.startSavedConfigJob('render');
  assert.equal(jobContext.notice,'空间不足，请稍后重试');
  console.log('PASS: latest A/B/C response, clear while loading, favorite/recency default 16, explicit expanded selection');
})().catch(error=>{console.error(error);process.exitCode=1;});
