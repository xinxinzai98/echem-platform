const CV_EIS_SVG_NS = "http://www.w3.org/2000/svg";

const cvEisState = {
  status: null,
  catalog: null,
  materials: [],
  favorites: new Set(),
  selectedId: "",
  curve: null,
  loadingCurve: false,
  chartView: null,
  baseDomain: null,
  geometry: null,
  drag: null,
};

const cvEisElements = Object.fromEntries(
  [
    "cvEisWorkbenchBrand", "workbenchVersion", "sidebarCvEisState", "cvEisNotice",
    "reloadCvEis", "printCvEis", "cvEisCvCount", "cvEisEisCount",
    "cvEisPairedCount", "cvEisReadyCount", "cvEisAttentionCount",
    "cvEisVisibleCount", "cvEisSearch", "cvEisStatusFilter", "cvEisSort",
    "cvEisMaterialList", "cvEisChartTitle", "cvEisChartSubtitle", "cvEisRsChip",
    "cvEisQualityChip", "showCvRaw", "showCvIr", "cvEisTarget",
    "cvEisZoomOut", "cvEisZoomIn", "cvEisAutoScale", "cvEisZoomLevel",
    "cvEisChartFrame", "cvEisChart", "cvEisChartEmpty", "cvEisTooltip",
    "cvEisZoomSelection", "cvEisContextMenu", "cvEisContextAutoScale",
    "cvEisOverpotentialCards", "cvEisCvSource", "cvEisEisSource",
    "cvEisPairing", "cvEisRsEvidence", "cvEisBasis", "cvEisWarnings",
  ].map((id) => [id, document.querySelector(`#${id}`)]),
);

class CvEisRequestError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function cvEisRequest(url) {
  const response = await fetch(url, { cache: "no-store" });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new CvEisRequestError(payload?.error || `请求失败（${response.status}）`, response.status);
  }
  return payload;
}

function cvEisElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function cvEisSvg(tag, attributes = {}) {
  const node = document.createElementNS(CV_EIS_SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  return node;
}

function formatCvEisInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatCvEisNumber(value, digits = 2) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
}

function setCvEisNotice(message = "", type = "warning") {
  cvEisElements.cvEisNotice.hidden = !message;
  cvEisElements.cvEisNotice.classList.toggle("error", type === "error");
  cvEisElements.cvEisNotice.classList.toggle("success", type === "success");
  cvEisElements.cvEisNotice.textContent = message;
}

function applyCvEisDeploymentProfile(payload) {
  document.querySelectorAll("[data-config-route]").forEach((link) => { link.href = "/start-stop"; });
  document.querySelectorAll("[data-workstations-route]").forEach((link) => { link.href = "/start-stop/workstations"; });
  document.querySelectorAll("[data-analysis-route]").forEach((link) => { link.href = "/start-stop/analysis"; });
  document.querySelectorAll("[data-cv-eis-route]").forEach((link) => { link.href = "/start-stop/cv-eis"; });
  document.querySelectorAll("[data-materials-route]").forEach((link) => { link.href = "/start-stop/materials"; });
  cvEisElements.cvEisWorkbenchBrand.href = "/start-stop";
  const serviceVersion = payload?.service?.version;
  cvEisElements.workbenchVersion.textContent = serviceVersion && serviceVersion !== "unknown"
    ? `版本 ${serviceVersion}`
    : "版本未知";
  cvEisElements.workbenchVersion.title = payload?.service?.image_reference || "当前运行服务版本";
}

function currentCvEisRecord() {
  return (cvEisState.catalog?.materials || []).find((item) => item.analysis_id === cvEisState.selectedId) || null;
}

function etaFor(record, target = 10) {
  return (record?.overpotentials || []).find((item) => Number(item.target_ma_cm2) === Number(target)) || null;
}

function recordIsFavorite(record) {
  return cvEisState.favorites.has(record.material_key);
}

