const state = {
  capabilities: null,
  draftId: "draft-main",
  protocol: null,
  outputFolder: "",
  allowedRunRoot: "",
  revision: 0,
  report: null,
  dirty: false,
  saveTimer: null,
  saveSequence: 0,
  saveQueue: Promise.resolve(),
};

const elements = {
  protocolName: document.querySelector("#protocolName"),
  protocolSampleId: document.querySelector("#protocolSampleId"),
  protocolOperator: document.querySelector("#protocolOperator"),
  protocolMaterial: document.querySelector("#protocolMaterial"),
  protocolElectrolyte: document.querySelector("#protocolElectrolyte"),
  protocolReference: document.querySelector("#protocolReference"),
  protocolArea: document.querySelector("#protocolArea"),
  protocolNotes: document.querySelector("#protocolNotes"),
  allowedRunRoot: document.querySelector("#allowedRunRoot"),
  outputFolder: document.querySelector("#outputFolder"),
  stepList: document.querySelector("#stepList"),
  stepCount: document.querySelector("#stepCount"),
  activeStepCount: document.querySelector("#activeStepCount"),
  expectedTime: document.querySelector("#expectedTime"),
  expectedTimeNote: document.querySelector("#expectedTimeNote"),
  validationMetric: document.querySelector("#validationMetric"),
  validationState: document.querySelector("#validationState"),
  validationStateNote: document.querySelector("#validationStateNote"),
  draftState: document.querySelector("#draftState"),
  saveDraftButton: document.querySelector("#saveDraftButton"),
  validateButton: document.querySelector("#validateButton"),
  compileButton: document.querySelector("#compileButton"),
  safetyList: document.querySelector("#safetyList"),
  issueList: document.querySelector("#issueList"),
  warningSection: document.querySelector("#warningSection"),
  warningList: document.querySelector("#warningList"),
  stepSummarySection: document.querySelector("#stepSummarySection"),
  stepSummary: document.querySelector("#stepSummary"),
  outputSection: document.querySelector("#outputSection"),
  outputList: document.querySelector("#outputList"),
  resultBadge: document.querySelector("#resultBadge"),
  macroPanel: document.querySelector("#macroPanel"),
  macroMeta: document.querySelector("#macroMeta"),
  macroPreview: document.querySelector("#macroPreview"),
  normalizedPreview: document.querySelector("#normalizedPreview"),
  copyMacroButton: document.querySelector("#copyMacroButton"),
  toast: document.querySelector("#toast"),
};

const metadataBindings = [
  [elements.protocolName, "name", false],
  [elements.protocolSampleId, "sample_id", false],
  [elements.protocolOperator, "operator", false],
  [elements.protocolMaterial, "material", false],
  [elements.protocolElectrolyte, "electrolyte", false],
  [elements.protocolReference, "reference", false],
  [elements.protocolArea, "area_cm2", true],
  [elements.protocolNotes, "notes", false],
];

const TECHNIQUE_LABELS = {
  cv: "CV",
  ocp: "OCP",
  lsv: "LSV / Tafel",
  eis: "EIS",
};

const TECHNIQUE_ALIASES = {
  cv: "cv",
  ocp: "ocp",
  ocpt: "ocp",
  lsv: "lsv",
  eis: "eis",
  imp: "eis",
};

