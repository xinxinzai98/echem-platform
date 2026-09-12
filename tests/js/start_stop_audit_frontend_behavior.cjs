// Synthetic inputs only. Execute the real page functions without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');

function node(tag = 'div') {
  const classes = new Set();
  return {
    tag, childNodes: [], attrs: {}, dataset: {}, value: '', textContent: '',
    clientWidth: 900, clientHeight: 600, style: {setProperty() {}},
    classList: {
      toggle(key, enabled) { if (enabled) classes.add(key); else classes.delete(key); },
      contains: key => classes.has(key), add: key => classes.add(key), remove: key => classes.delete(key),
    },
    setAttribute(key, value) { this.attrs[key] = String(value); },
    getAttribute(key) { return this.attrs[key]; },
    append(...children) { this.childNodes.push(...children); },
    replaceChildren(...children) { this.childNodes = children; },
    addEventListener() {}, removeEventListener() {}, focus() {},
    querySelector: () => node(),
    getBoundingClientRect: () => ({left: 0, top: 0, width: 900, height: 600}),
  };
}

function page(filename, bootstrap, expose, extra = {}) {
  const nodes = new Map();
  const context = {
    AbortController, URLSearchParams,
    window: {location: {search: '?view=live'}, setTimeout() {}, requestAnimationFrame() {}},
    document: {
      body: node('body'), querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, node());
        return nodes.get(selector);
      },
      querySelectorAll: () => [], createElement: node,
      createElementNS: (_namespace, tag) => node(tag),
      createDocumentFragment: () => node('fragment'), addEventListener() {},
    },
    ...extra,
  };
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, 'static', filename), 'utf8');
  vm.runInContext(source.replace(bootstrap, '') + '\n' + expose, context);
  return context;
}

function descendants(parent) {
  return [parent, ...parent.childNodes.flatMap(descendants)];
}

function checkStabilityNumbers() {
  const context = page('start-stop-stability.js', /initializeStabilityAnalysis\(\);\s*$/,
    'globalThis.Explorer = StabilityExplorer;');
  for (const value of [0, 10, 20, 100, 1000, -10]) {
    assert.equal(context.stabilityNumber(value, 0), String(value));
  }
  for (const value of [null, undefined, '', '  ']) assert.equal(context.stabilityNumber(value), '—');
  assert.equal(context.stabilityNumber(1.25), '1.25');
  assert.equal(context.stabilityNumber(10), '10');
  assert.equal(context.stabilityMetricValue(null, 'mV/h'), '—');

  const explorer = Object.create(context.Explorer.prototype);
  Object.assign(explorer, {
    mode: 'start_stop', highlighted: new Set(), onlyHighlights: false, chartView: null,
    elements: Object.fromEntries(['onlyHighlights', 'chart', 'chartTitle', 'chartDescription',
      'boundary', 'empty', 'zoomLevel', 'legend', 'summary'].map(key => [key, node()])),
    colorFor: () => '#123456', renderLegend() {}, renderSummary() {},
  });
  explorer.renderChart({metric: 'stress_endpoint', metric_spec: {x_key: 'cycle'},
    series: [{series_id: 'S', points: [{x: 1, y: -1.2}, {x: 1000, y: -1.3}]}]});
  const xTickY = explorer.elements.chart.clientHeight - 54 + 22;
  const labels = descendants(explorer.elements.chart)
    .filter(item => item.tag === 'text' && Number(item.attrs.y) === xTickY)
    .map(item => item.textContent);
  assert.deepEqual(labels, ['-28', '149', '326', '503', '680', '857']);
}