function visibleCvEisRecords() {
  const query = cvEisElements.cvEisSearch.value.trim().toLowerCase();
  const filter = cvEisElements.cvEisStatusFilter.value;
  let rows = (cvEisState.catalog?.materials || []).filter((record) => {
    const haystack = [
      record.display_name,
      record.stage_label,
      record.material_key,
      record.cv?.name,
      record.eis?.name,
      ...(record.warnings || []),
    ].filter(Boolean).join(" ").toLowerCase();
    const matchesQuery = !query || haystack.includes(query);
    const matchesFilter = filter === "all"
      || record.status === filter
      || (filter === "attention" && !["ready", "area_required"].includes(record.status));
    return matchesQuery && matchesFilter;
  });
  const mode = cvEisElements.cvEisSort.value;
  rows = rows.map((record, index) => ({ record, index }));
  rows.sort((left, right) => {
    if (mode === "favorite") {
      const favoriteOrder = Number(recordIsFavorite(right.record)) - Number(recordIsFavorite(left.record));
      if (favoriteOrder) return favoriteOrder;
      const statusOrder = Number(right.record.status === "ready") - Number(left.record.status === "ready");
      if (statusOrder) return statusOrder;
    } else if (mode === "eta") {
      const selectedTarget = Number(cvEisElements.cvEisTarget.value);
      const leftEta = etaFor(left.record, selectedTarget)?.ir90_eta_mv;
      const rightEta = etaFor(right.record, selectedTarget)?.ir90_eta_mv;
      if (Number.isFinite(Number(leftEta)) && Number.isFinite(Number(rightEta)) && leftEta !== rightEta) {
        return leftEta - rightEta;
      }
      if (Number.isFinite(Number(leftEta))) return -1;
      if (Number.isFinite(Number(rightEta))) return 1;
    } else if (mode === "rs") {
      const leftRs = Number(left.record.rs?.rs);
      const rightRs = Number(right.record.rs?.rs);
      if (Number.isFinite(leftRs) && Number.isFinite(rightRs) && leftRs !== rightRs) return leftRs - rightRs;
      if (Number.isFinite(leftRs)) return -1;
      if (Number.isFinite(rightRs)) return 1;
    } else if (mode === "name") {
      const order = String(left.record.display_name).localeCompare(String(right.record.display_name), "zh-CN", { numeric: true });
      if (order) return order;
    }
    return left.index - right.index;
  });
  return rows.map(({ record }) => record);
}

function renderCvEisMetrics() {
  const counts = cvEisState.catalog?.counts || {};
  cvEisElements.cvEisCvCount.textContent = formatCvEisInteger(counts.recognized_cv);
  cvEisElements.cvEisEisCount.textContent = formatCvEisInteger(counts.recognized_eis);
  cvEisElements.cvEisPairedCount.textContent = formatCvEisInteger(counts.paired);
  cvEisElements.cvEisReadyCount.textContent = formatCvEisInteger(counts.ready);
  cvEisElements.cvEisAttentionCount.textContent = formatCvEisInteger(counts.attention);
}

function renderCvEisMaterialList() {
  const rows = visibleCvEisRecords();
  const fragment = document.createDocumentFragment();
  for (const record of rows) {
    const selectable = ["ready", "area_required", "range_insufficient"].includes(record.status);
    const button = cvEisElement("button", "cv-eis-material-card");
    button.type = "button";
    button.disabled = !selectable;
    button.dataset.analysisId = record.analysis_id;
    button.setAttribute("aria-pressed", String(record.analysis_id === cvEisState.selectedId));
    button.setAttribute("aria-label", `${record.display_name} ${record.stage_label} ${record.status_label}`);

    const heading = cvEisElement("div", "cv-eis-material-card-heading");
    const name = cvEisElement("strong", "", record.display_name || record.material_key);
    name.title = record.display_name || record.material_key;
    heading.append(name);
    if (recordIsFavorite(record)) heading.append(cvEisElement("span", "cv-eis-favorite", "★"));
    heading.append(cvEisElement("span", `cv-eis-status ${record.status}`, record.status_label));

    const meta = cvEisElement("div", "cv-eis-material-card-meta");
    meta.append(
      cvEisElement("span", "", record.stage_label || "测试"),
      cvEisElement("span", "", record.rs ? `Rs ${formatCvEisNumber(record.rs.rs, 4)} ${record.rs.unit}` : "Rs —"),
    );
    const values = cvEisElement("div", "cv-eis-material-card-values");
    const selectedTarget = Number(cvEisElements.cvEisTarget.value);
    const selectedEta = etaFor(record, selectedTarget);
    values.append(
      cvEisElement("span", "", selectedEta ? `η${selectedTarget}（90% iR）` : "CV / EIS"),
      cvEisElement("strong", "", selectedEta ? `${formatCvEisNumber(selectedEta.ir90_eta_mv, 1)} mV` : `${record.cv?.name || "—"} / ${record.eis?.name || "—"}`),
    );
    const path = cvEisElement("div", "cv-eis-material-card-path", record.material_key || "");
    path.title = record.material_key || "";
    button.append(heading, meta, values, path);
    if (selectable) button.addEventListener("click", () => selectCvEisRecord(record.analysis_id));
    fragment.append(button);
  }
  cvEisElements.cvEisMaterialList.replaceChildren(
    fragment.childNodes.length ? fragment : cvEisElement("div", "start-stop-loading", "没有符合条件的 CV/EIS 材料"),
  );
  cvEisElements.cvEisMaterialList.setAttribute("aria-busy", "false");
  cvEisElements.cvEisVisibleCount.textContent = `${rows.length} / ${(cvEisState.catalog?.materials || []).length}`;
}

