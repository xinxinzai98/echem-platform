const state = {
  capabilities: null,
  preflight: null,
  draft: null,
  run: null,
  armToken: "",
  pollTimer: null,
};

const elements = {
  controlPill: document.querySelector("#controlPill"),
  readinessBox: document.querySelector(".hero-status"),
  readinessTitle: document.querySelector("#readinessTitle"),
  readinessNote: document.querySelector("#readinessNote"),
  preflightList: document.querySelector("#preflightList"),
  refreshButton: document.querySelector("#refreshButton"),
  draftRevision: document.querySelector("#draftRevision"),
  draftSummary: document.querySelector("#draftSummary"),
  prepareButton: document.querySelector("#prepareButton"),
  runPanel: document.querySelector("#runPanel"),
  runId: document.querySelector("#runId"),
  runStatus: document.querySelector("#runStatus"),
  protocolHash: document.querySelector("#protocolHash"),
  macroHash: document.querySelector("#macroHash"),
  chiPid: document.querySelector("#chiPid"),
  completionState: document.querySelector("#completionState"),
  confirmationPanel: document.querySelector("#confirmationPanel"),
  typedConfirmation: document.querySelector("#typedConfirmation"),
  armButton: document.querySelector("#armButton"),
  startButton: document.querySelector("#startButton"),
  runningPanel: document.querySelector("#runningPanel"),
  stopRequestButton: document.querySelector("#stopRequestButton"),
  stepState: document.querySelector("#stepState"),
  toast: document.querySelector("#toast"),
};

