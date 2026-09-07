"use strict";

const stabilityPalette = [
  "#1565C0", "#E56B1F", "#0A8F6A", "#7B4AB5",
  "#C28A00", "#D23A72", "#00869B", "#8C564B",
  "#A50F15", "#4D4D4D", "#6B8E23", "#3F8FC4",
  "#9B7E00", "#B85C9E", "#3F3C94", "#2B9B8D",
];

const stabilityState = {
  catalog: null,
  legacySeriesCount: 0,
  activeView: "start_stop",
  activeSource: "workstation",
  explorers: new Map(),
};

function stabilityElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function stabilitySvgElement(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

async function stabilityRequest(url, options = {}) {
  return StartStopClient.request(url, options);
}

function stabilityNumber(value, digits = 3) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return parsed.toFixed(digits).replace(/\.?0+$/, "");
}

function stabilityInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function stabilityMetricValue(value, unit, digits = 3) {
  const text = stabilityNumber(value, digits);
  return text === "—" ? text : `${text} ${unit}`;
}

function setStabilityView(mode) {
  stabilityState.activeView = mode;
  document.querySelectorAll("[data-stability-view]").forEach((button) => {
    const active = button.dataset.stabilityView === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelector("#startStopAnalysisView").hidden = mode !== "start_stop";
  document.querySelector("#constantCurrentAnalysisView").hidden = mode !== "constant_current";
  const explorer = stabilityState.explorers.get(mode);
  if (mode === "constant_current" && explorer) explorer.loadChart();
}

function setStartStopSource(source) {
  stabilityState.activeSource = source;
  document.querySelectorAll("[data-stability-source]").forEach((button) => {
    const active = button.dataset.stabilitySource === source;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  document.querySelector("#workstationStartStopSource").hidden = source !== "workstation";
  document.querySelector("#lanbtsStartStopSource").hidden = source !== "lanbts";
  if (source === "lanbts") stabilityState.explorers.get("start_stop")?.loadChart();
}

class StabilityExplorer {
  constructor(root, mode) {
    this.root = root;
    this.mode = mode;
    this.series = [];
    this.selected = new Set();
    this.protocolKey = "";
    this.highlighted = new Set();
    this.onlyHighlights = false;
    this.chartView = null;
    this.geometry = null;
    this.search = "";
    this.loading = false;
    this.lastPayload = null;
    this.elements = {
      search: root.querySelector("[data-stability-search]"),
      protocol: root.querySelector("[data-stability-protocol]"),
      selection: root.querySelector("[data-stability-selection]"),
      selectAll: root.querySelector("[data-stability-select-all]"),
      clear: root.querySelector("[data-stability-clear]"),
      list: root.querySelector("[data-stability-series-list]"),
      metric: root.querySelector("[data-stability-metric]"),
      chartTitle: root.querySelector("[data-stability-chart-title]"),
      chartDescription: root.querySelector("[data-stability-chart-description]"),
      boundary: root.querySelector("[data-stability-boundary]"),
      chart: root.querySelector("[data-stability-chart]"),
      empty: root.querySelector("[data-stability-empty]"),
      legend: root.querySelector("[data-stability-legend]"),
      summary: root.querySelector("[data-stability-summary]"),
    };
    this.buildChartControls();
    this.buildExportControls();
    this.elements.search.addEventListener("input", (event) => {
      this.search = event.target.value.trim().toLocaleLowerCase("zh-CN");
      this.renderList();
    });
    this.elements.metric.addEventListener("change", () => {
      if (this.elements.metric.value === "minimum_time" && this.selected.size > 1) {
        this.selected = new Set([...this.selected].slice(0, 1));
      }
      this.highlighted = new Set([...this.highlighted].filter(id => this.selected.has(id)));
      this.renderList();
      this.loadChart();
    });
    this.elements.protocol.addEventListener("change", () => {
      this.protocolKey = this.elements.protocol.value;
      this.selectDefault();
      this.renderList();
      this.loadChart();
    });
    this.elements.selectAll.addEventListener("click", () => {
      this.selected = new Set(this.filteredSeries().slice(0, this.selectionLimit()).map((item) => item.series_id));
      this.highlighted = new Set([...this.highlighted].filter(id => this.selected.has(id)));
      this.renderList();
      this.loadChart();
    });
    this.elements.clear.addEventListener("click", () => {
      this.selected.clear();
      this.highlighted.clear();
      this.onlyHighlights = false;
      this.renderList();
      this.loadChart();
    });
  }

  filteredSeries() {
    const inProtocol = this.series.filter(item => this.protocolFor(item) === this.protocolKey);
    if (!this.search) return inProtocol;
    return inProtocol.filter((item) => [
      item.display_name,
      item.source_file,
      item.protocol_label,
      item.classification_reason,
    ].join(" ").toLocaleLowerCase("zh-CN").includes(this.search));
  }

  setSeries(series) {
    this.series = [...series].sort((a, b) =>
      ((a.downsample_stride > 1) - (b.downsample_stride > 1))
      || ((Date.parse(b.source_modified_utc || b.database_created_utc) || 0) - (Date.parse(a.source_modified_utc || a.database_created_utc) || 0))
      || ((b.source_version_id || 0) - (a.source_version_id || 0)));
    const groups = new Map();
    for (const item of this.series) {
      const key = this.protocolFor(item);
      if (!groups.has(key)) groups.set(key, {label:item.protocol_label || "工步待确认（仅单条检查）", count:0});
      groups.get(key).count++;
    }
    const fragment = document.createDocumentFragment();
    for (const [key, group] of groups) {
      const option = stabilityElement("option", "", `${group.label}（${group.count} 条）`);
      option.value = key;
      fragment.append(option);
    }
    this.elements.protocol.replaceChildren(fragment);
    this.protocolKey = groups.has(this.protocolKey) ? this.protocolKey : (groups.keys().next().value || "");
    this.elements.protocol.value = this.protocolKey;
    this.elements.protocol.disabled = groups.size === 0;
    this.selectDefault();
    this.renderList();
  }

  protocolFor(item) { return item.protocol_key || `unknown:${item.series_id}`; }
  selectionLimit() { return this.elements.metric.value === "minimum_time" ? 1 : 16; }
  selectDefault() {
    this.highlighted.clear();
    this.onlyHighlights = false;
    const rows = this.series.filter(item => this.protocolFor(item) === this.protocolKey);
    const full = rows.filter(item => !(item.downsample_stride > 1) && item.point_count > 0);
    this.selected = new Set((full.length ? full : rows).slice(0, this.selectionLimit()).map(item => item.series_id));
    if (!full.length && rows.some(item => item.downsample_stride > 1)) this.elements.metric.value = "overview";
  }

  buildChartControls() {
    const frame = this.elements.chart.closest(".stability-chart-frame");
    frame.tabIndex = 0;
    frame.setAttribute("role", "group");
    frame.setAttribute("aria-label", this.mode === "start_stop" ? "蓝博启停图表操作" : "蓝博恒流图表操作");
    const toolbar = stabilityElement("div", "stability-plot-toolbar");
    const modes = new Map();
    const button = (label, action) => {
      const node = stabilityElement("button", "secondary-outline-button", label);
      node.type = "button";
      node.addEventListener("click", action);
      toolbar.append(node);
      return node;
    };
    for (const [mode, label] of [["inspect","检查"],["zoom","框选"],["pan","平移"]]) {
      const node = button(label, () => this.interaction.setMode(mode));
      node.setAttribute("aria-pressed", String(mode === "inspect"));
      modes.set(mode, node);
    }
    button("缩小", () => this.interaction.zoom(1.4));
    this.elements.zoomLevel = stabilityElement("span", "stability-zoom-level", "100%");
    this.elements.zoomLevel.setAttribute("role", "status");
    toolbar.append(this.elements.zoomLevel);
    button("放大", () => this.interaction.zoom(0.75));
    button("自动缩放", () => this.resetZoom());
    this.elements.onlyHighlights = button("仅看高亮", () => {
      this.onlyHighlights = !this.onlyHighlights;
      if (this.lastPayload) this.renderChart(this.lastPayload);
    });
    button("清除高亮", () => { this.highlighted.clear(); this.onlyHighlights = false;
      if (this.lastPayload) this.renderChart(this.lastPayload); this.renderList(); });
    frame.before(toolbar, stabilityElement("p", "stability-plot-hint", "点击图后滚轮缩放 · 框选放大 · 平移 · 右键自动缩放 · 点击曲线或图例高亮"));
    const selection = stabilityElement("div", "stability-zoom-selection");
    selection.hidden = true;
    const menu = stabilityElement("div", "stability-context-menu");
    menu.hidden = true;
    menu.setAttribute("role", "menu");
    const auto = stabilityElement("button", "", "自动缩放");
    auto.type = "button";
    auto.setAttribute("role", "menuitem");
    menu.append(auto);
    frame.append(selection, menu);
    this.interaction = new StartStopPlotInteraction({frame, selection, menu,
      geometry:() => this.geometry, domain:value => this.setDomain(value), reset:() => this.resetZoom(),
      inspect:point => this.inspectPoint(point), modeChanged:mode => {
        for (const [key, node] of modes) node.setAttribute("aria-pressed", String(key === mode));
      }});
    window.addEventListener("resize", () => {
      if (this.lastPayload && frame.getClientRects().length) this.renderChart(this.lastPayload);
    });
  }

  setDomain(domain) {
    if (!Object.values(domain).every(Number.isFinite)) return;
    const xSpan = domain.xMax-domain.xMin, ySpan = domain.yMax-domain.yMin;
    if (!(xSpan > 0 && ySpan > 0)) return;
    if (this.baseDomain && (xSpan < (this.baseDomain.xMax-this.baseDomain.xMin)/1e6 || ySpan < (this.baseDomain.yMax-this.baseDomain.yMin)/1e6)) return;
    this.chartView = domain;
    if (this.lastPayload) this.renderChart(this.lastPayload);
  }

  buildExportControls() {
    const bar = stabilityElement("div", "stability-export-bar");
    this.elements.exportStatus = stabilityElement("span", "", "请先高亮需要导出的材料");
    this.elements.exportStatus.setAttribute("role", "status");
    bar.append(this.elements.exportStatus);
    this.elements.exportButtons = [];
    for (const [format, label] of [["xlsx","导出原始与处理数据 Excel"],["pdf","导出高亮 PDF 图"]]) {
      const button = stabilityElement("button", "secondary-outline-button", label);
      button.type = "button";
      button.disabled = true;
      button.addEventListener("click", () => this.exportHighlighted(format));
      this.elements.exportButtons.push(button);
      bar.append(button);
    }
    this.elements.legend.after(bar);
  }
  updateExportControls() {
    if (!this.elements.exportButtons) return;
    const count = [...this.highlighted].filter(id => this.selected.has(id)).length;
    for (const button of this.elements.exportButtons) button.disabled = !count || this.exporting || this.loading;
    this.elements.exportStatus.textContent = this.exporting ? "正在生成导出文件…"
      : count ? `可导出 ${count} 条高亮记录 · Excel 包含完整原始数值` : "请先高亮需要导出的材料";
  }
  async exportHighlighted(format) {
    const ids = [...this.highlighted].filter(id => this.selected.has(id));
    if (!ids.length || this.exporting || this.loading) return;
    this.exporting = true;
    this.updateExportControls();
    let message;
    try {
      const query = new URLSearchParams({series:ids.join(","), analysis_mode:this.mode, metric:this.elements.metric.value, format});
      const {response, blob} = await StartStopClient.download(`/api/start-stop/stability/chart-export?${query}`);
      const url = URL.createObjectURL(blob);
      const anchor = stabilityElement("a");
      anchor.href = url;
      anchor.download = StartStopClient.downloadFilename(response, `蓝博_高亮数据.${format}`);
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      message = `${ids.length} 条高亮记录已导出为 ${format.toUpperCase()}`;
    } catch (error) { message = `导出失败：${error.message}`; }
    finally {
      this.exporting = false;
      this.updateExportControls();
      this.elements.exportStatus.textContent = message;
    }
  }
  resetZoom() { this.chartView = null; if (this.lastPayload) this.renderChart(this.lastPayload); }
  colorFor(seriesId) {
    const index = (this.lastPayload?.series || []).findIndex(item => item.series_id === seriesId);
    return stabilityPalette[Math.max(0, index) % stabilityPalette.length];
  }
  toggleHighlight(seriesId) {
    if (!this.selected.has(seriesId)) {
      if (this.selected.size >= this.selectionLimit()) return;
      this.selected.add(seriesId);
      this.highlighted.add(seriesId);
      this.renderList();
      this.loadChart();
      return;
    }
    if (this.highlighted.has(seriesId)) this.highlighted.delete(seriesId);
    else this.highlighted.add(seriesId);
    if (this.lastPayload) this.renderChart(this.lastPayload);
    this.renderList();
  }
  inspectPoint(point) {
    if (!this.geometry || !this.lastPayload) return;
    const {xScale, yScale} = this.geometry;
    let nearest = null, best = 14;
    for (const series of this.lastPayload.series || []) {
      if (this.onlyHighlights && this.highlighted.size && !this.highlighted.has(series.series_id)) continue;
      let previous = null;
      for (const row of series.points || []) {
        const x = xScale(row.x), y = yScale(row.y);
        let distance = Math.hypot(point.x-x, point.y-y);
        if (previous) {
          const dx = x-previous.x, dy = y-previous.y;
          const weight = Math.max(0, Math.min(1, ((point.x-previous.x)*dx+(point.y-previous.y)*dy)/(dx*dx+dy*dy || 1)));
          distance = Math.min(distance, Math.hypot(point.x-previous.x-weight*dx, point.y-previous.y-weight*dy));
        }
        if (distance < best) { best = distance; nearest = series.series_id; }
        previous = {x, y};
      }
    }
    if (nearest) this.toggleHighlight(nearest);
  }

  renderList() {
    const rows = this.filteredSeries();
    const fragment = document.createDocumentFragment();
    if (!rows.length) {
      const empty = stabilityElement(
        "div",
        "start-stop-loading",
        this.series.length ? "没有符合搜索条件的数据" : "暂无已导入数据；点击“下载并更新”后自动识别稳定 BTS 文件",
      );
      fragment.append(empty);
    }
    rows.forEach((item) => {
      const label = stabilityElement("div", "stability-series-row");
      const checkLabel = stabilityElement("label", "stability-series-check");
      const input = document.createElement("input");
      input.type = this.selectionLimit() === 1 ? "radio" : "checkbox";
      input.name = `stability-${this.mode}-selection`;
      input.setAttribute("aria-label", `检查 ${item.display_name}`);
      input.checked = this.selected.has(item.series_id);
      input.addEventListener("change", () => {
        if (input.checked) {
          if (this.selectionLimit() === 1) this.selected.clear();
          if (this.selected.size >= this.selectionLimit()) {
            input.checked = false;
            return;
          }
          this.selected.add(item.series_id);
        } else {
          this.selected.delete(item.series_id);
          this.highlighted.delete(item.series_id);
        }
        this.updateSelection();
        this.loadChart();
      });
      const copy = stabilityElement("span", "stability-series-copy");
      copy.append(stabilityElement("strong", "", item.display_name));
      copy.append(stabilityElement("span", "", item.protocol_label));
      const detail = [
        item.channel ? `通道 ${item.channel}` : "",
        item.electrode_area_cm2 ? `${stabilityNumber(item.electrode_area_cm2)} cm²` : "面积未设",
        item.source_file,
      ].filter(Boolean).join(" · ");
      copy.append(stabilityElement("small", "", detail));
      const reason = stabilityElement("small", "", item.classification_reason || "分类依据待更新");
      reason.title = item.classification_reason || "";
      copy.append(reason);
      checkLabel.append(input, copy);
      const highlight = stabilityElement("button", "stability-highlight-button", "高亮");
      highlight.type = "button";
      highlight.setAttribute("aria-label", `高亮 ${item.display_name}`);
      highlight.setAttribute("aria-pressed", String(this.highlighted.has(item.series_id)));
      highlight.addEventListener("click", () => this.toggleHighlight(item.series_id));
      label.append(checkLabel, highlight);
      fragment.append(label);
    });
    this.elements.list.replaceChildren(fragment);
    this.elements.list.setAttribute("aria-busy", "false");
    this.updateSelection();
  }

  updateSelection() {
    const count = this.series.filter(item => this.protocolFor(item) === this.protocolKey).length;
    this.elements.selection.textContent = `已选 ${this.selected.size} / 当前工步 ${count} 条${this.selectionLimit() === 1 ? " · 异常判断仅单条" : ""}`;
    this.updateExportControls();
  }

  async loadChart() {
    const requestId = (this.requestId || 0) + 1;
    this.requestId = requestId;
    this.requestController?.abort();
    const controller = new AbortController();
    this.requestController = controller;
    const selected = [...this.selected];
    const metric = this.elements.metric.value;
    this.chartView = null;
    this.geometry = null;
    if (this.elements.zoomLevel) this.elements.zoomLevel.textContent = "100%";
    this.lastPayload = null;
    this.elements.chart.replaceChildren();
    this.elements.legend.replaceChildren();
    this.elements.summary.replaceChildren();
    if (!selected.length) {
      this.loading = false;
      this.lastPayload = null;
      this.elements.chart.replaceChildren();
      this.elements.legend.replaceChildren();
      this.elements.summary.replaceChildren();
      this.elements.empty.hidden = false;
      this.elements.empty.textContent = this.series.length ? "请选择左侧曲线" : "暂无已导入数据";
      return;
    }
    this.loading = true;
    this.updateExportControls();
    this.elements.empty.hidden = false;
    this.elements.empty.textContent = "正在读取并计算稳定性曲线…";
    try {
      const query = new URLSearchParams({
        series: selected.join(","),
        analysis_mode: this.mode,
        metric,
        max_points: "6000",
      });
      const payload = await stabilityRequest(`/api/start-stop/stability/chart?${query}`, {
        signal: controller.signal,
      });
      if (this.requestId !== requestId) return;
      this.lastPayload = payload;
      this.renderChart(payload);
    } catch (error) {
      if (this.requestId !== requestId || controller.signal.aborted) return;
      this.lastPayload = null;
      this.elements.chart.replaceChildren();
      this.elements.legend.replaceChildren();
      this.elements.summary.replaceChildren();
      this.elements.empty.hidden = false;
      this.elements.empty.textContent = `曲线读取失败：${error.message}`;
    } finally {
      if (this.requestId === requestId) this.loading = false;
      this.updateExportControls();
    }
  }

  renderChart(payload) {
    this.elements.onlyHighlights.setAttribute("aria-pressed", String(this.onlyHighlights));
    const series = (Array.isArray(payload.series) ? payload.series : []).filter(item =>
      !this.onlyHighlights || !this.highlighted.size || this.highlighted.has(item.series_id));
    const validPoint = point => point.x !== null && point.y !== null && point.x !== "" && point.y !== "" && Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.y));
    const allPoints = series.flatMap((item) => item.points || []).filter(validPoint);
    this.elements.chartTitle.textContent = payload.metric_spec?.label || "稳定性曲线";
    this.elements.chartDescription.textContent = `${payload.metric_spec?.description || ""} · 网页显示下采样数据`;
    this.elements.boundary.textContent = payload.measurement_boundary?.message || "蓝博电压参照尚未确认。";
    if (!allPoints.length) {
      this.geometry = null;
      this.elements.chart.replaceChildren();
      this.elements.legend.replaceChildren();
      this.renderSummary(series);
      this.elements.chartTitle.textContent = payload.metric_spec?.label || "稳定性曲线";
      this.elements.empty.hidden = false;
      this.elements.empty.textContent = payload.metric === "current_density"
        ? "所选记录没有电流密度数据；请先确认本次电极面积，再重新下载对应记录。"
        : payload.analysis_mode === "start_stop"
        ? "所选记录在当前指标下没有完整循环；可切换“全程电压”查看原始记录。"
        : "所选记录在当前指标下没有有效数据。";
      return;
    }
    this.elements.empty.hidden = true;
    this.elements.chartTitle.textContent = payload.metric_spec?.label || "稳定性曲线";
    this.elements.chartDescription.textContent = `${payload.metric_spec?.description || ""} · 网页显示下采样数据`;
    this.elements.boundary.textContent = payload.measurement_boundary?.message || "蓝博电压参照尚未确认。";

    const width = Math.max(480, this.elements.chart.clientWidth || 900);
    const height = Math.max(360, this.elements.chart.clientHeight || 490);
    const margin = { top: 24, right: 24, bottom: 54, left: 72 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
    for (const point of allPoints) {
      xMin = Math.min(xMin, Number(point.x)); xMax = Math.max(xMax, Number(point.x));
      yMin = Math.min(yMin, Number(point.y)); yMax = Math.max(yMax, Number(point.y));
    }
    if (xMin === xMax) { xMin -= 0.5; xMax += 0.5; }
    if (yMin === yMax) { yMin -= 0.5; yMax += 0.5; }
    const xPad = (xMax - xMin) * 0.03;
    xMin -= xPad;
    xMax += xPad;
    const yPad = (yMax - yMin) * 0.07;
    yMin -= yPad;
    yMax += yPad;
    this.baseDomain = {xMin, xMax, yMin, yMax};
    if (this.chartView) ({xMin,xMax,yMin,yMax} = this.chartView);
    const xScale = (value) => margin.left + (Number(value) - xMin) / (xMax - xMin) * plotWidth;
    const yScale = (value) => margin.top + (yMax - Number(value)) / (yMax - yMin) * plotHeight;
    this.geometry = {width, height, plot:{left:margin.left, top:margin.top, width:plotWidth, height:plotHeight},
      domain:{xMin,xMax,yMin,yMax}, xScale, yScale};
    this.elements.zoomLevel.textContent = `${Math.round(100*(this.baseDomain.xMax-this.baseDomain.xMin)/(xMax-xMin))}%`;

    const fragment = document.createDocumentFragment();
    for (let index = 0; index <= 5; index += 1) {
      const yValue = yMin + (yMax - yMin) * index / 5;
      const y = yScale(yValue);
      fragment.append(stabilitySvgElement("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "stability-chart-grid" }));
      const label = stabilitySvgElement("text", { x: margin.left - 10, y: y + 4, "text-anchor": "end", class: "stability-chart-tick" });
      label.textContent = stabilityNumber(yValue, Math.min(8, Math.max(1, Math.ceil(-Math.log10((yMax-yMin)/5))+1)));
      fragment.append(label);
    }
    const xTicks = [];
    if (payload.metric_spec?.x_key === "cycle" && xMax-xMin >= 1) {
      const step = Math.max(1, Math.ceil((xMax-xMin)/6));
      for (let value = Math.ceil(xMin); value <= Math.floor(xMax); value += step) xTicks.push(value);
    } else {
      for (let index = 0; index <= 6; index++) xTicks.push(xMin+(xMax-xMin)*index/6);
    }
    for (const xValue of xTicks) {
      const x = xScale(xValue);
      fragment.append(stabilitySvgElement("line", { x1: x, y1: margin.top, x2: x, y2: height - margin.bottom, class: "stability-chart-grid" }));
      const label = stabilitySvgElement("text", { x, y: height - margin.bottom + 22, "text-anchor": "middle", class: "stability-chart-tick" });
      label.textContent = stabilityNumber(xValue, Math.min(8, Math.max(0, Math.ceil(-Math.log10((xMax-xMin)/6))+1)));
      fragment.append(label);
    }
    fragment.append(stabilitySvgElement("line", { x1: margin.left, y1: height - margin.bottom, x2: width - margin.right, y2: height - margin.bottom, class: "stability-chart-axis" }));
    fragment.append(stabilitySvgElement("line", { x1: margin.left, y1: margin.top, x2: margin.left, y2: height - margin.bottom, class: "stability-chart-axis" }));

    const defs = stabilitySvgElement("defs");
    const clip = stabilitySvgElement("clipPath", {id:`stability-clip-${this.mode}`});
    clip.append(stabilitySvgElement("rect", {x:margin.left, y:margin.top, width:plotWidth, height:plotHeight}));
    defs.append(clip);
    fragment.append(defs);
    const plot = stabilitySvgElement("g", {"clip-path":`url(#stability-clip-${this.mode})`});
    [...series].sort((a,b)=> Number(this.highlighted.has(a.series_id))-Number(this.highlighted.has(b.series_id))).forEach((item) => {
      const color = this.colorFor(item.series_id);
      const points = (item.points || []).filter(validPoint);
      if (!points.length) return;
      const commands = points.map((point, pointIndex) => `${pointIndex ? "L" : "M"}${xScale(point.x).toFixed(2)} ${yScale(point.y).toFixed(2)}`);
      const line = stabilitySvgElement("path", { d: commands.join(" "), stroke: color, class: "stability-chart-line" });
      line.style.strokeWidth = this.highlighted.has(item.series_id) ? "4.5px" : "2.5px";
      line.style.opacity = this.highlighted.size && !this.highlighted.has(item.series_id) ? "0.16" : "1";
      plot.append(line);
      if (points.length <= 120) {
        points.forEach((point) => {
          const circle = stabilitySvgElement("circle", {
            cx: xScale(point.x),
            cy: yScale(point.y),
            r: point.status === "abnormal" ? 4 : 3,
            fill: color,
            class: `stability-chart-point ${point.status || ""}`,
          });
          const title = stabilitySvgElement("title");
          title.textContent = `${item.display_name} · x ${stabilityNumber(point.x, 3)} · y ${stabilityNumber(point.y, 4)}`;
          circle.append(title);
          circle.style.opacity = this.highlighted.size && !this.highlighted.has(item.series_id) ? "0.16" : "1";
          plot.append(circle);
        });
      }
    });
    fragment.append(plot);
    const xLabel = stabilitySvgElement("text", { x: margin.left + plotWidth / 2, y: height - 12, "text-anchor": "middle", class: "stability-chart-tick" });
    xLabel.textContent = payload.metric_spec?.x_label || "横轴";
    fragment.append(xLabel);
    const yLabel = stabilitySvgElement("text", { x: 18, y: margin.top + plotHeight / 2, transform: `rotate(-90 18 ${margin.top + plotHeight / 2})`, "text-anchor": "middle", class: "stability-chart-tick" });
    yLabel.textContent = `${payload.metric_spec?.label || "指标"} / ${payload.metric_spec?.unit || ""}`;
    fragment.append(yLabel);
    this.elements.chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
    this.elements.chart.replaceChildren(fragment);
    this.renderLegend(series);
    this.renderSummary(series);
  }

  renderLegend(series) {
    const fragment = document.createDocumentFragment();
    series.forEach((item) => {
      const legend = stabilityElement("button", "stability-legend-item");
      legend.type = "button";
      legend.setAttribute("aria-pressed", String(this.highlighted.has(item.series_id)));
      legend.addEventListener("click", () => this.toggleHighlight(item.series_id));
      const line = stabilityElement("span", "stability-legend-line");
      line.style.backgroundColor = this.colorFor(item.series_id);
      legend.append(line, stabilityElement("span", "", item.display_name));
      fragment.append(legend);
    });
    this.elements.legend.replaceChildren(fragment);
  }

  renderSummary(series) {
    const fragment = document.createDocumentFragment();
    series.forEach((item) => {
      const summary = item.summary || {};
      const card = stabilityElement("article", "stability-summary-card");
      card.append(stabilityElement("span", "", item.display_name));
      if (summary.calculation_status === "preview_only") {
        card.append(stabilityElement("strong", "", "仅抽样预览"));
        card.append(stabilityElement("small", "", "重新下载全量记录后才会计算循环统计与漂移。"));
      } else if (this.mode === "start_stop") {
        card.append(stabilityElement("strong", "", `${stabilityInteger(summary.complete_cycles)} 个完整循环`));
        card.append(stabilityElement("small", "", `${stabilityInteger(summary.normal_cycles)} 正常 · ${stabilityInteger(summary.abnormal_cycles)} 异常`));
      } else {
        card.append(stabilityElement("strong", "", stabilityMetricValue(summary.linear_drift_mv_per_h, "mV/h", 3)));
        card.append(stabilityElement("small", "", `${stabilityMetricValue(summary.duration_h, "h", 2)} · 电压变化 ${stabilityMetricValue(summary.voltage_change_mv, "mV", 2)} · 电流 ${stabilityMetricValue(summary.current_median_ma, "mA", 2)}`));
      }
      fragment.append(card);
    });
    this.elements.summary.replaceChildren(fragment);
  }
}

async function initializeStabilityAnalysis() {
  const viewTabs = [...document.querySelectorAll("[data-stability-view]")];
  viewTabs.forEach((button, index) => {
    button.addEventListener("click", () => setStabilityView(button.dataset.stabilityView));
    button.addEventListener("keydown", (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const offset = event.key === 'ArrowRight' ? 1 : -1;
      const target = viewTabs[(index + offset + viewTabs.length) % viewTabs.length];
      setStabilityView(target.dataset.stabilityView);
      target.focus();
    });
  });
  document.querySelectorAll("[data-stability-source]").forEach((button) => {
    button.addEventListener("click", () => setStartStopSource(button.dataset.stabilitySource));
  });
  document.querySelectorAll("[data-stability-explorer]").forEach((root) => {
    const mode = root.dataset.stabilityExplorer;
    stabilityState.explorers.set(mode, new StabilityExplorer(root, mode));
  });
  try {
    const [catalog, legacy] = await Promise.all([
      stabilityRequest("/api/start-stop/stability/catalog"),
      stabilityRequest("/api/start-stop/series").catch(() => ({ count: 0 })),
    ]);
    stabilityState.catalog = catalog;
    stabilityState.legacySeriesCount = Number(legacy.count || 0);
    const series = Array.isArray(catalog.series) ? catalog.series : [];
    stabilityState.explorers.get("start_stop")?.setSeries(series.filter((item) => item.analysis_mode === "start_stop"));
    stabilityState.explorers.get("constant_current")?.setSeries(series.filter((item) => item.analysis_mode === "constant_current"));
    const startCount = stabilityState.legacySeriesCount + Number(catalog.counts?.start_stop || 0);
    const constantCount = Number(catalog.counts?.constant_current || 0);
    document.querySelector("#stabilityStartStopCount").textContent = stabilityInteger(startCount);
    document.querySelector("#stabilityStartStopCount").title = `${stabilityState.legacySeriesCount} 条工作站曲线 · ${stabilityInteger(catalog.counts?.start_stop)} 条蓝博曲线`;
    document.querySelector("#stabilityConstantCurrentCount").textContent = stabilityInteger(constantCount);
    stabilityState.explorers.get("start_stop")?.loadChart();
  } catch (error) {
    document.querySelector("#stabilityStartStopCount").textContent = "!";
    document.querySelector("#stabilityConstantCurrentCount").textContent = "!";
    stabilityState.explorers.forEach((explorer) => {
      explorer.elements.list.replaceChildren(stabilityElement("div", "workspace-notice error", `稳定性数据读取失败：${error.message}`));
      explorer.elements.list.setAttribute("aria-busy", "false");
    });
  }
}

initializeStabilityAnalysis();
