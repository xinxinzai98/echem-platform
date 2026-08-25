const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_CHART_SELECTION = 64;
const MAX_HIGHLIGHTED_SERIES = 16;
const LIVE_PREVIEW_SERIES_PREFIX = "live-preview:";
const LIVE_COMPARISON_POLL_MS = 30_000;

const state = {
  status: null,
  materials: [],
  series: [],
  workSteps: [],
  formalMaterials: [],
  formalSeries: [],
  formalWorkSteps: [],
  livePreview: null,
  livePreviewItems: new Map(),
  liveComparisonRequested: new URLSearchParams(window.location.search).get("view") === "live",
  liveComparisonLoading: false,
  liveComparisonPollTimer: null,
  liveComparisonMessage: "",
  liveComparisonError: "",
  activeWorkStepKey: "",
  selectedSeries: new Set(),
  highlightedSeries: new Set(),
  anomalyMaterialKey: "",
  chartData: null,
  metric: "cathodic",
  mode: "raw",
  xAxis: "cycle",
  showMarkers: true,
  onlyHighlights: false,
  loadingChart: false,
  chartRequestId: 0,
  chartView: null,
  chartGeometry: null,
  chartInteractionMode: "inspect",
  chartGesture: null,
  chartRenderFrame: null,
  chartSuppressClickUntil: 0,
  chartContextMenuReturnFocus: false,
};

const palette = [
  "#0057B8", "#E66100", "#009E73", "#7A3E9D",
  "#C58A00", "#D81B60", "#007C91", "#8C564B",
  "#A50F15", "#4D4D4D", "#6B8E23", "#56B4E9",
  "#B79F00", "#CC79A7", "#332288", "#44AA99",
  "#AA4499", "#117733", "#882255", "#6699CC",
  "#1B9E77", "#D95F02", "#7570B3", "#E7298A",
  "#66A61E", "#E6AB02", "#A6761D", "#1F78B4",
  "#33A02C", "#E31A1C", "#6A3D9A", "#B15928",
];
const seriesLineStyles = ["", "9 4"];

const elements = Object.fromEntries(
  [
    "sidebarStartStopState",
    "workbenchBrand", "workbenchBrandTitle", "workbenchBrandSubtitle",
    "workbenchEnvironmentTitle", "workbenchEnvironmentNote", "workbenchVersion", "startStopNavNumber",
    "startStopWorkstationsNavNumber",
    "startStopConfigNavNumber", "startStopCvEisNavNumber", "startStopMaterialsNavNumber", "printCurrentView", "workspaceNotice",
    "materialSearch", "startStopStepFilter", "startStopStepSummary",
    "materialFilter", "materialSort", "materialFilterSummary", "clearMaterialFilters",
    "selectIncludedForChart", "inspectionSelectionTitle", "inspectionSelectionCopy",
    "selectAllForChart", "clearAllForChart", "selectedSeriesMetric",
    "materialList", "chartTitle",
    "chartSubtitle", "clearHighlights", "showOnlyHighlights", "compensationMode",
    "xAxisMode", "showAnomalyMarkers", "visibleSeriesCount", "chartFrame",
    "startStopChart", "chartEmpty", "chartTooltip", "chartLegend", "potentialBasisRule",
    "endpointRule", "anomalyRule", "waterRule", "highlightCount",
    "highlightDetails", "inspectMode", "zoomSelectMode", "panMode", "zoomOut", "zoomIn",
    "resetZoom", "zoomLevel", "zoomSelection", "chartInteractionHint",
    "liveComparisonBanner", "liveComparisonMeta", "liveComparisonState", "refreshLiveComparison",
  ].map((id) => [id, document.querySelector(`#${id}`)]),
);

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, String(value));
  }
  return node;
}

async function request(url) {
  const response = await fetch(url, {
    cache: "no-store",
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) {
    throw new Error(payload?.error || `请求失败（${response.status}）`);
  }
  return payload;
}

function formatInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatNumber(value, digits = 3) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function setNotice(message = "", type = "warning") {
  elements.workspaceNotice.hidden = !message;
  elements.workspaceNotice.classList.toggle("error", type === "error");
  elements.workspaceNotice.classList.toggle("success", type === "success");
  elements.workspaceNotice.textContent = message;
}

function isLanReadOnly() {
  return state.status?.access_mode === "lan_read_only";
}

function applyDeploymentProfile(payload) {
  const repositoryMode = payload?.deployment_profile === "start_stop_repository"
    || payload?.repository?.storage_mode === "sqlite_blob_repository";
  document.querySelectorAll("[data-config-route]").forEach((link) => {
    link.href = "/start-stop";
  });
  document.querySelectorAll("[data-workstations-route]").forEach((link) => {
    link.href = "/start-stop/workstations";
  });
  document.querySelectorAll("[data-analysis-route]").forEach((link) => {
    link.href = "/start-stop/analysis";
  });
  document.querySelectorAll("[data-cv-eis-route]").forEach((link) => {
    link.href = "/start-stop/cv-eis";
  });
  document.querySelectorAll("[data-materials-route]").forEach((link) => {
    link.href = "/start-stop/materials";
  });
  if (!repositoryMode) return false;
  elements.workbenchBrand.href = "/start-stop";
  elements.workbenchBrand.setAttribute("aria-label", "返回启停数据分析首页");
  elements.workbenchBrandTitle.textContent = "Start–stop Studio";
  elements.workbenchBrandSubtitle.textContent = "启停数据分析";
  const serviceVersion = payload?.service?.version;
  elements.workbenchVersion.textContent = serviceVersion && serviceVersion !== "unknown"
    ? `版本 ${serviceVersion}`
    : "版本未知";
  elements.workbenchVersion.title = payload?.service?.image_reference || "当前运行服务版本";
  elements.startStopConfigNavNumber.textContent = "01";
  elements.startStopWorkstationsNavNumber.textContent = "02";
  elements.startStopNavNumber.textContent = "03";
  elements.startStopCvEisNavNumber.textContent = "04";
  elements.startStopMaterialsNavNumber.textContent = "05";
  return true;
}

function updateStatusView() {
  const payload = state.status;
  applyDeploymentProfile(payload);
  if (!payload?.available) {
    elements.sidebarStartStopState.textContent = "启停工作区不可用";
    setNotice(payload?.message || "无法读取启停分析工作区。", "error");
    return;
  }
  elements.sidebarStartStopState.textContent = payload.export_ready
    ? "启停图集已就绪"
    : "启停图集待更新";
  const lanReadOnly = isLanReadOnly();
  document.body.classList.toggle("lan-read-only", lanReadOnly);
  if (lanReadOnly) {
    setNotice("当前是局域网只读入口：可以选线、高亮、缩放并导出当前检查视图。数据管理请前往第一页“材料与绘图配置”。");
  } else if (!payload.export_ready) {
    setNotice(payload.data_stale
      ? "第一页有新数据尚未确认；当前曲线仍来自上一次已生成结果。"
      : "材料配置已修改但尚未重新绘图；请在第一页完成重绘后再检查结果。");
  } else {
    setNotice();
  }
  if (payload.rules) {
    elements.potentialBasisRule.textContent = "原始实测电位 vs Hg/HgO（不做参比换算）";
    elements.endpointRule.textContent = payload.rules.endpoint || "最后 1 s 中位数";
    elements.anomalyRule.textContent = payload.rules.anomaly || "最低点 <15 s 异常，≥15 s 正常";
    const slope = formatNumber(payload.rules.water_slope_mv_per_h, 4);
    elements.waterRule.textContent = `${payload.rules.water_reference} · ${slope} mV/h`;
  }
}

function materialForSeries(seriesId) {
  const series = state.series.find((item) => item.series_id === seriesId);
  return state.materials.find((item) => item.key === series?.material_relative_path);
}

function displayNameForSeries(seriesId, fallback = seriesId) {
  return materialForSeries(seriesId)?.plot_name || fallback;
}

function activeWorkStep() {
  return state.workSteps.find((item) => item.work_step_key === state.activeWorkStepKey) || null;
}

function liveSeriesId(item) {
  return `${LIVE_PREVIEW_SERIES_PREFIX}${String(item?.source_id || "")}`;
}

function liveMaterialKey(item) {
  return `${LIVE_PREVIEW_SERIES_PREFIX}${String(item?.source_id || "")}`;
}

function liveAnalysis(item) {
  return item?.analysis && typeof item.analysis === "object" ? item.analysis : {};
}

function liveWorkStepKey(item) {
  return String(liveAnalysis(item).work_step_key || "");
}

function liveItemsForActiveStep() {
  return [...state.livePreviewItems.values()].filter((item) => (
    liveWorkStepKey(item) === state.activeWorkStepKey
  ));
}

function plottableLivePreviewItems(payload) {
  const raw = Array.isArray(payload?.preview?.items) ? payload.preview.items : [];
  const seen = new Set();
  return raw.filter((item) => {
    const sourceId = String(item?.source_id || "");
    const analysis = liveAnalysis(item);
    const overview = Array.isArray(analysis.overview_points) ? analysis.overview_points : [];
    const cycles = Array.isArray(analysis.cycle_points) ? analysis.cycle_points : [];
    if (!sourceId || seen.has(sourceId) || !liveWorkStepKey(item) || (!overview.length && !cycles.length)) return false;
    seen.add(sourceId);
    return overview.some((point) => (
      Number.isFinite(Number(point?.continuous_time_h))
      && Number.isFinite(Number(point?.potential_raw_v))
    )) || cycles.some((point) => Number.isFinite(Number(point?.cycle)));
  });
}

function liveDisplayName(item) {
  const material = String(item?.display_name || item?.material_key || "正在测试材料").trim();
  const machine = String(item?.machine_name || item?.hostname || "").trim();
  return machine ? `${material} · ${machine}` : material;
}

function installLivePreviewWorkspace(payload, { activate = false } = {}) {
  const previousLiveIds = new Set(state.livePreviewItems.keys());
  const previousSelected = new Set(state.selectedSeries);
  const previousHighlighted = new Set(state.highlightedSeries);
  const items = plottableLivePreviewItems(payload);

  state.livePreview = payload;
  state.livePreviewItems = new Map(items.map((item) => [liveSeriesId(item), item]));
  state.materials = [...state.formalMaterials];
  state.series = [...state.formalSeries];
  state.workSteps = [...state.formalWorkSteps];

  for (const item of items) {
    const analysis = liveAnalysis(item);
    const seriesId = liveSeriesId(item);
    const materialKey = liveMaterialKey(item);
    const name = `${liveDisplayName(item)}（正在测试）`;
    const taskLabel = String(analysis.work_step_test_type_label_zh || item?.task?.task_label || "实时启停");
    const machine = String(item?.machine_name || item?.hostname || "实验机");
    const fileName = String(item?.file_name || "正在写入文件");
    const continuationLabel = analysis.continuation_mode === "replace_active_segment"
      ? "续写同一文件段"
      : analysis.continuation_mode === "append_new_segment"
        ? "接续到正式序列末尾"
        : "尚未入库的新序列";
    state.materials.push({
      key: materialKey,
      plot_name: name,
      auto_name: String(item?.display_name || item?.material_key || name),
      favorite: Boolean(item?.favorite),
      include_in_summary_atlas: true,
      status: "正在测试",
      test_types: taskLabel,
      latest_source_modified_at: item?.source_modified_utc || item?.captured_at_utc || "",
      ordered_source_files: Array.isArray(analysis.ordered_source_files) && analysis.ordered_source_files.length
        ? analysis.ordered_source_files
        : [fileName],
      notes: `${machine} · ${continuationLabel} · 按正式启停规则临时计算，不进入数据库`,
      live_preview: true,
      live_complete_row_count: Number(item?.complete_row_count || 0),
      live_complete_cycles: Number(analysis.complete_cycles || 0),
    });
    state.series.push({
      series_id: seriesId,
      series_display_name: name,
      material_relative_path: materialKey,
      work_step_key: String(analysis.work_step_key || ""),
      work_step_label: String(analysis.work_step_label || taskLabel),
      work_step_test_type_label_zh: taskLabel,
      work_step_test_type: String(analysis.work_step_test_type || analysis.test_type || ""),
      work_step_cathodic_current_ma_cm2: analysis.work_step_cathodic_current_ma_cm2,
      work_step_recovery_current_ma_cm2: analysis.work_step_recovery_current_ma_cm2,
      work_step_cathodic_duration_s: analysis.work_step_cathodic_duration_s,
      work_step_recovery_duration_s: analysis.work_step_recovery_duration_s,
      complete_cycles: Number(analysis.continuation_cycle_offset || 0) + Number(analysis.complete_cycles || 0),
      abnormal_fraction: 0,
      live_preview: true,
      live_complete_row_count: Number(item?.complete_row_count || 0),
      captured_at_utc: item?.captured_at_utc || "",
      formal_series_id: String(analysis.formal_series_id || ""),
      style_series_id: String(analysis.formal_series_id || seriesId),
    });
  }

  const workStepMap = new Map(state.workSteps.map((item) => [item.work_step_key, { ...item }]));
  for (const item of items) {
    const analysis = liveAnalysis(item);
    const key = liveWorkStepKey(item);
    const current = workStepMap.get(key) || {
      work_step_key: key,
      work_step_label: String(analysis.work_step_label || "正在测试启停工步"),
      material_count: 0,
      series_count: 0,
    };
    current.material_count = Number(current.material_count || 0) + 1;
    current.series_count = Number(current.series_count || 0) + 1;
    current.live_preview_count = Number(current.live_preview_count || 0) + 1;
    workStepMap.set(key, current);
  }
  state.workSteps = [...workStepMap.values()];

  if (activate && items.length) {
    state.activeWorkStepKey = liveWorkStepKey(items[0]);
  }
  if (activate || previousLiveIds.size) {
    const nextIds = new Set(state.livePreviewItems.keys());
    state.selectedSeries = new Set(
      [...previousSelected].filter((seriesId) => !previousLiveIds.has(seriesId)),
    );
    for (const seriesId of nextIds) {
      const item = state.livePreviewItems.get(seriesId);
      if (
        liveWorkStepKey(item) === state.activeWorkStepKey
        && (activate || previousSelected.has(seriesId) || !previousLiveIds.has(seriesId))
      ) state.selectedSeries.add(seriesId);
    }
    state.highlightedSeries = new Set(
      [...previousHighlighted].filter((seriesId) => (
        !previousLiveIds.has(seriesId) || nextIds.has(seriesId)
      )),
    );
  }
  return items.length;
}

function formatLiveComparisonTime(value) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}

