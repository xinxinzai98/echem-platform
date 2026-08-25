"use strict";

const workstationState = {
  payload: null,
  livePreview: null,
  status: null,
  loading: false,
  timer: null,
};

const workstationElements = {
  workbenchVersion: document.querySelector("#workbenchVersion"),
  workstationSidebarState: document.querySelector("#workstationSidebarState"),
  workstationNotice: document.querySelector("#workstationNotice"),
  monitorOverallState: document.querySelector("#monitorOverallState"),
  runningStationMetric: document.querySelector("#runningStationMetric"),
  monitorUpdatedMetric: document.querySelector("#monitorUpdatedMetric"),
  monitorFreshnessDetail: document.querySelector("#monitorFreshnessDetail"),
  workstationStationGrid: document.querySelector("#workstationStationGrid"),
  workstationMachineList: document.querySelector("#workstationMachineList"),
  refreshWorkstations: document.querySelector("#refreshWorkstations"),
  monitorAutoRefresh: document.querySelector("#monitorAutoRefresh"),
  livePreviewOverview: document.querySelector("#livePreviewOverview"),
  livePreviewDescription: document.querySelector("#livePreviewDescription"),
  livePreviewStatus: document.querySelector("#livePreviewStatus"),
  livePreviewGrid: document.querySelector("#livePreviewGrid"),
};

const WORKSTATION_SVG_NS = "http://www.w3.org/2000/svg";

function workstationElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

async function workstationRequest(path) {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(payload?.error || `请求失败（${response.status}）`);
  }
  return payload;
}

function setWorkstationRoutes() {
  document.querySelectorAll("[data-config-route]").forEach((link) => { link.href = "/start-stop"; });
  document.querySelectorAll("[data-workstations-route]").forEach((link) => { link.href = "/start-stop/workstations"; });
  document.querySelectorAll("[data-analysis-route]").forEach((link) => { link.href = "/start-stop/analysis"; });
  document.querySelectorAll("[data-cv-eis-route]").forEach((link) => { link.href = "/start-stop/cv-eis"; });
  document.querySelectorAll("[data-materials-route]").forEach((link) => { link.href = "/start-stop/materials"; });
  workstationElements.workstationWorkbenchBrand?.setAttribute("href", "/start-stop");
}