const PARAMETER_KEYS = {
  cv: new Set([
    "initial_v",
    "high_v",
    "low_v",
    "direction",
    "scan_rate_v_s",
    "cycles",
    "segments",
    "sample_interval_v",
    "quiet_time_s",
    "auto_sensitivity",
    "sensitivity_a_v",
  ]),
  ocp: new Set([
    "duration_s",
    "sample_interval_s",
    "quiet_time_s",
    "upper_limit_v",
    "lower_limit_v",
  ]),
  lsv: new Set([
    "initial_v",
    "final_v",
    "scan_rate_v_s",
    "sample_interval_v",
    "quiet_time_s",
    "auto_sensitivity",
    "sensitivity_a_v",
  ]),
  eis: new Set([
    "bias_mode",
    "dc_potential_v",
    "high_frequency_hz",
    "low_frequency_hz",
    "amplitude_v",
    "quiet_time_s",
  ]),
};

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function hasOwn(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.style.background = isError ? "#9d3b2c" : "#17212b";
  elements.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => elements.toast.classList.remove("show"), 2800);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    cache: "no-store",
    ...options,
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { error: `HTTP ${response.status}` };
  }
  if (!response.ok) {
    const firstIssue = Array.isArray(payload.issues) ? payload.issues[0] : null;
    const message =
      firstIssue?.message ||
      payload.message ||
      (typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`);
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function defaultParams(technique) {
  if (technique === "cv") {
    return {
      initial_v: 0.05,
      high_v: 0.05,
      low_v: -0.05,
      direction: "n",
      scan_rate_v_s: 0.01,
      segments: 2,
      sample_interval_v: 0.001,
      quiet_time_s: 0,
      auto_sensitivity: true,
    };
  }
  if (technique === "ocp") {
    return {
      duration_s: 10,
      sample_interval_s: 1,
      quiet_time_s: 0,
      upper_limit_v: 2,
      lower_limit_v: -2,
    };
  }
  if (technique === "lsv") {
    return {
      initial_v: 0,
      final_v: -0.05,
      scan_rate_v_s: 0.01,
      sample_interval_v: 0.001,
      quiet_time_s: 0,
      auto_sensitivity: true,
    };
  }
  return {
    bias_mode: "ocp",
    high_frequency_hz: 1000,
    low_frequency_hz: 10,
    amplitude_v: 0.005,
    quiet_time_s: 0,
  };
}

function normalizeStepForUi(raw, index) {
  const technique = TECHNIQUE_ALIASES[String(raw?.technique || "").toLowerCase()] || "cv";
  const sourceParams = raw?.params && typeof raw.params === "object" ? raw.params : {};
  const params = defaultParams(technique);
  Object.entries(sourceParams).forEach(([key, value]) => {
    if (PARAMETER_KEYS[technique].has(key)) params[key] = value;
  });
  if (technique === "cv") {
    if (hasOwn(sourceParams, "cycles")) {
      delete params.segments;
      params.cycles = sourceParams.cycles;
    } else {
      delete params.cycles;
    }
  }
  if (
    ["cv", "lsv"].includes(technique) &&
    params.auto_sensitivity !== false
  ) {
    params.auto_sensitivity = true;
    delete params.sensitivity_a_v;
  }
  if (technique === "eis" && params.bias_mode !== "potential") {
    params.bias_mode = "ocp";
    delete params.dc_potential_v;
  }
  return {
    id: String(raw?.id || `step-${String(index + 1).padStart(2, "0")}`),
    name: String(raw?.name || `${TECHNIQUE_LABELS[technique]} 工步`),
    technique,
    enabled: raw?.enabled !== false,
    save_basename: String(
      raw?.save_basename || `${String(index + 1).padStart(2, "0")}_${technique.toUpperCase()}`,
    ),
    params,
  };
}

function normalizeDraftForUi(draft) {
  const protocol = draft?.protocol && typeof draft.protocol === "object"
    ? deepClone(draft.protocol)
    : {};
  protocol.schema_version = protocol.schema_version ?? 1;
  protocol.execution_mode = protocol.execution_mode ?? "single_macro";
  protocol.steps = Array.isArray(protocol.steps)
    ? protocol.steps.map(normalizeStepForUi)
    : [];
  return {
    id: String(draft?.id || "draft-main"),
    protocol,
    output_folder: String(draft?.output_folder || ""),
    allowed_run_root: String(draft?.allowed_run_root || ""),
    revision: Number(draft?.revision || 0),
  };
}

function fillMetadataInputs() {
  metadataBindings.forEach(([element, key]) => {
    element.value = state.protocol[key] ?? "";
  });
  elements.allowedRunRoot.value = state.allowedRunRoot;
  elements.outputFolder.value = state.outputFolder;
}

function techniqueOptions(selected) {
  return Object.entries(TECHNIQUE_LABELS)
    .map(
      ([value, label]) =>
        `<option value="${value}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`,
    )
    .join("");
}

function selectOptions(options, selected) {
  return options
    .map(
      ([value, label]) =>
        `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`,
    )
    .join("");
}

function numberField(label, key, value, options = {}) {
  const wide = options.wide ? " wide" : "";
  const step = options.step || "any";
  return `
    <label class="step-field${wide}">${escapeHtml(label)}
      <input type="number" step="${escapeHtml(step)}" data-param="${escapeHtml(key)}" value="${escapeHtml(value ?? "")}">
    </label>`;
}

