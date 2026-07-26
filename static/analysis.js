// Data-analysis module state.
const state = {
  tree: null,
  runs: [],
  selectedId: null,
  selectedRun: null,
  runsRequestId: 0,
  detailRequestId: 0,
  analysisRequestId: 0,
  analysisHistoryRequestId: 0,
  analysisPreview: null,
  analysisPreviewSaved: false,
  analysisDirty: false,
  analysisBusy: false,
  openFolders: new Set(),
};

const elements = {
  scanButton: document.querySelector("#scanButton"),
  searchInput: document.querySelector("#searchInput"),
  techniqueFilter: document.querySelector("#techniqueFilter"),
  treeCount: document.querySelector("#treeCount"),
  fileTree: document.querySelector("#fileTree"),
  detailInstrument: document.querySelector("#detailInstrument"),
  detailTitle: document.querySelector("#detailTitle"),
  detailTechnique: document.querySelector("#detailTechnique"),
  detailHash: document.querySelector("#detailHash"),
  curveChart: document.querySelector("#curveChart"),
  chartEmpty: document.querySelector("#chartEmpty"),
  chartEmptyMessage: document.querySelector("#chartEmpty p"),
  chartStats: document.querySelector("#chartStats"),
  methodAnalysisPanel: document.querySelector("#methodAnalysisPanel"),
  analysisModeBadge: document.querySelector("#analysisModeBadge"),
  analysisSaveState: document.querySelector("#analysisSaveState"),
  analysisWelcome: document.querySelector("#analysisWelcome"),
  analysisUnsupported: document.querySelector("#analysisUnsupported"),
  analysisWorkspace: document.querySelector("#analysisWorkspace"),
  analysisForm: document.querySelector("#analysisForm"),
  analysisError: document.querySelector("#analysisError"),
  eisFields: document.querySelector("#eisFields"),
  cvFields: document.querySelector("#cvFields"),
  cvSolution: document.querySelector("#cvSolution"),
  cvSolutionCustomField: document.querySelector("#cvSolutionCustomField"),
  cvSolutionCustom: document.querySelector("#cvSolutionCustom"),
  cvPh: document.querySelector("#cvPh"),
  cvReaction: document.querySelector("#cvReaction"),
  cvReference: document.querySelector("#cvReference"),
  cvReferenceCustomField: document.querySelector("#cvReferenceCustomField"),
  cvReferenceCustom: document.querySelector("#cvReferenceCustom"),
  cvReferenceOffset: document.querySelector("#cvReferenceOffset"),
  cvCompensation: document.querySelector("#cvCompensation"),
  cvResistance: document.querySelector("#cvResistance"),
  cvArea: document.querySelector("#cvArea"),
  cvTargetCurrent: document.querySelector("#cvTargetCurrent"),
  cvScanBranch: document.querySelector("#cvScanBranch"),
  cvOnlineCompensation: document.querySelector("#cvOnlineCompensation"),
  previewAnalysis: document.querySelector("#previewAnalysis"),
  saveAnalysis: document.querySelector("#saveAnalysis"),
  analysisResults: document.querySelector("#analysisResults"),
  analysisResultFreshness: document.querySelector("#analysisResultFreshness"),
  analysisHistoryCount: document.querySelector("#analysisHistoryCount"),
  analysisHistoryList: document.querySelector("#analysisHistoryList"),
  toast: document.querySelector("#toast"),
};

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    cache: "no-store",
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    throw error;
  }
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

function makeFolderNode(name = "") {
  return { name, folders: new Map(), files: [] };
}

function buildNestedTree(root) {
  const node = makeFolderNode(root.name);
  root.files.forEach((run) => {
    const parts = run.relative_path.split("/").filter(Boolean);
    const fileName = parts.pop() || run.source_name;
    let current = node;
    parts.forEach((part) => {
      if (!current.folders.has(part)) {
        current.folders.set(part, makeFolderNode(part));
      }
      current = current.folders.get(part);
    });
    current.files.push({ ...run, source_name: fileName });
  });
  return node;
}

function selectedFolderKeys() {
  const selected = state.runs.find((run) => run.id === state.selectedId);
  if (!selected) return new Set();
  const keys = new Set([selected.root_id]);
  const parts = selected.relative_path.split("/").filter(Boolean);
  parts.pop();
  let key = selected.root_id;
  parts.forEach((part) => {
    key = `${key}/${part}`;
    keys.add(key);
  });
  return keys;
}