function formatMonitorTime(value, { includeDate = true } = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: includeDate ? "numeric" : undefined,
    day: includeDate ? "numeric" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function monitorAgeLabel(seconds) {
  if (!Number.isFinite(Number(seconds))) return "缓存时间未知";
  const value = Math.max(0, Math.round(Number(seconds)));
  if (value < 5) return "刚刚更新";
  if (value < 60) return `${value} 秒前更新`;
  return `${Math.floor(value / 60)} 分钟前更新`;
}

function setNotice(message = "") {
  workstationElements.workstationNotice.hidden = !message;
  workstationElements.workstationNotice.textContent = message;
}

function setOverallState(payload) {
  const status = String(payload?.status || "unavailable");
  const labels = {
    ready: "监控正常",
    partial: "部分可用",
    stale: "缓存已过期",
    unavailable: "当前不可用",
    initializing: "首次检查中",
  };
  workstationElements.monitorOverallState.className = `monitor-state-chip ${status === "initializing" ? "loading" : status}`;
  workstationElements.monitorOverallState.textContent = labels[status] || "状态未知";
  workstationElements.workstationSidebarState.textContent = labels[status] || "状态未知";
}

function applyServiceStatus(payload) {
  workstationState.status = payload;
  const serviceVersion = String(payload?.service?.version || "").trim();
  const imageReference = String(payload?.service?.image_reference || "").trim();
  workstationElements.workbenchVersion.textContent = serviceVersion && serviceVersion !== "unknown" ? `版本 ${serviceVersion}` : "版本未知";
  workstationElements.workbenchVersion.title = imageReference || "当前服务未提供镜像标识";
}

function renderMetrics(payload, runningStations) {
  workstationElements.runningStationMetric.textContent = String(runningStations);
  workstationElements.monitorUpdatedMetric.textContent = formatMonitorTime(payload?.generated_at_utc);
  workstationElements.monitorFreshnessDetail.textContent = monitorAgeLabel(payload?.cache_age_seconds);
}

function activeActivities(machine) {
  return (Array.isArray(machine?.material_activities) ? machine.material_activities : [])
    .filter((activity) => activity.activity_status === "active");
}

function stationActivity(machine, station, stationIndex) {
  if (station?.assigned_activity?.activity_status === "active") return station.assigned_activity;
  return activeActivities(machine)[stationIndex] || null;
}

function stationViewState(machine, station, activity) {
  if (!machine?.reachable || station?.status === "offline") return "offline";
  if (activity) {
    if (!station?.online || station?.status === "running_attention" || station?.status === "attention") {
      return "running_attention";
    }
    return "running";
  }
  if (!station?.online || station?.status === "attention") return "attention";
  return "idle";
}

function renderOverviewStation(machine, station, machineIndex, stationIndex) {
  const activity = stationActivity(machine, station, stationIndex);
  const state = stationViewState(machine, station, activity);
  const labels = {
    running: "运行中",
    running_attention: "运行中 · 需检查",
    idle: "空闲",
    attention: "需检查",
    offline: "离线",
  };
  const ordinal = machineIndex * 2 + stationIndex + 1;
  const card = workstationElement("article", `station-overview-card ${state}`);
  card.dataset.stationState = state;
  card.setAttribute("aria-label", `${machine?.name || "实验电脑"} ${station?.label || `工作站 ${stationIndex + 1}`}：${labels[state]}`);

  const heading = workstationElement("div", "station-overview-card-heading");
  const identity = workstationElement("div", "station-overview-identity");
  identity.append(workstationElement("span", "station-overview-number", String(ordinal).padStart(2, "0")));
  const title = workstationElement("div");
  title.append(workstationElement("span", "station-overview-location", machine?.name || "实验电脑"));
  title.append(workstationElement("h3", "", station?.label || `工作站 ${stationIndex + 1}`));
  identity.append(title);
  heading.append(identity);
  heading.append(workstationElement("span", `station-overview-state ${state}`, labels[state]));
  card.append(heading);

  const statusLine = workstationElement("div", "station-overview-status-line");
  statusLine.append(workstationElement("i", "station-overview-dot"));
  statusLine.append(workstationElement("strong", "", labels[state]));
  card.append(statusLine);

  const material = workstationElement("div", "station-overview-material");
  material.append(workstationElement("span", "", activity ? "当前材料" : "当前状态"));
  const materialName = activity?.display_name
    || (state === "idle" ? "当前无活动材料" : state === "offline" ? "电脑不可达" : "软件或串口信号不完整");
  const materialStrong = workstationElement("strong", "", `${activity?.favorite ? "★ " : ""}${materialName}`);
  materialStrong.title = materialName;
  material.append(materialStrong);
  card.append(material);

  const taskLabel = activity?.task?.task_label
    || (state === "idle" ? "未检测到文件持续写入" : station?.status_label || "等待下一次检查");
  const task = workstationElement("p", "station-overview-task", taskLabel);
  task.title = taskLabel;
  card.append(task);

  const footer = workstationElement("div", "station-overview-footer");
  footer.append(workstationElement("span", "", machine?.hostname || "—"));
  footer.append(workstationElement("span", "", activity ? formatMonitorTime(activity.last_write_utc, { includeDate: false }) : station?.online ? "软件与串口在线" : "信号待检查"));
  card.append(footer);
  return { card, running: state === "running" || state === "running_attention" };
}

function renderStationOverview(payload) {
  const machines = Array.isArray(payload?.machines) ? payload.machines : [];
  const fragment = document.createDocumentFragment();
  let running = 0;
  machines.slice(0, 3).forEach((machine, machineIndex) => {
    const stations = Array.isArray(machine.stations) ? machine.stations : [];
    for (let stationIndex = 0; stationIndex < 2; stationIndex += 1) {
      const rendered = renderOverviewStation(machine, stations[stationIndex] || {}, machineIndex, stationIndex);
      running += rendered.running ? 1 : 0;
      fragment.append(rendered.card);
    }
  });
  while (fragment.childElementCount < 6) {
    const missingIndex = fragment.childElementCount;
    const rendered = renderOverviewStation(
      { name: "未配置", hostname: "—", reachable: false, material_activities: [] },
      { label: `工作站 ${missingIndex + 1}`, status: "offline", online: false },
      Math.floor(missingIndex / 2),
      missingIndex % 2,
    );
    fragment.append(rendered.card);
  }
  workstationElements.workstationStationGrid.replaceChildren(fragment);
  workstationElements.workstationStationGrid.setAttribute("aria-busy", "false");
  return running;
}

function signalItem(label, value) {
  const item = workstationElement("div", "machine-signal-item");
  item.append(workstationElement("span", "", label));
  item.append(workstationElement("strong", "", value));
  return item;
}

function renderMaterialActivity(activity) {
  const active = activity.activity_status === "active";
  const card = workstationElement("article", `material-activity-card ${active ? "active" : "recent"}`);
  const heading = workstationElement("div", "material-activity-heading");
  heading.append(workstationElement("strong", "", `${activity.favorite ? "★ " : ""}${activity.display_name || "未识别材料"}`));
  const chips = workstationElement("div", "activity-chip-row");
  chips.append(workstationElement("span", `material-activity-state ${active ? "active" : "recent"}`, active ? "当前活动" : "最近材料"));
  chips.append(workstationElement("span", `material-match-state ${activity.material_match || "unmatched"}`, activity.material_match === "matched" ? "材料库全名" : "新目录待确认"));
  heading.append(chips);
  card.append(heading);
  card.append(workstationElement("p", "material-task-label", activity.task?.task_label || "工步未识别"));
  card.append(workstationElement("p", "material-phase-note", activity.task?.phase || "当前仪器阶段未读取"));
  card.append(workstationElement("p", "material-source-line", `最近文件：${activity.current_file || "—"} · ${formatMonitorTime(activity.last_write_utc)}`));
  card.append(workstationElement("p", "material-source-line", `材料目录：${activity.relative_folder || "—"}`));
  return card;
}

function renderStation(station) {
  const card = workstationElement("article", "workstation-station-card");
  const heading = workstationElement("div", "station-card-heading");
  heading.append(workstationElement("strong", "", station.label || "工作站"));
  heading.append(workstationElement("span", `station-state-chip ${station.status || "attention"}`, station.status_label || "状态未知"));
  card.append(heading);

  const detail = workstationElement("div", "station-detail-grid");
  const port = workstationElement("div");
  port.append(workstationElement("span", "", "串口"));
  port.append(workstationElement("strong", "", station.serial_port?.device_id || "未识别"));
  const software = workstationElement("div");
  software.append(workstationElement("span", "", "工作站软件"));
  software.append(workstationElement("strong", "", station.software ? `${station.software.process_name} · PID ${station.software.pid}` : "未识别"));
  detail.append(port, software);
  card.append(detail);
  card.append(workstationElement("p", "station-material-note", station.mapping_note || "尚无材料对应信息。"));
  return card;
}

function renderMachine(machine) {
  const card = workstationElement("article", `workstation-machine-card status-${machine.status || "attention"}`);
  const heading = workstationElement("div", "machine-card-heading");
  const title = workstationElement("div");
  title.append(workstationElement("h2", "", machine.name || "实验电脑"));
  title.append(workstationElement("p", "machine-identity", `${machine.hostname || "—"} · ${machine.ip || "—"}`));
  heading.append(title);
  heading.append(workstationElement("span", `machine-state-chip ${machine.status || "attention"}`, machine.status_label || "状态未知"));
  card.append(heading);

  const signalStrip = workstationElement("div", "machine-signal-strip");
  signalStrip.append(
    signalItem("SSH", machine.reachable ? "可达" : "不可达"),
    signalItem("工作站软件", `${Number(machine.software_instances || 0)} 个进程`),
    signalItem("串口", `${Number(machine.serial_ports_online || 0)} 个可见`),
    signalItem("本轮检查", machine.scan_mode === "discovery" ? "重新发现目录" : machine.scan_mode === "quick" ? "快速复查" : "—"),
  );
  card.append(signalStrip);

  const activitySection = workstationElement("section", "material-activity-section");
  activitySection.append(workstationElement("h3", "", "当前 / 最近材料与任务"));
  const activityList = workstationElement("div", "material-activity-list");
  const activities = Array.isArray(machine.material_activities) ? machine.material_activities : [];
  if (activities.length) activities.forEach((activity) => activityList.append(renderMaterialActivity(activity)));
  else activityList.append(workstationElement("div", "machine-empty-activity", machine.reachable ? "尚未在已配置目录中观察到材料文件。" : "电脑不可达，无法读取最近材料元数据。"));
  activitySection.append(activityList);
  card.append(activitySection);

  const stationSection = workstationElement("section", "station-section");
  stationSection.append(workstationElement("h3", "", "两套工作站信号"));
  const stationGrid = workstationElement("div", "station-grid");
  (machine.stations || []).forEach((station) => stationGrid.append(renderStation(station)));
  stationSection.append(stationGrid);
  stationSection.append(workstationElement("p", "machine-assignment-note", machine.assignment_note || ""));
  card.append(stationSection);
  return card;
}

function renderMachines(payload) {
  const machines = Array.isArray(payload?.machines) ? payload.machines : [];
  const fragment = document.createDocumentFragment();
  machines.forEach((machine) => fragment.append(renderMachine(machine)));
  if (!machines.length) fragment.append(workstationElement("div", "start-stop-loading", "暂无实验电脑详情。"));
  workstationElements.workstationMachineList.replaceChildren(fragment);
  workstationElements.workstationMachineList.setAttribute("aria-busy", "false");
}

function livePreviewSvgElement(tag, attributes = {}) {
  const node = document.createElementNS(WORKSTATION_SVG_NS, tag);
  Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, String(value)));
  return node;
}