function selectField(label, key, value, options, extra = "") {
  return `
    <label class="step-field">${escapeHtml(label)}
      <select data-param="${escapeHtml(key)}" ${extra}>${selectOptions(options, value)}</select>
    </label>`;
}

function sensitivityFields(params) {
  const auto = params.auto_sensitivity !== false;
  return `
    <label class="field-check">
      <input type="checkbox" data-param="auto_sensitivity" ${auto ? "checked" : ""}>
      自动灵敏度
    </label>
    ${auto ? "" : numberField("固定灵敏度 / A·V⁻¹", "sensitivity_a_v", params.sensitivity_a_v ?? 0.1)}`;
}

function techniqueFields(step) {
  const params = step.params;
  if (step.technique === "cv") {
    const countMode = hasOwn(params, "cycles") ? "cycles" : "segments";
    return [
      numberField("初始电位 / V", "initial_v", params.initial_v),
      numberField("高电位 / V", "high_v", params.high_v),
      numberField("低电位 / V", "low_v", params.low_v),
      selectField("扫描方向", "direction", params.direction, [["n", "负向 n"], ["p", "正向 p"]]),
      numberField("扫描速率 / V·s⁻¹", "scan_rate_v_s", params.scan_rate_v_s),
      `
        <label class="step-field">计数方式
          <select data-ui-field="count_mode">
            <option value="segments" ${countMode === "segments" ? "selected" : ""}>扫描段数</option>
            <option value="cycles" ${countMode === "cycles" ? "selected" : ""}>圈数（受限换算）</option>
          </select>
        </label>`,
      numberField(
        countMode === "cycles" ? "圈数" : "扫描段数",
        countMode,
        params[countMode],
        { step: "1" },
      ),
      numberField("采样间隔 / V", "sample_interval_v", params.sample_interval_v),
      numberField("Quiet time / s", "quiet_time_s", params.quiet_time_s),
      sensitivityFields(params),
    ].join("");
  }
  if (step.technique === "ocp") {
    return [
      numberField("记录时长 / s", "duration_s", params.duration_s),
      numberField("采样间隔 / s", "sample_interval_s", params.sample_interval_s),
      numberField("Quiet time / s", "quiet_time_s", params.quiet_time_s),
      numberField("记录上限 / V", "upper_limit_v", params.upper_limit_v),
      numberField("记录下限 / V", "lower_limit_v", params.lower_limit_v),
    ].join("");
  }
  if (step.technique === "lsv") {
    return [
      numberField("初始电位 / V", "initial_v", params.initial_v),
      numberField("终止电位 / V", "final_v", params.final_v),
      numberField("扫描速率 / V·s⁻¹", "scan_rate_v_s", params.scan_rate_v_s),
      numberField("采样间隔 / V", "sample_interval_v", params.sample_interval_v),
      numberField("Quiet time / s", "quiet_time_s", params.quiet_time_s),
      sensitivityFields(params),
    ].join("");
  }
  const potentialField = params.bias_mode === "potential"
    ? numberField("直流偏置电位 / V", "dc_potential_v", params.dc_potential_v ?? 0)
    : "";
  return [
    selectField(
      "偏置模式",
      "bias_mode",
      params.bias_mode,
      [["ocp", "OCP 偏置"], ["potential", "固定电位"]],
    ),
    potentialField,
    numberField("高频 / Hz", "high_frequency_hz", params.high_frequency_hz),
    numberField("低频 / Hz", "low_frequency_hz", params.low_frequency_hz),
    numberField("振幅 / V", "amplitude_v", params.amplitude_v),
    numberField("Quiet time / s", "quiet_time_s", params.quiet_time_s),
  ].join("");
}