function renderFileButton(run) {
  const sourceState = run.source_available
    ? ""
    : '<span class="source-state">源文件不可用</span>';
  const parsedState = run.parse_status === "metadata_only"
    ? "仅元数据"
    : `${run.point_count.toLocaleString("zh-CN")} 点`;
  return `
    <button
      class="file-node ${run.id === state.selectedId ? "active" : ""} ${run.source_available ? "" : "source-missing"}"
      data-run-id="${run.id}"
      type="button"
      title="${escapeHtml(run.relative_path)}"
      ${run.id === state.selectedId ? 'aria-current="true"' : ""}
    >
      <span class="file-node-main">
        <span class="file-node-name">${escapeHtml(run.source_name)}</span>
        <span class="file-node-badges">
          <span class="parser-badge">${escapeHtml(run.instrument)}</span>
          <span class="technique-badge">${escapeHtml(run.technique)}</span>
        </span>
      </span>
      <span class="file-node-meta">
        <span>${escapeHtml(parsedState)}</span>
        <span class="file-time">${formatDate(run.modified_utc)}</span>
        ${sourceState}
      </span>
    </button>`;
}

function renderFolderContents(node, key, depth, forceOpen) {
  const folders = [...node.folders.values()].sort((a, b) =>
    a.name.localeCompare(b.name, "zh-CN", { numeric: true }),
  );
  const files = [...node.files].sort((a, b) =>
    new Date(b.modified_utc).getTime() - new Date(a.modified_utc).getTime(),
  );
  const folderMarkup = folders
    .map((folder) => {
      const folderKey = `${key}/${folder.name}`;
      const count = countFolderFiles(folder);
      const open = forceOpen || state.openFolders.has(folderKey);
      return `
        <details class="folder-node nested-folder" data-folder-key="${escapeHtml(folderKey)}" ${open ? "open" : ""}>
          <summary>
            <span class="folder-summary-content">
              <span class="folder-name">${escapeHtml(folder.name)}</span>
              <span class="folder-count">${count}</span>
            </span>
          </summary>
          <div class="folder-children">
            ${renderFolderContents(folder, folderKey, depth + 1, forceOpen)}
          </div>
        </details>`;
    })
    .join("");
  return folderMarkup + files.map(renderFileButton).join("");
}

function countFolderFiles(node) {
  return node.files.length + [...node.folders.values()]
    .reduce((sum, child) => sum + countFolderFiles(child), 0);
}

function renderFileTree() {
  const tree = state.tree;
  const selectedKeys = selectedFolderKeys();
  selectedKeys.forEach((key) => state.openFolders.add(key));
  const forceOpen = Boolean(elements.searchInput.value.trim());
  elements.treeCount.textContent = tree?.truncated
    ? `${tree.total}+ 个文件`
    : `${tree?.total || 0} 个文件`;
  if (!tree || !tree.roots.length) {
    elements.fileTree.innerHTML = '<div class="empty-list">尚未配置数据文件夹</div>';
    return;
  }
  elements.fileTree.innerHTML = tree.roots
    .map((root) => {
      const rootNode = buildNestedTree(root);
      const rootOpen = forceOpen || state.openFolders.has(root.id) || root.file_count > 0;
      const availability = root.available ? "" : '<span class="folder-state">不可用</span>';
      const empty = root.file_count
        ? renderFolderContents(rootNode, root.id, 0, forceOpen)
        : '<div class="empty-folder">该目录下没有符合条件的实验文件</div>';
      return `
        <details class="folder-node tree-root" data-folder-key="${escapeHtml(root.id)}" ${rootOpen ? "open" : ""}>
          <summary>
            <span class="folder-summary-content">
              <span class="folder-name">${escapeHtml(root.name)}</span>
              <span class="folder-count">${root.file_count}</span>
              ${availability}
            </span>
          </summary>
          <div class="folder-children">${empty}</div>
        </details>`;
    })
    .join("");
  elements.fileTree.querySelectorAll("details[data-folder-key]").forEach((details) => {
    details.addEventListener("toggle", () => {
      if (details.open) state.openFolders.add(details.dataset.folderKey);
      else state.openFolders.delete(details.dataset.folderKey);
    });
  });
  elements.fileTree.querySelectorAll(".file-node").forEach((button) => {
    button.addEventListener("click", () => selectRun(Number(button.dataset.runId)));
  });
}

function sourceAvailabilityChip(run) {
  if (!run) return "";
  const className = run.source_available ? "source-ok" : "source-warning";
  const label = run.source_available ? "源文件可用" : "源文件已移动或删除";
  return `<span class="stat-chip ${className}">${label}</span>`;
}

function analysisTypeForRun(run) {
  const technique = String(run?.technique || "").trim().toUpperCase();
  if (technique === "EIS") return "eis_resistance";
  if (technique === "CV") return "cv_overpotential";
  return null;
}