function renderLiveComparisonBanner() {
  const active = state.liveComparisonRequested;
  elements.liveComparisonBanner.hidden = !active;
  document.body.classList.toggle("live-comparison-mode", active);
  if (!active) return;
  const activeItems = liveItemsForActiveStep();
  const itemCount = activeItems.length;
  const cycleCount = activeItems.reduce(
    (total, item) => total + Number(liveAnalysis(item).complete_cycles || 0),
    0,
  );
  const generatedAt = state.livePreview?.preview?.generated_at_utc
    || state.livePreview?.last_completed_utc
    || state.livePreview?.last_finished_utc;
  const enabledText = state.livePreview?.enabled === true
    ? "平台每 5 分钟生成新快照，本页会自动读取"
    : "实时抓取目前已关闭，当前显示最后一次缓存";
  elements.liveComparisonMeta.textContent = itemCount
    ? `当前工步已叠加 ${itemCount} 条正在测试曲线、${formatInteger(cycleCount)} 个完整循环 · 快照 ${formatLiveComparisonTime(generatedAt)} · ${enabledText}。按正式启停规则临时计算，未稳定文件不会正式入库。`
    : state.livePreviewItems.size
      ? `当前工步没有正在测试曲线；可切换工步查看其他活动文件 · ${enabledText}。`
      : `当前快照没有可计算的活动启停文件 · ${enabledText}。`;
  elements.liveComparisonState.className = `live-comparison-state${state.liveComparisonError ? " is-error" : state.liveComparisonLoading ? " is-refreshing" : ""}`;
  elements.liveComparisonState.textContent = state.liveComparisonError
    || (state.liveComparisonLoading ? "正在读取最新快照" : state.liveComparisonMessage || "实时对比已就绪");
  elements.refreshLiveComparison.disabled = state.liveComparisonLoading;
}

function seriesForMaterial(key) {
  if (!state.activeWorkStepKey) return [];
  return state.series.filter((item) => (
    item.material_relative_path === key
    && item.work_step_key === state.activeWorkStepKey
  ));
}

function materialKeyForSeries(seriesId) {
  return state.series.find((item) => item.series_id === seriesId)?.material_relative_path || "";
}

function isAnomalyMode() {
  return state.metric === "minimum_time";
}

function checkedFilterValues(selector) {
  return new Set(
    [...document.querySelectorAll(selector)]
      .filter((input) => input.checked)
      .map((input) => input.value),
  );
}

function materialProcessTags(material) {
  const text = [material.plot_name, material.auto_name, material.key, material.test_types]
    .filter(Boolean)
    .join(" ");
  const tags = new Set();
  if (/脉冲|(?:^|[^A-Za-z])pulse(?:[^A-Za-z]|$)/i.test(text)) tags.add("pulse");
  if (/恒流/.test(text)) tags.add("constant");
  return tags;
}

function chemicalFormulaTokens(text) {
  const formulas = [];
  for (const match of String(text || "").matchAll(/[A-Z][A-Za-z0-9.]*/g)) {
    const chunk = match[0];
    const tokens = chunk.match(/[A-Z][a-z]?(?:\d+(?:\.\d+)?)?/g) || [];
    if (tokens.join("") !== chunk) continue;
    formulas.push(tokens.map((token) => token.replace(/[\d.]+$/g, "")));
  }
  return formulas;
}

function materialElementTags(material) {
  // Paths and notes are deliberately excluded: names such as LPF,
  // Control_Programs or pulse must never become false P/Co matches.
  const text = [material.plot_name, material.auto_name].filter(Boolean).join(" ");
  const formulas = chemicalFormulaTokens(text);
  const flattened = formulas.flat();
  const tags = new Set();
  for (const symbol of ["Ni", "Mo", "Co"]) {
    if (flattened.includes(symbol)) tags.add(symbol);
  }
  const formulaContext = ["Ni", "Mo", "Co"].some((symbol) => tags.has(symbol));
  const standaloneP = /(^|[^A-Za-z])P(?:\d+(?:\.\d+)?)?($|[^A-Za-z])/.test(text);
  if ((formulaContext && flattened.includes("P")) || standaloneP) tags.add("P");
  for (const [word, symbol] of [["镍", "Ni"], ["钼", "Mo"], ["磷", "P"], ["钴", "Co"]]) {
    if (text.includes(word)) tags.add(symbol);
  }
  return tags;
}

function ensureAnomalyMaterial() {
  if (seriesForMaterial(state.anomalyMaterialKey).length) return;
  const preferredSeries = [
    ...state.highlightedSeries,
    ...state.selectedSeries,
  ];
  const preferredKey = preferredSeries.map(materialKeyForSeries).find(Boolean);
  state.anomalyMaterialKey = preferredKey
    || visibleMaterialsForSelection()[0]?.key
    || state.materials[0]?.key
    || "";
}

function activeSeriesIds() {
  if (!isAnomalyMode()) {
    return [...state.selectedSeries].filter((seriesId) => (
      state.series.some((item) => (
        item.series_id === seriesId
        && item.work_step_key === state.activeWorkStepKey
      ))
    ));
  }
  ensureAnomalyMaterial();
  return seriesForMaterial(state.anomalyMaterialKey).map((item) => item.series_id);
}

function defaultWorkStepKey() {
  const includedCounts = new Map();
  for (const series of state.series) {
    const material = state.materials.find((item) => item.key === series.material_relative_path);
    if (!material?.include_in_summary_atlas) continue;
    includedCounts.set(
      series.work_step_key,
      (includedCounts.get(series.work_step_key) || 0) + 1,
    );
  }
  return [...state.workSteps]
    .sort((left, right) => (
      (includedCounts.get(right.work_step_key) || 0)
      - (includedCounts.get(left.work_step_key) || 0)
      || Number(right.series_count || 0) - Number(left.series_count || 0)
    ))[0]?.work_step_key || "";
}

function renderWorkStepFilter() {
  const previous = state.activeWorkStepKey;
  const availableKeys = new Set(state.workSteps.map((item) => item.work_step_key));
  state.activeWorkStepKey = availableKeys.has(previous) ? previous : defaultWorkStepKey();
  const fragment = document.createDocumentFragment();
  for (const workStep of state.workSteps) {
    const option = document.createElement("option");
    option.value = workStep.work_step_key;
    option.textContent = `${workStep.work_step_label}（${formatInteger(workStep.material_count)} 个材料）`;
    fragment.append(option);
  }
  if (!fragment.childNodes.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "暂无可识别的启停工步";
    fragment.append(option);
  }
  elements.startStopStepFilter.replaceChildren(fragment);
  elements.startStopStepFilter.value = state.activeWorkStepKey;
  elements.startStopStepFilter.disabled = state.workSteps.length === 0;
}

function selectIncludedSeriesForActiveWorkStep() {
  state.selectedSeries.clear();
  state.highlightedSeries.clear();
  const prioritized = [...state.series].sort((left, right) => (
    Number(Boolean(right.live_preview)) - Number(Boolean(left.live_preview))
  ));
  for (const series of prioritized) {
    if (series.work_step_key !== state.activeWorkStepKey) continue;
    const material = state.materials.find((item) => item.key === series.material_relative_path);
    if (material?.include_in_summary_atlas) state.selectedSeries.add(series.series_id);
  }
  enforceSelectionLimit();
}