function renderSteps() {
  if (!state.protocol.steps.length) {
    elements.stepList.innerHTML = `
      <div class="step-empty">还没有工步。使用右上角按钮添加 CV、OCP、LSV 或 EIS。</div>`;
    return;
  }
  elements.stepList.innerHTML = state.protocol.steps
    .map(
      (step, index) => `
        <article class="step-card technique-${escapeHtml(step.technique)} ${step.enabled ? "" : "disabled"}" data-step-index="${index}">
          <div class="step-header">
            <span class="step-index">${String(index + 1).padStart(2, "0")}</span>
            <select class="step-technique-select" data-step-field="technique">
              ${techniqueOptions(step.technique)}
            </select>
            <input class="step-name-input" data-step-field="name" maxlength="120" value="${escapeHtml(step.name)}" aria-label="工步名称">
            <div class="step-actions">
              <button class="step-action" type="button" data-action="up" title="上移" ${index === 0 ? "disabled" : ""}>↑</button>
              <button class="step-action" type="button" data-action="down" title="下移" ${index === state.protocol.steps.length - 1 ? "disabled" : ""}>↓</button>
              <button class="step-action" type="button" data-action="copy" title="复制">复制</button>
              <button class="step-action danger" type="button" data-action="delete" title="删除">删除</button>
            </div>
          </div>
          <div class="step-fields">
            <label class="field-check">
              <input type="checkbox" data-step-field="enabled" ${step.enabled ? "checked" : ""}>
              启用此工步
            </label>
            <label class="step-field wide">保存文件名
              <input data-step-field="save_basename" maxlength="64" spellcheck="false" value="${escapeHtml(step.save_basename)}">
            </label>
            ${techniqueFields(step)}
          </div>
        </article>`,
    )
    .join("");
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "未知";
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  if (hours) return `${hours} h ${minutes} min`;
  if (minutes) return `${minutes} min ${remainder} s`;
  return `${remainder} s`;
}

function renderMetrics() {
  const steps = state.protocol?.steps || [];
  const active = steps.filter((step) => step.enabled);
  elements.stepCount.textContent = String(steps.length);
  elements.activeStepCount.textContent = String(active.length);
  if (state.report) {
    const suffix = state.report.expected_seconds_complete ? "" : " + 未知工步";
    elements.expectedTime.textContent =
      `${formatDuration(state.report.known_expected_seconds)}${suffix}`;
    elements.expectedTimeNote.textContent = state.report.expected_seconds_complete
      ? "全部启用工步均可估算"
      : "包含 EIS 等不可精确估时工步";
  } else {
    elements.expectedTime.textContent = "待校验";
    elements.expectedTimeNote.textContent = "EIS 不生成虚假精确时间";
  }
}

function setValidationState(kind, title, note) {
  elements.validationMetric.classList.remove("valid", "invalid");
  if (kind === "valid" || kind === "invalid") {
    elements.validationMetric.classList.add(kind);
  }
  elements.validationState.textContent = title;
  elements.validationStateNote.textContent = note;
  elements.resultBadge.className = `result-badge ${kind === "dirty" ? "neutral" : kind}`;
  elements.resultBadge.textContent =
    kind === "valid" ? "通过" : kind === "invalid" ? "未通过" : "等待";
}

function invalidateReport() {
  state.report = null;
  elements.macroPanel.hidden = true;
  elements.warningSection.hidden = true;
  elements.stepSummarySection.hidden = true;
  elements.outputSection.hidden = true;
  elements.issueList.innerHTML = `<div class="result-empty">参数已变更，请重新校验。</div>`;
  elements.safetyList.innerHTML = `
    <div class="safety-row manual"><span>○</span><p>重新校验后更新安全检查。</p></div>`;
  setValidationState("dirty", "待重新校验", "参数变化后旧预览已失效");
  renderMetrics();
}

function setDraftState(message, kind = "") {
  elements.draftState.textContent = message;
  elements.draftState.className = `draft-state ${kind}`.trim();
}

function markDirty({ structural = false } = {}) {
  state.dirty = true;
  setDraftState("有未保存更改", "dirty");
  invalidateReport();
  if (structural) renderSteps();
  renderMetrics();
  window.clearTimeout(state.saveTimer);
  state.saveTimer = window.setTimeout(() => {
    saveDraft({ silent: true }).catch(() => {});
  }, 650);
}

function nextStepId() {
  const used = new Set(state.protocol.steps.map((step) => step.id));
  for (let index = 1; index <= 1000; index += 1) {
    const candidate = `step-${String(index).padStart(2, "0")}`;
    if (!used.has(candidate)) return candidate;
  }
  return `step-${Date.now()}`;
}

function uniqueBasename(base) {
  const used = new Set(
    state.protocol.steps.map((step) => step.save_basename.toLowerCase()),
  );
  const safeBase = String(base || "STEP")
    .replace(/[^A-Za-z0-9_-]/g, "_")
    .slice(0, 52) || "STEP";
  if (!used.has(safeBase.toLowerCase())) return safeBase;
  for (let index = 2; index <= 999; index += 1) {
    const candidate = `${safeBase}_${index}`.slice(0, 64);
    if (!used.has(candidate.toLowerCase())) return candidate;
  }
  return `STEP_${Date.now()}`.slice(0, 64);
}