function evidenceValue(path, sha256) {
  if (!path) return "—";
  return `${path} · SHA-256 ${String(sha256 || "").slice(0, 12)}…`;
}

function renderCvEisEvidence(record) {
  if (!record) {
    for (const key of ["cvEisCvSource", "cvEisEisSource", "cvEisPairing", "cvEisRsEvidence", "cvEisBasis", "cvEisWarnings"]) {
      cvEisElements[key].textContent = "—";
    }
    return;
  }
  cvEisElements.cvEisCvSource.textContent = evidenceValue(record.cv?.repository_path, record.cv?.sha256);
  cvEisElements.cvEisEisSource.textContent = evidenceValue(record.eis?.repository_path, record.eis?.sha256);
  cvEisElements.cvEisPairing.textContent = record.eis
    ? `${record.stage_label} · 同材料目录 + 阶段名/文件名配对`
    : "没有可配对的 EIS";
  cvEisElements.cvEisRsEvidence.textContent = record.rs
    ? `${formatCvEisNumber(record.rs.rs, 6)} ${record.rs.unit} · ${formatCvEisNumber(record.rs.crossing_frequency_hz, 1)} Hz 高频交点`
    : "—";
  cvEisElements.cvEisBasis.textContent = record.current_basis === "density"
    ? `CV ${record.current_unit}；EIS ${record.rs?.unit || "—"}；Hg/HgO + 0.9268 V；90% iR`
    : `CV ${record.current_unit || "绝对电流"}；缺少面积时不报告电流密度过电位`;
  cvEisElements.cvEisWarnings.textContent = (record.warnings || []).join("；") || "质量门槛通过";
}

function renderCvEisOverpotentials(record) {
  const rows = record?.overpotentials || [];
  if (!rows.length) {
    const message = record?.status === "area_required"
      ? "该文件使用绝对电流且未提供几何面积，暂不报告 mA/cm² 过电位。"
      : "当前没有可安全报告的目标电流密度结果。";
    cvEisElements.cvEisOverpotentialCards.replaceChildren(cvEisElement("p", "", message));
    return;
  }
  const selectedTarget = Number(cvEisElements.cvEisTarget.value);
  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const card = cvEisElement("article", Number(row.target_ma_cm2) === selectedTarget ? "selected" : "");
    card.append(
      cvEisElement("span", "", `η${row.target_ma_cm2}`),
      cvEisElement("strong", "", `${formatCvEisNumber(row.ir90_eta_mv, 1)} mV`),
      cvEisElement("small", "", `原始 ${formatCvEisNumber(row.raw_eta_mv, 1)} mV`),
    );
    fragment.append(card);
  }
  cvEisElements.cvEisOverpotentialCards.replaceChildren(fragment);
}