function seriesStyle(seriesId) {
  const summary = state.series.find((item) => item.series_id === seriesId);
  const styleId = String(summary?.style_series_id || seriesId);
  const ordered = [...new Set(state.series.map((item) => (
    String(item.style_series_id || item.series_id)
  )))];
  const index = Math.max(0, ordered.indexOf(styleId));
  return {
    color: palette[index % palette.length],
    dasharray: seriesLineStyles[Math.floor(index / palette.length)] || "",
    index,
  };
}

function chartDasharray(style, variant = "raw", livePreview = false) {
  if (livePreview && variant === "water") return "8 3 2 3";
  if (livePreview) return "2 3";
  if (variant === "water") return style.dasharray ? "9 3 2 3" : "5 3";
  return style.dasharray;
}

function createSwatch(seriesId, variant = "raw", livePreview = false) {
  const style = seriesStyle(seriesId);
  const svg = svgElement("svg", { class: "line-swatch", viewBox: "0 0 29 7", "aria-hidden": "true" });
  const line = svgElement("line", { x1: 1, y1: 3.5, x2: 28, y2: 3.5, stroke: style.color });
  const dasharray = chartDasharray(style, variant, livePreview);
  if (dasharray) line.setAttribute("stroke-dasharray", dasharray);
  svg.append(line);
  return svg;
}

function materialMatchesActiveFilter(material) {
  const query = elements.materialSearch.value.trim().toLowerCase();
  const filter = elements.materialFilter.value;
  const processFilters = checkedFilterValues("[data-process-filter]");
  const elementFilters = checkedFilterValues("[data-element-filter]");
  const haystack = `${material.plot_name} ${material.auto_name} ${material.key}`.toLowerCase();
  const matchesQuery = !query || haystack.includes(query);
  const matchesFilter = filter === "all"
    || (filter === "favorite" && material.favorite)
    || (filter === "included" && material.include_in_summary_atlas)
    || (filter === "excluded" && !material.include_in_summary_atlas)
    || (filter === "changed" && material.status !== "未变化");
  const processTags = materialProcessTags(material);
  const elementTags = materialElementTags(material);
  const matchesProcess = processFilters.size === 0
    || [...processFilters].some((tag) => processTags.has(tag));
  const matchesElements = [...elementFilters].every((tag) => elementTags.has(tag));
  const matchesWorkStep = seriesForMaterial(material.key).length > 0;
  return matchesWorkStep && matchesQuery && matchesFilter && matchesProcess && matchesElements;
}

function completeCyclesForMaterial(material) {
  return seriesForMaterial(material.key).reduce(
    (sum, item) => sum + Number(item.complete_cycles || 0),
    0,
  );
}

function materialModifiedTimestamp(material) {
  const timestamp = Date.parse(String(material.latest_source_modified_at || ""));
  return Number.isFinite(timestamp) ? timestamp : null;
}

function formatMaterialModifiedAt(material) {
  const timestamp = materialModifiedTimestamp(material);
  if (timestamp === null) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}

function sortVisibleMaterials(materials) {
  const sortMode = elements.materialSort?.value || "favorites_first";
  if (sortMode === "default") return materials;
  const indexed = materials.map((material, index) => ({ material, index }));
  if (sortMode === "favorites_first") {
    indexed.sort((left, right) => (
      Number(Boolean(right.material.favorite))
      - Number(Boolean(left.material.favorite))
      || left.index - right.index
    ));
    return indexed.map(({ material }) => material);
  }
  const ascending = sortMode.endsWith("_asc");
  const byUpdatedAt = sortMode.startsWith("updated_");
  indexed.sort((left, right) => {
    const leftValue = byUpdatedAt
      ? materialModifiedTimestamp(left.material)
      : completeCyclesForMaterial(left.material);
    const rightValue = byUpdatedAt
      ? materialModifiedTimestamp(right.material)
      : completeCyclesForMaterial(right.material);
    if (leftValue === null && rightValue !== null) return 1;
    if (rightValue === null && leftValue !== null) return -1;
    if (leftValue !== rightValue) {
      return ascending ? leftValue - rightValue : rightValue - leftValue;
    }
    return left.index - right.index;
  });
  return indexed.map(({ material }) => material);
}

function visibleMaterialsForSelection() {
  return sortVisibleMaterials(state.materials.filter(materialMatchesActiveFilter));
}

function updateMaterialSelectionControls(visibleMaterials) {
  const anomalyMode = isAnomalyMode();
  const liveCount = liveItemsForActiveStep().length;
  const workStepMaterials = state.materials.filter((material) => (
    seriesForMaterial(material.key).length > 0
  ));
  const visibleSeriesIds = visibleMaterials.flatMap((material) => (
    seriesForMaterial(material.key).map((item) => item.series_id)
  ));
  const allVisibleSelected = visibleSeriesIds.length > 0
    && visibleSeriesIds.every((seriesId) => state.selectedSeries.has(seriesId));
  const selectedMaterialCount = workStepMaterials.filter((material) => (
    seriesForMaterial(material.key).some((item) => state.selectedSeries.has(item.series_id))
  )).length;
  elements.selectAllForChart.disabled = anomalyMode || !visibleSeriesIds.length || allVisibleSelected;
  elements.selectIncludedForChart.disabled = anomalyMode;
  elements.clearAllForChart.disabled = anomalyMode
    ? !state.anomalyMaterialKey
    : state.selectedSeries.size === 0;
  elements.selectAllForChart.textContent = visibleMaterials.length < workStepMaterials.length
    ? `全选可见（${visibleMaterials.length}）`
    : "全选检查";
  elements.selectedSeriesMetric.textContent = anomalyMode
    ? state.anomalyMaterialKey ? "异常判断：1 个材料" : "异常判断：未选材料"
    : `已选 ${selectedMaterialCount} / ${workStepMaterials.length}`;
  elements.materialFilterSummary.textContent = `当前工步可见 ${visibleMaterials.length} / ${workStepMaterials.length} 个材料${liveCount ? `，含 ${liveCount} 个正在测试` : ""}`;
  const workStep = activeWorkStep();
  elements.startStopStepSummary.textContent = workStep
    ? `当前工步共 ${formatInteger(workStep.series_count)} 条分析序列；切换工步后会重置检查与高亮，不会混合绘制。`
    : "暂无可识别的启停工步";
  elements.inspectionSelectionTitle.textContent = anomalyMode
    ? "异常判断一次只分析一个材料"
    : "此处选择只用于当前检查";
  elements.inspectionSelectionCopy.textContent = anomalyMode
    ? "切换材料会替换当前判断对象；返回其他图表后恢复原来的多材料选择。"
    : `“全选检查”不会改变总结图集的入图配置${liveCount ? "；正在测试曲线也不会正式入库。" : "。"}`;
}

function renderMaterialList() {
  const visibleMaterials = visibleMaterialsForSelection();
  const fragment = document.createDocumentFragment();
  for (const material of visibleMaterials) {
    const anomalyMode = isAnomalyMode();
    const related = seriesForMaterial(material.key);
    const primarySeries = related[0]?.series_id || "";
    const highlightedSeries = anomalyMode
      ? null
      : related.find((item) => state.highlightedSeries.has(item.series_id));
    const row = element("article", "material-row");
    row.dataset.materialKey = material.key;
    row.classList.toggle("is-anomaly-selected", anomalyMode && material.key === state.anomalyMaterialKey);
    if (highlightedSeries) {
      row.classList.add("is-highlighted");
      row.style.setProperty("--material-highlight-color", seriesStyle(highlightedSeries.series_id).color);
    }

    const checkColumn = element("div", "material-check-column");
    const chartCheck = document.createElement("input");
    chartCheck.type = anomalyMode ? "radio" : "checkbox";
    if (anomalyMode) chartCheck.name = "anomaly-material";
    chartCheck.checked = anomalyMode
      ? material.key === state.anomalyMaterialKey
      : related.some((item) => state.selectedSeries.has(item.series_id));
    chartCheck.disabled = related.length === 0;
    chartCheck.setAttribute(
      "aria-label",
      anomalyMode ? `单独判断 ${material.plot_name} 的异常点` : `在图中检查 ${material.plot_name}`,
    );
    chartCheck.addEventListener("change", () => {
      if (anomalyMode) {
        if (!chartCheck.checked) return;
        state.anomalyMaterialKey = material.key;
        renderMaterialList();
        loadChart();
        return;
      }
      for (const series of related) {
        if (chartCheck.checked) state.selectedSeries.add(series.series_id);
        else {
          state.selectedSeries.delete(series.series_id);
          state.highlightedSeries.delete(series.series_id);
        }
      }
      enforceSelectionLimit();
      renderMaterialList();
      loadChart();
    });
    const highlightButton = element("button", "material-highlight-button", "◆");
    highlightButton.type = "button";
    highlightButton.disabled = !primarySeries || anomalyMode;
    if (primarySeries) {
      highlightButton.style.setProperty("--material-highlight-color", seriesStyle(primarySeries).color);
    }
    highlightButton.setAttribute("aria-label", `高亮 ${material.plot_name}`);
    highlightButton.setAttribute("aria-pressed", String(state.highlightedSeries.has(primarySeries)));
    highlightButton.addEventListener("click", () => toggleHighlight(primarySeries));
    checkColumn.append(chartCheck, highlightButton);

    const main = element("div", "material-main");
    const materialName = material.plot_name || material.auto_name || material.key;
    const name = element("strong", "material-display-name", materialName);
    name.title = materialName;
    const nameRow = element("div", "material-name-row");
    if (material.favorite) {
      const favorite = element("span", "material-favorite-badge", "★ 收藏");
      favorite.title = "已在材料库中收藏";
      nameRow.append(favorite);
    }
    nameRow.append(name);
    const meta = element("div", "material-meta");
    if (primarySeries) meta.append(createSwatch(primarySeries, "raw", Boolean(related[0]?.live_preview)));
    const status = element("span", `material-status${material.status === "未变化" ? "" : " changed"}`, material.status);
    const type = element(
      "span",
      "material-work-step-badge",
      related[0]?.work_step_test_type_label_zh || material.test_types || "启停",
    );
    const cycles = completeCyclesForMaterial(material);
    const count = element(
      "span",
      "",
      material.live_preview
        ? `${formatInteger(material.live_complete_cycles)} 个完整循环`
        : `${formatInteger(cycles)} 循环`,
    );
    meta.append(status, type, count);
    const modifiedAt = formatMaterialModifiedAt(material);
    if (modifiedAt) meta.append(element("span", "material-updated-at", `更新 ${modifiedAt}`));
    const orderedSources = material.ordered_source_files?.length
      ? material.ordered_source_files
      : [material.key];
    const source = element("div", "material-source-line", orderedSources.join(" → "));
    source.title = orderedSources.join("\n");
    const sourceDetails = element("details", "material-source-details");
    const sourceSummary = element("summary", "", "查看完整来源与接续");
    const sourceFullList = element("div", "material-source-full-list");
    orderedSources.forEach((sourceName, index) => {
      sourceFullList.append(element("div", "", `${index + 1}. ${sourceName}`));
    });
    sourceDetails.append(sourceSummary, sourceFullList);
    main.append(nameRow, meta, source, sourceDetails);
    if (material.notes) {
      const notes = element("div", "material-notes-line", material.notes);
      notes.title = material.notes;
      main.append(notes);
    }

    row.append(checkColumn, main);
    fragment.append(row);
  }
  elements.materialList.replaceChildren(
    fragment.childNodes.length
      ? fragment
      : element("div", "start-stop-loading", "没有符合筛选条件的材料"),
  );
  elements.materialList.setAttribute("aria-busy", "false");
  updateMaterialSelectionControls(visibleMaterials);
  renderHighlights();
}