function addStep(technique) {
  if (state.protocol.steps.length >= 100) {
    showToast("单个工步方案最多允许 100 个工步。", true);
    return;
  }
  const index = state.protocol.steps.length + 1;
  state.protocol.steps.push({
    id: nextStepId(),
    name: `${TECHNIQUE_LABELS[technique]} 工步`,
    technique,
    enabled: true,
    save_basename: uniqueBasename(`${String(index).padStart(2, "0")}_${technique.toUpperCase()}`),
    params: defaultParams(technique),
  });
  markDirty({ structural: true });
}

function moveStep(index, direction) {
  const target = index + direction;
  if (target < 0 || target >= state.protocol.steps.length) return;
  const [step] = state.protocol.steps.splice(index, 1);
  state.protocol.steps.splice(target, 0, step);
  markDirty({ structural: true });
}

function copyStep(index) {
  if (state.protocol.steps.length >= 100) {
    showToast("单个工步方案最多允许 100 个工步。", true);
    return;
  }
  const copy = deepClone(state.protocol.steps[index]);
  copy.id = nextStepId();
  copy.name = `${copy.name} 副本`.slice(0, 120);
  copy.save_basename = uniqueBasename(`${copy.save_basename}_COPY`);
  state.protocol.steps.splice(index + 1, 0, copy);
  markDirty({ structural: true });
}

function updateMetadataFromInput(element, key, numeric) {
  if (numeric) {
    if (element.value === "") delete state.protocol[key];
    else state.protocol[key] = Number(element.value);
  } else {
    state.protocol[key] = element.value;
  }
  markDirty();
}

function requestPayload() {
  return {
    protocol: deepClone(state.protocol),
    output_folder: state.outputFolder,
    allowed_run_root: state.allowedRunRoot,
  };
}

function saveDraft({ silent = false } = {}) {
  window.clearTimeout(state.saveTimer);
  const sequence = ++state.saveSequence;
  if (!silent) setDraftState("保存中…");
  const body = JSON.stringify({
    id: state.draftId,
    ...requestPayload(),
  });
  const operation = state.saveQueue
    .catch(() => undefined)
    .then(() =>
      request("/api/protocols", {
        method: "POST",
        body,
      }),
    );
  state.saveQueue = operation;
  return operation
    .then((saved) => {
      if (sequence !== state.saveSequence) return saved;
      state.revision = saved.revision;
      state.dirty = false;
      setDraftState(`草稿已保存 · 修订 ${saved.revision}`);
      if (!silent) showToast("工步方案已保存到本机数据库");
      return saved;
    })
    .catch((error) => {
      if (sequence === state.saveSequence) {
        setDraftState("草稿保存失败", "error");
      }
      if (!silent) showToast(error.message, true);
      throw error;
    });
}

function renderSafety(checks) {
  elements.safetyList.innerHTML = checks
    .map(
      (check) => `
        <div class="safety-row ${check.status === "manual" ? "manual" : ""}">
          <span>${check.status === "passed" ? "✓" : "○"}</span>
          <p>${escapeHtml(check.label)}</p>
        </div>`,
    )
    .join("");
}