function updateCvEisSelectionHeader(record) {
  if (!record) {
    cvEisElements.cvEisChartTitle.textContent = "请选择材料";
    cvEisElements.cvEisChartSubtitle.textContent = "原始与 90% iR 补偿后的 CV 回扫";
    cvEisElements.cvEisRsChip.textContent = "Rs —";
    cvEisElements.cvEisQualityChip.textContent = "尚未选择";
    cvEisElements.cvEisTarget.disabled = true;
    return;
  }
  cvEisElements.cvEisChartTitle.textContent = `${record.display_name} · ${record.stage_label}`;
  cvEisElements.cvEisChartSubtitle.textContent = `${record.cv?.name || "CV"} + ${record.eis?.name || "EIS"} · 最后一次阴极转折后的回扫`;
  cvEisElements.cvEisRsChip.textContent = record.rs
    ? `Rs ${formatCvEisNumber(record.rs.rs, 4)} ${record.rs.unit}`
    : "Rs —";
  cvEisElements.cvEisQualityChip.textContent = record.status_label || "待检查";
  cvEisElements.cvEisTarget.disabled = record.current_basis !== "density";
}

async function selectCvEisRecord(analysisId) {
  if (!analysisId || cvEisState.loadingCurve) return;
  cvEisState.selectedId = analysisId;
  cvEisState.curve = null;
  cvEisState.chartView = null;
  renderCvEisMaterialList();
  const record = currentCvEisRecord();
  updateCvEisSelectionHeader(record);
  renderCvEisEvidence(record);
  renderCvEisOverpotentials(record);
  renderCvEisChart();
  cvEisState.loadingCurve = true;
  cvEisElements.cvEisChartEmpty.hidden = false;
  cvEisElements.cvEisChartEmpty.textContent = "正在读取并校验 CV 曲线…";
  try {
    cvEisState.curve = await cvEisRequest(`/api/start-stop/cv-eis/curve?analysis_id=${encodeURIComponent(analysisId)}`);
    cvEisState.chartView = null;
    renderCvEisChart();
  } catch (error) {
    setCvEisNotice(error.message, "error");
    cvEisElements.cvEisChartEmpty.textContent = "无法读取该材料的 CV 曲线";
  } finally {
    cvEisState.loadingCurve = false;
  }
}

function chartSeries() {
  const points = cvEisState.curve?.points || [];
  const result = [];
  if (cvEisElements.showCvRaw.checked) {
    result.push({ key: "raw", label: "原始回扫", x: "raw_e_rhe_v", color: "#667085", dash: "8 5" });
  }
  if (cvEisElements.showCvIr.checked) {
    result.push({ key: "ir", label: "90% iR 补偿", x: "ir90_e_rhe_v", color: "#175cd3", dash: "" });
  }
  return result.map((series) => ({ ...series, points }));
}

function paddedDomain(minimum, maximum, ratio = 0.06) {
  const span = Math.max(1e-9, maximum - minimum);
  return [minimum - span * ratio, maximum + span * ratio];
}

function baseChartDomain(series) {
  const xValues = series.flatMap((item) => item.points.map((point) => Number(point[item.x]))).filter(Number.isFinite);
  const yValues = series.flatMap((item) => item.points.map((point) => Number(point.current))).filter(Number.isFinite);
  if (!xValues.length || !yValues.length) return null;
  const [xMin, xMax] = paddedDomain(Math.min(...xValues), Math.max(...xValues));
  const [yMin, yMax] = paddedDomain(Math.min(...yValues), Math.max(...yValues));
  return { xMin, xMax, yMin, yMax };
}

function tickValues(minimum, maximum, count = 6) {
  return Array.from({ length: count }, (_, index) => minimum + (maximum - minimum) * index / (count - 1));
}