function enforceSelectionLimit() {
  const selected = [...state.selectedSeries];
  if (selected.length <= MAX_CHART_SELECTION) return;
  for (const seriesId of selected.slice(MAX_CHART_SELECTION)) {
    state.selectedSeries.delete(seriesId);
    state.highlightedSeries.delete(seriesId);
  }
  setNotice(`交互图一次最多检查 ${MAX_CHART_SELECTION} 条曲线；已保留前 ${MAX_CHART_SELECTION} 条。`);
}

function updateAnalysisModeControls() {
  const anomalyMode = isAnomalyMode();
  document.body.classList.toggle("anomaly-single-mode", anomalyMode);
  if (anomalyMode) {
    ensureAnomalyMaterial();
    state.onlyHighlights = false;
    elements.showOnlyHighlights.setAttribute("aria-pressed", "false");
  }
  document.querySelectorAll(".chart-tab").forEach((button) => {
    button.disabled = false;
  });
  elements.compensationMode.disabled = anomalyMode;
  elements.compensationMode.title = anomalyMode
    ? "异常判断使用最低点时间，与水位补偿无关"
    : "";
  elements.xAxisMode.disabled = state.metric === "overview";
  elements.xAxisMode.title = "";
  elements.showAnomalyMarkers.disabled = false;
  elements.showAnomalyMarkers.title = "";
  elements.clearHighlights.disabled = anomalyMode;
  elements.showOnlyHighlights.disabled = anomalyMode;
  renderLiveComparisonBanner();
}

function toggleHighlight(seriesId) {
  if (isAnomalyMode()) return;
  if (!seriesId) return;
  const wasSelected = state.selectedSeries.has(seriesId);
  state.selectedSeries.add(seriesId);
  if (state.highlightedSeries.has(seriesId)) {
    state.highlightedSeries.delete(seriesId);
  } else {
    if (state.highlightedSeries.size >= MAX_HIGHLIGHTED_SERIES) {
      const first = state.highlightedSeries.values().next().value;
      state.highlightedSeries.delete(first);
    }
    state.highlightedSeries.add(seriesId);
  }
  enforceSelectionLimit();
  renderMaterialList();
  if (wasSelected) renderChart();
  else loadChart();
}

function renderHighlights() {
  if (isAnomalyMode()) {
    elements.highlightCount.textContent = "单材料";
    elements.highlightDetails.replaceChildren(element(
      "p",
      "",
      "异常判断按左侧单选材料显示全部循环；此模式不叠加其他材料或高亮线。",
    ));
    return;
  }
  const highlighted = [...state.highlightedSeries];
  elements.highlightCount.textContent = `${highlighted.length} / ${MAX_HIGHLIGHTED_SERIES}`;
  if (!highlighted.length) {
    const paragraph = element(
      "p",
      "",
      `点击曲线、图例或材料行的“高亮”即可固定检查，最多 ${MAX_HIGHLIGHTED_SERIES} 条。`,
    );
    elements.highlightDetails.replaceChildren(paragraph);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const seriesId of highlighted) {
    const summary = state.series.find((item) => item.series_id === seriesId) || {};
    const card = element("div", "highlight-card");
    const color = element("span", "highlight-card-color");
    color.style.background = seriesStyle(seriesId).color;
    const copy = element("div");
    const detail = summary.live_preview
      ? `${formatInteger(summary.complete_cycles)} 个接续后循环 · 快照 ${formatLiveComparisonTime(summary.captured_at_utc)}`
      : `${formatInteger(summary.complete_cycles)} 循环 · 异常 ${Number(summary.abnormal_fraction || 0) * 100 < 0.05 ? "0.0" : (Number(summary.abnormal_fraction || 0) * 100).toFixed(1)}%`;
    copy.append(
      element("strong", "", displayNameForSeries(seriesId, summary.series_display_name)),
      element("small", "", detail),
    );
    const remove = element("button", "", "移除");
    remove.type = "button";
    remove.addEventListener("click", () => toggleHighlight(seriesId));
    card.append(color, copy, remove);
    fragment.append(card);
  }
  elements.highlightDetails.replaceChildren(fragment);
}

async function loadWorkspaceData() {
  const [status, materialsPayload, seriesResult] = await Promise.all([
    request("/api/start-stop/status"),
    request("/api/start-stop/materials"),
    request("/api/start-stop/series").then(
      (payload) => ({ payload, error: null }),
      (error) => ({ payload: { series: [], work_steps: [] }, error }),
    ),
  ]);
  if (seriesResult.error && !state.liveComparisonRequested) {
    throw seriesResult.error;
  }
  const seriesPayload = seriesResult.payload;
  state.status = status;
  state.formalMaterials = materialsPayload.materials || [];
  state.formalSeries = seriesPayload.series || [];
  state.formalWorkSteps = seriesPayload.work_steps || [];
  state.materials = [...state.formalMaterials];
  state.series = [...state.formalSeries];
  state.workSteps = [...state.formalWorkSteps];
  if (state.liveComparisonRequested) {
    try {
      const livePreview = await request("/api/start-stop/live-preview");
      installLivePreviewWorkspace(livePreview, { activate: true });
      state.liveComparisonMessage = seriesResult.error
        ? "正式图线待重绘，当前先显示实时快照"
        : "已读取最近快照";
      state.liveComparisonError = "";
    } catch (error) {
      installLivePreviewWorkspace(
        { available: false, enabled: false, preview: { items: [], errors: [] } },
        { activate: true },
      );
      const formalMessage = seriesResult.error
        ? `正式图线读取失败：${seriesResult.error.message}；`
        : "";
      state.liveComparisonError = `${formalMessage}实时快照读取失败：${error.message}`;
    }
  }
  renderWorkStepFilter();
  selectIncludedSeriesForActiveWorkStep();
  ensureAnomalyMaterial();
  updateAnalysisModeControls();
  updateStatusView();
  renderMaterialList();
  await loadChart();
  scheduleLiveComparisonPoll();
}

const liveMetricSpecs = {
  cathodic: {
    label: "阴极段末端电位",
    unit: "V vs Hg/HgO",
    raw: "cathodic_last1s_median_raw_v",
    water: "cathodic_last1s_median_water_compensated_v",
  },
  negative_shift: {
    label: "阴极电位负移",
    unit: "mV",
    raw: "cathodic_negative_shift_mv",
    water: "cathodic_negative_shift_water_compensated_mv",
  },
  reverse: {
    label: "恢复段末端电位",
    unit: "V vs Hg/HgO",
    raw: "reverse_last1s_median_raw_v",
    water: "reverse_last1s_median_water_compensated_v",
  },
  minimum_time: {
    label: "阴极段最低点位置",
    unit: "s",
    raw: "cathodic_phase_min_time_s",
    water: "cathodic_phase_min_time_s",
  },
  overview: {
    label: "全程电位",
    unit: "V vs Hg/HgO",
    raw: "potential_raw_v",
    water: "potential_water_compensated_v",
  },
};

function downsampleLivePoints(points, limit) {
  if (points.length <= limit) return points;
  const indices = new Set();
  for (let index = 0; index < limit; index += 1) {
    indices.add(Math.round(index * (points.length - 1) / (limit - 1)));
  }
  return [...indices].sort((left, right) => left - right).map((index) => points[index]);
}

function liveChartPoints(item, variant, maxPoints) {
  const analysis = liveAnalysis(item);
  const spec = liveMetricSpecs[state.metric] || liveMetricSpecs.cathodic;
  const field = spec[variant];
  const overview = state.metric === "overview";
  const rows = overview
    ? Array.isArray(analysis.overview_points) ? analysis.overview_points : []
    : Array.isArray(analysis.cycle_points) ? analysis.cycle_points : [];
  const points = rows.map((row) => {
    const x = overview
      ? Number(row?.continuous_time_h)
      : state.xAxis === "time"
        ? Number(state.metric === "reverse" ? row?.reverse_endpoint_time_h : row?.cathodic_endpoint_time_h)
        : Number(row?.cycle);
    return {
      x,
      y: Number(row?.[field]),
      cycle: Number(row?.cycle),
      status: String(row?.status || ""),
      current_a_cm2: Number(
        overview
          ? row?.current_a_cm2
          : state.metric === "reverse"
            ? row?.recovery_current_median_a_cm2
            : row?.cathodic_current_median_a_cm2,
      ),
      source_file: String(row?.source_file || item?.file_name || ""),
      segment_index: Number(row?.segment_index),
      captured_at_utc: String(item?.captured_at_utc || ""),
    };
  }).filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
  points.sort((left, right) => left.x - right.x);
  return downsampleLivePoints(points, maxPoints);
}

function emptyLiveChartPayload() {
  const spec = liveMetricSpecs[state.metric] || liveMetricSpecs.cathodic;
  const workStep = activeWorkStep() || {};
  return {
    metric: state.metric,
    metric_label: spec.label,
    unit: spec.unit,
    x_axis: state.metric === "overview" ? "time" : state.xAxis,
    x_label: state.metric === "overview" || state.xAxis === "time" ? "累计时间 / h" : "循环数",
    mode: isAnomalyMode() ? "raw" : state.mode,
    work_step_key: state.activeWorkStepKey,
    work_step_label: workStep.work_step_label || "正在测试启停工步",
    series: [],
    anomaly_boundary_s: 15,
  };
}

function mergeLiveComparisonChart(payload, selectedLiveIds, maxPoints) {
  const merged = {
    ...payload,
    series: (payload?.series || []).map((item) => ({
      ...item,
      points: [...(item.points || [])],
    })),
  };
  for (const seriesId of selectedLiveIds) {
    const item = state.livePreviewItems.get(seriesId);
    if (!item) continue;
    const analysis = liveAnalysis(item);
    const formalSeriesId = String(analysis.formal_series_id || "");
    const replaceFrom = Number(analysis.replace_from_segment_index);
    if (formalSeriesId && Number.isInteger(replaceFrom) && replaceFrom > 0) {
      for (const formal of merged.series) {
        if (formal.series_id !== formalSeriesId || formal.live_preview) continue;
        formal.points = formal.points.filter((point) => (
          !Number.isFinite(Number(point.segment_index))
          || Number(point.segment_index) < replaceFrom
        ));
      }
    }
    const variants = isAnomalyMode()
      ? ["raw"]
      : state.mode === "compare" ? ["raw", "water"] : [state.mode];
    for (const variant of variants) {
      const points = liveChartPoints(item, variant, maxPoints);
      if (!points.length) continue;
      merged.series.push({
        series_id: seriesId,
        style_series_id: formalSeriesId || seriesId,
        name: liveDisplayName(item),
        variant,
        points,
        live_preview: true,
      });
    }
  }
  merged.live_preview = selectedLiveIds.length > 0;
  return merged;
}