function renderReport(report) {
  state.report = report;
  setValidationState("valid", "校验通过", "当前预览只用于离线审查");
  elements.issueList.innerHTML = `
    <div class="safety-row">
      <span>✓</span>
      <p>工步方案、路径和生成宏均通过静态复核。没有执行或写入仪器宏。</p>
    </div>`;
  renderSafety(report.safety_checks);

  elements.warningSection.hidden = report.warnings.length === 0;
  elements.warningList.innerHTML = report.warnings
    .map((warning) => `<div class="warning-item">${escapeHtml(warning)}</div>`)
    .join("");

  elements.stepSummarySection.hidden = false;
  elements.stepSummary.innerHTML = report.steps
    .map(
      (step) => `
        <div class="summary-row ${step.enabled ? "" : "disabled"}">
          <span class="summary-number">${String(step.index).padStart(2, "0")}</span>
          <span class="summary-main">
            <strong>${escapeHtml(step.name)}</strong>
            <small>${escapeHtml(step.technique.toUpperCase())} · ${escapeHtml(step.save_basename)}${step.enabled ? "" : " · 已停用"}</small>
          </span>
          <span class="summary-time">${step.enabled ? formatDuration(step.expected_seconds) : "跳过"}</span>
        </div>`,
    )
    .join("");

  elements.outputSection.hidden = false;
  elements.outputList.innerHTML = report.output_files
    .map(
      (file) => `
        <div class="output-row">
          <span>${escapeHtml(file.binary)}<br>${escapeHtml(file.text)}</span>
          <span class="output-kind">BIN + TXT</span>
        </div>`,
    )
    .join("");

  if (report.macro_preview) {
    elements.macroPanel.hidden = false;
    elements.macroPreview.textContent = report.macro_preview;
    elements.normalizedPreview.textContent = JSON.stringify(
      report.normalized_protocol,
      null,
      2,
    );
    elements.macroMeta.innerHTML = [
      `Header ${report.macro_header_hex}`,
      `${report.macro_bytes} bytes`,
      `Macro ${report.macro_sha256.slice(0, 12)}…`,
      `Protocol ${report.protocol_sha256.slice(0, 12)}…`,
      report.output_folder,
    ]
      .map((value) => `<span class="macro-chip" title="${escapeHtml(value)}">${escapeHtml(value)}</span>`)
      .join("");
  } else {
    elements.macroPanel.hidden = true;
  }
  renderMetrics();
}

function renderValidationError(error) {
  state.report = null;
  setValidationState("invalid", "校验未通过", "请修正下列字段后重试");
  const issues = Array.isArray(error.payload?.issues)
    ? error.payload.issues
    : [
        {
          path: error.payload?.code || "request",
          message: error.payload?.message || error.message,
        },
      ];
  elements.issueList.innerHTML = issues
    .map(
      (issue) => `
        <div class="issue-item">
          <span class="issue-path">${escapeHtml(issue.path || issue.code || "request")}</span>
          <span class="issue-message">${escapeHtml(issue.message || "校验失败")}</span>
        </div>`,
    )
    .join("");
  elements.warningSection.hidden = true;
  elements.stepSummarySection.hidden = true;
  elements.outputSection.hidden = true;
  elements.macroPanel.hidden = true;
  elements.safetyList.innerHTML = `
    <div class="safety-row manual"><span>○</span><p>工步方案未通过校验，未生成可用宏预览。</p></div>`;
  renderMetrics();
}

async function validateOrCompile(includeMacroPreview) {
  const button = includeMacroPreview ? elements.compileButton : elements.validateButton;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = includeMacroPreview ? "正在编译预览…" : "正在校验…";
  try {
    await saveDraft({ silent: true });
    const report = await request(
      includeMacroPreview ? "/api/protocols/compile" : "/api/protocols/validate",
      {
        method: "POST",
        body: JSON.stringify(requestPayload()),
      },
    );
    renderReport(report);
    showToast(includeMacroPreview ? "Dry-run 宏预览已生成" : "工步方案校验通过");
  } catch (error) {
    renderValidationError(error);
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function loadPage() {
  try {
    state.capabilities = await request("/api/control/capabilities");
    const dryRunCapabilities = state.capabilities.dry_run;
    if (!dryRunCapabilities || typeof dryRunCapabilities !== "object") {
      throw new Error("Dry-run 能力声明缺失，页面拒绝加载。");
    }
    const lockedFlags = [
      "instrument_control_enabled",
      "instrument_started",
      "launch_available",
      "serial_access",
      "network_control",
    ];
    if (lockedFlags.some((flag) => dryRunCapabilities[flag] !== false)) {
      throw new Error("安全能力状态异常，页面拒绝加载。");
    }
    let draft;
    try {
      draft = await request(`/api/protocols/${state.draftId}`);
    } catch (error) {
      if (error.status !== 404) throw error;
      draft = dryRunCapabilities.default_draft;
    }
    const normalized = normalizeDraftForUi(draft);
    state.draftId = normalized.id;
    state.protocol = normalized.protocol;
    state.outputFolder = normalized.output_folder;
    state.allowedRunRoot = normalized.allowed_run_root;
    state.revision = normalized.revision;
    fillMetadataInputs();
    renderSteps();
    renderMetrics();
    setDraftState(
      state.revision ? `已恢复草稿 · 修订 ${state.revision}` : "已载入公开格式示例",
    );
    setValidationState("dirty", "未校验", "参数变化后需重新校验");
  } catch (error) {
    setDraftState("页面初始化失败", "error");
    showToast(error.message, true);
  }
}

metadataBindings.forEach(([element, key, numeric]) => {
  element.addEventListener("input", () => updateMetadataFromInput(element, key, numeric));
});

elements.allowedRunRoot.addEventListener("input", () => {
  state.allowedRunRoot = elements.allowedRunRoot.value;
  markDirty();
});

elements.outputFolder.addEventListener("input", () => {
  state.outputFolder = elements.outputFolder.value;
  markDirty();
});

document.querySelectorAll("[data-add-technique]").forEach((button) => {
  button.addEventListener("click", () => addStep(button.dataset.addTechnique));
});

elements.stepList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const card = button.closest("[data-step-index]");
  const index = Number(card?.dataset.stepIndex);
  if (!Number.isInteger(index)) return;
  const action = button.dataset.action;
  if (action === "up") moveStep(index, -1);
  else if (action === "down") moveStep(index, 1);
  else if (action === "copy") copyStep(index);
  else if (action === "delete") {
    state.protocol.steps.splice(index, 1);
    markDirty({ structural: true });
  }
});

