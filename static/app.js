const state = {
  runs: [],
  selectedId: null,
  selectedRun: null,
  status: null,
  refreshTimer: null,
  runsRequestId: 0,
};

const elements = {
  totalCount: document.querySelector("#totalCount"),
  parsedCount: document.querySelector("#parsedCount"),
  integrityCount: document.querySelector("#integrityCount"),
  sourceSummary: document.querySelector("#sourceSummary"),
  scanButton: document.querySelector("#scanButton"),
  searchInput: document.querySelector("#searchInput"),
  instrumentFilter: document.querySelector("#instrumentFilter"),
  techniqueFilter: document.querySelector("#techniqueFilter"),
  runList: document.querySelector("#runList"),
  detailInstrument: document.querySelector("#detailInstrument"),
  detailTitle: document.querySelector("#detailTitle"),
  detailTechnique: document.querySelector("#detailTechnique"),
  detailHash: document.querySelector("#detailHash"),
  curveChart: document.querySelector("#curveChart"),
  chartEmpty: document.querySelector("#chartEmpty"),
  chartStats: document.querySelector("#chartStats"),
  metadataForm: document.querySelector("#metadataForm"),
  recordId: document.querySelector("#recordId"),
  sampleId: document.querySelector("#sampleId"),
  material: document.querySelector("#material"),
  electrolyte: document.querySelector("#electrolyte"),
  areaCm2: document.querySelector("#areaCm2"),
  tags: document.querySelector("#tags"),
  notes: document.querySelector("#notes"),
  saveMetadata: document.querySelector("#saveMetadata"),
  saveState: document.querySelector("#saveState"),
  auditList: document.querySelector("#auditList"),
  watchRoots: document.querySelector("#watchRoots"),
  toast: document.querySelector("#toast"),
};

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    cache: "no-store",
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatNumber(value) {
  if (!Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  if (magnitude !== 0 && (magnitude >= 1e4 || magnitude < 1e-3)) {
    return value.toExponential(3);
  }
  return new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 5 }).format(value);
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.style.background = isError ? "#9d3b2c" : "#17212b";
  elements.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => elements.toast.classList.remove("show"), 2600);
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  elements.totalCount.textContent = status.total;
  elements.parsedCount.textContent = status.parsed;
  elements.integrityCount.textContent = status.total;
  const available = status.watch_roots.filter((root) => root.available).length;
  elements.sourceSummary.textContent = `${available}/${status.watch_roots.length} 个目录可用`;
  elements.watchRoots.innerHTML = status.watch_roots
    .map(
      (root) =>
        `<span class="root-chip ${root.available ? "" : "missing"}" title="${escapeHtml(root.path)}">` +
        `${escapeHtml(root.path)}${root.available ? "" : " · 不可用"}</span>`,
    )
    .join("");
}

function renderRunList() {
  if (!state.runs.length) {
    elements.runList.innerHTML = `<div class="empty-list">当前筛选条件下没有数据记录</div>`;
    return;
  }
  elements.runList.innerHTML = state.runs
    .map((run) => {
      const label = run.sample_id || run.source_name;
      const secondary = run.sample_id ? run.source_name : run.instrument;
      return `
        <button class="run-item ${run.id === state.selectedId ? "active" : ""}" data-run-id="${run.id}" type="button">
          <span class="run-item-top">
            <span class="run-item-name" title="${escapeHtml(label)}">${escapeHtml(label)}</span>
            <span class="run-item-method">${escapeHtml(run.technique)}</span>
          </span>
          <span class="run-item-meta">
            <span>${escapeHtml(secondary)}</span>
            <span>${run.point_count.toLocaleString("zh-CN")} 点 · ${formatDate(run.modified_utc)}</span>
          </span>
        </button>`;
    })
    .join("");
  elements.runList.querySelectorAll(".run-item").forEach((button) => {
    button.addEventListener("click", () => selectRun(Number(button.dataset.runId)));
  });
}

function fillMetadata(run) {
  elements.recordId.value = run?.id ?? "";
  elements.sampleId.value = run?.sample_id ?? "";
  elements.material.value = run?.material ?? "";
  elements.electrolyte.value = run?.electrolyte ?? "";
  elements.areaCm2.value = run?.area_cm2 ?? "";
  elements.tags.value = run?.tags ?? "";
  elements.notes.value = run?.notes ?? "";
  elements.saveMetadata.disabled = !run;
}