function scheduleLiveComparisonPoll() {
  window.clearTimeout(state.liveComparisonPollTimer);
  state.liveComparisonPollTimer = null;
  if (!state.liveComparisonRequested || state.livePreview?.enabled !== true) return;
  state.liveComparisonPollTimer = window.setTimeout(() => {
    refreshLiveComparison({ silent: true });
  }, LIVE_COMPARISON_POLL_MS);
}

async function refreshLiveComparison({ silent = false } = {}) {
  if (state.liveComparisonLoading) return;
  state.liveComparisonLoading = true;
  state.liveComparisonError = "";
  state.liveComparisonMessage = "";
  renderLiveComparisonBanner();
  const previousGeneratedAt = String(
    state.livePreview?.preview?.generated_at_utc || state.livePreview?.last_completed_utc || "",
  );
  try {
    const payload = await request("/api/start-stop/live-preview");
    const nextGeneratedAt = String(
      payload?.preview?.generated_at_utc || payload?.last_completed_utc || "",
    );
    installLivePreviewWorkspace(payload);
    renderWorkStepFilter();
    updateAnalysisModeControls();
    renderMaterialList();
    await loadChart();
    state.liveComparisonMessage = previousGeneratedAt && previousGeneratedAt === nextGeneratedAt
      ? "当前已是最新快照"
      : "已更新到最新快照";
  } catch (error) {
    state.liveComparisonError = `刷新失败：${error.message}`;
    if (!silent) setNotice(state.liveComparisonError, "error");
  } finally {
    state.liveComparisonLoading = false;
    renderLiveComparisonBanner();
    scheduleLiveComparisonPoll();
  }
}

async function loadChart() {
  resetChartZoom(false);
  const selected = activeSeriesIds();
  state.chartRequestId += 1;
  const requestId = state.chartRequestId;
  elements.chartTooltip.hidden = true;
  if (!selected.length) {
    state.chartData = null;
    renderChart();
    return;
  }
  state.loadingChart = true;
  elements.chartEmpty.hidden = false;
  elements.chartEmpty.textContent = "正在读取曲线…";
  const perSeriesCap = state.metric === "overview" ? 2200 : 6000;
  const totalPointBudget = state.metric === "overview" ? 80000 : 192000;
  const maxPoints = Math.max(
    500,
    Math.min(perSeriesCap, Math.floor(totalPointBudget / Math.max(1, selected.length))),
  );
  const selectedLive = selected.filter((seriesId) => state.livePreviewItems.has(seriesId));
  const selectedFormal = selected.filter((seriesId) => !state.livePreviewItems.has(seriesId));
  const formalDependencies = selectedLive.map((seriesId) => (
    String(liveAnalysis(state.livePreviewItems.get(seriesId)).formal_series_id || "")
  )).filter((seriesId) => state.formalSeries.some((item) => (
    item.series_id === seriesId && item.work_step_key === state.activeWorkStepKey
  )));
  const requestedFormal = [...new Set([...selectedFormal, ...formalDependencies])];
  try {
    let payload = emptyLiveChartPayload();
    if (requestedFormal.length) {
      const query = new URLSearchParams({
        series: requestedFormal.join(","),
        metric: state.metric,
        x: state.xAxis,
        mode: isAnomalyMode() ? "raw" : state.mode,
        max_points: String(maxPoints),
      });
      payload = await request(`/api/start-stop/chart?${query}`);
    }
    payload = mergeLiveComparisonChart(payload, selectedLive, maxPoints);
    if (requestId !== state.chartRequestId) return;
    if (payload.work_step_key !== state.activeWorkStepKey) {
      throw new Error("曲线工步与当前筛选不一致，请刷新页面后重试。");
    }
    state.chartData = payload;
    renderChart();
  } catch (error) {
    if (requestId !== state.chartRequestId) return;
    state.chartData = null;
    elements.chartEmpty.hidden = false;
    elements.chartEmpty.textContent = error.message;
  } finally {
    if (requestId === state.chartRequestId) state.loadingChart = false;
  }
}

function chartSeriesToShow() {
  const series = state.chartData?.series || [];
  if (isAnomalyMode()) return series;
  if (!state.onlyHighlights || !state.highlightedSeries.size) return series;
  return series.filter((item) => state.highlightedSeries.has(item.series_id));
}