function renderCvEisChart() {
  const series = chartSeries();
  cvEisElements.cvEisChart.replaceChildren();
  if (!cvEisState.curve || !series.length || !series.some((item) => item.points.length)) {
    cvEisElements.cvEisChartEmpty.hidden = false;
    if (!cvEisState.loadingCurve) cvEisElements.cvEisChartEmpty.textContent = cvEisState.selectedId
      ? "请至少选择一条曲线"
      : "请从左侧选择可绘制的材料";
    cvEisState.geometry = null;
    return;
  }
  cvEisElements.cvEisChartEmpty.hidden = true;
  const width = 1040;
  const height = 570;
  const margin = { left: 82, right: 28, top: 24, bottom: 66 };
  const plot = { left: margin.left, top: margin.top, width: width - margin.left - margin.right, height: height - margin.top - margin.bottom };
  cvEisElements.cvEisChart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  cvEisState.baseDomain = baseChartDomain(series);
  const domain = cvEisState.chartView || cvEisState.baseDomain;
  if (!domain) return;
  const xScale = (value) => plot.left + (value - domain.xMin) / (domain.xMax - domain.xMin) * plot.width;
  const yScale = (value) => plot.top + (domain.yMax - value) / (domain.yMax - domain.yMin) * plot.height;
  cvEisState.geometry = { width, height, plot, domain, xScale, yScale };

  const definitions = cvEisSvg("defs");
  const clip = cvEisSvg("clipPath", { id: "cv-eis-plot-clip" });
  clip.append(cvEisSvg("rect", { x: plot.left, y: plot.top, width: plot.width, height: plot.height }));
  definitions.append(clip);
  cvEisElements.cvEisChart.append(definitions);
  const grid = cvEisSvg("g", { class: "cv-eis-grid" });
  for (const tick of tickValues(domain.xMin, domain.xMax)) {
    const x = xScale(tick);
    grid.append(cvEisSvg("line", { x1: x, y1: plot.top, x2: x, y2: plot.top + plot.height, stroke: "#edf0f2" }));
    const label = cvEisSvg("text", { x, y: plot.top + plot.height + 25, "text-anchor": "middle", fill: "#52606a", "font-size": 12 });
    label.textContent = tick.toFixed(2);
    grid.append(label);
  }
  for (const tick of tickValues(domain.yMin, domain.yMax)) {
    const y = yScale(tick);
    grid.append(cvEisSvg("line", { x1: plot.left, y1: y, x2: plot.left + plot.width, y2: y, stroke: "#edf0f2" }));
    const label = cvEisSvg("text", { x: plot.left - 12, y: y + 4, "text-anchor": "end", fill: "#52606a", "font-size": 12 });
    label.textContent = tick.toFixed(0);
    grid.append(label);
  }
  grid.append(
    cvEisSvg("line", { x1: plot.left, y1: plot.top + plot.height, x2: plot.left + plot.width, y2: plot.top + plot.height, stroke: "#344054", "stroke-width": 1.4 }),
    cvEisSvg("line", { x1: plot.left, y1: plot.top, x2: plot.left, y2: plot.top + plot.height, stroke: "#344054", "stroke-width": 1.4 }),
  );
  const xTitle = cvEisSvg("text", { x: plot.left + plot.width / 2, y: height - 16, "text-anchor": "middle", fill: "#344054", "font-size": 14, "font-weight": 750 });
  xTitle.textContent = "E vs RHE (V)";
  const yTitle = cvEisSvg("text", { x: 18, y: plot.top + plot.height / 2, transform: `rotate(-90 18 ${plot.top + plot.height / 2})`, "text-anchor": "middle", fill: "#344054", "font-size": 14, "font-weight": 750 });
  yTitle.textContent = `阴极电流 (${cvEisState.curve.current_unit})`;
  grid.append(xTitle, yTitle);
  cvEisElements.cvEisChart.append(grid);

  const plotGroup = cvEisSvg("g", { "clip-path": "url(#cv-eis-plot-clip)" });
  for (const item of series) {
    const commands = item.points.map((point, index) => `${index ? "L" : "M"}${xScale(Number(point[item.x])).toFixed(2)},${yScale(Number(point.current)).toFixed(2)}`).join(" ");
    const path = cvEisSvg("path", { d: commands, fill: "none", stroke: item.color, "stroke-width": item.key === "ir" ? 3 : 2.4, "stroke-linejoin": "round", "stroke-linecap": "round" });
    if (item.dash) path.setAttribute("stroke-dasharray", item.dash);
    plotGroup.append(path);
  }
  const target = Number(cvEisElements.cvEisTarget.value);
  if (cvEisState.curve.current_unit === "mA/cm²" && domain.yMin <= -target && -target <= domain.yMax) {
    const y = yScale(-target);
    plotGroup.append(cvEisSvg("line", { x1: plot.left, y1: y, x2: plot.left + plot.width, y2: y, stroke: "#b7791f", "stroke-width": 1.8, "stroke-dasharray": "3 4" }));
    const eta = (cvEisState.curve.overpotentials || []).find((item) => Number(item.target_ma_cm2) === target);
    if (eta) {
      for (const [key, color] of [["raw_eta_mv", "#667085"], ["ir90_eta_mv", "#175cd3"]]) {
        const x = xScale(-Number(eta[key]) / 1000);
        plotGroup.append(cvEisSvg("circle", { cx: x, cy: y, r: 5, fill: "#fff", stroke: color, "stroke-width": 2.5 }));
      }
    }
  }
  cvEisElements.cvEisChart.append(plotGroup);
  updateCvEisZoomControls();
}