function drawChart(run) {
  const canvas = elements.curveChart;
  const points = run?.points || [];
  if (!points.length) {
    elements.chartEmpty.style.display = "grid";
    elements.chartEmptyMessage.textContent = run
      ? "该文件当前没有可绘制的二维曲线"
      : "从左侧数据文件夹中选择一个实验文件";
    elements.chartStats.innerHTML = run
      ? `<span class="stat-chip">${escapeHtml(run.parse_error || "该文件当前仅登记元数据")}</span>${sourceAvailabilityChip(run)}`
      : "";
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  elements.chartEmpty.style.display = "none";
  const isEis = analysisTypeForRun(run) === "eis_resistance";
  const eisYAlreadyNegated = /^\s*[-−]\s*z/i.test(String(run?.y_name || ""));
  const displayPoints = isEis
    ? points.map((point) => [point[0], (eisYAlreadyNegated ? 1 : -1) * point[1]])
    : points;

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
  let xMin = Math.min(...displayPoints.map((point) => point[0]));
  let xMax = Math.max(...displayPoints.map((point) => point[0]));
  let yMin = Math.min(...displayPoints.map((point) => point[1]));
  let yMax = Math.max(...displayPoints.map((point) => point[1]));
  if (xMin === xMax) [xMin, xMax] = [xMin - 1, xMax + 1];
  if (yMin === yMax) [yMin, yMax] = [yMin - 1, yMax + 1];
  const xPadding = (xMax - xMin) * 0.03;
  const yPadding = (yMax - yMin) * 0.08;
  xMin -= xPadding;
  xMax += xPadding;
  yMin -= yPadding;
  yMax += yPadding;
  if (isEis) {
    const xCenter = (xMin + xMax) / 2;
    const yCenter = (yMin + yMax) / 2;
    const unitsPerPixel = Math.max(
      (xMax - xMin) / plotWidth,
      (yMax - yMin) / plotHeight,
    );
    const equalXSpan = unitsPerPixel * plotWidth;
    const equalYSpan = unitsPerPixel * plotHeight;
    xMin = xCenter - equalXSpan / 2;
    xMax = xCenter + equalXSpan / 2;
    yMin = yCenter - equalYSpan / 2;
    yMax = yCenter + equalYSpan / 2;
  }

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

  if (isEis) {
    context.strokeStyle = "#087f75";
  } else {
    const gradient = context.createLinearGradient(margin.left, 0, margin.left + plotWidth, 0);
    gradient.addColorStop(0, "#e9862b");
    gradient.addColorStop(0.45, "#087f75");
    gradient.addColorStop(1, "#075e58");
    context.strokeStyle = gradient;
  }
  context.lineWidth = 2.1;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.beginPath();
  displayPoints.forEach((point, index) => {
    const x = xScale(point[0]);
    const y = yScale(point[1]);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.stroke();

  context.fillStyle = "#3c4a52";
  context.textAlign = "center";
  context.font = '11px Inter, "PingFang SC", sans-serif';
  const xAxisName = run.x_name || "X";
  const yAxisName = isEis
    ? (eisYAlreadyNegated ? "−Z″" : "−Z″（由原始 Z″ 取负显示）")
    : (run.y_name || "Y");
  context.fillText(xAxisName, margin.left + plotWidth / 2, height - 13);
  context.save();
  context.translate(17, margin.top + plotHeight / 2);
  context.rotate(-Math.PI / 2);
  context.fillText(yAxisName, 0, 0);
  context.restore();
  canvas.setAttribute(
    "aria-label",
    isEis
      ? `EIS Nyquist 原始曲线，横轴 ${xAxisName}，纵轴负虚部，共 ${run.point_count} 个原始点`
      : `${run.technique || "电化学"}原始曲线，横轴 ${xAxisName}，纵轴 ${run.y_name || "Y"}，共 ${run.point_count} 个原始点`,
  );

  elements.chartStats.innerHTML = [
    `${run.point_count.toLocaleString("zh-CN")} 个原始点`,
    `X: ${formatNumber(Math.min(...displayPoints.map((p) => p[0])))} → ${formatNumber(Math.max(...displayPoints.map((p) => p[0])))}`,
    `${isEis ? "−Z″" : "Y"}: ${formatNumber(Math.min(...displayPoints.map((p) => p[1])))} → ${formatNumber(Math.max(...displayPoints.map((p) => p[1])))}`,
    isEis ? "Nyquist 等比例坐标 · 纵轴显示 −Z″" : null,
    run.encoding,
    run.parser_id,
  ]
    .filter(Boolean)
    .map((value) => `<span class="stat-chip">${escapeHtml(value)}</span>`)
    .join("") + sourceAvailabilityChip(run);
}

function setAnalysisError(message = "") {
  elements.analysisError.textContent = message;
  elements.analysisError.hidden = !message;
}

function resetAnalysisResult() {
  state.analysisPreview = null;
  state.analysisPreviewSaved = false;
  state.analysisDirty = false;
  state.analysisBusy = false;
  elements.saveAnalysis.disabled = true;
  elements.analysisResults.setAttribute("aria-busy", "false");
  elements.analysisResultFreshness.textContent = "尚未计算";
  elements.analysisResultFreshness.className = "result-freshness";
  elements.analysisResults.innerHTML =
    '<div class="analysis-result-empty">设置参数后预览；平台会显示公式、单位与质量提示。</div>';
  setAnalysisError();
}

function analysisSourceReady(run = state.selectedRun) {
  return Boolean(
    analysisTypeForRun(run) &&
    run?.source_available &&
    Array.isArray(run?.points) &&
    run.points.length,
  );
}

function syncAnalysisControls(run = state.selectedRun) {
  const analysisType = analysisTypeForRun(run);
  elements.eisFields.disabled =
    state.analysisBusy || analysisType !== "eis_resistance";
  elements.cvFields.disabled =
    state.analysisBusy || analysisType !== "cv_overpotential";
  elements.previewAnalysis.disabled =
    state.analysisBusy || !analysisSourceReady(run);
  elements.saveAnalysis.disabled =
    state.analysisBusy ||
    !state.analysisPreview ||
    state.analysisPreviewSaved ||
    state.analysisDirty;
}

function setAnalysisBusy(busy) {
  state.analysisBusy = busy;
  elements.analysisResults.setAttribute("aria-busy", String(busy));
  syncAnalysisControls();
}

function selectedOption(control) {
  return control.options[control.selectedIndex] || null;
}

function selectedSolutionValue() {
  return elements.cvSolution.value === "custom"
    ? elements.cvSolutionCustom.value.trim()
    : elements.cvSolution.value;
}

function selectedReferenceValue() {
  return elements.cvReference.value === "custom"
    ? elements.cvReferenceCustom.value.trim()
    : elements.cvReference.value;
}

function normalizeSolutionLabel(value) {
  return String(value)
    .trim()
    .toLowerCase()
    .replaceAll(/\s+/g, "")
    .replace(/^(\d+(?:\.\d+)?)m/, (_match, concentration) =>
      `${Number(concentration)}m`,
    );
}

function syncSolutionPreset(resetCustom = false) {
  const option = selectedOption(elements.cvSolution);
  const custom = elements.cvSolution.value === "custom";
  elements.cvSolutionCustomField.hidden = !custom;
  elements.cvSolutionCustom.disabled = !custom;
  elements.cvSolutionCustom.required = custom;
  if (custom) {
    if (resetCustom) {
      elements.cvSolutionCustom.value = "";
      elements.cvPh.value = "";
    }
    elements.cvPh.placeholder = "输入实测 pH";
    return;
  }
  elements.cvSolutionCustom.value = "";
  elements.cvPh.value = option?.dataset.ph || "";
  elements.cvPh.placeholder = option?.dataset.ph
    ? "按所选溶液自动填写"
    : "选择溶液后自动填写";
}

function selectSolutionForRun(electrolyte = "") {
  const savedSolution = String(electrolyte).trim();
  const normalizedSavedSolution = normalizeSolutionLabel(savedSolution);
  const preset = [...elements.cvSolution.options].find(
    (option) =>
      option.value &&
      option.value !== "custom" &&
      normalizeSolutionLabel(option.value) === normalizedSavedSolution,
  );
  elements.cvSolutionCustom.value = "";
  elements.cvPh.value = "";
  if (preset) {
    elements.cvSolution.value = preset.value;
  } else if (savedSolution) {
    elements.cvSolution.value = "custom";
    elements.cvSolutionCustom.value = savedSolution;
  } else {
    elements.cvSolution.value = "";
  }
  syncSolutionPreset();
}

function syncReferenceOffset(resetCustom = false) {
  const option = selectedOption(elements.cvReference);
  const custom = elements.cvReference.value === "custom";
  elements.cvReferenceCustomField.hidden = !custom;
  elements.cvReferenceCustom.disabled = !custom;
  elements.cvReferenceCustom.required = custom;
  elements.cvReferenceOffset.readOnly = !custom;
  if (custom) {
    if (resetCustom) {
      elements.cvReferenceCustom.value = "";
      elements.cvReferenceOffset.value = "";
    }
    elements.cvReferenceOffset.placeholder = "输入实测标定值";
    return;
  }
  elements.cvReferenceCustom.value = "";
  elements.cvReferenceOffset.value = option?.dataset.offset || "";
  elements.cvReferenceOffset.placeholder = option?.dataset.offset !== undefined
    ? "按所选参比电极自动填写"
    : "选择参比电极后自动填写";
}

function configureAnalysis(run, preserveResult = false) {
  const analysisType = analysisTypeForRun(run);
  elements.analysisSaveState.textContent = "";
  elements.analysisWelcome.hidden = Boolean(run);
  elements.analysisUnsupported.hidden = !run || Boolean(analysisType);
  elements.analysisWorkspace.hidden = !analysisType;
  elements.eisFields.hidden = analysisType !== "eis_resistance";
  elements.cvFields.hidden = analysisType !== "cv_overpotential";
  elements.analysisModeBadge.textContent = !run
    ? "等待选择"
    : analysisType === "eis_resistance"
      ? "EIS 电阻分析"
      : analysisType === "cv_overpotential"
        ? "CV 过电位分析"
        : "仅原始曲线";
  if (analysisType === "cv_overpotential" && !preserveResult) {
    selectSolutionForRun(run.electrolyte);
    elements.cvReaction.value = "HER";
    elements.cvReference.value = "";
    elements.cvReferenceCustom.value = "";
    elements.cvReferenceOffset.value = "";
    syncReferenceOffset();
    elements.cvCompensation.value = "85";
    elements.cvCompensation.disabled = false;
    elements.cvResistance.value = "";
    elements.cvArea.value = run.area_cm2 || "";
    elements.cvTargetCurrent.value = "10";
    elements.cvScanBranch.innerHTML = `
      <option value="auto">自动（仅单分支）</option>
      <option value="forward">正扫</option>
      <option value="reverse">回扫</option>`;
    elements.cvScanBranch.value = "auto";
    elements.cvOnlineCompensation.value = "not_compensated";
  }
  if (!preserveResult) resetAnalysisResult();
  const sourceReady = analysisSourceReady(run);
  syncAnalysisControls(run);
  if (analysisType && !sourceReady) {
    setAnalysisError("完整源文件当前不可用或没有可解析数据，因此不能执行定量分析。上方缓存曲线仍可检视。");
  }
  elements.analysisHistoryCount.textContent = "0";
  elements.analysisHistoryList.innerHTML =
    '<div class="analysis-history-empty">尚无已保存记录</div>';
}

function pickValue(object, paths) {
  for (const path of paths) {
    let value = object;
    for (const key of path.split(".")) value = value?.[key];
    if (value !== undefined && value !== null) return value;
  }
  return null;
}

function displayMetric(value, unit = "") {
  if (typeof value === "number") return `${formatNumber(value)}${unit ? ` ${unit}` : ""}`;
  if (value === null || value === undefined || value === "") return "—";
  return `${escapeHtml(value)}${unit ? ` ${unit}` : ""}`;
}

function resultWarnings(result, analysis = null) {
  const sources = [
    analysis?.warnings,
    result?.warnings,
    result?.quality_warnings,
  ];
  const warnings = sources.flatMap((value) =>
    Array.isArray(value) ? value : (value ? [value] : []),
  );
  return [...new Set(warnings.filter(Boolean))];
}

function renderResultMetrics(analysis) {
  const type = analysis.analysis_type;
  const result = analysis.result || {};
  let metrics;
  if (type === "eis_resistance") {
    metrics = [
      {
        label: "溶液电阻 Rs",
        value: pickValue(result, [
          "rs_ohm",
          "solution_resistance",
          "solution_resistance_ohm",
          "rs",
          "high_frequency_intercept_ohm",
          "high_frequency_crossing.z_real",
        ]),
        unit: pickValue(result, ["resistance_unit", "unit"]) || "Ω",
      },
      {
        label: "低频实轴交点",
        value: pickValue(result, [
          "low_frequency_intercept_ohm",
          "low_frequency_intercept",
          "low_frequency_crossing.z_real",
        ]),
        unit: pickValue(result, ["resistance_unit", "unit"]) || "Ω",
      },
      {
        label: "表观弧直径",
        value: pickValue(result, [
          "apparent_arc_resistance_ohm",
          "apparent_polarization_resistance",
          "apparent_rp_ohm",
          "arc_diameter_ohm",
          "rp_screening_ohm",
        ]),
        unit: pickValue(result, ["resistance_unit", "unit"]) || "Ω",
      },
      {
        label: "完整分析点",
        value: pickValue(result, ["point_count", "analysis_point_count", "points_used"]),
        unit: "点",
      },
    ];
  } else {
    metrics = [
      {
        label: "目标过电位 η",
        value: pickValue(result, ["target_point.overpotential_mv", "overpotential_mv", "eta_mv", "overpotential"]),
        unit: "mV",
      },
      {
        label: "iR 修正后电位",
        value: pickValue(result, [
          "corrected_potential_rhe_v",
          "corrected_potential_v",
          "target_point.compensated_potential_v",
          "e_comp_v",
          "potential_at_target_v",
        ]),
        unit: "V vs RHE",
      },
      {
        label: "原始测量电位",
        value: pickValue(result, ["target_point.measured_potential_v", "measured_potential_v", "raw_potential_v", "e_measured_v"]),
        unit: "V",
      },
      {
        label: "目标电流密度",
        value: pickValue(result, ["target_point.current_density_ma_cm2", "target_current_density_ma_cm2", "target_j_ma_cm2"]),
        unit: "mA·cm⁻²",
      },
    ];
  }
  return metrics
    .map(
      (metric) => `
        <div class="analysis-metric">
          <span>${escapeHtml(metric.label)}</span>
          <strong>${displayMetric(metric.value, metric.unit)}</strong>
        </div>`,
    )
    .join("");
}

function renderAnalysisResult(analysis, saved = false) {
  const result = analysis.result || {};
  const warnings = resultWarnings(result, analysis);
  const parameters = analysis.parameters || {};
  const type = analysis.analysis_type;
  const formula = type === "cv_overpotential"
    ? [
        pickValue(result, ["formula.rhe"]) ||
          "E_RHE = E_measured + E_reference_vs_SHE + 0.05916 × pH",
        pickValue(result, ["formula.ir"]) ||
          "E_compensated = E_RHE - compensation_fraction × I × R_solution",
        pickValue(result, ["formula.overpotential"]) ||
          "按 HER/OER 方向校验有符号过电位后报告幅值",
      ].join("；")
    : "Rs = 高频端 Z″ = 0 时的 Z′ 线性插值";
  const traceEntries = type === "cv_overpotential"
    ? [
        ["溶液", parameters.solution],
        ["pH", parameters.ph],
        ["反应", parameters.reaction],
        ["参比", parameters.reference_electrode],
        ["参比电势", parameters.reference_offset_v == null ? null : `${parameters.reference_offset_v} V vs SHE`],
        ["补偿", parameters.compensation_percent == null ? null : `${parameters.compensation_percent}%`],
        ["Rs", parameters.solution_resistance_ohm == null ? null : `${parameters.solution_resistance_ohm} Ω`],
        ["扫描分支", parameters.scan_branch],
      ]
    : [
        ["算法", analysis.algorithm_id],
        ["算法版本", analysis.algorithm_version],
        ["源 SHA", analysis.source_sha256 ? `${analysis.source_sha256.slice(0, 12)}…` : null],
      ];
  const statusMessage = pickValue(result, ["message", "quality_note", "status_detail"]);
  elements.analysisResults.innerHTML = `
    <div class="analysis-metric-grid">${renderResultMetrics(analysis)}</div>
    ${statusMessage ? `<p class="analysis-result-message">${escapeHtml(statusMessage)}</p>` : ""}
    ${warnings.length ? `
      <div class="analysis-warning-list">
        <strong>质量提示</strong>
        <ul>${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>
      </div>` : ""}
    <div class="analysis-trace">
      <strong>计算依据</strong>
      <code>${escapeHtml(formula)}</code>
      <dl>
        ${traceEntries
          .filter(([, value]) => value !== null && value !== undefined && value !== "")
          .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`)
          .join("")}
      </dl>
    </div>`;
  elements.analysisResultFreshness.textContent = saved ? "已保存" : "预览 · 未保存";
  elements.analysisResultFreshness.className =
    `result-freshness ${saved ? "saved" : "preview"}`;
}

function markAnalysisDirty() {
  if (!state.analysisPreview) return;
  state.analysisDirty = true;
  syncAnalysisControls();
  elements.analysisResultFreshness.textContent = "参数已修改 · 请重新预览";
  elements.analysisResultFreshness.className = "result-freshness stale";
}

function renderAnalysisHistory(items) {
  elements.analysisHistoryCount.textContent = String(items.length);
  if (!items.length) {
    elements.analysisHistoryList.innerHTML =
      '<div class="analysis-history-empty">尚无已保存记录</div>';
    return;
  }
  elements.analysisHistoryList.innerHTML = items
    .map((item) => {
      const result = item.result || {};
      const headline = item.analysis_type === "eis_resistance"
        ? `Rs ${displayMetric(pickValue(result, ["solution_resistance", "rs_ohm", "solution_resistance_ohm", "rs"]), pickValue(result, ["resistance_unit", "unit"]) || "Ω")}`
        : `η ${displayMetric(pickValue(result, ["target_point.overpotential_mv", "overpotential_mv", "eta_mv", "overpotential"]), "mV")}`;
      return `
        <article class="analysis-history-item">
          <div>
            <strong>${headline}</strong>
            <span>
              ${formatDate(item.created_utc)} · ${escapeHtml(item.algorithm_version || item.algorithm_id || "已记录")}
              ${item.stale ? " · 源数据已变化" : ""}
            </span>
          </div>
          <span class="history-hash ${item.stale ? "stale" : ""}" title="${escapeHtml(item.source_sha256 || "")}">
            ${item.source_sha256 ? `SHA ${escapeHtml(item.source_sha256.slice(0, 8))}` : "SHA —"}
          </span>
        </article>`;
    })
    .join("");
}

async function loadAnalysisHistory(runId) {
  const requestId = ++state.analysisHistoryRequestId;
  try {
    const items = await request(`/api/runs/${runId}/analyses`);
    if (
      requestId !== state.analysisHistoryRequestId ||
      state.selectedId !== runId
    ) return;
    renderAnalysisHistory(Array.isArray(items) ? items : (items.analyses || []));
  } catch (error) {
    if (
      requestId !== state.analysisHistoryRequestId ||
      state.selectedId !== runId
    ) return;
    elements.analysisHistoryList.innerHTML =
      `<div class="analysis-history-empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderDetail(run) {
  state.selectedRun = run;
  elements.detailInstrument.textContent = run ? `${run.instrument} 文件` : "选择一个实验文件";
  elements.detailTitle.textContent = run ? run.source_name : "曲线预览";
  elements.detailTechnique.textContent = run ? run.technique : "—";
  elements.detailHash.textContent = run ? `SHA ${run.sha256.slice(0, 12)}…` : "SHA-256";
  elements.detailHash.title = run?.sha256 ?? "";
  drawChart(run);
  configureAnalysis(run);
}

function renderDetailLoading(runId) {
  const summary = state.runs.find((run) => run.id === runId);
  renderDetail(null);
  elements.detailInstrument.textContent = "正在读取";
  elements.detailTitle.textContent = summary?.source_name || "加载实验文件…";
  elements.chartEmptyMessage.textContent = "正在读取所选实验文件…";
  elements.analysisModeBadge.textContent = "正在读取";
}

function renderDetailFailure(runId, error) {
  const summary = state.runs.find((run) => run.id === runId);
  renderDetail(null);
  elements.detailInstrument.textContent = "读取失败";
  elements.detailTitle.textContent = summary?.source_name || "无法打开所选文件";
  elements.chartEmptyMessage.textContent = error.message;
  elements.chartStats.innerHTML =
    `<span class="stat-chip source-warning">${escapeHtml(error.message)}</span>`;
  elements.analysisModeBadge.textContent = "读取失败";
}

async function loadFileTree(preserveSelection = true) {
  const requestId = ++state.runsRequestId;
  const parameters = new URLSearchParams();
  if (elements.techniqueFilter.value) parameters.set("technique", elements.techniqueFilter.value);
  if (elements.searchInput.value.trim()) parameters.set("q", elements.searchInput.value.trim());
  const tree = await request(`/api/files/tree?${parameters.toString()}`);
  if (requestId !== state.runsRequestId) return;
  state.tree = tree;
  state.runs = tree.roots.flatMap((root) =>
    root.files.map((run) => ({ ...run, root_id: root.id })),
  );
  if (!preserveSelection || !state.runs.some((run) => run.id === state.selectedId)) {
    state.selectedId = state.runs[0]?.id ?? null;
  }
  renderFileTree();
  if (state.selectedId) await selectRun(state.selectedId, false);
  else renderDetail(null);
}

async function selectRun(runId, rerenderList = true) {
  const requestId = ++state.detailRequestId;
  ++state.analysisRequestId;
  ++state.analysisHistoryRequestId;
  const treeRequestId = state.runsRequestId;
  state.analysisBusy = false;
  state.selectedId = runId;
  if (rerenderList) renderFileTree();
  renderDetailLoading(runId);
  let run;
  try {
    run = await request(`/api/runs/${runId}`);
  } catch (error) {
    if (
      requestId === state.detailRequestId &&
      treeRequestId === state.runsRequestId &&
      state.selectedId === runId
    ) {
      renderDetailFailure(runId, error);
      showToast(error.message, true);
    }
    return;
  }
  if (
    requestId !== state.detailRequestId ||
    treeRequestId !== state.runsRequestId ||
    state.selectedId !== runId
  ) return;
  renderDetail(run);
  if (analysisTypeForRun(run)) {
    await loadAnalysisHistory(runId);
  }
}

async function refreshAll(preserveSelection = true) {
  try {
    await loadFileTree(preserveSelection);
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
  searchDelay = window.setTimeout(() => loadFileTree(false), 220);
});
elements.techniqueFilter.addEventListener("change", () => loadFileTree(false));

function collectAnalysisParameters() {
  if (analysisTypeForRun(state.selectedRun) !== "cv_overpotential") return {};
  return {
    solution: selectedSolutionValue(),
    ph: Number(elements.cvPh.value),
    reaction: elements.cvReaction.value,
    reference_electrode: selectedReferenceValue(),
    reference_offset_v: Number(elements.cvReferenceOffset.value),
    compensation_percent: Number(elements.cvCompensation.value),
    solution_resistance_ohm: Number(elements.cvResistance.value),
    area_cm2: Number(elements.cvArea.value),
    target_current_density_ma_cm2: Number(elements.cvTargetCurrent.value),
    scan_branch: elements.cvScanBranch.value,
    online_compensation_status: elements.cvOnlineCompensation.value,
  };
}

function applyCvBranchOptions(options, preferredValue = "") {
  if (!Array.isArray(options) || !options.length) return;
  const currentValue = preferredValue || elements.cvScanBranch.value;
  elements.cvScanBranch.innerHTML = [
    '<option value="auto">自动（仅单分支）</option>',
    ...options.map((option) => {
      const value = option.value || option.id;
      const rows = option.source_row_start && option.source_row_end
        ? ` · 行 ${option.source_row_start}–${option.source_row_end}`
        : "";
      return `<option value="${escapeHtml(value)}">${escapeHtml(option.label || value)}${escapeHtml(rows)}</option>`;
    }),
  ].join("");
  const available = [...elements.cvScanBranch.options].some(
    (option) => option.value === currentValue,
  );
  elements.cvScanBranch.value = available
    ? currentValue
    : (options[0].value || options[0].id);
}

async function calculateAnalysis(save) {
  if (state.analysisBusy) return;
  const run = state.selectedRun;
  const runId = run?.id;
  const analysisType = analysisTypeForRun(run);
  if (!runId || !analysisType || !run.source_available) return;
  if (!elements.analysisForm.reportValidity()) return;

  const parameters = collectAnalysisParameters();
  const requestId = ++state.analysisRequestId;
  const endpoint = save
    ? `/api/runs/${runId}/analyses`
    : `/api/runs/${runId}/analyses/preview`;
  setAnalysisError();
  setAnalysisBusy(true);
  elements.analysisSaveState.textContent = save ? "计算并保存中…" : "计算中…";
  elements.analysisResultFreshness.textContent = "正在读取完整源数据…";
  elements.analysisResultFreshness.className = "result-freshness busy";
  try {
    const analysis = await request(endpoint, {
      method: "POST",
      body: JSON.stringify({
        analysis_type: analysisType,
        parameters,
      }),
    });
    if (requestId !== state.analysisRequestId || state.selectedId !== runId) return;
    state.analysisPreview = analysis;
    state.analysisPreviewSaved = save;
    state.analysisDirty = false;
    if (analysisType === "cv_overpotential") {
      applyCvBranchOptions(
        analysis.result?.branch_options,
        analysis.parameters?.scan_branch,
      );
    }
    renderAnalysisResult(analysis, save);
    syncAnalysisControls();
    elements.analysisSaveState.textContent = save ? "结果已保存" : "预览完成 · 尚未保存";
    if (save) {
      showToast("分析结果已保存到平台数据库");
      await loadAnalysisHistory(runId);
    }
  } catch (error) {
    if (requestId !== state.analysisRequestId || state.selectedId !== runId) return;
    const branchOptions = error.payload?.analysis_error?.details?.branch_options;
    if (analysisType === "cv_overpotential" && Array.isArray(branchOptions)) {
      applyCvBranchOptions(branchOptions);
    }
    setAnalysisError(error.message);
    elements.analysisResultFreshness.textContent = "计算失败";
    elements.analysisResultFreshness.className = "result-freshness stale";
    elements.analysisSaveState.textContent = "";
    showToast(error.message, true);
  } finally {
    if (requestId === state.analysisRequestId && state.selectedId === runId) {
      setAnalysisBusy(false);
      window.setTimeout(() => {
        if (requestId === state.analysisRequestId) elements.analysisSaveState.textContent = "";
      }, 2400);
    }
  }
}

elements.analysisForm.addEventListener("submit", (event) => {
  event.preventDefault();
  calculateAnalysis(false);
});

elements.saveAnalysis.addEventListener("click", () => calculateAnalysis(true));

elements.analysisForm.addEventListener("input", markAnalysisDirty);
elements.analysisForm.addEventListener("change", markAnalysisDirty);

elements.cvSolution.addEventListener("change", () => {
  syncSolutionPreset(true);
  markAnalysisDirty();
});

elements.cvReference.addEventListener("change", () => {
  syncReferenceOffset(true);
  markAnalysisDirty();
});

elements.cvOnlineCompensation.addEventListener("change", () => {
  const offlineCompensationAllowed =
    elements.cvOnlineCompensation.value === "not_compensated";
  elements.cvCompensation.disabled = !offlineCompensationAllowed;
  if (!offlineCompensationAllowed) elements.cvCompensation.value = "0";
  markAnalysisDirty();
});

window.addEventListener("resize", () => {
  if (state.selectedRun) drawChart(state.selectedRun);
});

refreshAll(false);