function pathFromPoints(points, xScale, yScale) {
  return points.map((point, index) => {
    const x = xScale(Number(point.x));
    const y = yScale(Number(point.y));
    return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}

function markerPaths(points, xScale, yScale) {
  let normal = "";
  let abnormal = "";
  for (const point of points) {
    if (point.status !== "normal" && point.status !== "abnormal") continue;
    const x = xScale(Number(point.x));
    const y = yScale(Number(point.y));
    if (point.status === "normal") {
      normal += `M${(x - 2).toFixed(2)},${y.toFixed(2)}a2,2 0 1,0 4,0a2,2 0 1,0 -4,0 `;
    } else {
      abnormal += `M${x.toFixed(2)},${(y - 3).toFixed(2)}L${(x + 3).toFixed(2)},${y.toFixed(2)}L${x.toFixed(2)},${(y + 3).toFixed(2)}L${(x - 3).toFixed(2)},${y.toFixed(2)}Z `;
    }
  }
  return { normal, abnormal };
}

function chartDomainForSeries(series) {
  let xMin = Infinity;
  let xMax = -Infinity;
  let yMin = Infinity;
  let yMax = -Infinity;
  for (const item of series) {
    for (const point of item.points || []) {
      const x = Number(point.x);
      const y = Number(point.y);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      if (x < xMin) xMin = x;
      if (x > xMax) xMax = x;
      if (y < yMin) yMin = y;
      if (y > yMax) yMax = y;
    }
  }
  if (![xMin, xMax, yMin, yMax].every(Number.isFinite)) return null;
  if (xMin === xMax) { xMin -= 1; xMax += 1; }
  if (yMin === yMax) { yMin -= 0.1; yMax += 0.1; }
  const yPad = Math.max(
    (yMax - yMin) * 0.08,
    state.chartData?.unit === "V vs Hg/HgO" ? 0.01 : 0.5,
  );
  return { xMin, xMax, yMin: yMin - yPad, yMax: yMax + yPad };
}

function copyDomain(domain) {
  return domain ? {
    xMin: domain.xMin,
    xMax: domain.xMax,
    yMin: domain.yMin,
    yMax: domain.yMax,
  } : null;
}

function clampAxisRange(minimum, maximum, fullMinimum, fullMaximum, minimumSpan) {
  const fullSpan = fullMaximum - fullMinimum;
  let span = Math.abs(maximum - minimum);
  if (!Number.isFinite(span) || span <= 0) span = fullSpan;
  span = Math.max(Math.min(span, fullSpan), Math.min(minimumSpan, fullSpan));
  let low = Math.min(minimum, maximum);
  if (!Number.isFinite(low)) low = fullMinimum;
  let high = low + span;
  if (low < fullMinimum) {
    high += fullMinimum - low;
    low = fullMinimum;
  }
  if (high > fullMaximum) {
    low -= high - fullMaximum;
    high = fullMaximum;
  }
  return [Math.max(fullMinimum, low), Math.min(fullMaximum, high)];
}

function clampChartView(view, fullDomain) {
  if (!view || !fullDomain) return copyDomain(fullDomain);
  const fullXSpan = fullDomain.xMax - fullDomain.xMin;
  const fullYSpan = fullDomain.yMax - fullDomain.yMin;
  const cycleFloor = state.chartData?.x_axis === "cycle" ? 5 : 0;
  const minimumXSpan = Math.max(fullXSpan / 250, Math.min(cycleFloor, fullXSpan));
  const minimumYSpan = fullYSpan / 250;
  const [xMin, xMax] = clampAxisRange(
    view.xMin, view.xMax, fullDomain.xMin, fullDomain.xMax, minimumXSpan,
  );
  const [yMin, yMax] = clampAxisRange(
    view.yMin, view.yMax, fullDomain.yMin, fullDomain.yMax, minimumYSpan,
  );
  return { xMin, xMax, yMin, yMax };
}

function chartZoomLevel(fullDomain, viewDomain) {
  if (!fullDomain || !viewDomain) return 1;
  const xLevel = (fullDomain.xMax - fullDomain.xMin) / (viewDomain.xMax - viewDomain.xMin);
  const yLevel = (fullDomain.yMax - fullDomain.yMin) / (viewDomain.yMax - viewDomain.yMin);
  return Math.max(1, xLevel, yLevel);
}

function axisTickDigits(step, minimumDigits, maximumDigits = 5) {
  if (!Number.isFinite(step) || step <= 0) return minimumDigits;
  const needed = Math.max(0, -Math.floor(Math.log10(step)));
  return Math.max(minimumDigits, Math.min(maximumDigits, needed));
}

function updateZoomControls(fullDomain = state.chartGeometry?.fullDomain, viewDomain = state.chartGeometry?.viewDomain) {
  const hasData = Boolean(fullDomain && viewDomain);
  const level = hasData ? chartZoomLevel(fullDomain, viewDomain) : 1;
  elements.zoomLevel.textContent = `${Math.round(level * 100)}%`;
  elements.zoomIn.disabled = !hasData || level >= 249;
  elements.zoomOut.disabled = !hasData || level <= 1.001;
  elements.resetZoom.disabled = !hasData || level <= 1.001;
  elements.chartFrame.classList.toggle("chart-mode-inspect", state.chartInteractionMode === "inspect");
  elements.chartFrame.classList.toggle("chart-mode-zoom", state.chartInteractionMode === "zoom");
  elements.chartFrame.classList.toggle("chart-mode-pan", state.chartInteractionMode === "pan");
}

function ensureChartContextMenu() {
  if (elements.chartContextMenu) return elements.chartContextMenu;
  const menu = element("div", "chart-context-menu");
  menu.id = "chartContextMenu";
  menu.hidden = true;
  menu.tabIndex = -1;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "图表快捷菜单");

  const autoScale = element("button", "chart-context-menu-item");
  autoScale.id = "chartAutoScale";
  autoScale.type = "button";
  autoScale.setAttribute("role", "menuitem");
  const title = element("span", "chart-context-menu-title", "自动缩放");
  const detail = element("span", "chart-context-menu-detail", "恢复完整数据范围");
  autoScale.append(title, detail);
  menu.append(autoScale);
  elements.chartFrame.append(menu);
  elements.chartContextMenu = menu;
  elements.chartAutoScale = autoScale;
  return menu;
}

function closeChartContextMenu({ restoreFocus = false } = {}) {
  const menu = elements.chartContextMenu;
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  state.chartContextMenuReturnFocus = false;
  if (restoreFocus) elements.chartFrame.focus({ preventScroll: true });
}

function openChartContextMenu(event) {
  const menu = ensureChartContextMenu();
  if (state.chartGesture) finishChartGesture(null, true);
  elements.chartTooltip.hidden = true;
  menu.hidden = false;
  elements.chartAutoScale.disabled = !state.chartGeometry;

  const frameRect = elements.chartFrame.getBoundingClientRect();
  const inset = 8;
  const requestedLeft = event.clientX - frameRect.left;
  const requestedTop = event.clientY - frameRect.top;
  const maximumLeft = Math.max(inset, frameRect.width - menu.offsetWidth - inset);
  const maximumTop = Math.max(inset, frameRect.height - menu.offsetHeight - inset);
  menu.style.left = `${Math.max(inset, Math.min(maximumLeft, requestedLeft))}px`;
  menu.style.top = `${Math.max(inset, Math.min(maximumTop, requestedTop))}px`;
  state.chartContextMenuReturnFocus = document.activeElement === elements.chartFrame;
  elements.chartAutoScale.focus({ preventScroll: true });
}

function resetChartZoom(render = true) {
  state.chartView = null;
  state.chartGeometry = null;
  if (render) renderChart();
  else updateZoomControls(null, null);
}

function applyChartZoomFactor(factor, anchorX = 0.5, anchorY = 0.5, renderImmediately = true) {
  const geometry = state.chartGeometry;
  if (!geometry) return;
  const view = clampChartView(state.chartView || geometry.viewDomain, geometry.fullDomain);
  const xSpan = view.xMax - view.xMin;
  const ySpan = view.yMax - view.yMin;
  const nextXSpan = xSpan * factor;
  const nextYSpan = ySpan * factor;
  const anchorDataX = view.xMin + anchorX * xSpan;
  const anchorDataY = view.yMax - anchorY * ySpan;
  const next = {
    xMin: anchorDataX - anchorX * nextXSpan,
    xMax: anchorDataX + (1 - anchorX) * nextXSpan,
    yMin: anchorDataY - (1 - anchorY) * nextYSpan,
    yMax: anchorDataY + anchorY * nextYSpan,
  };
  state.chartView = clampChartView(next, geometry.fullDomain);
  if (renderImmediately) renderChart();
  else scheduleChartRender();
}

function zoomChartByFactor(factor, anchorX = 0.5, anchorY = 0.5) {
  applyChartZoomFactor(factor, anchorX, anchorY, true);
}

function queueChartZoomByFactor(factor, anchorX = 0.5, anchorY = 0.5) {
  applyChartZoomFactor(factor, anchorX, anchorY, false);
}

function panChartByFraction(xFraction, yFraction) {
  const geometry = state.chartGeometry;
  if (!geometry) return;
  const view = geometry.viewDomain;
  const xShift = (view.xMax - view.xMin) * xFraction;
  const yShift = (view.yMax - view.yMin) * yFraction;
  state.chartView = clampChartView({
    xMin: view.xMin + xShift,
    xMax: view.xMax + xShift,
    yMin: view.yMin + yShift,
    yMax: view.yMax + yShift,
  }, geometry.fullDomain);
  renderChart();
}

function plotRatiosForClient(clientX, clientY, geometry) {
  const rect = elements.startStopChart.getBoundingClientRect();
  const viewBoxWidth = Number(elements.startStopChart.viewBox.baseVal.width || rect.width);
  const viewBoxHeight = Number(elements.startStopChart.viewBox.baseVal.height || rect.height);
  const svgX = ((clientX - rect.left) / rect.width) * viewBoxWidth;
  const svgY = ((clientY - rect.top) / rect.height) * viewBoxHeight;
  return {
    x: Math.max(0, Math.min(1, (svgX - geometry.margin.left) / geometry.width)),
    y: Math.max(0, Math.min(1, (svgY - geometry.margin.top) / geometry.height)),
    svgX,
    svgY,
  };
}

function clientPlotRect(geometry) {
  const rect = elements.startStopChart.getBoundingClientRect();
  const viewBoxWidth = Number(elements.startStopChart.viewBox.baseVal.width || rect.width);
  const viewBoxHeight = Number(elements.startStopChart.viewBox.baseVal.height || rect.height);
  return {
    left: rect.left + (geometry.margin.left / viewBoxWidth) * rect.width,
    right: rect.left + ((geometry.margin.left + geometry.width) / viewBoxWidth) * rect.width,
    top: rect.top + (geometry.margin.top / viewBoxHeight) * rect.height,
    bottom: rect.top + ((geometry.margin.top + geometry.height) / viewBoxHeight) * rect.height,
  };
}

function updateZoomSelection(gesture, clientX, clientY) {
  const plotRect = clientPlotRect(gesture.geometry);
  const frameRect = elements.chartFrame.getBoundingClientRect();
  const startX = Math.max(plotRect.left, Math.min(plotRect.right, gesture.startClientX));
  const startY = Math.max(plotRect.top, Math.min(plotRect.bottom, gesture.startClientY));
  const endX = Math.max(plotRect.left, Math.min(plotRect.right, clientX));
  const endY = Math.max(plotRect.top, Math.min(plotRect.bottom, clientY));
  elements.zoomSelection.style.left = `${Math.min(startX, endX) - frameRect.left}px`;
  elements.zoomSelection.style.top = `${Math.min(startY, endY) - frameRect.top}px`;
  elements.zoomSelection.style.width = `${Math.abs(endX - startX)}px`;
  elements.zoomSelection.style.height = `${Math.abs(endY - startY)}px`;
  elements.zoomSelection.hidden = false;
}

function scheduleChartRender() {
  if (state.chartRenderFrame !== null) return;
  state.chartRenderFrame = window.requestAnimationFrame(() => {
    state.chartRenderFrame = null;
    renderChart();
  });
}

function handleChartPointerMove(event) {
  const gesture = state.chartGesture;
  if (!gesture || event.pointerId !== gesture.pointerId) return;
  const dx = event.clientX - gesture.startClientX;
  const dy = event.clientY - gesture.startClientY;
  gesture.lastClientX = event.clientX;
  gesture.lastClientY = event.clientY;
  if (Math.hypot(dx, dy) >= 6) gesture.moved = true;
  if (gesture.mode === "zoom") {
    updateZoomSelection(gesture, event.clientX, event.clientY);
  } else if (gesture.moved) {
    const rect = elements.startStopChart.getBoundingClientRect();
    const view = gesture.startView;
    const xShift = -(dx / rect.width) * (gesture.geometry.frameWidth / gesture.geometry.width) * (view.xMax - view.xMin);
    const yShift = (dy / rect.height) * (gesture.geometry.frameHeight / gesture.geometry.height) * (view.yMax - view.yMin);
    state.chartView = clampChartView({
      xMin: view.xMin + xShift,
      xMax: view.xMax + xShift,
      yMin: view.yMin + yShift,
      yMax: view.yMax + yShift,
    }, gesture.geometry.fullDomain);
    scheduleChartRender();
  }
  event.preventDefault();
}

function finishChartGesture(event = null, cancelled = false) {
  const gesture = state.chartGesture;
  if (!gesture || (event && event.pointerId !== gesture.pointerId)) return;
  window.removeEventListener("pointermove", handleChartPointerMove);
  window.removeEventListener("pointerup", handleChartPointerUp);
  window.removeEventListener("pointercancel", handleChartPointerCancel);
  window.removeEventListener("blur", handleChartWindowBlur);
  try {
    if (gesture.captureTarget?.hasPointerCapture(gesture.pointerId)) {
      gesture.captureTarget.releasePointerCapture(gesture.pointerId);
    }
  } catch (_error) {
    // The SVG overlay may already have been replaced during a pan redraw.
  }
  elements.chartFrame.classList.remove("is-interacting");
  elements.zoomSelection.hidden = true;

  if (cancelled) {
    if (gesture.mode === "pan") state.chartView = copyDomain(gesture.startView);
    renderChart();
  } else if (gesture.mode === "zoom" && gesture.moved) {
    const endClientX = event?.clientX ?? gesture.lastClientX;
    const endClientY = event?.clientY ?? gesture.lastClientY;
    if (Math.abs(endClientX - gesture.startClientX) >= 8 && Math.abs(endClientY - gesture.startClientY) >= 8) {
      const start = plotRatiosForClient(gesture.startClientX, gesture.startClientY, gesture.geometry);
      const end = plotRatiosForClient(endClientX, endClientY, gesture.geometry);
      const view = gesture.startView;
      const xSpan = view.xMax - view.xMin;
      const ySpan = view.yMax - view.yMin;
      const firstY = view.yMax - start.y * ySpan;
      const secondY = view.yMax - end.y * ySpan;
      state.chartView = clampChartView({
        xMin: view.xMin + Math.min(start.x, end.x) * xSpan,
        xMax: view.xMin + Math.max(start.x, end.x) * xSpan,
        yMin: Math.min(firstY, secondY),
        yMax: Math.max(firstY, secondY),
      }, gesture.geometry.fullDomain);
      renderChart();
    }
  }
  if (gesture.moved) {
    state.chartSuppressClickUntil = performance.now() + 700;
  }
  state.chartGesture = null;
}

function handleChartPointerUp(event) {
  finishChartGesture(event, false);
}

function handleChartPointerCancel(event) {
  finishChartGesture(event, true);
}

function handleChartWindowBlur() {
  finishChartGesture(null, true);
}

function beginChartGesture(event, series, geometry) {
  if (event.button !== 0 || state.chartGesture) return;
  elements.chartFrame.focus({ preventScroll: true });
  const mode = state.chartInteractionMode;
  if (mode === "inspect") return;
  const captureTarget = event.currentTarget;
  try {
    captureTarget.setPointerCapture(event.pointerId);
  } catch (_error) {
    // Window-level listeners remain the fallback when capture is unavailable.
  }
  state.chartGesture = {
    pointerId: event.pointerId,
    mode,
    moved: false,
    startClientX: event.clientX,
    startClientY: event.clientY,
    lastClientX: event.clientX,
    lastClientY: event.clientY,
    startView: copyDomain(geometry.viewDomain),
    geometry: { ...geometry, fullDomain: copyDomain(geometry.fullDomain), viewDomain: copyDomain(geometry.viewDomain) },
    series,
    captureTarget,
  };
  elements.chartTooltip.hidden = true;
  elements.chartFrame.classList.add("is-interacting");
  if (mode === "zoom") updateZoomSelection(state.chartGesture, event.clientX, event.clientY);
  window.addEventListener("pointermove", handleChartPointerMove, { passive: false });
  window.addEventListener("pointerup", handleChartPointerUp);
  window.addEventListener("pointercancel", handleChartPointerCancel);
  window.addEventListener("blur", handleChartWindowBlur);
  event.preventDefault();
}

function setChartInteractionMode(mode) {
  if (!new Set(["inspect", "zoom", "pan"]).has(mode)) return;
  if (state.chartGesture) finishChartGesture(null, true);
  state.chartInteractionMode = mode;
  elements.inspectMode.setAttribute("aria-pressed", String(mode === "inspect"));
  elements.zoomSelectMode.setAttribute("aria-pressed", String(mode === "zoom"));
  elements.panMode.setAttribute("aria-pressed", String(mode === "pan"));
  updateZoomControls();
}

function renderChart() {
  const svg = elements.startStopChart;
  svg.replaceChildren();
  elements.chartLegend.replaceChildren();
  const series = chartSeriesToShow().filter((item) => item.points?.length);
  const liveSeriesCount = new Set(
    series.filter((item) => item.live_preview).map((item) => item.series_id),
  ).size;
  elements.visibleSeriesCount.textContent = `${new Set(series.map((item) => item.series_id)).size} 条曲线${liveSeriesCount ? `，含 ${liveSeriesCount} 条正在测试` : ""}`;
  if (!series.length) {
    state.chartGeometry = null;
    updateZoomControls(null, null);
    elements.chartEmpty.hidden = false;
    elements.chartEmpty.textContent = state.loadingChart
      ? "正在读取曲线…"
      : state.liveComparisonRequested && state.livePreviewItems.size === 0
        ? "当前快照没有可计算的活动启停文件"
        : "请从左侧选择要检查的材料";
    return;
  }
  elements.chartEmpty.hidden = true;

  const frameWidth = Math.max(720, elements.chartFrame.clientWidth || 900);
  const frameHeight = Math.max(540, elements.chartFrame.clientHeight || 600);
  svg.setAttribute("viewBox", `0 0 ${frameWidth} ${frameHeight}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const margin = { top: 22, right: 26, bottom: 53, left: 70 };
  const width = frameWidth - margin.left - margin.right;
  const height = frameHeight - margin.top - margin.bottom;
  const fullDomain = chartDomainForSeries(series);
  if (!fullDomain) {
    state.chartGeometry = null;
    updateZoomControls(null, null);
    elements.chartEmpty.hidden = false;
    elements.chartEmpty.textContent = "当前曲线没有可显示的有效数值";
    return;
  }
  const viewDomain = clampChartView(state.chartView, fullDomain);
  const { xMin, xMax, yMin, yMax } = viewDomain;
  const xScale = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * width;
  const yScale = (value) => margin.top + (1 - (value - yMin) / (yMax - yMin)) * height;
  const geometry = {
    frameWidth,
    frameHeight,
    margin,
    width,
    height,
    xMin,
    xMax,
    yMin,
    yMax,
    xScale,
    yScale,
    fullDomain,
    viewDomain,
  };
  state.chartGeometry = geometry;
  updateZoomControls(fullDomain, viewDomain);
  const xTickDigits = state.chartData.x_axis === "cycle"
    ? 0
    : axisTickDigits((xMax - xMin) / 5, 1, 4);
  const yTickDigits = axisTickDigits(
    (yMax - yMin) / 5,
    state.chartData.unit === "V vs Hg/HgO" ? 2 : 1,
    state.chartData.unit === "V vs Hg/HgO" ? 5 : 3,
  );

  const clipId = "start-stop-chart-plot-clip";
  const defs = svgElement("defs");
  const clipPath = svgElement("clipPath", { id: clipId });
  clipPath.append(svgElement("rect", { x: margin.left, y: margin.top, width, height }));
  defs.append(clipPath);
  svg.append(defs);

  for (let index = 0; index <= 5; index += 1) {
    const x = margin.left + (index / 5) * width;
    const y = margin.top + (index / 5) * height;
    svg.append(
      svgElement("line", { class: "chart-grid-line", x1: x, y1: margin.top, x2: x, y2: margin.top + height }),
      svgElement("line", { class: "chart-grid-line", x1: margin.left, y1: y, x2: margin.left + width, y2: y }),
    );
    const xLabel = svgElement("text", { class: "chart-tick-label", x, y: margin.top + height + 21, "text-anchor": "middle" });
    xLabel.textContent = (xMin + (index / 5) * (xMax - xMin)).toFixed(xTickDigits);
    const yLabel = svgElement("text", { class: "chart-tick-label", x: margin.left - 10, y: y + 3, "text-anchor": "end" });
    yLabel.textContent = (yMax - (index / 5) * (yMax - yMin)).toFixed(yTickDigits);
    svg.append(xLabel, yLabel);
  }
  svg.append(
    svgElement("line", { class: "chart-axis-line", x1: margin.left, y1: margin.top + height, x2: margin.left + width, y2: margin.top + height }),
    svgElement("line", { class: "chart-axis-line", x1: margin.left, y1: margin.top, x2: margin.left, y2: margin.top + height }),
  );
  const xAxisLabel = svgElement("text", { class: "chart-axis-label", x: margin.left + width / 2, y: frameHeight - 12, "text-anchor": "middle" });
  xAxisLabel.textContent = state.chartData.x_label;
  const yAxisLabel = svgElement("text", { class: "chart-axis-label", x: 16, y: margin.top + height / 2, transform: `rotate(-90 16 ${margin.top + height / 2})`, "text-anchor": "middle" });
  yAxisLabel.textContent = `${state.chartData.metric_label} / ${state.chartData.unit}`;
  svg.append(xAxisLabel, yAxisLabel);

  const hasHighlights = !isAnomalyMode() && state.highlightedSeries.size > 0;
  const plotGroup = svgElement("g", { "clip-path": `url(#${clipId})` });
  const dimLineGroup = svgElement("g");
  const haloGroup = svgElement("g");
  const highlightedLineGroup = svgElement("g");
  const dimMarkerGroup = svgElement("g");
  const highlightedMarkerGroup = svgElement("g");
  for (const item of series) {
    const style = seriesStyle(item.series_id);
    const highlighted = state.highlightedSeries.has(item.series_id);
    const opacity = hasHighlights ? (highlighted ? 1 : 0.075) : 0.88;
    const widthValue = highlighted ? 4.5 : hasHighlights ? 1.1 : 1.35;
    const pathData = pathFromPoints(item.points, xScale, yScale);
    const path = svgElement("path", {
      class: `chart-series-path${highlighted ? " is-highlighted" : ""}`,
      d: pathData,
      stroke: style.color,
      "stroke-width": widthValue,
      opacity,
    });
    const dasharray = chartDasharray(style, item.variant, Boolean(item.live_preview));
    if (dasharray) path.setAttribute("stroke-dasharray", dasharray);
    if (highlighted) {
      const halo = svgElement("path", {
        class: "chart-highlight-halo",
        d: pathData,
      });
      if (dasharray) halo.setAttribute("stroke-dasharray", dasharray);
      haloGroup.append(halo);
      highlightedLineGroup.append(path);
    } else {
      dimLineGroup.append(path);
    }

    if (state.showMarkers && item.variant !== "water" && state.metric !== "overview") {
      const marker = markerPaths(item.points, xScale, yScale);
      const markerGroup = highlighted ? highlightedMarkerGroup : dimMarkerGroup;
      const markerOpacity = hasHighlights ? (highlighted ? 1 : 0.035) : 0.94;
      if (marker.normal) markerGroup.append(svgElement("path", { class: "anomaly-normal", d: marker.normal, opacity: markerOpacity }));
      if (marker.abnormal) markerGroup.append(svgElement("path", { class: "anomaly-abnormal", d: marker.abnormal, opacity: markerOpacity }));
    }
  }
  plotGroup.append(
    dimLineGroup,
    dimMarkerGroup,
    haloGroup,
    highlightedLineGroup,
    highlightedMarkerGroup,
  );
  svg.append(plotGroup);

  const overlay = svgElement("rect", { class: "chart-interaction-layer", x: margin.left, y: margin.top, width, height, fill: "transparent" });
  overlay.addEventListener("pointerdown", (event) => beginChartGesture(event, series, geometry));
  overlay.addEventListener("wheel", (event) => {
    if (document.activeElement !== elements.chartFrame && !event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const pointer = plotRatiosForClient(event.clientX, event.clientY, geometry);
    const factor = Math.exp(Math.max(-500, Math.min(500, event.deltaY)) * 0.0015);
    queueChartZoomByFactor(factor, pointer.x, pointer.y);
  }, { passive: false });
  overlay.addEventListener("dblclick", (event) => {
    event.preventDefault();
    resetChartZoom();
  });
  overlay.addEventListener("mousemove", (event) => {
    if (!state.chartGesture) showNearestTooltip(event, series, geometry);
  });
  overlay.addEventListener("click", (event) => {
    elements.chartFrame.focus({ preventScroll: true });
    if (performance.now() < state.chartSuppressClickUntil) {
      state.chartSuppressClickUntil = 0;
      return;
    }
    const nearest = nearestPointForEvent(event, series, geometry);
    if (nearest) toggleHighlight(nearest.item.series_id);
  });
  overlay.addEventListener("mouseleave", () => { elements.chartTooltip.hidden = true; });
  svg.append(overlay);
  renderLegend(series);
  updateChartHeading();
}

function distanceToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lengthSquared = dx * dx + dy * dy;
  if (!lengthSquared) return { distance: Math.hypot(px - x1, py - y1), ratio: 0 };
  const ratio = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lengthSquared));
  return {
    distance: Math.hypot(px - (x1 + ratio * dx), py - (y1 + ratio * dy)),
    ratio,
  };
}

function nearestPointForEvent(event, series, geometry, maximumDistance = 16) {
  const rect = elements.startStopChart.getBoundingClientRect();
  const svgX = ((event.clientX - rect.left) / rect.width) * Number(elements.startStopChart.viewBox.baseVal.width || rect.width);
  const svgY = ((event.clientY - rect.top) / rect.height) * Number(elements.startStopChart.viewBox.baseVal.height || rect.height);
  const targetX = geometry.xMin + ((svgX - geometry.margin.left) / geometry.width) * (geometry.xMax - geometry.xMin);
  let nearest = null;
  for (const item of series) {
    const points = item.points;
    if (!points?.length) continue;
    let low = 0;
    let high = points.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (Number(points[middle].x) < targetX) low = middle + 1;
      else high = middle;
    }
    const firstSegment = Math.max(0, low - 3);
    const lastSegment = Math.min(points.length - 2, low + 1);
    for (let index = firstSegment; index <= lastSegment; index += 1) {
      const first = points[index];
      const second = points[index + 1];
      const x1 = geometry.xScale(Number(first.x));
      const y1 = geometry.yScale(Number(first.y));
      const x2 = geometry.xScale(Number(second.x));
      const y2 = geometry.yScale(Number(second.y));
      if (![x1, y1, x2, y2].every(Number.isFinite)) continue;
      const candidate = distanceToSegment(svgX, svgY, x1, y1, x2, y2);
      if (!nearest || candidate.distance < nearest.distance) {
        nearest = {
          item,
          point: candidate.ratio < 0.5 ? first : second,
          distance: candidate.distance,
        };
      }
    }
    if (points.length === 1) {
      const point = points[0];
      const distance = Math.hypot(
        svgX - geometry.xScale(Number(point.x)),
        svgY - geometry.yScale(Number(point.y)),
      );
      if (!nearest || distance < nearest.distance) nearest = { item, point, distance };
    }
  }
  return nearest && nearest.distance <= maximumDistance ? nearest : null;
}