const confirmationBoxes = [
  ...document.querySelectorAll("[data-confirmation]"),
];

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.style.background = isError ? "#9d3b2c" : "#17212b";
  elements.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(
    () => elements.toast.classList.remove("show"),
    3200,
  );
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
    payload = { message: `HTTP ${response.status}` };
  }
  if (!response.ok) {
    const error = new Error(payload.message || payload.error || `HTTP ${response.status}`);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function shortHash(value) {
  if (!value) return "—";
  return `${value.slice(0, 12)}…${value.slice(-8)}`;
}

function techniqueLabel(step) {
  const technique = String(step?.technique || "").toLowerCase();
  if (["ocp", "ocpt"].includes(technique)) return "OCP";
  return technique.toUpperCase() || "—";
}

function renderPreflight() {
  const preflight = state.preflight;
  if (!preflight) return;
  elements.readinessBox.classList.remove("ready", "blocked");
  elements.readinessBox.classList.add(preflight.ready ? "ready" : "blocked");
  elements.readinessTitle.textContent = preflight.ready ? "预检通过" : "启动保持锁定";
  elements.readinessNote.textContent = preflight.ready
    ? "可以创建只读运行快照"
    : `仍有 ${preflight.blocked_checks.length} 项未通过`;
  elements.preflightList.replaceChildren(
    ...preflight.checks.map((check) => {
      const row = document.createElement("div");
      row.className = `preflight-row ${check.status}`;
      const icon = document.createElement("span");
      icon.className = "icon";
      icon.textContent = check.status === "passed" ? "✓" : "!";
      const label = document.createElement("strong");
      label.textContent = check.label;
      const detail = document.createElement("small");
      detail.textContent = check.detail || "";
      row.append(icon, label, detail);
      return row;
    }),
  );
  renderActionAvailability();
}

function renderDraft() {
  const draft = state.draft;
  if (!draft) return;
  const steps = Array.isArray(draft.protocol?.steps)
    ? draft.protocol.steps.filter((step) => step.enabled !== false)
    : [];
  const step = steps[0];
  const rows = [
    ["协议", draft.protocol?.name || "未命名"],
    ["启用工步", `${steps.length} 个`],
    ["技术", techniqueLabel(step)],
    ["时长", step?.params?.duration_s == null ? "—" : `${step.params.duration_s} s`],
    ["保存名", step?.save_basename || "—"],
  ];
  elements.draftRevision.textContent = draft.persisted === false
    ? "默认草稿"
    : `草稿 r${draft.revision || 0}`;
  elements.draftSummary.replaceChildren(
    ...rows.map(([labelText, valueText]) => {
      const row = document.createElement("div");
      const label = document.createElement("span");
      label.textContent = labelText;
      const value = document.createElement("strong");
      value.textContent = valueText;
      row.append(label, value);
      return row;
    }),
  );
}

function renderActionAvailability() {
  const enabled = Boolean(state.capabilities?.instrument_control_enabled);
  const ready = Boolean(state.preflight?.ready);
  elements.controlPill.textContent = enabled ? "阶段 C 已启用" : "阶段 C 控制锁定";
  elements.controlPill.classList.toggle("enabled", enabled);
  elements.prepareButton.disabled = !enabled || !ready || !state.draft;
}

function renderRun() {
  const run = state.run;
  if (!run) {
    elements.runPanel.hidden = true;
    return;
  }
  elements.runPanel.hidden = false;
  elements.runId.textContent = run.run_id;
  elements.runStatus.textContent = run.status;
  elements.protocolHash.textContent = shortHash(run.protocol_sha256);
  elements.protocolHash.title = run.protocol_sha256 || "";
  elements.macroHash.textContent = shortHash(run.macro_sha256);
  elements.macroHash.title = run.macro_sha256 || "";
  elements.chiPid.textContent = run.chi_pid || "—";
  elements.completionState.textContent = run.completion_confirmed
    ? "已确认完成"
    : (run.status === "completed" ? "数据需复核" : "未完成");
  elements.typedConfirmation.placeholder = run.run_id;

  const canArm = ["prepared", "armed"].includes(run.status);
  const active = ["starting", "running", "stop_requested"].includes(run.status);
  elements.confirmationPanel.hidden = !canArm;
  elements.runningPanel.hidden = !active;
  elements.armButton.disabled = !canArm;
  elements.startButton.disabled = !state.armToken || run.status !== "armed";
  elements.stopRequestButton.disabled = run.status === "stop_requested";

  const step = run.steps?.[0];
  if (step) {
    const dataNote = step.data_status ? `；数据：${step.data_status}` : "";
    elements.stepState.textContent =
      `${step.name} · ${techniqueLabel(step)} · ${step.status}${dataNote}`;
  } else {
    elements.stepState.textContent = "等待工步状态";
  }
}

async function refreshReadiness() {
  const [capabilities, preflight] = await Promise.all([
    request("/api/control/capabilities"),
    request("/api/control/preflight"),
  ]);
  state.capabilities = capabilities;
  state.preflight = preflight;
  renderPreflight();
  renderActionAvailability();
}

async function loadDraft() {
  state.draft = await request("/api/protocols/draft-main");
  renderDraft();
  renderActionAvailability();
}

async function loadLatestRun() {
  if (!state.capabilities?.instrument_control_enabled) return;
  const runs = await request("/api/control/runs?limit=1");
  if (runs.length) {
    state.run = runs[0];
    renderRun();
    scheduleRunPoll();
  }
}

async function prepareRun() {
  state.armToken = "";
  const run = await request("/api/control/runs", {
    method: "POST",
    body: JSON.stringify({ protocol: state.draft.protocol }),
  });
  state.run = run;
  renderRun();
  showToast("已创建不可变运行快照；仪器尚未启动。");
}

function collectConfirmations() {
  return Object.fromEntries(
    confirmationBoxes.map((box) => [box.dataset.confirmation, box.checked]),
  );
}

async function armRun() {
  const payload = await request(`/api/control/runs/${state.run.run_id}/arm`, {
    method: "POST",
    body: JSON.stringify({
      confirmations: collectConfirmations(),
      typed_confirmation: elements.typedConfirmation.value.trim(),
    }),
  });
  state.run = payload.run;
  state.armToken = payload.arm_token;
  renderRun();
  showToast(`一次性确认有效至 ${payload.expires_utc}`);
}

async function startRun() {
  const accepted = window.confirm(
    "即将由设备电脑本地启动一个 60 秒 OCP。确认现场仍有人、接线无误且 CHI 当前空闲？",
  );
  if (!accepted) return;
  const token = state.armToken;
  state.armToken = "";
  renderRun();
  const run = await request(`/api/control/runs/${state.run.run_id}/start`, {
    method: "POST",
    body: JSON.stringify({ arm_token: token }),
  });
  state.run = run;
  renderRun();
  scheduleRunPoll();
  showToast("CHI 启动请求已提交，正在观察进程和输出文件。");
}

async function requestStop() {
  const run = await request(
    `/api/control/runs/${state.run.run_id}/request-stop`,
    { method: "POST", body: "{}" },
  );
  state.run = run;
  renderRun();
  showToast("已记录停止请求；请在 CHI 软件中人工停止。");
}

async function pollRun() {
  if (!state.run) return;
  try {
    state.run = await request(`/api/control/runs/${state.run.run_id}`);
    renderRun();
  } catch (error) {
    showToast(error.message, true);
  }
  scheduleRunPoll();
}

function scheduleRunPoll() {
  window.clearTimeout(state.pollTimer);
  if (
    state.run &&
    ["starting", "running", "stop_requested"].includes(state.run.status)
  ) {
    state.pollTimer = window.setTimeout(pollRun, 1000);
  }
}

elements.refreshButton.addEventListener("click", async () => {
  try {
    await refreshReadiness();
    showToast("预检状态已刷新。");
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.prepareButton.addEventListener("click", async () => {
  try {
    await prepareRun();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.armButton.addEventListener("click", async () => {
  try {
    await armRun();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.startButton.addEventListener("click", async () => {
  try {
    await startRun();
  } catch (error) {
    state.armToken = "";
    renderRun();
    showToast(error.message, true);
  }
});

elements.stopRequestButton.addEventListener("click", async () => {
  try {
    await requestStop();
  } catch (error) {
    showToast(error.message, true);
  }
});

Promise.all([refreshReadiness(), loadDraft()])
  .then(loadLatestRun)
  .catch((error) => showToast(error.message, true));