function livePreviewNumber(value, digits = 3) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return parsed.toFixed(digits).replace(/\.?0+$/, "");
}

function livePreviewElapsed(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const remaining = Math.floor(value % 60);
  return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(remaining).padStart(2, "0")}s`;
}

function renderLivePreviewChart(item) {
  const width = 720;
  const height = 250;
  const margin = { left: 67, right: 18, top: 18, bottom: 43 };
  const chartWidth = width - margin.left - margin.right;
  const chartHeight = height - margin.top - margin.bottom;
  const points = (Array.isArray(item?.points) ? item.points : [])
    .map((point) => [Number(point?.[0]) / 3600, Number(point?.[1])])
    .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  const svg = livePreviewSvgElement("svg", {
    class: "live-preview-chart",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${item?.display_name || "材料"}实时原始电位曲线`,
  });
  if (!points.length) return svg;
  let xMin = Math.min(...points.map((point) => point[0]));
  let xMax = Math.max(...points.map((point) => point[0]));
  let yMin = Math.min(...points.map((point) => point[1]));
  let yMax = Math.max(...points.map((point) => point[1]));
  if (xMax <= xMin) xMax = xMin + 1;
  if (yMax <= yMin) {
    yMin -= 0.01;
    yMax += 0.01;
  } else {
    const padding = Math.max(0.005, (yMax - yMin) * 0.08);
    yMin -= padding;
    yMax += padding;
  }
  const xScale = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * chartWidth;
  const yScale = (value) => margin.top + (1 - (value - yMin) / (yMax - yMin)) * chartHeight;
  for (let index = 0; index <= 4; index += 1) {
    const ratio = index / 4;
    const x = margin.left + ratio * chartWidth;
    const y = margin.top + ratio * chartHeight;
    svg.append(
      livePreviewSvgElement("line", { class: "live-preview-grid-line", x1: x, y1: margin.top, x2: x, y2: margin.top + chartHeight }),
      livePreviewSvgElement("line", { class: "live-preview-grid-line", x1: margin.left, y1: y, x2: margin.left + chartWidth, y2: y }),
    );
    const xText = livePreviewSvgElement("text", { class: "live-preview-tick-label", x, y: margin.top + chartHeight + 20, "text-anchor": "middle" });
    xText.textContent = livePreviewNumber(xMin + ratio * (xMax - xMin), 2);
    const yText = livePreviewSvgElement("text", { class: "live-preview-tick-label", x: margin.left - 10, y: y + 4, "text-anchor": "end" });
    yText.textContent = livePreviewNumber(yMax - ratio * (yMax - yMin), 3);
    svg.append(xText, yText);
  }
  svg.append(
    livePreviewSvgElement("line", { class: "live-preview-axis-line", x1: margin.left, y1: margin.top + chartHeight, x2: margin.left + chartWidth, y2: margin.top + chartHeight }),
    livePreviewSvgElement("line", { class: "live-preview-axis-line", x1: margin.left, y1: margin.top, x2: margin.left, y2: margin.top + chartHeight }),
  );
  const xLabel = livePreviewSvgElement("text", { class: "live-preview-axis-label", x: margin.left + chartWidth / 2, y: height - 9, "text-anchor": "middle" });
  xLabel.textContent = "时间 / h";
  const yLabel = livePreviewSvgElement("text", { class: "live-preview-axis-label", x: 16, y: margin.top + chartHeight / 2, transform: `rotate(-90 16 ${margin.top + chartHeight / 2})`, "text-anchor": "middle" });
  yLabel.textContent = "原始电位 / V vs Hg/HgO";
  const path = points.map(([x, y], index) => `${index ? "L" : "M"}${xScale(x).toFixed(2)},${yScale(y).toFixed(2)}`).join(" ");
  svg.append(livePreviewSvgElement("path", { class: "live-preview-line", d: path }), xLabel, yLabel);
  return svg;
}