function showNearestTooltip(event, series, geometry) {
  const nearest = nearestPointForEvent(event, series, geometry);
  if (!nearest) {
    elements.chartTooltip.hidden = true;
    return;
  }
  const variant = nearest.item.live_preview
    ? nearest.item.variant === "water" ? "正在测试·水位补偿" : "正在测试·原始实测"
    : nearest.item.variant === "water" ? "水位补偿后" : "原始实测";
  const status = nearest.point.status === "normal" ? "正常" : nearest.point.status === "abnormal" ? "异常" : "—";
  const currentDensity = Number(nearest.point.current_a_cm2);
  elements.chartTooltip.textContent = [
    displayNameForSeries(nearest.item.series_id, nearest.item.name),
    `${variant} · ${state.chartData.x_label} ${formatNumber(nearest.point.x, state.chartData.x_axis === "cycle" ? 0 : 3)}`,
    `${state.chartData.metric_label} ${formatNumber(nearest.point.y, state.chartData.unit === "V vs Hg/HgO" ? 4 : 2)} ${state.chartData.unit}`,
    Number.isFinite(currentDensity) ? `电流密度 ${formatNumber(currentDensity * 1000, 2)} mA·cm⁻²` : "",
    nearest.point.cycle ? `循环 ${nearest.point.cycle} · ${status}` : status,
    nearest.point.source_file || "",
  ].filter(Boolean).join("\n");
  elements.chartTooltip.style.whiteSpace = "pre-line";
  elements.chartTooltip.style.left = `${Math.min(event.clientX - elements.chartFrame.getBoundingClientRect().left + 14, elements.chartFrame.clientWidth - 325)}px`;
  elements.chartTooltip.style.top = `${Math.max(8, event.clientY - elements.chartFrame.getBoundingClientRect().top - 50)}px`;
  elements.chartTooltip.hidden = false;
}