function handleStepField(event) {
  const target = event.target;
  if (
    (event.type === "input" && (target.tagName === "SELECT" || target.type === "checkbox")) ||
    (event.type === "change" && target.tagName !== "SELECT" && target.type !== "checkbox")
  ) {
    return;
  }
  const card = target.closest("[data-step-index]");
  if (!card) return;
  const index = Number(card.dataset.stepIndex);
  const step = state.protocol.steps[index];
  if (!step) return;

  const stepField = target.dataset.stepField;
  if (stepField) {
    if (stepField === "enabled") {
      step.enabled = target.checked;
      markDirty({ structural: true });
    } else if (stepField === "technique") {
      step.technique = target.value;
      step.params = defaultParams(target.value);
      step.save_basename = uniqueBasename(
        `${String(index + 1).padStart(2, "0")}_${target.value.toUpperCase()}`,
      );
      markDirty({ structural: true });
    } else {
      step[stepField] = target.value;
      markDirty();
    }
    return;
  }

  if (target.dataset.uiField === "count_mode") {
    if (target.value === "cycles") {
      const previous = Number(step.params.segments);
      delete step.params.segments;
      step.params.cycles = Number.isFinite(previous) ? Math.max(1, Math.ceil(previous / 2)) : 1;
    } else {
      const previous = Number(step.params.cycles);
      delete step.params.cycles;
      step.params.segments = Number.isFinite(previous) ? Math.max(1, previous * 2) : 2;
    }
    markDirty({ structural: true });
    return;
  }

  const parameter = target.dataset.param;
  if (!parameter) return;
  if (target.type === "checkbox") {
    step.params[parameter] = target.checked;
    if (parameter === "auto_sensitivity") {
      if (target.checked) delete step.params.sensitivity_a_v;
      else step.params.sensitivity_a_v = 0.1;
      markDirty({ structural: true });
    } else {
      markDirty();
    }
  } else if (target.type === "number") {
    step.params[parameter] = target.value === "" ? "" : Number(target.value);
    markDirty();
  } else {
    step.params[parameter] = target.value;
    if (parameter === "bias_mode") {
      if (target.value === "potential") step.params.dc_potential_v = 0;
      else delete step.params.dc_potential_v;
      markDirty({ structural: true });
    } else {
      markDirty();
    }
  }
}

elements.stepList.addEventListener("input", handleStepField);
elements.stepList.addEventListener("change", handleStepField);

elements.saveDraftButton.addEventListener("click", () => {
  saveDraft().catch(() => {});
});
elements.validateButton.addEventListener("click", () => validateOrCompile(false));
elements.compileButton.addEventListener("click", () => validateOrCompile(true));

elements.copyMacroButton.addEventListener("click", async () => {
  if (!elements.macroPreview.textContent) return;
  try {
    await navigator.clipboard.writeText(elements.macroPreview.textContent);
    showToast("宏预览文本已复制；它仍未被执行");
  } catch {
    showToast("浏览器未允许复制，请手动选择文本。", true);
  }
});

loadPage();