function renderLivePreviewCard(item) {
  const card = workstationElement("article", "live-preview-card");
  const heading = workstationElement("div", "live-preview-card-heading");
  const title = workstationElement("div");
  title.append(workstationElement("h3", "", `${item?.favorite ? "★ " : ""}${item?.display_name || "未识别材料"}`));
  title.append(workstationElement("p", "", `${item?.machine_name || "实验电脑"} · ${item?.hostname || "—"}`));
  const phase = workstationElement("span", `live-preview-phase${item?.phase === "恢复段" ? " recovery" : ""}`, item?.phase || "阶段未识别");
  heading.append(title, phase);
  card.append(heading);
  card.append(workstationElement("p", "live-preview-task", item?.task?.task_label || "启停工步未识别"));
  card.append(renderLivePreviewChart(item));
  const metrics = workstationElement("div", "live-preview-card-metrics");
  [
    ["最新原始电位", `${livePreviewNumber(item?.last_potential_v, 5)} V`],
    ["当前密度", `${livePreviewNumber(Number(item?.last_current_a_cm2) * 1000, 1)} mA·cm⁻²`],
    ["文件内时间", livePreviewElapsed(item?.time_end_s)],
    ["完整数据行", Number(item?.complete_row_count || 0).toLocaleString("zh-CN")],
  ].forEach(([label, value]) => {
    const metric = workstationElement("div");
    metric.append(workstationElement("span", "", label), workstationElement("strong", "", value));
    metrics.append(metric);
  });
  card.append(metrics);
  card.append(workstationElement(
    "p",
    "live-preview-source-note",
    `${item?.file_name || "活动文件"} · ${formatMonitorTime(item?.captured_at_utc)} · ${item?.grew_during_snapshot ? "快照期间仍在写入" : "读取固定大小快照"}${item?.skipped_partial_tail ? " · 已跳过末尾未完成行" : ""}`,
  ));
  return card;
}