function drawChart(run) {
  const canvas = elements.curveChart;
  const points = run?.points || [];
  if (!points.length) {
    elements.chartEmpty.style.display = "grid";
    elements.chartStats.innerHTML = run
      ? `<span class="stat-chip">${escapeHtml(run.parse_error || "该文件当前仅登记元数据")}</span>`
      : "";
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  elements.chartEmpty.style.display = "none";

  const rect = canvas.getBoundingClientRect();
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);

  const width = rect.width;
  const height = rect.height;
  const margin = { left: 72, right: 22, top: 20, bottom: 52 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  let xMin = Math.min(...points.map((point) => point[0]));
  let xMax = Math.max(...points.map((point) => point[0]));
  let yMin = Math.min(...points.map((point) => point[1]));
  let yMax = Math.max(...points.map((point) => point[1]));
  if (xMin === xMax) [xMin, xMax] = [xMin - 1, xMax + 1];
  if (yMin === yMax) [yMin, yMax] = [yMin - 1, yMax + 1];
  const xPadding = (xMax - xMin) * 0.03;
  const yPadding = (yMax - yMin) * 0.08;
  xMin -= xPadding;
  xMax += xPadding;
  yMin -= yPadding;
  yMax += yPadding;

  const xScale = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * plotWidth;
  const yScale = (value) => margin.top + (1 - (value - yMin) / (yMax - yMin)) * plotHeight;

  context.clearRect(0, 0, width, height);
  context.lineWidth = 1;
  context.font = '10px Inter, "PingFang SC", sans-serif';
  context.fillStyle = "#667480";
  context.strokeStyle = "#dce3e6";

  for (let index = 0; index <= 5; index += 1) {
    const x = margin.left + (index / 5) * plotWidth;
    const y = margin.top + (index / 5) * plotHeight;
    context.beginPath();
    context.moveTo(x, margin.top);
    context.lineTo(x, margin.top + plotHeight);
    context.stroke();
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(margin.left + plotWidth, y);
    context.stroke();

    const xValue = xMin + (index / 5) * (xMax - xMin);
    const yValue = yMax - (index / 5) * (yMax - yMin);
    context.textAlign = "center";
    context.fillText(formatNumber(xValue), x, margin.top + plotHeight + 19);
    context.textAlign = "right";
    context.fillText(formatNumber(yValue), margin.left - 9, y + 3);
  }

  if (yMin < 0 && yMax > 0) {
    context.strokeStyle = "rgba(23, 33, 43, 0.28)";
    context.beginPath();
    context.moveTo(margin.left, yScale(0));
    context.lineTo(margin.left + plotWidth, yScale(0));
    context.stroke();
  }

  const gradient = context.createLinearGradient(margin.left, 0, margin.left + plotWidth, 0);
  gradient.addColorStop(0, "#e9862b");
  gradient.addColorStop(0.45, "#087f75");
  gradient.addColorStop(1, "#075e58");
  context.strokeStyle = gradient;
  context.lineWidth = 2.1;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.beginPath();
  points.forEach((point, index) => {
    const x = xScale(point[0]);
    const y = yScale(point[1]);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.stroke();

  context.fillStyle = "#3c4a52";
  context.textAlign = "center";
  context.font = '11px Inter, "PingFang SC", sans-serif';
  context.fillText(run.x_name || "X", margin.left + plotWidth / 2, height - 13);
  context.save();
  context.translate(17, margin.top + plotHeight / 2);
  context.rotate(-Math.PI / 2);
  context.fillText(run.y_name || "Y", 0, 0);
  context.restore();

  elements.chartStats.innerHTML = [
    `${run.point_count.toLocaleString("zh-CN")} 个原始点`,
    `X: ${formatNumber(Math.min(...points.map((p) => p[0])))} → ${formatNumber(Math.max(...points.map((p) => p[0])))}`,
    `Y: ${formatNumber(Math.min(...points.map((p) => p[1])))} → ${formatNumber(Math.max(...points.map((p) => p[1])))}`,
    run.encoding,
  ]
    .map((value) => `<span class="stat-chip">${escapeHtml(value)}</span>`)
    .join("");
}

function renderDetail(run) {
  state.selectedRun = run;
  elements.detailInstrument.textContent = run ? run.instrument : "选择一条记录";
  elements.detailTitle.textContent = run ? run.source_name : "曲线预览";
  elements.detailTechnique.textContent = run ? run.technique : "—";
  elements.detailHash.textContent = run ? `SHA ${run.sha256.slice(0, 12)}…` : "SHA-256";
  elements.detailHash.title = run?.sha256 ?? "";
  fillMetadata(run);
  drawChart(run);
}

function renderAudit(items) {
  if (!items.length) {
    elements.auditList.innerHTML = `<div class="empty-list">尚无审计记录</div>`;
    return;
  }
  const labels = {
    imported: "导入记录",
    updated: "源文件更新",
    metadata_updated: "样品信息更新",
    deferred: "暂缓读取",
    read_error: "读取异常",
  };
  elements.auditList.innerHTML = items
    .map(
      (item) => `
        <div class="audit-item">
          <span class="audit-dot"></span>
          <div>
            <div class="audit-main">${escapeHtml(labels[item.action] || item.action)} · ${escapeHtml(item.target)}</div>
            <div class="audit-detail">${escapeHtml(item.detail)}</div>
            <div class="audit-time">${formatDate(item.created_utc)}</div>
          </div>
        </div>`,
    )
    .join("");
}

async function loadStatus() {
  state.status = await request("/api/status");
  renderStatus();
}

async function loadRuns(preserveSelection = true) {
  const requestId = ++state.runsRequestId;
  const parameters = new URLSearchParams();
  if (elements.instrumentFilter.value) parameters.set("instrument", elements.instrumentFilter.value);
  if (elements.techniqueFilter.value) parameters.set("technique", elements.techniqueFilter.value);
  if (elements.searchInput.value.trim()) parameters.set("q", elements.searchInput.value.trim());
  const runs = await request(`/api/runs?${parameters.toString()}`);
  if (requestId !== state.runsRequestId) return;
  state.runs = runs;
  if (!preserveSelection || !state.runs.some((run) => run.id === state.selectedId)) {
    state.selectedId = state.runs[0]?.id ?? null;
  }
  renderRunList();
  if (state.selectedId) await selectRun(state.selectedId, false);
  else renderDetail(null);
}

async function loadAudit() {
  renderAudit(await request("/api/audit?limit=30"));
}

async function selectRun(runId, rerenderList = true) {
  state.selectedId = runId;
  if (rerenderList) renderRunList();
  const run = await request(`/api/runs/${runId}`);
  renderDetail(run);
}

async function refreshAll(preserveSelection = true) {
  try {
    await Promise.all([loadStatus(), loadAudit()]);
    await loadRuns(preserveSelection);
  } catch (error) {
    showToast(error.message, true);
  }
}

elements.scanButton.addEventListener("click", async () => {
  elements.scanButton.disabled = true;
  elements.scanButton.textContent = "扫描中…";
  try {
    const result = await request("/api/scan", { method: "POST", body: "{}" });
    if (result.status === "busy") showToast("后台扫描正在进行");
    else {
      showToast(`扫描完成：新增 ${result.imported}，更新 ${result.updated}`);
      await refreshAll();
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    elements.scanButton.disabled = false;
    elements.scanButton.textContent = "立即扫描";
  }
});

let searchDelay;
elements.searchInput.addEventListener("input", () => {
  window.clearTimeout(searchDelay);
  searchDelay = window.setTimeout(() => loadRuns(false), 220);
});
elements.instrumentFilter.addEventListener("change", () => loadRuns(false));
elements.techniqueFilter.addEventListener("change", () => loadRuns(false));

elements.metadataForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const runId = Number(elements.recordId.value);
  if (!runId) return;
  elements.saveMetadata.disabled = true;
  elements.saveState.textContent = "保存中…";
  const payload = {
    sample_id: elements.sampleId.value,
    material: elements.material.value,
    electrolyte: elements.electrolyte.value,
    area_cm2: elements.areaCm2.value,
    tags: elements.tags.value,
    notes: elements.notes.value,
  };
  try {
    const run = await request(`/api/runs/${runId}/metadata`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderDetail(run);
    await Promise.all([loadRuns(), loadAudit()]);
    elements.saveState.textContent = "已保存";
    showToast("样品信息已保存到平台数据库");
  } catch (error) {
    elements.saveState.textContent = "保存失败";
    showToast(error.message, true);
  } finally {
    elements.saveMetadata.disabled = false;
    window.setTimeout(() => (elements.saveState.textContent = ""), 2000);
  }
});

window.addEventListener("resize", () => {
  if (state.selectedRun) drawChart(state.selectedRun);
});

refreshAll(false);
state.refreshTimer = window.setInterval(() => {
  Promise.all([loadStatus(), loadAudit()]).catch(() => {});
}, 10000);