function updateCvEisZoomControls() {
  const zoomed = Boolean(cvEisState.chartView && cvEisState.baseDomain);
  cvEisElements.cvEisAutoScale.disabled = !zoomed;
  if (!zoomed) {
    cvEisElements.cvEisZoomLevel.textContent = "100%";
    return;
  }
  const base = cvEisState.baseDomain;
  const view = cvEisState.chartView;
  const xRatio = (base.xMax - base.xMin) / (view.xMax - view.xMin);
  const yRatio = (base.yMax - base.yMin) / (view.yMax - view.yMin);
  cvEisElements.cvEisZoomLevel.textContent = `${Math.round(Math.sqrt(xRatio * yRatio) * 100)}%`;
}

function resetCvEisZoom() {
  cvEisState.chartView = null;
  closeCvEisContextMenu();
  renderCvEisChart();
}

function zoomCvEis(factor, centerX = 0.5, centerY = 0.5) {
  const source = cvEisState.chartView || cvEisState.baseDomain;
  if (!source) return;
  const xCenter = source.xMin + (source.xMax - source.xMin) * centerX;
  const yCenter = source.yMax - (source.yMax - source.yMin) * centerY;
  const xSpan = (source.xMax - source.xMin) * factor;
  const ySpan = (source.yMax - source.yMin) * factor;
  cvEisState.chartView = {
    xMin: xCenter - xSpan * centerX,
    xMax: xCenter + xSpan * (1 - centerX),
    yMin: yCenter - ySpan * (1 - centerY),
    yMax: yCenter + ySpan * centerY,
  };
  renderCvEisChart();
}

function frameRatios(event) {
  const geometry = cvEisState.geometry;
  const rect = cvEisElements.cvEisChart.getBoundingClientRect();
  if (!geometry || !rect.width || !rect.height) return null;
  const svgX = (event.clientX - rect.left) / rect.width * geometry.width;
  const svgY = (event.clientY - rect.top) / rect.height * geometry.height;
  return {
    x: Math.max(0, Math.min(1, (svgX - geometry.plot.left) / geometry.plot.width)),
    y: Math.max(0, Math.min(1, (svgY - geometry.plot.top) / geometry.plot.height)),
    svgX,
    svgY,
  };
}

function beginCvEisDrag(event) {
  if (event.button !== 0 || !cvEisState.geometry) return;
  const ratios = frameRatios(event);
  if (!ratios || ratios.x <= 0 || ratios.x >= 1 || ratios.y <= 0 || ratios.y >= 1) return;
  cvEisState.drag = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, endX: event.clientX, endY: event.clientY };
  cvEisElements.cvEisChartFrame.setPointerCapture?.(event.pointerId);
  event.preventDefault();
}

function moveCvEisDrag(event) {
  if (!cvEisState.drag || event.pointerId !== cvEisState.drag.pointerId) return;
  cvEisState.drag.endX = event.clientX;
  cvEisState.drag.endY = event.clientY;
  const frame = cvEisElements.cvEisChartFrame.getBoundingClientRect();
  const left = Math.max(0, Math.min(cvEisState.drag.startX, event.clientX) - frame.left);
  const top = Math.max(0, Math.min(cvEisState.drag.startY, event.clientY) - frame.top);
  const width = Math.min(frame.width - left, Math.abs(event.clientX - cvEisState.drag.startX));
  const height = Math.min(frame.height - top, Math.abs(event.clientY - cvEisState.drag.startY));
  Object.assign(cvEisElements.cvEisZoomSelection.style, { left: `${left}px`, top: `${top}px`, width: `${width}px`, height: `${height}px` });
  cvEisElements.cvEisZoomSelection.hidden = width < 4 || height < 4;
  event.preventDefault();
}