function renderLivePreview(payload) {
  workstationState.livePreview = payload;
  const available = payload?.available !== false;
  const enabled = payload?.enabled === true;
  const status = String(payload?.last_status || "never_run");
  const statusView = !available
    ? ["unavailable", "预览缓存未就绪"]
    : enabled
    ? status === "running"
      ? ["loading", payload?.message || "正在更新"]
      : status === "completed"
        ? ["ready", "预览已更新"]
        : status === "completed_with_warnings"
          ? ["partial", "部分预览可用"]
          : status === "failed"
            ? ["unavailable", "预览更新失败"]
            : ["loading", "等待首次更新"]
    : ["loading", "实时预览已关闭"];
  workstationElements.livePreviewStatus.className = `monitor-state-chip ${statusView[0]}`;
  workstationElements.livePreviewStatus.textContent = statusView[1];
  const generated = payload?.preview?.generated_at_utc;
  workstationElements.livePreviewDescription.textContent = generated
    ? `最近曲线：${formatMonitorTime(generated)}；固定每 5 分钟更新，只显示完整数据行和 Hg/HgO 原始电位。`
    : "开启后每 5 分钟抓取一次只读快照，只显示完整数据行；电位保持文件中的 Hg/HgO 原始标尺。";
  const items = Array.isArray(payload?.preview?.items) ? payload.preview.items : [];
  const fragment = document.createDocumentFragment();
  if (items.length) {
    items.forEach((item) => fragment.append(renderLivePreviewCard(item)));
  } else {
    fragment.append(workstationElement(
      "div",
      "live-preview-empty",
      !available
        ? payload?.message || "服务器尚未发布实时预览状态。"
        : enabled
          ? payload?.message || "当前没有识别到正在写入的启停文件。"
          : "实时数据预览尚未开启；可在运行与绘图配置页开启。",
    ));
  }
  workstationElements.livePreviewGrid.replaceChildren(fragment);
  workstationElements.livePreviewGrid.setAttribute("aria-busy", "false");
}