async function checkLiveCompensation() {
  const context = page('start-stop.js', /initialize\(\);\s*$/,
    'globalThis.testState = state; globalThis.testElements = elements;');
  const state = context.testState;
  const elements = context.testElements;
  state.status = {available: true, analyzed_data_mode: 'both'};
  state.mode = 'water';
  state.metric = 'cathodic';
  state.activeWorkStepKey = 'step';
  state.workSteps = [{work_step_key: 'step', work_step_label: 'synthetic step'}];
  elements.compensationMode.options = [{value: 'raw'}, {value: 'water'}, {value: 'compare'}];
  const item = {source_id: 'S', display_name: 'synthetic sample', analysis: {
    work_step_key: 'step', water_compensation_available: false,
    cycle_points: [1, 2].map(cycle => ({cycle,
      cathodic_last1s_median_raw_v: -1 - cycle / 10,
      cathodic_last1s_median_water_compensated_v: null})),
  }};
  state.livePreviewItems = new Map([['live-preview:S', item]]);
  state.series = [{series_id: 'live-preview:S', material_relative_path: 'live-preview:S', work_step_key: 'step'}];
  state.selectedSeries = new Set(['live-preview:S']);
  context.updateAnalysisModeControls();
  // The global formal-analysis mode can allow water; each snapshot must still be checked.
  assert.equal(elements.compensationMode.options[1].disabled, false);
  await context.loadChart();
  assert.equal(state.chartData.series[0].variant, 'raw');
  assert.deepEqual(Array.from(state.chartData.series[0].points, p => p.y), [-1.1, -1.2]);
  assert.equal(state.chartData.live_water_unavailable_count, 1);
  assert.match(elements.chartSubtitle.textContent, /缺少水位补偿模型，已保留原始曲线/);
  const paths = descendants(elements.startStopChart).filter(n => n.attrs.class === 'chart-series-path');
  assert.equal(paths.length, 1);
  assert.notEqual(paths[0].attrs.d, 'M70.00,284.50 L874.00,284.50');
  assert.equal(context.liveChartPoints(item, 'water', 6000).length, 0);

  state.mode = 'compare';
  await context.loadChart();
  assert.deepEqual(Array.from(state.chartData.series, series => series.variant), ['raw']);
  item.analysis.water_compensation_available = true;
  item.analysis.cycle_points.forEach(row => { row.cathodic_last1s_median_water_compensated_v = -row.cycle / 10; });
  await context.loadChart();
  assert.deepEqual(Array.from(state.chartData.series, series => series.variant), ['raw', 'water']);
  assert.equal(state.chartData.live_water_unavailable_count, 0);
  assert.doesNotMatch(elements.chartSubtitle.textContent, /缺少水位补偿模型/);
  state.mode = 'water';
  await context.loadChart();
  assert.deepEqual(Array.from(state.chartData.series, series => series.variant), ['water']);
  assert.deepEqual(Array.from(state.chartData.series[0].points, p => p.y), [-0.1, -0.2]);
  delete item.analysis.water_compensation_available;
  await context.loadChart();
  assert.equal(state.chartData.series[0].variant, 'raw');

  item.analysis.cycle_points = [
    {cycle: null, cathodic_last1s_median_raw_v: -1},
    {cycle: 1, cathodic_last1s_median_raw_v: null},
    {cycle: 2, cathodic_last1s_median_raw_v: ''},
    {cycle: ' ', cathodic_last1s_median_raw_v: -1},
    {cycle: 0, cathodic_last1s_median_raw_v: 0, cathodic_current_median_a_cm2: null},
  ];
  const points = context.liveChartPoints(item, 'raw', 6000);
  assert.equal(points.length, 1);
  assert.equal(points[0].x, 0);
  assert.equal(points[0].y, 0);
  assert(Number.isNaN(points[0].current_a_cm2));
  item.analysis.cycle_points = [{cycle: null}];
  item.analysis.overview_points = [{continuous_time_h: null, potential_raw_v: null}];
  assert.equal(context.plottableLivePreviewItems({preview: {items: [item]}}).length, 0);
}

function record(analysisId = 'old', version = 1) {
  return {analysis_id: analysisId, cv_source_version_id: version, material_key: 'M',
    display_name: 'synthetic sample', status: 'ready', status_label: '可计算过电位',
    cv: {name: 'CV', repository_path: analysisId + '.txt'}, eis: {name: 'EIS', source_version_id: 2},
    instrument_ir_applied: false, ir_correction_available: true, current_basis: 'density', overpotentials: []};
}

function cvPage(request) {
  const context = page('start-stop-cv-eis.js', /loadCvEisWorkspace\(\);\s*$/,
    'globalThis.testState = cvEisState; globalThis.testElements = cvEisElements;',
    {StartStopClient: {request, applyRoutes() {}}});
  const state = context.testState;
  const elements = context.testElements;
  state.catalog = {can_review: true, review_revision: 0, materials: [record()]};
  state.selectedId = 'old';
  state.curve = {points: [{raw_e_rhe_v: -0.1, current: -10}]};
  elements.cvEisReviewSource.value = '2';
  elements.cvEisReviewIr.value = 'false';
  elements.cvEisSearch.value = '';
  elements.cvEisStatusFilter.value = 'all';
  elements.cvEisSort.value = 'favorite';
  elements.cvEisTarget.value = '10';
  elements.showCvRaw.checked = true;
  return context;
}