function finishCvEisDrag(event) {
  const drag = cvEisState.drag;
  if (!drag || (event && event.pointerId !== drag.pointerId)) return;
  cvEisState.drag = null;
  cvEisElements.cvEisZoomSelection.hidden = true;
  if (Math.abs(drag.endX - drag.startX) < 10 || Math.abs(drag.endY - drag.startY) < 10) return;
  const first = frameRatios({ clientX: drag.startX, clientY: drag.startY });
  const second = frameRatios({ clientX: drag.endX, clientY: drag.endY });
  const source = cvEisState.chartView || cvEisState.baseDomain;
  if (!first || !second || !source) return;
  const x1 = source.xMin + first.x * (source.xMax - source.xMin);
  const x2 = source.xMin + second.x * (source.xMax - source.xMin);
  const y1 = source.yMax - first.y * (source.yMax - source.yMin);
  const y2 = source.yMax - second.y * (source.yMax - source.yMin);
  cvEisState.chartView = { xMin: Math.min(x1, x2), xMax: Math.max(x1, x2), yMin: Math.min(y1, y2), yMax: Math.max(y1, y2) };
  renderCvEisChart();
}

function showCvEisTooltip(event) {
  if (cvEisState.drag || !cvEisState.geometry || !cvEisState.curve?.points?.length) return;
  const ratios = frameRatios(event);
  if (!ratios) return;
  const geometry = cvEisState.geometry;
  let nearest = null;
  for (const point of cvEisState.curve.points) {
    for (const key of [cvEisElements.showCvRaw.checked ? "raw_e_rhe_v" : "", cvEisElements.showCvIr.checked ? "ir90_e_rhe_v" : ""].filter(Boolean)) {
      const x = geometry.xScale(Number(point[key]));
      const y = geometry.yScale(Number(point.current));
      const distance = Math.hypot(x - ratios.svgX, y - ratios.svgY);
      if (!nearest || distance < nearest.distance) nearest = { point, key, distance };
    }
  }
  if (!nearest || nearest.distance > 28) {
    cvEisElements.cvEisTooltip.hidden = true;
    return;
  }
  const label = nearest.key === "raw_e_rhe_v" ? "原始" : "90% iR";
  cvEisElements.cvEisTooltip.textContent = `${label} · E ${formatCvEisNumber(nearest.point[nearest.key], 4)} V vs RHE · 电流 ${formatCvEisNumber(nearest.point.current, 2)} ${cvEisState.curve.current_unit}`;
  const frame = cvEisElements.cvEisChartFrame.getBoundingClientRect();
  cvEisElements.cvEisTooltip.style.left = `${Math.min(frame.width - 320, Math.max(8, event.clientX - frame.left + 14))}px`;
  cvEisElements.cvEisTooltip.style.top = `${Math.max(8, event.clientY - frame.top - 46)}px`;
  cvEisElements.cvEisTooltip.hidden = false;
}

function openCvEisContextMenu(event) {
  event.preventDefault();
  const frame = cvEisElements.cvEisChartFrame.getBoundingClientRect();
  cvEisElements.cvEisContextMenu.style.left = `${Math.min(frame.width - 165, Math.max(6, event.clientX - frame.left))}px`;
  cvEisElements.cvEisContextMenu.style.top = `${Math.min(frame.height - 55, Math.max(6, event.clientY - frame.top))}px`;
  cvEisElements.cvEisContextMenu.hidden = false;
  cvEisElements.cvEisContextAutoScale.focus();
}

function closeCvEisContextMenu() {
  cvEisElements.cvEisContextMenu.hidden = true;
}