function renderLegend(series) {
  const unique = [];
  const seen = new Set();
  for (const item of series) {
    const key = `${item.series_id}:${item.variant}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(item);
  }
  const fragment = document.createDocumentFragment();
  for (const item of unique) {
    const highlighted = !isAnomalyMode() && state.highlightedSeries.has(item.series_id);
    const button = element("button", `legend-item${highlighted ? " highlighted" : ""}`);
    button.type = "button";
    button.disabled = isAnomalyMode();
    button.style.setProperty("--legend-highlight-color", seriesStyle(item.series_id).color);
    button.setAttribute("aria-pressed", String(highlighted));
    button.addEventListener("click", () => toggleHighlight(item.series_id));
    const swatch = createSwatch(item.series_id, item.variant, Boolean(item.live_preview));
    const variantLabel = item.live_preview
      ? item.variant === "water" ? "（正在测试·补偿）" : "（正在测试·原始）"
      : state.mode === "compare" ? item.variant === "water" ? "（补偿）" : "（原始）" : "";
    const label = element("span", "", `${displayNameForSeries(item.series_id, item.name)}${variantLabel}`);
    button.append(swatch, label);
    fragment.append(button);
  }
  elements.chartLegend.replaceChildren(fragment);
}

function updateChartHeading() {
  const labels = {
    cathodic: ["阴极段稳态电位", "方波启停取阶段最后 1 s 中位数；ADT 取末点"],
    negative_shift: ["阴极电位负移", "以各序列前 10 循环为基准，正值表示变得更负"],
    reverse: ["恢复段稳态电位", "按各材料实测恢复阶段末端统计"],
    minimum_time: ["阴极偏移异常判断", "绿色为最低点位于后 15 s，红色为最低点位于前 15 s"],
    overview: ["全程电位–时间曲线", "同一材料文件按时间接续，特殊文件名依规则分段"],
  };
  let [title, subtitle] = labels[state.metric] || labels.cathodic;
  const workStep = activeWorkStep();
  if (workStep) subtitle = `${workStep.work_step_label} · ${subtitle}`;
  const liveCount = liveItemsForActiveStep().length;
  if (liveCount) subtitle = `${subtitle} · 已按正式规则接续 ${liveCount} 条正在测试曲线`;
  if (isAnomalyMode() && state.anomalyMaterialKey) {
    const material = state.materials.find((item) => item.key === state.anomalyMaterialKey);
    if (material) subtitle = `${material.plot_name} · ${subtitle}`;
  }
  elements.chartTitle.textContent = title;
  elements.chartSubtitle.textContent = subtitle;
}

function wireEvents() {
  ensureChartContextMenu();
  if (!elements.chartInteractionHint.textContent.includes("右键")) {
    elements.chartInteractionHint.textContent = `${elements.chartInteractionHint.textContent.trim()} · 右键图表可自动缩放`;
  }
  elements.materialSearch.addEventListener("input", renderMaterialList);
  elements.startStopStepFilter.addEventListener("change", () => {
    state.activeWorkStepKey = elements.startStopStepFilter.value;
    state.anomalyMaterialKey = "";
    selectIncludedSeriesForActiveWorkStep();
    ensureAnomalyMaterial();
    updateAnalysisModeControls();
    updateChartHeading();
    renderMaterialList();
    scheduleLiveComparisonPoll();
    loadChart();
  });
  elements.materialFilter.addEventListener("change", renderMaterialList);
  elements.materialSort.addEventListener("change", renderMaterialList);
  document.querySelectorAll("[data-process-filter], [data-element-filter]").forEach((input) => {
    input.addEventListener("change", renderMaterialList);
  });
  elements.clearMaterialFilters.addEventListener("click", () => {
    elements.materialSearch.value = "";
    elements.materialFilter.value = "all";
    document.querySelectorAll("[data-process-filter], [data-element-filter]").forEach((input) => {
      input.checked = false;
    });
    renderMaterialList();
    elements.materialSearch.focus();
  });
  elements.selectIncludedForChart.addEventListener("click", () => {
    if (isAnomalyMode()) return;
    selectIncludedSeriesForActiveWorkStep();
    renderMaterialList();
    loadChart();
  });
  elements.selectAllForChart.addEventListener("click", () => {
    if (isAnomalyMode()) return;
    for (const material of visibleMaterialsForSelection()) {
      for (const series of seriesForMaterial(material.key)) {
        state.selectedSeries.add(series.series_id);
      }
    }
    enforceSelectionLimit();
    renderMaterialList();
    loadChart();
  });
  elements.clearAllForChart.addEventListener("click", () => {
    if (isAnomalyMode()) {
      state.anomalyMaterialKey = "";
      renderMaterialList();
      loadChart();
      return;
    }
    state.selectedSeries.clear();
    state.highlightedSeries.clear();
    renderMaterialList();
    loadChart();
  });
  elements.clearHighlights.addEventListener("click", () => {
    state.highlightedSeries.clear();
    renderMaterialList();
    renderChart();
  });
  elements.showOnlyHighlights.addEventListener("click", () => {
    state.onlyHighlights = !state.onlyHighlights;
    elements.showOnlyHighlights.setAttribute("aria-pressed", String(state.onlyHighlights));
    resetChartZoom(false);
    renderChart();
  });
  elements.inspectMode.addEventListener("click", () => setChartInteractionMode("inspect"));
  elements.zoomSelectMode.addEventListener("click", () => setChartInteractionMode("zoom"));
  elements.panMode.addEventListener("click", () => setChartInteractionMode("pan"));
  elements.zoomIn.addEventListener("click", () => zoomChartByFactor(2 / 3));
  elements.zoomOut.addEventListener("click", () => zoomChartByFactor(1.5));
  elements.resetZoom.addEventListener("click", () => resetChartZoom());
  elements.chartFrame.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    if (event.target.closest(".chart-context-menu")) return;
    openChartContextMenu(event);
  });
  elements.chartAutoScale.addEventListener("click", () => {
    closeChartContextMenu({ restoreFocus: true });
    resetChartZoom();
  });
  elements.chartContextMenu.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeChartContextMenu({ restoreFocus: true });
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      elements.chartAutoScale.focus({ preventScroll: true });
    }
  });
  elements.chartFrame.addEventListener("keydown", (event) => {
    const smallStep = event.shiftKey ? 0.025 : 0.1;
    if (event.key === "+" || event.key === "=") zoomChartByFactor(2 / 3);
    else if (event.key === "-" || event.key === "_") zoomChartByFactor(1.5);
    else if (event.key === "ArrowLeft") panChartByFraction(-smallStep, 0);
    else if (event.key === "ArrowRight") panChartByFraction(smallStep, 0);
    else if (event.key === "ArrowUp") panChartByFraction(0, smallStep);
    else if (event.key === "ArrowDown") panChartByFraction(0, -smallStep);
    else if (event.key === "0" || event.key === "Home") resetChartZoom();
    else if (event.key.toLowerCase() === "z") setChartInteractionMode("zoom");
    else if (event.key.toLowerCase() === "p") setChartInteractionMode("pan");
    else if (event.key === "Escape" && state.chartGesture) finishChartGesture(null, true);
    else return;
    event.preventDefault();
  });
  document.querySelectorAll(".chart-tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.metric = button.dataset.metric;
      document.querySelectorAll(".chart-tab").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      if (state.metric === "overview") {
        state.xAxis = "time";
        elements.xAxisMode.value = "time";
        elements.xAxisMode.disabled = true;
      } else {
        elements.xAxisMode.disabled = false;
      }
      updateAnalysisModeControls();
      updateChartHeading();
      renderMaterialList();
      loadChart();
    });
  });
  elements.compensationMode.addEventListener("change", () => {
    state.mode = elements.compensationMode.value;
    loadChart();
  });
  elements.xAxisMode.addEventListener("change", () => {
    state.xAxis = elements.xAxisMode.value;
    loadChart();
  });
  elements.showAnomalyMarkers.addEventListener("change", () => {
    state.showMarkers = elements.showAnomalyMarkers.checked;
    renderChart();
  });
  elements.refreshLiveComparison.addEventListener("click", () => {
    refreshLiveComparison();
  });
  elements.printCurrentView.addEventListener("click", () => {
    window.print();
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".chart-context-menu")) closeChartContextMenu();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest(".chart-context-menu")) closeChartContextMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || elements.chartContextMenu.hidden) return;
    event.preventDefault();
    closeChartContextMenu({ restoreFocus: state.chartContextMenuReturnFocus });
  });
  window.addEventListener("resize", () => {
    closeChartContextMenu();
    window.clearTimeout(window.__startStopResizeTimer);
    window.__startStopResizeTimer = window.setTimeout(renderChart, 120);
  });
}

async function initialize() {
  wireEvents();
  try {
    await loadWorkspaceData();
  } catch (error) {
    setNotice(error.message, "error");
    elements.materialList.replaceChildren(
      element("div", "start-stop-loading", "无法读取启停材料数据"),
    );
  }
}

initialize();