async function checkCvReview() {
  const submit = {preventDefault() {}};
  const curve = {current_unit: 'mA/cm²', rules: {ir_correction_available: true}, overpotentials: [],
    points: [{raw_e_rhe_v: -0.1, ir90_e_rhe_v: -0.09, current: -10},
      {raw_e_rhe_v: -0.2, ir90_e_rhe_v: -0.18, current: -20}]};
  for (const failure of ['catalog', 'curve', 'missing']) {
    const requests = [];
    const context = cvPage(async (url, options) => {
      requests.push([url, options?.method]);
      if (options?.method === 'PUT') return {};
      if (failure === 'catalog' || url.includes('/curve?')) throw new Error('synthetic read failure');
      if (url === '/api/start-stop/cv-eis') return {can_review: true, counts: {},
        materials: failure === 'missing' ? [record('other', 3)] : [record('new')]};
      return {};
    });
    await context.saveCvEisReview(submit);
    const state = context.testState, elements = context.testElements;
    assert.equal(state.curve, null);
    assert.match(elements.cvEisNotice.textContent, /确认已保存，但当前曲线未能刷新/);
    assert.equal(elements.cvEisNotice.classList.contains('success'), false);
    if (failure !== 'curve') {
      assert.equal(state.selectedId, '');
      assert.equal(elements.cvEisCvSource.textContent, '—');
      assert.equal(elements.cvEisReviewForm.hidden, true);
      assert.equal(requests.some(([url]) => url.includes('/curve?')), false);
    } else {
      assert.equal(state.selectedId, 'new');
      assert.equal(elements.cvEisCvSource.textContent.startsWith('new.txt'), true);
    }
  }

  const requests = [];
  const success = cvPage(async (url, options) => {
    requests.push([url, options?.method]);
    if (options?.method === 'PUT') return {};
    if (url === '/api/start-stop/cv-eis') return {can_review: true, counts: {},
      materials: [record('other', 3), record('confirmed')]};
    if (url.includes('/curve?')) return curve;
    return {};
  });
  await success.saveCvEisReview(submit);
  assert.equal(success.testState.selectedId, 'confirmed');
  assert.equal(success.testState.curve, curve);
  assert.equal(success.testElements.cvEisNotice.classList.contains('success'), true);
  assert.match(success.testElements.cvEisNotice.textContent, /当前曲线已按确认结果更新/);
  assert.deepEqual(requests.filter(([url]) => url.includes('/curve?')).map(([url]) => url),
    ['/api/start-stop/cv-eis/curve?analysis_id=confirmed']);
  // Ordinary reload remains usable as an event handler and can select the default record.
  assert.equal(await success.loadCvEisWorkspace({type: 'click'}), true);
  assert.equal(success.testState.selectedId, 'other');

  const rejected = cvPage(async () => { throw new Error('synthetic PUT conflict'); });
  const oldCurve = rejected.testState.curve;
  await rejected.saveCvEisReview(submit);
  assert.equal(rejected.testState.curve, oldCurve);
  assert.match(rejected.testElements.cvEisReviewState.textContent, /synthetic PUT conflict/);

  let resolveOld;
  const empty = cvPage(async url => {
    if (url.includes('/curve?')) return await new Promise(resolve => { resolveOld = resolve; });
    return url === '/api/start-stop/cv-eis' ? {materials: [], counts: {}} : {};
  });
  const pending = empty.selectCvEisRecord('old');
  assert.equal(await empty.loadCvEisWorkspace(), true);
  resolveOld(curve);
  assert.equal(await pending, false);
  assert.equal(empty.testState.selectedId, '');
  assert.equal(empty.testState.curve, null);
  assert.equal(empty.testState.geometry, null);
}

(async () => {
  checkStabilityNumbers();
  await checkLiveCompensation();
  await checkCvReview();
  console.log('PASS: integer chart ticks, missing live values and per-snapshot compensation, CV review save/read outcomes');
})().catch(error => { console.error(error); process.exitCode = 1; });