async function loadCvEisWorkspace() {
  cvEisElements.cvEisMaterialList.setAttribute("aria-busy", "true");
  cvEisElements.reloadCvEis.disabled = true;
  setCvEisNotice();
  try {
    const [status, catalog, materials] = await Promise.all([
      cvEisRequest("/api/start-stop/status"),
      cvEisRequest("/api/start-stop/cv-eis"),
      cvEisRequest("/api/start-stop/materials"),
    ]);
    cvEisState.status = status;
    cvEisState.catalog = catalog;
    cvEisState.materials = materials.materials || [];
    cvEisState.favorites = new Set(cvEisState.materials.filter((item) => item.favorite).map((item) => item.key));
    applyCvEisDeploymentProfile(status);
    renderCvEisMetrics();
    cvEisElements.sidebarCvEisState.textContent = `${formatCvEisInteger(catalog.counts?.ready)} 项可计算过电位`;
    renderCvEisMaterialList();
    const preferred = (catalog.materials || []).find((item) => recordIsFavorite(item) && item.status === "ready")
      || (catalog.materials || []).find((item) => item.status === "ready")
      || (catalog.materials || []).find((item) => ["area_required", "range_insufficient"].includes(item.status));
    if (preferred) await selectCvEisRecord(preferred.analysis_id);
    else updateCvEisSelectionHeader(null);
    if (catalog.counts?.attention) {
      setCvEisNotice(`${formatCvEisInteger(catalog.counts.attention)} 项未通过全部计算门槛；页面保留原因，不会跨材料借用 Rs 或强制计算。`);
    }
  } catch (error) {
    setCvEisNotice(error.message, "error");
    cvEisElements.sidebarCvEisState.textContent = "CV/EIS 分析不可用";
    cvEisElements.cvEisMaterialList.replaceChildren(cvEisElement("div", "start-stop-loading", "无法读取 CV/EIS 数据"));
  } finally {
    cvEisElements.reloadCvEis.disabled = false;
    cvEisElements.cvEisMaterialList.setAttribute("aria-busy", "false");
  }
}

cvEisElements.cvEisSearch.addEventListener("input", renderCvEisMaterialList);
cvEisElements.cvEisStatusFilter.addEventListener("change", renderCvEisMaterialList);
cvEisElements.cvEisSort.addEventListener("change", renderCvEisMaterialList);
cvEisElements.reloadCvEis.addEventListener("click", loadCvEisWorkspace);
cvEisElements.printCvEis.addEventListener("click", () => window.print());
cvEisElements.showCvRaw.addEventListener("change", renderCvEisChart);
cvEisElements.showCvIr.addEventListener("change", renderCvEisChart);
cvEisElements.cvEisTarget.addEventListener("change", () => {
  renderCvEisMaterialList();
  renderCvEisOverpotentials(currentCvEisRecord());
  renderCvEisChart();
});
cvEisElements.cvEisZoomIn.addEventListener("click", () => zoomCvEis(0.72));
cvEisElements.cvEisZoomOut.addEventListener("click", () => zoomCvEis(1.38));
cvEisElements.cvEisAutoScale.addEventListener("click", resetCvEisZoom);
cvEisElements.cvEisContextAutoScale.addEventListener("click", resetCvEisZoom);
cvEisElements.cvEisChartFrame.addEventListener("wheel", (event) => {
  const ratios = frameRatios(event);
  if (!ratios) return;
  event.preventDefault();
  zoomCvEis(Math.exp(Math.max(-500, Math.min(500, event.deltaY)) * 0.0015), ratios.x, ratios.y);
}, { passive: false });
cvEisElements.cvEisChartFrame.addEventListener("pointerdown", beginCvEisDrag);
cvEisElements.cvEisChartFrame.addEventListener("pointermove", (event) => {
  moveCvEisDrag(event);
  showCvEisTooltip(event);
});
cvEisElements.cvEisChartFrame.addEventListener("pointerup", finishCvEisDrag);
cvEisElements.cvEisChartFrame.addEventListener("pointercancel", finishCvEisDrag);
cvEisElements.cvEisChartFrame.addEventListener("mouseleave", () => { cvEisElements.cvEisTooltip.hidden = true; });
cvEisElements.cvEisChartFrame.addEventListener("dblclick", resetCvEisZoom);
cvEisElements.cvEisChartFrame.addEventListener("contextmenu", openCvEisContextMenu);
cvEisElements.cvEisChartFrame.addEventListener("keydown", (event) => {
  if (["0", "Home"].includes(event.key)) resetCvEisZoom();
  else if (["+", "="].includes(event.key)) zoomCvEis(0.72);
  else if (["-", "_"].includes(event.key)) zoomCvEis(1.38);
  else if (event.key === "Escape") closeCvEisContextMenu();
  else return;
  event.preventDefault();
});
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".cv-eis-context-menu")) closeCvEisContextMenu();
});

loadCvEisWorkspace();