function renderLivePreviewError(message) {
  workstationElements.livePreviewStatus.className = "monitor-state-chip unavailable";
  workstationElements.livePreviewStatus.textContent = "预览读取失败";
  workstationElements.livePreviewGrid.replaceChildren(
    workstationElement("div", "live-preview-empty", message || "无法读取实时预览。"),
  );
  workstationElements.livePreviewGrid.setAttribute("aria-busy", "false");
}

function renderWorkstationPayload(payload) {
  workstationState.payload = payload;
  setOverallState(payload);
  const runningStations = renderStationOverview(payload);
  renderMetrics(payload, runningStations);
  renderMachines(payload);
  if (Number(payload?.counts?.unassigned_active_materials || 0) > 0) {
    setNotice("活动任务超过当前六个显示位，请展开详情核对。");
  } else if (["partial", "stale", "unavailable"].includes(payload?.status)) setNotice(payload.message || "工作站监控当前不完整。");
  else setNotice("");
}

async function loadWorkstations({ manual = false } = {}) {
  if (workstationState.loading) return;
  workstationState.loading = true;
  workstationElements.refreshWorkstations.disabled = true;
  workstationElements.refreshWorkstations.setAttribute("aria-busy", "true");
  if (manual) workstationElements.monitorAutoRefresh.textContent = "正在读取服务器缓存…";
  try {
    const [monitorResult, previewResult] = await Promise.allSettled([
      workstationRequest("/api/start-stop/workstations"),
      workstationRequest("/api/start-stop/live-preview"),
    ]);
    if (monitorResult.status === "rejected") throw monitorResult.reason;
    renderWorkstationPayload(monitorResult.value);
    if (previewResult.status === "fulfilled") renderLivePreview(previewResult.value);
    else renderLivePreviewError(previewResult.reason?.message);
    workstationElements.monitorAutoRefresh.textContent = "每 10 秒刷新显示";
  } catch (error) {
    setNotice(error.message || "无法读取工作站监控。");
    workstationElements.monitorOverallState.className = "monitor-state-chip unavailable";
    workstationElements.monitorOverallState.textContent = "读取失败";
    workstationElements.workstationSidebarState.textContent = "监控读取失败";
  } finally {
    workstationState.loading = false;
    workstationElements.refreshWorkstations.disabled = false;
    workstationElements.refreshWorkstations.removeAttribute("aria-busy");
  }
}

async function initializeWorkstations() {
  setWorkstationRoutes();
  workstationElements.refreshWorkstations.addEventListener("click", () => loadWorkstations({ manual: true }));
  try {
    applyServiceStatus(await workstationRequest("/api/start-stop/status"));
  } catch (_error) {
    workstationElements.workbenchVersion.textContent = "版本读取失败";
  }
  await loadWorkstations();
  workstationState.timer = window.setInterval(() => {
    if (document.visibilityState === "visible") loadWorkstations();
  }, 10_000);
}

window.addEventListener("pagehide", () => {
  if (workstationState.timer) window.clearInterval(workstationState.timer);
});

initializeWorkstations();
