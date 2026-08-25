const DEFAULT_MAX_UPLOAD_FILE_BYTES = 128 * 1024 * 1024;
const DEFAULT_MAX_UPLOAD_FILES = 4096;
const DEFAULT_UPLOAD_EXTENSIONS = [
  ".txt", ".bin", ".cor", ".z60", ".dta", ".mpr", ".mpt", ".csv",
  ".tsv", ".xls", ".xlsx", ".dat", ".zip", ".json", ".jsonl", ".log",
];
const AUTO_UPDATE_INTERVALS = new Set([30, 60, 180, 360, 720, 1440]);

const FALLBACK_CONNECTIVITY_MACHINES = [
  {
    id: "01_测试室1_AGHID-G_192.168.110.153",
    name: "测试室 1",
    hostname: "AGHID-G",
    ip: "192.168.110.153",
    status: "unchecked",
    reachable: null,
    message: "尚未检查连通性",
  },
  {
    id: "02_测试室2_AGHID-H_192.168.110.155",
    name: "测试室 2",
    hostname: "AGHID-H",
    ip: "192.168.110.155",
    status: "unchecked",
    reachable: null,
    message: "尚未检查连通性",
  },
  {
    id: "03_制备室_A-9_192.168.110.164",
    name: "制备室",
    hostname: "A-9",
    ip: "192.168.110.164",
    status: "unchecked",
    reachable: null,
    message: "尚未检查连通性",
  },
];

const configState = {
  status: null,
  collectionConfig: null,
  collectionConfigDraft: [],
  collectionConfigDirty: false,
  collectionConfigSaving: false,
  autoUpdate: null,
  autoUpdateSaving: false,
  autoUpdatePollTimer: null,
  livePreview: null,
  livePreviewSaving: false,
  livePreviewPollTimer: null,
  checkingPaths: new Set(),
  pathCheckResults: new Map(),
  jobPollTimer: null,
  jobElapsedTimer: null,
  lastJobAnnouncement: "",
  uploadQueue: [],
  uploading: false,
  uploadIgnored: [],
  connectivity: {
    available: true,
    can_check: false,
    machines: FALLBACK_CONNECTIVITY_MACHINES.map((machine) => ({ ...machine })),
  },
  connectivityLoading: true,
  connectivityError: "",
  checkingMachineIds: new Set(),
};

const configElements = Object.fromEntries(
  [
    "workbenchBrand", "workbenchBrandTitle", "workbenchBrandSubtitle",
    "workbenchEnvironmentTitle", "workbenchEnvironmentNote", "workbenchVersion", "startStopNavNumber",
    "startStopWorkstationsNavNumber",
    "startStopConfigNavNumber", "startStopCvEisNavNumber", "startStopMaterialsNavNumber",
    "sidebarConfigState", "configAccessState", "configNotice",
    "updateCapability", "jobStatus", "refreshData", "renderAtlas", "openUpload",
    "renderDataMode", "renderMaterialScope",
    "toggleAutoUpdate", "autoUpdateInterval", "autoUpdatePanel", "autoUpdateState",
    "autoUpdateNextRun", "autoUpdateLastResult",
    "toggleLivePreview", "plotLivePreview", "livePreviewPanel", "livePreviewState",
    "livePreviewNextRun", "livePreviewLastResult", "livePreviewPlotHint",
    "jobProgressPanel", "jobProgressTitle", "jobProgressState", "jobProgressBar",
    "jobProgressPercent", "jobProgressPhase", "jobProgressCurrentLabel",
    "jobProgressCurrent", "jobProgressCountLabel", "jobProgressCount",
    "jobProgressElapsed", "jobProgressSteps", "jobProgressDetail",
    "jobProgressSummary", "jobProgressMachines", "jobProgressAnnouncement",
    "machineConnectivityList", "machineConnectivitySummary", "checkAllMachines",
    "safetyOverallState", "safetyStatusGrid",
    "safetyStorageCard", "safetyStorageState", "safetyStoragePrimary",
    "safetyStorageDetail", "safetyStorageWarning",
    "safetyBackupCard", "safetyBackupState", "safetyBackupPrimary",
    "safetyBackupDetail", "safetyBackupWarning",
    "safetyProvenanceCard", "safetyProvenanceState", "safetyProvenancePrimary",
    "safetyProvenanceDetail", "safetyAnalysisRun", "safetySnapshot",
    "safetyConfigRevision", "safetyArtifactGeneration", "safetyManifestHash",
    "safetyScriptHash", "safetyImageReference",
    "collectionConfigRevision", "collectionConfigUpdatedAt", "collectionConfigDirtyBadge",
    "collectionConfigNotice", "collectionMachineList", "collectionConfigSaveState",
    "reloadCollectionConfig", "saveCollectionConfig",
    "uploadDialog", "closeUpload", "cancelUpload",
    "uploadGroupName", "chooseUploadFiles", "chooseUploadFolder", "uploadDropZone",
    "uploadFiles", "uploadFolder", "uploadQueueSummary", "uploadQueue",
    "clearUploadQueue", "uploadProgressArea", "uploadProgressLabel", "uploadProgressValue",
    "uploadProgress", "uploadResult", "startUpload",
  ].map((id) => [id, document.querySelector(`#${id}`)]),
);

class ConfigRequestError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function configRequest(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
    headers: options.body
      ? { "Content-Type": "application/json", ...(options.headers || {}) }
      : options.headers,
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new ConfigRequestError(payload?.error || `请求失败（${response.status}）`, response.status);
  }
  return payload;
}

function uploadRequest(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    });
    xhr.addEventListener("load", () => {
      const payload = xhr.response && typeof xhr.response === "object" ? xhr.response : {};
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new ConfigRequestError(payload.error || `上传失败（${xhr.status}）`, xhr.status));
    });
    xhr.addEventListener("error", () => reject(new Error("网络连接中断，请重试。")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消。")));
    xhr.send(file);
  });
}

function configElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function configFormatInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function configFormatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = bytes;
  let unit = "B";
  for (const candidate of units) {
    amount /= 1024;
    unit = candidate;
    if (amount < 1024) break;
  }
  return `${amount.toFixed(amount >= 100 ? 0 : amount >= 10 ? 1 : 2)} ${unit}`;
}

function configFormatTime(value) {
  if (!value) return "尚未保存";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function safetyHasValue(payload, keys) {
  if (!payload || typeof payload !== "object") return false;
  return keys.some((key) => payload[key] !== null && payload[key] !== undefined && payload[key] !== "");
}

function safetyFiniteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function safetyShortValue(value, fallback = "尚未启用") {
  if (value === null || value === undefined || value === "") return fallback;
  const normalized = String(value).trim().replace(/^sha256:/i, "");
  if (!normalized) return fallback;
  return normalized.length > 12 ? `${normalized.slice(0, 12)}…` : normalized;
}

function safetyShortReference(value) {
  if (value === null || value === undefined || value === "") return "尚未启用";
  const normalized = String(value).trim();
  if (!normalized) return "尚未启用";
  const digestMarker = normalized.toLowerCase().lastIndexOf("@sha256:");
  if (digestMarker >= 0) {
    const imageName = normalized.slice(0, digestMarker).split("/").pop() || "image";
    const digest = normalized.slice(digestMarker + 8);
    return `${imageName}@${safetyShortValue(digest)}`;
  }
  if (normalized.length <= 42) return normalized;
  return `${normalized.slice(0, 27)}…${normalized.slice(-10)}`;
}

function setSafetyCardState(card, badge, state, label) {
  card.dataset.safetyState = state;
  badge.className = `safety-state-badge ${state}`;
  badge.textContent = label;
}

function renderStorageSafety(storage) {
  const enabled = safetyHasValue(storage, [
    "ok", "free_bytes", "total_bytes", "used_percent", "preflight_ok", "shortfall_bytes",
  ]);
  if (!enabled) {
    setSafetyCardState(configElements.safetyStorageCard, configElements.safetyStorageState, "neutral", "尚未启用");
    configElements.safetyStoragePrimary.textContent = "尚未启用";
    configElements.safetyStorageDetail.textContent = "服务器尚未提供存储安全状态。";
    configElements.safetyStorageWarning.hidden = true;
    configElements.safetyStorageWarning.textContent = "";
    return false;
  }

  const freeBytes = safetyFiniteNumber(storage.free_bytes);
  const totalBytes = safetyFiniteNumber(storage.total_bytes);
  const usedPercent = safetyFiniteNumber(storage.used_percent);
  const shortfallBytes = safetyFiniteNumber(storage.shortfall_bytes);
  const blockedRoles = Array.isArray(storage.blocked_roles)
    ? storage.blocked_roles.filter((role) => ["database", "scratch", "cache"].includes(role))
    : [];
  const blockedRoleText = blockedRoles.map((role) => ({
    database: "数据库存储",
    scratch: "任务临时空间",
    cache: "网页图集缓存",
  })[role]).filter(Boolean).join("、");
  const isRisk = storage.ok === false || storage.preflight_ok === false;
  const state = isRisk ? "danger" : storage.ok === true || storage.preflight_ok === true ? "success" : "neutral";
  const label = isRisk ? "空间风险" : state === "success" ? "状态正常" : "已启用";
  setSafetyCardState(configElements.safetyStorageCard, configElements.safetyStorageState, state, label);
  configElements.safetyStoragePrimary.textContent = freeBytes === null
    ? label
    : `${configFormatBytes(freeBytes)} 可用`;
  const details = [];
  if (totalBytes !== null) details.push(`总容量 ${configFormatBytes(totalBytes)}`);
  if (usedPercent !== null) details.push(`已使用 ${usedPercent.toFixed(usedPercent >= 10 ? 1 : 2)}%`);
  if (storage.preflight_ok === true) details.push("下一次任务容量预检通过");
  configElements.safetyStorageDetail.textContent = details.length ? details.join(" · ") : "存储状态已启用。";
  configElements.safetyStorageWarning.hidden = !isRisk;
  configElements.safetyStorageWarning.textContent = isRisk
    ? shortfallBytes !== null && shortfallBytes > 0
      ? `警告：安全完成下一次任务预计还缺 ${configFormatBytes(shortfallBytes)}${blockedRoleText ? `；受限位置：${blockedRoleText}` : ""}。`
      : `警告：当前空间不足以安全完成下一次任务${blockedRoleText ? `；受限位置：${blockedRoleText}` : ""}。`
    : "";
  return true;
}

function renderBackupSafety(backup) {
  const enabled = safetyHasValue(backup, [
    "configured", "available", "backup_count", "invalid_count", "latest_created_utc",
    "latest_valid", "same_filesystem",
  ]);
  if (!enabled) {
    setSafetyCardState(configElements.safetyBackupCard, configElements.safetyBackupState, "neutral", "尚未启用");
    configElements.safetyBackupPrimary.textContent = "尚未启用";
    configElements.safetyBackupDetail.textContent = "服务器尚未提供备份状态。";
    configElements.safetyBackupWarning.hidden = true;
    configElements.safetyBackupWarning.textContent = "";
    return false;
  }

  const backupCount = Math.max(0, safetyFiniteNumber(backup.backup_count) || 0);
  const invalidCount = Math.max(0, safetyFiniteNumber(backup.invalid_count) || 0);
  const verificationFailed = invalidCount > 0
    || (backupCount > 0 && backup.latest_valid === false);
  const destinationFull = backup.destination_preflight_ok === false;
  const tieredPolicy = backup.policy_mode === "risk_tiered";
  const scheduledState = String(backup.scheduled_state || "");
  const waitingForFirstBackup = backupCount < 1
    && invalidCount === 0
    && backup.destination_preflight_ok === true;
  let state = "neutral";
  let label = "已启用";
  if (backup.configured === false) label = "尚未配置";
  else if (verificationFailed) {
    state = "danger";
    label = "清单异常";
  } else if (destinationFull) {
    state = "warning";
    label = "后台备份空间不足";
  } else if (waitingForFirstBackup) {
    state = "warning";
    label = "等待首次备份";
  } else if (backup.available === false) {
    state = "warning";
    label = "等待后台备份";
  } else if (backup.same_filesystem === true) {
    state = "warning";
    label = "仅同盘回滚";
  } else if (tieredPolicy) {
    state = "success";
    label = "按风险执行";
  } else {
    state = "success";
    label = "异盘已登记";
  }
  setSafetyCardState(configElements.safetyBackupCard, configElements.safetyBackupState, state, label);
  configElements.safetyBackupPrimary.textContent = scheduledState === "running"
    ? "后台全量备份正在运行"
    : backup.latest_created_utc
      ? configFormatTime(backup.latest_created_utc)
      : backupCount > 0 ? "有备份，时间未记录" : "尚无可用备份";
  const details = [`已记录 ${configFormatInteger(backupCount)} 份备份`];
  if (tieredPolicy && backup.routine_full_backup_required === false) {
    details.push("普通更新、下载和绘图不做全量备份");
  }
  if (
    tieredPolicy
    && backup.schema_change_full_backup_required === true
    && backup.database_repair_full_backup_required === true
  ) {
    details.push("数据库迁移或修复前强制全量备份");
  }
  if (backup.scheduled_background_enabled === true) {
    details.push(backup.scheduled_background_label || "低频后台执行全量备份");
  }
  if (backup.scheduled_next_due_utc) {
    details.push(`下次 ${configFormatTime(backup.scheduled_next_due_utc)}`);
  }
  if (invalidCount > 0) details.push(`${configFormatInteger(invalidCount)} 份清单或大小异常`);
  else if (backupCount > 0 && backup.latest_valid === true) details.push("当前清单与文件大小一致");
  if (backup.latest_created_with_full_verification === true) {
    details.push("创建时完成 SHA-256 与 SQLite 检查");
  }
  configElements.safetyBackupDetail.textContent = details.join(" · ");
  const warnings = [];
  if (backup.same_filesystem === true) {
    warnings.push("警告：备份与数据库位于同一磁盘，只能作为快速回滚点，不能替代异盘备份。");
  }
  if (verificationFailed) warnings.push("存在清单或文件大小异常的备份，请勿将其作为唯一恢复点。");
  if (destinationFull) warnings.push("备份目标剩余空间不足，创建新备份前需要释放空间或更换目标磁盘。");
  if (scheduledState === "failed") {
    warnings.push("最近一次后台全量备份失败；普通更新与分析不受阻塞，请在空闲时检查 Docker 和备份磁盘。");
  }
  if (backupCount > 0 && backup.latest_verification_level === "manifest_and_size") {
    warnings.push("页面状态为轻量检查；实际恢复前请再执行完整哈希与 SQLite 核验。");
  }
  configElements.safetyBackupWarning.hidden = warnings.length === 0;
  configElements.safetyBackupWarning.textContent = warnings.join(" ");
  return true;
}

function renderProvenanceSafety(provenance) {
  const enabled = safetyHasValue(provenance, [
    "state", "analysis_run_id", "snapshot_id", "config_revision", "artifact_generation_id",
    "artifact_manifest_sha256", "analysis_script_sha256", "image_reference", "created_utc",
  ]);
  if (!enabled) {
    setSafetyCardState(configElements.safetyProvenanceCard, configElements.safetyProvenanceState, "neutral", "尚未启用");
    configElements.safetyProvenancePrimary.textContent = "尚未启用";
    configElements.safetyProvenanceDetail.textContent = "服务器尚未提供图集封存状态。";
  } else {
    const provenanceState = String(provenance.state || "").trim().toLowerCase();
    const sealed = provenanceState === "sealed";
    const legacy = provenanceState === "legacy";
    const state = sealed ? "success" : legacy ? "warning" : "warning";
    const label = sealed ? "已封存" : legacy ? "旧版图集" : "待封存";
    setSafetyCardState(configElements.safetyProvenanceCard, configElements.safetyProvenanceState, state, label);
    configElements.safetyProvenancePrimary.textContent = sealed
      ? "当前图集已封存"
      : legacy ? "当前图集为旧版结果" : "当前图集尚未封存";
    configElements.safetyProvenanceDetail.textContent = provenance.created_utc
      ? `生成于 ${configFormatTime(provenance.created_utc)}`
      : "未记录图集生成时间。";
  }

  configElements.safetyAnalysisRun.textContent = safetyShortValue(provenance?.analysis_run_id);
  configElements.safetySnapshot.textContent = safetyShortValue(provenance?.snapshot_id);
  configElements.safetyConfigRevision.textContent = provenance?.config_revision === null
    || provenance?.config_revision === undefined || provenance?.config_revision === ""
    ? "尚未启用" : `r${provenance.config_revision}`;
  configElements.safetyArtifactGeneration.textContent = provenance?.artifact_generation_id === null
    || provenance?.artifact_generation_id === undefined || provenance?.artifact_generation_id === ""
    ? "尚未启用" : `g${provenance.artifact_generation_id}`;
  configElements.safetyManifestHash.textContent = safetyShortValue(provenance?.artifact_manifest_sha256);
  configElements.safetyScriptHash.textContent = safetyShortValue(provenance?.analysis_script_sha256);
  configElements.safetyImageReference.textContent = safetyShortReference(provenance?.image_reference);
  return enabled;
}

function renderSafetyStatus(safety = null) {
  const enabled = [
    renderStorageSafety(safety?.storage),
    renderBackupSafety(safety?.backup),
    renderProvenanceSafety(safety?.provenance),
  ];
  const states = [
    configElements.safetyStorageCard.dataset.safetyState,
    configElements.safetyBackupCard.dataset.safetyState,
    configElements.safetyProvenanceCard.dataset.safetyState,
  ];
  let state = "neutral";
  let label = enabled.some(Boolean) ? "部分已启用" : "尚未启用";
  if (states.includes("danger")) {
    state = "danger";
    label = "需要处理";
  } else if (states.includes("warning")) {
    state = "warning";
    label = "请留意";
  } else if (enabled.every(Boolean) && states.every((item) => item === "success")) {
    state = "success";
    label = "状态正常";
  }
  configElements.safetyOverallState.className = `safety-overall-state ${state}`;
  configElements.safetyOverallState.textContent = label;
  configElements.safetyStatusGrid.setAttribute("aria-busy", "false");
}

function autoUpdateResultText(payload) {
  const status = String(payload?.last_status || payload?.last_outcome || "");
  const labels = {
    completed: "上次自动更新已完成",
    completed_with_warnings: "上次自动更新完成，但有警告",
    failed: "上次自动更新失败，将按计划重试",
    busy: "上次到期时有其他任务，已延后",
    waiting_for_current_task: "有其他任务运行，自动更新已延后",
    unavailable: "上次到期时更新环境不可用",
    interrupted_by_restart: "服务重启后已恢复自动更新计划",
    queued: "自动更新任务已排队",
    running: "正在执行自动更新",
  };
  const time = payload?.last_completed_utc || payload?.last_attempt_utc || payload?.last_triggered_utc;
  const message = labels[status] || payload?.last_message || "尚未自动更新";
  return time ? `${message} · ${configFormatTime(time)}` : message;
}

function renderAutoUpdate() {
  const payload = configState.autoUpdate;
  const readOnly = isConfigReadOnly();
  const available = Boolean(payload?.available !== false && payload && !readOnly);
  const enabled = payload?.enabled === true;
  const interval = Number(payload?.interval_minutes || 60);
  if (AUTO_UPDATE_INTERVALS.has(interval)) configElements.autoUpdateInterval.value = String(interval);
  configElements.toggleAutoUpdate.setAttribute("aria-pressed", String(enabled));
  configElements.toggleAutoUpdate.textContent = configState.autoUpdateSaving
    ? "正在保存…"
    : enabled ? "关闭自动更新" : "开启自动更新";
  configElements.toggleAutoUpdate.disabled = !available || configState.autoUpdateSaving;
  configElements.autoUpdateInterval.disabled = !available || configState.autoUpdateSaving;
  configElements.autoUpdateState.textContent = !available
    ? readOnly ? "局域网只读" : "自动更新不可用"
    : enabled ? `已开启 · 每 ${interval < 60 ? `${interval} 分钟` : `${interval / 60} 小时`}` : "已关闭";
  configElements.autoUpdateNextRun.textContent = enabled
    ? configFormatTime(payload?.next_run_utc)
    : "开启后从一个周期后开始";
  configElements.autoUpdateLastResult.textContent = autoUpdateResultText(payload);
  configElements.autoUpdateLastResult.title = configElements.autoUpdateLastResult.textContent;
  configElements.autoUpdatePanel.classList.toggle("is-enabled", enabled);
}

function scheduleAutoUpdatePoll() {
  window.clearTimeout(configState.autoUpdatePollTimer);
  if (configState.autoUpdate?.enabled !== true || isConfigReadOnly()) return;
  configState.autoUpdatePollTimer = window.setTimeout(async () => {
    await Promise.all([loadAutoUpdate({ silent: true }), loadConfigWorkspace()]);
  }, 15000);
}

async function loadAutoUpdate({ silent = false } = {}) {
  if (isConfigReadOnly()) {
    configState.autoUpdate = { available: false, enabled: false };
    renderAutoUpdate();
    return;
  }
  try {
    configState.autoUpdate = await configRequest("/api/start-stop/auto-update");
    renderAutoUpdate();
    scheduleAutoUpdatePoll();
  } catch (error) {
    configState.autoUpdate = { available: false, enabled: false, last_message: error.message };
    renderAutoUpdate();
    if (!silent && error.status !== 403) setConfigNotice(`无法读取自动更新设置：${error.message}`, "error");
  }
}

async function saveAutoUpdate(enabled) {
  if (isConfigReadOnly() || configState.autoUpdateSaving) return;
  const intervalMinutes = Number(configElements.autoUpdateInterval.value);
  if (!AUTO_UPDATE_INTERVALS.has(intervalMinutes)) {
    setConfigNotice("自动更新周期无效，请重新选择。", "error");
    return;
  }
  configState.autoUpdateSaving = true;
  renderAutoUpdate();
  try {
    configState.autoUpdate = await configRequest("/api/start-stop/auto-update", {
      method: "PUT",
      body: JSON.stringify({
        expected_revision: Number(configState.autoUpdate?.revision || 0),
        enabled: Boolean(enabled),
        interval_minutes: intervalMinutes,
      }),
    });
    renderAutoUpdate();
    scheduleAutoUpdatePoll();
    setConfigNotice(
      enabled
        ? "自动更新已开启；首次检查将在一个完整周期后执行，期间不会自动重新绘图。"
        : "自动更新已关闭；已经开始的数据任务不会被中断。",
      "success",
    );
  } catch (error) {
    if (error.status === 409) await loadAutoUpdate({ silent: true });
    setConfigNotice(error.message, "error");
  } finally {
    configState.autoUpdateSaving = false;
    renderAutoUpdate();
  }
}

function livePreviewResultText(payload) {
  const status = String(payload?.last_status || "never_run");
  const labels = {
    never_run: "尚未生成预览",
    running: payload?.message || "正在更新实时预览",
    completed: "最近一次实时预览已完成",
    completed_with_warnings: "最近一次预览完成，但有文件未处理",
    failed: payload?.message || "最近一次实时预览失败",
  };
  const previewItems = Array.isArray(payload?.preview?.items) ? payload.preview.items : [];
  const itemCount = previewItems.filter((item) => (
    item?.analysis?.work_step_key
    && (
      Array.isArray(item?.analysis?.cycle_points) && item.analysis.cycle_points.length
      || Array.isArray(item?.analysis?.overview_points) && item.analysis.overview_points.length
    )
  )).length;
  const finished = payload?.last_finished_utc;
  const suffix = status === "completed" || status === "completed_with_warnings"
    ? ` · ${itemCount} 条曲线`
    : "";
  return `${labels[status] || payload?.message || "尚未生成预览"}${suffix}${finished ? ` · ${configFormatTime(finished)}` : ""}`;
}

function renderLivePreview() {
  const payload = configState.livePreview;
  const readOnly = isConfigReadOnly();
  const available = Boolean(payload?.available !== false && payload && !readOnly);
  const enabled = payload?.enabled === true;
  const running = payload?.last_status === "running";
  const itemCount = Array.isArray(payload?.preview?.items) ? payload.preview.items.length : 0;
  const hasPlottableItems = available && itemCount > 0;
  configElements.toggleLivePreview.setAttribute("aria-pressed", String(enabled));
  configElements.toggleLivePreview.textContent = configState.livePreviewSaving
    ? "正在保存…"
    : enabled ? "关闭实时预览" : "开启实时预览";
  configElements.toggleLivePreview.disabled = !available || configState.livePreviewSaving;
  configElements.livePreviewState.textContent = !available
    ? readOnly ? "局域网只读" : "实时预览不可用"
    : running ? payload?.message || "正在更新"
    : enabled ? "已开启 · 每 5 分钟" : "已关闭";
  configElements.livePreviewNextRun.textContent = enabled
    ? configFormatTime(payload?.next_run_utc)
    : "开启后立即执行";
  configElements.livePreviewLastResult.textContent = livePreviewResultText(payload);
  configElements.livePreviewLastResult.title = configElements.livePreviewLastResult.textContent;
  configElements.plotLivePreview.disabled = !hasPlottableItems;
  configElements.plotLivePreview.textContent = hasPlottableItems
    ? `在 03 页叠加正在测试数据（${itemCount}）`
    : "在 03 页叠加正在测试数据";
  configElements.plotLivePreview.title = hasPlottableItems
    ? "进入 03 启停分析，按正式分段、计算和接续规则叠加当前活动曲线"
    : "等待实时预览按正式启停规则计算出可绘制数据";
  configElements.livePreviewPlotHint.textContent = hasPlottableItems
    ? `${running ? "本轮快照正在刷新；" : ""}点击后在 03 启停分析的同一工步、同一张图中叠加 ${itemCount} 条活动曲线，仍不会正式入库。`
    : enabled
      ? "正在等待识别完整循环并按正式启停规则计算第一份快照。"
      : "开启实时预览并完成首次正式规则计算后即可叠加绘图。";
  configElements.livePreviewPanel.classList.toggle("is-enabled", enabled);
}

function scheduleLivePreviewPoll() {
  window.clearTimeout(configState.livePreviewPollTimer);
  if (configState.livePreview?.enabled !== true || isConfigReadOnly()) return;
  configState.livePreviewPollTimer = window.setTimeout(async () => {
    await loadLivePreview({ silent: true });
  }, 10_000);
}

async function loadLivePreview({ silent = false } = {}) {
  if (isConfigReadOnly()) {
    configState.livePreview = { available: false, enabled: false };
    renderLivePreview();
    return;
  }
  try {
    configState.livePreview = await configRequest("/api/start-stop/live-preview");
    renderLivePreview();
    scheduleLivePreviewPoll();
  } catch (error) {
    configState.livePreview = { available: false, enabled: false, message: error.message };
    renderLivePreview();
    if (!silent && error.status !== 403) setConfigNotice(`无法读取实时数据预览设置：${error.message}`, "error");
  }
}

async function saveLivePreview(enabled) {
  if (isConfigReadOnly() || configState.livePreviewSaving) return;
  configState.livePreviewSaving = true;
  renderLivePreview();
  try {
    configState.livePreview = await configRequest("/api/start-stop/live-preview", {
      method: "PUT",
      body: JSON.stringify({
        expected_revision: Number(configState.livePreview?.revision || 0),
        enabled: Boolean(enabled),
      }),
    });
    renderLivePreview();
    scheduleLivePreviewPoll();
    setConfigNotice(
      enabled
        ? "实时数据预览已开启；正在执行首次识别，之后每 5 分钟自动更新。"
        : "实时数据预览已关闭；已生成的最近一次预览仍可查看。",
      "success",
    );
  } catch (error) {
    if (error.status === 409) await loadLivePreview({ silent: true });
    setConfigNotice(error.message, "error");
  } finally {
    configState.livePreviewSaving = false;
    renderLivePreview();
  }
}

function isConfigReadOnly() {
  return configState.status?.access_mode === "lan_read_only";
}

function setConfigNotice(message = "", type = "warning") {
  configElements.configNotice.hidden = !message;
  configElements.configNotice.classList.toggle("error", type === "error");
  configElements.configNotice.classList.toggle("success", type === "success");
  configElements.configNotice.textContent = message;
}

function applyConfigDeploymentProfile(payload) {
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
  configElements.workbenchBrand.href = "/start-stop";
  configElements.workbenchBrand.setAttribute("aria-label", "返回启停运行配置");
  configElements.workbenchBrandTitle.textContent = "Start–stop Studio";
  configElements.workbenchBrandSubtitle.textContent = "启停数据分析";
  const serviceVersion = payload?.service?.version;
  configElements.workbenchVersion.textContent = serviceVersion && serviceVersion !== "unknown"
    ? `版本 ${serviceVersion}`
    : "版本未知";
  configElements.workbenchVersion.title = payload?.service?.image_reference || "当前运行服务版本";
  if (!repositoryMode) {
    configElements.startStopConfigNavNumber.textContent = "01";
    configElements.startStopWorkstationsNavNumber.textContent = "02";
    configElements.startStopNavNumber.textContent = "03";
    configElements.startStopCvEisNavNumber.textContent = "04";
    configElements.startStopMaterialsNavNumber.textContent = "05";
    return false;
  }
  configElements.startStopConfigNavNumber.textContent = "01";
  configElements.startStopWorkstationsNavNumber.textContent = "02";
  configElements.startStopNavNumber.textContent = "03";
  configElements.startStopCvEisNavNumber.textContent = "04";
  configElements.startStopMaterialsNavNumber.textContent = "05";
  return true;
}

function canSaveConfig() {
  if (isConfigReadOnly()) return false;
  const capabilities = configState.status?.capabilities;
  return capabilities
    ? capabilities.can_save_configuration === true
    : Boolean(configState.status?.available);
}

function canRenderConfig() {
  if (isConfigReadOnly()) return false;
  const capabilities = configState.status?.capabilities;
  return capabilities
    ? capabilities.can_render_atlas === true
    : Boolean(configState.status?.available && configState.status?.execution?.render_ready);
}

function configJobBusy() {
  return ["queued", "running"].includes(configState.status?.job?.status);
}

function configCapabilities(payload = configState.status || {}) {
  const readOnly = payload?.access_mode === "lan_read_only";
  const repositoryMode = payload?.deployment_profile === "start_stop_repository"
    || payload?.repository?.storage_mode === "sqlite_blob_repository";
  const hasCapabilities = Boolean(payload?.capabilities);
  const updateReady = payload?.execution?.update_ready ?? payload?.execution?.ready ?? false;
  const renderReady = payload?.execution?.render_ready ?? payload?.execution?.ready ?? false;
  const canUpdate = hasCapabilities
    ? payload.capabilities.can_update_data === true
    : Boolean(payload?.available && updateReady && !readOnly);
  const canRender = hasCapabilities
    ? payload.capabilities.can_render_atlas === true
    : Boolean(payload?.available && renderReady && !readOnly);
  const canUpload = repositoryMode && (hasCapabilities
    ? payload.capabilities.can_upload_data === true
    : Boolean(payload?.available && updateReady && !readOnly));
  const canPrepareUpload = repositoryMode && (hasCapabilities
    ? payload.capabilities.can_prepare_upload === true
    : canUpload);
  return {
    readOnly,
    repositoryMode,
    updateReady,
    renderReady,
    canUpdate,
    canRender,
    canUpload,
    canPrepareUpload,
    busy: ["queued", "running"].includes(payload?.job?.status),
  };
}

function uploadLimits() {
  const capabilities = configState.status?.capabilities || {};
  return {
    extensions: Array.isArray(capabilities.upload_extensions)
      ? capabilities.upload_extensions.map((item) => String(item).toLowerCase())
      : DEFAULT_UPLOAD_EXTENSIONS,
    maxFileBytes: Number(capabilities.upload_max_file_bytes) || DEFAULT_MAX_UPLOAD_FILE_BYTES,
    maxFiles: Number(capabilities.upload_max_batch_files) || DEFAULT_MAX_UPLOAD_FILES,
  };
}

function applyUploadCapabilityLimits() {
  const accept = uploadLimits().extensions.join(",");
  configElements.uploadFiles.accept = accept;
  configElements.uploadFolder.accept = accept;
}

function updateCapabilityView(payload = configState.status || {}) {
  const capability = configCapabilities(payload);
  let stateName = "blocked";
  let label = "当前不能更新";
  let detail = payload?.capabilities?.message || payload?.message || "启停数据工作区当前不可用。";
  if (capability.readOnly) {
    stateName = "read-only";
    label = "局域网只读";
  } else if (capability.busy) {
    stateName = "busy";
    if (payload?.job?.stage === "collecting_remote") {
      const machineCount = Number(payload?.execution?.collection_machine_count || 0);
      label = machineCount ? `正在检查 ${machineCount} 台实验机` : "正在下载到数据库";
    } else if (payload?.job?.stage === "refreshing_material_table") {
      label = "正在更新材料表";
    } else {
      label = payload?.job?.action === "render" ? "正在更新平台分析" : "正在更新数据";
    }
    detail = payload?.job?.message || label;
  } else if (capability.canUpdate) {
    stateName = "ready";
    const machineCount = Number(payload?.execution?.collection_machine_count || 0);
    label = machineCount ? `可从 ${machineCount} 台实验机下载入库` : "可下载到独立数据库";
    detail = machineCount
      ? `已配置 ${machineCount} 台实验机，可以检查并下载新数据。`
      : "数据更新功能已准备就绪。";
  }
  configElements.updateCapability.className = `update-capability-chip ${stateName}`;
  configElements.updateCapability.textContent = label;
  configElements.updateCapability.title = detail;
  configElements.configAccessState.textContent = label;
  return capability;
}

function updateJobView(job = {}) {
  const status = job.status || "idle";
  configElements.jobStatus.classList.toggle("running", ["queued", "running"].includes(status));
  configElements.jobStatus.classList.toggle("failed", ["failed", "interrupted"].includes(status));
  configElements.jobStatus.classList.toggle("completed", status === "completed");
  configElements.jobStatus.classList.toggle("warning", status === "completed_with_warnings");
  const compactRunningLabel = {
    scan: "正在下载并更新",
    prepare_upload: "正在更新材料表",
    render: "正在更新平台分析",
  }[job.action];
  const labels = {
    idle: "没有运行中的任务",
    queued: "任务已排队",
    running: compactRunningLabel || job.message || "正在处理",
    completed: job.message || "任务已完成",
    completed_with_warnings: job.message || "任务完成，但有未完成项",
    failed: job.message || "任务失败",
    interrupted: job.message || "服务重启导致任务中断，可重新执行",
  };
  configElements.jobStatus.textContent = labels[status] || status;
}

const JOB_PHASE_LABELS = {
  idle: "尚未开始",
  queued: "任务已排队，等待执行",
  collecting_remote: "正在连接实验电脑并检查新文件",
  connecting_remote: "正在连接实验电脑",
  connecting_machines: "正在连接实验电脑",
  scanning_remote: "正在扫描实验电脑数据源",
  scanning_files: "正在扫描文件",
  scanning_sources: "正在扫描数据源与文件",
  inventorying: "正在统计可更新文件",
  downloading_files: "正在下载新文件",
  storing_files: "正在将文件写入数据库",
  writing_database: "正在写入独立数据库",
  ingesting_files: "正在将文件写入独立数据库",
  freezing_snapshot: "正在固定本次数据快照",
  materializing_snapshot: "正在从数据库准备分析数据",
  refreshing_material_table: "正在更新材料表",
  preparing_upload: "正在准备已上传的数据",
  rendering: "正在计算平台分析结果",
  publishing: "正在发布数据库产物",
  finalizing: "正在整理任务结果",
  prepared: "材料表已准备完成",
  rendered: "平台分析结果已生成",
  completed: "任务已完成",
  failed: "任务未完成",
  interrupted: "任务因服务重启而中断",
};

const JOB_STATUS_LABELS = {
  queued: "等待执行",
  running: "正在进行",
  completed: "已完成",
  completed_with_warnings: "已完成，有警告",
  failed: "执行失败",
  interrupted: "已中断，可重新执行",
};

const RENDER_PROGRESS_STEPS = [
  "准备分析数据",
  "读取并校验文件",
  "计算循环与结果表",
  "准备网页曲线",
  "计算水位补偿数据",
  "校验输出文件",
  "发布平台分析结果",
];

const JOB_MACHINE_STATUS_LABELS = {
  queued: "等待",
  pending: "等待",
  connecting: "连接中",
  checking: "检查中",
  scanning: "扫描中",
  inventorying: "统计中",
  downloading: "下载中",
  storing: "写入数据库",
  importing: "入库中",
  refreshing: "刷新中",
  ingesting: "入库中",
  running: "处理中",
  completed: "已完成",
  completed_with_warnings: "完成，有警告",
  done: "已完成",
  success: "已完成",
  reachable: "已连接",
  skipped: "已跳过",
  warning: "有警告",
  failed: "失败",
  error: "失败",
  unreachable: "无法连接",
};

function finiteJobProgressNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function jobProgressUnit(unit = "") {
  const normalized = String(unit).trim().toLowerCase();
  return {
    file: "个文件",
    files: "个文件",
    machine: "台实验机",
    machines: "台实验机",
    root: "个数据源",
    roots: "个数据源",
    source: "个数据源",
    sources: "个数据源",
    step: "个阶段",
    steps: "个阶段",
    material: "种材料",
    materials: "种材料",
    page: "页",
    pages: "页",
    item: "项",
    items: "项",
  }[normalized] || String(unit || "项");
}

function jobProgressCurrentLabel(progress, phase) {
  if (progress?.current_label) return String(progress.current_label);
  const currentKind = String(progress?.current_kind || "").toLowerCase();
  if (["machine", "computer", "host"].includes(currentKind)) return "当前实验机";
  if (["source", "root", "folder"].includes(currentKind)) return "当前数据源";
  if (["file", "filename"].includes(currentKind)) return "当前文件";
  if (/connect|machine|host/.test(phase)) return "当前实验机";
  if (/download|files|ingest|storing|writing/.test(phase)) return "当前文件";
  if (/scan|inventor|source|root/.test(phase)) return "当前数据源或文件";
  return "当前处理对象";
}

function normalizeJobProgress(job = {}) {
  const progress = job.progress && typeof job.progress === "object" ? job.progress : {};
  const status = String(job.status || "idle");
  const phase = String(progress.phase || job.stage || status || "idle");
  const phaseLabel = String(progress.phase_label || JOB_PHASE_LABELS[phase] || job.message || "正在处理");
  const completed = finiteJobProgressNumber(progress.completed);
  const total = finiteJobProgressNumber(progress.total);
  const phaseIndex = finiteJobProgressNumber(progress.phase_index);
  const phaseCount = finiteJobProgressNumber(progress.phase_count);
  let percent = finiteJobProgressNumber(progress.percent);
  if (percent === null && completed !== null && total !== null && total > 0) {
    percent = (100 * completed) / total;
  }
  if (["completed", "completed_with_warnings"].includes(status)) percent = 100;
  if (status === "queued" && percent === null) percent = 0;
  if (percent !== null) percent = Math.min(100, Math.max(0, percent));
  const declaredMode = String(progress.mode || "");
  const mode = declaredMode === "determinate" || declaredMode === "indeterminate"
    ? declaredMode
    : percent === null ? "indeterminate" : "determinate";
  const currentItem = String(progress.current_item || "");
  return {
    progress,
    status,
    phase,
    phaseLabel,
    completed,
    total,
    phaseIndex,
    phaseCount,
    percent,
    mode,
    currentItem,
    currentLabel: jobProgressCurrentLabel(progress, phase),
    detail: String(progress.detail || job.message || phaseLabel),
    unit: jobProgressUnit(progress.unit),
    machines: Array.isArray(progress.machines) ? progress.machines : [],
  };
}

function jobProgressCountText(progress) {
  if (String(progress.progress?.unit || "").toLowerCase() === "bytes") {
    if (progress.completed !== null && progress.total !== null) {
      return `${configFormatBytes(progress.completed)} / ${configFormatBytes(progress.total)}`;
    }
    if (progress.completed !== null) return `已处理 ${configFormatBytes(progress.completed)}`;
    if (progress.total !== null) return `共 ${configFormatBytes(progress.total)}`;
  }
  if (progress.completed !== null && progress.total !== null) {
    return `已完成 ${configFormatInteger(progress.completed)} / ${configFormatInteger(progress.total)} ${progress.unit}`;
  }
  if (progress.completed !== null) return `已完成 ${configFormatInteger(progress.completed)} ${progress.unit}`;
  if (progress.total !== null) return `共 ${configFormatInteger(progress.total)} ${progress.unit}`;
  if (progress.phaseIndex !== null && progress.phaseCount !== null) {
    return `第 ${configFormatInteger(progress.phaseIndex)} / ${configFormatInteger(progress.phaseCount)} 个阶段`;
  }
  return ["completed", "completed_with_warnings", "failed", "interrupted"].includes(progress.status)
    ? "任务已结束"
    : "正在统计处理数量";
}

function jobProgressSummaryValues(job, progress) {
  const source = progress.progress?.summary && typeof progress.progress.summary === "object"
    ? progress.progress.summary
    : {};
  const result = job.result && typeof job.result === "object" ? job.result : {};
  const firstNumber = (...values) => {
    for (const value of values) {
      const number = finiteJobProgressNumber(value);
      if (number !== null) return Math.max(0, number);
    }
    return null;
  };
  if (job.action === "render" && ["completed", "completed_with_warnings"].includes(progress.status)) {
    return [
      ["已分析材料", firstNumber(result.materials_analyzed)],
      ["已入图材料", firstNumber(result.materials_in_atlas)],
      ["完整循环", firstNumber(result.complete_cycles)],
    ].filter(([, value]) => value !== null);
  }
  let skipped = firstNumber(source.skipped, result.collection_skipped);
  if (skipped === null && ["completed", "completed_with_warnings", "failed"].includes(progress.status)) {
    const hasSkipCounts = [
      "collection_already_collected",
      "collection_unsettled_skipped",
      "collection_changed_during_collection",
    ].some((key) => Object.prototype.hasOwnProperty.call(result, key));
    const already = firstNumber(result.collection_already_collected) || 0;
    const unsettled = firstNumber(result.collection_unsettled_skipped) || 0;
    const changed = firstNumber(result.collection_changed_during_collection) || 0;
    if (hasSkipCounts) skipped = already + unsettled + changed;
  }
  return [
    ["成功", firstNumber(
      result.collection_ingested,
      result.collection_downloaded,
      result.collection_copied,
      source.succeeded,
      source.success,
    )],
    ["跳过", skipped],
    ["失败", firstNumber(source.failed, source.errors, result.collection_errors)],
  ].filter(([, value]) => value !== null);
}

function renderJobProgressSummary(job, progress) {
  const values = jobProgressSummaryValues(job, progress);
  const fragment = document.createDocumentFragment();
  for (const [label, value] of values) {
    const item = configElement("span", "job-progress-summary-item");
    item.append(configElement("span", "", label), configElement("strong", "", configFormatInteger(value)));
    fragment.append(item);
  }
  configElements.jobProgressSummary.replaceChildren(fragment);
  configElements.jobProgressSummary.hidden = !values.length;
}

function renderJobProgressMachines(machines, unit) {
  const fragment = document.createDocumentFragment();
  for (const machine of machines) {
    if (!machine || typeof machine !== "object") continue;
    const row = configElement("div", "job-machine-progress-row");
    const name = String(machine.name || machine.hostname || machine.id || machine.machine_id || "实验机");
    const rawStatus = String(machine.status || "running").toLowerCase();
    const safeStatus = Object.prototype.hasOwnProperty.call(JOB_MACHINE_STATUS_LABELS, rawStatus)
      ? rawStatus
      : "running";
    const status = configElement(
      "span",
      `job-machine-progress-status ${safeStatus}`,
      JOB_MACHINE_STATUS_LABELS[rawStatus] || machine.status_label || "处理中",
    );
    const completed = finiteJobProgressNumber(machine.completed);
    const total = finiteJobProgressNumber(machine.total);
    const count = completed !== null && total !== null
      ? ` · ${configFormatInteger(completed)} / ${configFormatInteger(total)} ${jobProgressUnit(machine.unit || unit)}`
      : "";
    const detail = String(machine.current_item || machine.detail || machine.message || machine.phase_label || "等待进度更新");
    const detailNode = configElement("small", "", `${detail}${count}`);
    detailNode.title = `${detail}${count}`;
    const nameNode = configElement("strong", "", name);
    nameNode.title = name;
    row.append(nameNode, status, detailNode);
    fragment.append(row);
  }
  const hasMachines = fragment.childNodes.length > 0;
  configElements.jobProgressMachines.replaceChildren(fragment);
  configElements.jobProgressMachines.hidden = !hasMachines;
}

function jobElapsedText(job = {}) {
  const started = Date.parse(job.started_utc || "");
  if (!Number.isFinite(started)) return "00:00";
  const terminal = Date.parse(job.completed_utc || "");
  const ended = Number.isFinite(terminal) ? terminal : Date.now();
  const seconds = Math.max(0, Math.floor((ended - started) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  const pair = (value) => String(value).padStart(2, "0");
  return hours ? `${hours}:${pair(minutes)}:${pair(remainder)}` : `${pair(minutes)}:${pair(remainder)}`;
}

function updateJobElapsed(job, isBusy) {
  window.clearInterval(configState.jobElapsedTimer);
  const render = () => {
    configElements.jobProgressElapsed.textContent = jobElapsedText(job);
  };
  render();
  if (isBusy && job.started_utc) {
    configState.jobElapsedTimer = window.setInterval(render, 1000);
  }
}

function renderJobProgressSteps(progress, isRender) {
  if (!isRender) {
    configElements.jobProgressSteps.hidden = true;
    configElements.jobProgressSteps.replaceChildren();
    return;
  }
  const activeIndex = Math.max(1, Math.min(
    RENDER_PROGRESS_STEPS.length,
    Math.round(progress.phaseIndex || (progress.status === "queued" ? 1 : 1)),
  ));
  const terminalComplete = ["completed", "completed_with_warnings"].includes(progress.status);
  const fragment = document.createDocumentFragment();
  RENDER_PROGRESS_STEPS.forEach((label, index) => {
    const stepNumber = index + 1;
    const item = configElement("li", "job-progress-step");
    const complete = terminalComplete || stepNumber < activeIndex;
    const active = !terminalComplete && stepNumber === activeIndex;
    item.classList.toggle("is-complete", complete);
    item.classList.toggle("is-active", active);
    item.classList.toggle("is-failed", active && progress.status === "failed");
    item.append(
      configElement("span", "job-progress-step-number", complete ? "✓" : String(stepNumber)),
      configElement("span", "job-progress-step-label", label),
    );
    fragment.append(item);
  });
  configElements.jobProgressSteps.replaceChildren(fragment);
  configElements.jobProgressSteps.hidden = false;
}

function announceJobProgress(job, progress) {
  const key = [job.id, progress.status, progress.phaseIndex, progress.phaseLabel].join("|");
  if (key === configState.lastJobAnnouncement) return;
  configState.lastJobAnnouncement = key;
  configElements.jobProgressAnnouncement.textContent = [
    JOB_STATUS_LABELS[progress.status] || "任务状态更新",
    progress.phaseLabel,
  ].filter(Boolean).join("：");
}

function updateJobProgressView(job = {}) {
  const progress = normalizeJobProgress(job);
  const visible = progress.status !== "idle" && Boolean(job.id || job.action || job.stage || job.progress);
  configElements.jobProgressPanel.hidden = !visible;
  if (!visible) {
    window.clearInterval(configState.jobElapsedTimer);
    return;
  }

  const isBusy = ["queued", "running"].includes(progress.status);
  const isRender = job.action === "render";
  const statusClass = progress.status === "completed_with_warnings"
    ? "warning"
    : ["queued", "running", "completed", "failed", "interrupted"].includes(progress.status)
      ? progress.status
      : "failed";
  const visualStatusClass = statusClass === "interrupted" ? "failed" : statusClass;
  configElements.jobProgressPanel.classList.remove(
    "status-queued", "status-running", "status-completed", "status-warning", "status-failed", "task-render",
  );
  configElements.jobProgressPanel.classList.add(`status-${visualStatusClass}`);
  configElements.jobProgressPanel.classList.toggle("task-render", isRender);
  configElements.jobProgressTitle.textContent = {
    scan: "三台实验电脑更新进度",
    prepare_upload: "上传数据处理进度",
    render: "平台分析进度",
  }[job.action] || "数据任务进度";

  configElements.jobProgressState.className = `job-progress-state ${visualStatusClass === "queued" ? "running" : visualStatusClass}`;
  configElements.jobProgressState.textContent = JOB_STATUS_LABELS[progress.status] || "任务已结束";
  configElements.jobProgressPhase.textContent = progress.phaseLabel;
  configElements.jobProgressCurrentLabel.textContent = isRender ? "正在处理" : progress.currentLabel;
  configElements.jobProgressCurrent.textContent = progress.currentItem;
  configElements.jobProgressCurrent.title = progress.currentItem;
  const currentCell = configElements.jobProgressCurrent.closest("div");
  currentCell.hidden = !progress.currentItem || (isRender && !isBusy);
  configElements.jobProgressCountLabel.textContent = isRender ? "当前阶段进度" : "完成数量";
  configElements.jobProgressCount.textContent = jobProgressCountText(progress);
  configElements.jobProgressDetail.textContent = progress.detail;
  updateJobElapsed(job, isBusy);
  renderJobProgressSteps(progress, isRender);

  if (progress.mode === "indeterminate" && isBusy) {
    configElements.jobProgressBar.removeAttribute("value");
    configElements.jobProgressBar.textContent = "正在处理";
    configElements.jobProgressBar.setAttribute("aria-valuetext", "正在处理，完成比例统计中");
    configElements.jobProgressPercent.textContent = "进行中";
  } else if (progress.percent !== null) {
    const rounded = Math.round(progress.percent);
    configElements.jobProgressBar.value = rounded;
    configElements.jobProgressBar.textContent = `${rounded}%`;
    configElements.jobProgressBar.setAttribute("aria-valuetext", `已完成 ${rounded}%`);
    configElements.jobProgressPercent.textContent = `${rounded}%`;
  } else {
    configElements.jobProgressBar.removeAttribute("value");
    configElements.jobProgressBar.textContent = "未能确定完成比例";
    configElements.jobProgressBar.setAttribute("aria-valuetext", "未能确定完成比例");
    configElements.jobProgressPercent.textContent = "—";
  }
  renderJobProgressSummary(job, progress);
  renderJobProgressMachines(isRender ? [] : progress.machines, progress.progress.unit);
  announceJobProgress(job, progress);
}

function machineConnectivityState(machine) {
  if (configState.checkingMachineIds.has(machine.id)) return "checking";
  if (machine.reachable === true || machine.status === "reachable") return "reachable";
  if (machine.reachable === false || machine.status === "unreachable") return "unreachable";
  if (machine.status === "error") return "error";
  return "unchecked";
}

function machineConnectivityLabel(stateName) {
  return {
    unchecked: "未检查",
    checking: "检查中",
    reachable: "可连接",
    unreachable: "无法连接",
    error: "检查失败",
  }[stateName] || "未检查";
}

function machineConnectivityMessage(machine, stateName) {
  if (stateName === "checking") return "正在建立只读 SSH 连接，请稍候…";
  const parts = [machine.message || (stateName === "unchecked" ? "尚未检查连通性" : "检查完成")];
  const elapsed = Number(machine.elapsed_ms);
  if (Number.isFinite(elapsed) && elapsed >= 0) parts.push(`耗时 ${elapsed.toLocaleString("zh-CN")} ms`);
  if (machine.checked_at_utc) parts.push(`检查于 ${configFormatTime(machine.checked_at_utc)}`);
  return parts.join(" · ");
}

function renderMachineConnectivity() {
  const machines = Array.isArray(configState.connectivity?.machines)
    ? configState.connectivity.machines
    : [];
  const canCheck = configState.connectivity?.can_check === true
    && !isConfigReadOnly()
    && !configJobBusy();
  const checking = configState.checkingMachineIds.size > 0;
  const fragment = document.createDocumentFragment();

  for (const machine of machines) {
    const stateName = machineConnectivityState(machine);
    const row = configElement("article", "machine-status-row");
    row.dataset.machineId = machine.id;

    const identity = configElement("div", "machine-identity");
    identity.append(
      configElement("strong", "", machine.name || machine.hostname || machine.id),
      configElement("span", "", [machine.hostname, machine.ip].filter(Boolean).join(" · ")),
    );

    const status = configElement(
      "span",
      `machine-status-pill ${stateName}`,
      machineConnectivityLabel(stateName),
    );
    const message = configElement("p", "", machineConnectivityMessage(machine, stateName));
    message.title = message.textContent;

    const button = configElement("button", "machine-check-button secondary-outline-button local-write-only", "检查连通性");
    button.type = "button";
    button.dataset.machineId = machine.id;
    button.disabled = !canCheck || stateName === "checking";
    if (!canCheck) {
      button.title = isConfigReadOnly()
        ? "局域网入口仅显示状态"
        : configJobBusy() ? "请等待当前数据任务结束" : "当前不能执行连通性检查";
    }
    row.append(identity, status, message, button);
    fragment.append(row);
  }

  if (!machines.length) {
    fragment.append(configElement("div", "start-stop-loading", "未配置可检查的实验机"));
  }
  configElements.machineConnectivityList.replaceChildren(fragment);
  configElements.machineConnectivityList.setAttribute(
    "aria-busy",
    String(configState.connectivityLoading || checking),
  );

  const checked = machines.filter((machine) => ["reachable", "unreachable"].includes(machineConnectivityState(machine))).length;
  const reachable = machines.filter((machine) => machineConnectivityState(machine) === "reachable").length;
  if (configState.connectivityLoading) {
    configElements.machineConnectivitySummary.textContent = "正在读取实验机列表…";
  } else if (configState.connectivityError) {
    configElements.machineConnectivitySummary.textContent = `连接状态暂不可用：${configState.connectivityError}`;
  } else if (checking) {
    configElements.machineConnectivitySummary.textContent = `正在检查 ${configState.checkingMachineIds.size} 台实验机…`;
  } else {
    configElements.machineConnectivitySummary.textContent = checked
      ? `已检查 ${checked} / ${machines.length} 台，其中 ${reachable} 台可以连接`
      : `${machines.length} 台实验机等待检查`;
  }
  configElements.checkAllMachines.disabled = !canCheck || checking || !machines.length;
  configElements.checkAllMachines.title = canCheck ? "" : isConfigReadOnly()
    ? "局域网入口仅显示状态"
    : configJobBusy() ? "请等待当前数据任务结束" : "当前不能执行连通性检查";
}

function mergeMachineConnectivity(payload) {
  if (!payload || !Array.isArray(payload.machines)) return;
  configState.connectivity = {
    ...configState.connectivity,
    ...payload,
    machines: payload.machines.map((machine) => ({ ...machine })),
  };
  configState.connectivityLoading = false;
  configState.connectivityError = "";
  renderMachineConnectivity();
}

async function loadMachineConnectivity() {
  configState.connectivityLoading = true;
  renderMachineConnectivity();
  try {
    mergeMachineConnectivity(await configRequest("/api/start-stop/connectivity"));
  } catch (error) {
    configState.connectivityLoading = false;
    configState.connectivityError = error.message;
    configState.connectivity = {
      ...configState.connectivity,
      available: false,
      can_check: false,
    };
    renderMachineConnectivity();
  }
}

function replaceConnectivityMachine(machine) {
  const machines = [...(configState.connectivity?.machines || [])];
  const index = machines.findIndex((candidate) => candidate.id === machine.id);
  if (index >= 0) machines[index] = { ...machines[index], ...machine };
  else machines.push({ ...machine });
  configState.connectivity = { ...configState.connectivity, machines };
}

async function checkMachineConnectivity(machineId) {
  if (
    !machineId
    || isConfigReadOnly()
    || configJobBusy()
    || configState.connectivity?.can_check !== true
  ) return;
  configState.checkingMachineIds.add(machineId);
  renderMachineConnectivity();
  try {
    const machine = await configRequest("/api/start-stop/connectivity/check", {
      method: "POST",
      body: JSON.stringify({ machine_id: machineId }),
    });
    replaceConnectivityMachine(machine);
    configState.connectivityError = "";
  } catch (error) {
    replaceConnectivityMachine({
      id: machineId,
      status: "error",
      reachable: null,
      message: error.message,
      checked_at_utc: new Date().toISOString(),
    });
  } finally {
    configState.checkingMachineIds.delete(machineId);
    renderMachineConnectivity();
  }
}

async function checkAllMachineConnectivity() {
  const machineIds = (configState.connectivity?.machines || []).map((machine) => machine.id).filter(Boolean);
  await Promise.all(machineIds.map((machineId) => checkMachineConnectivity(machineId)));
}

function setCollectionConfigNotice(message = "", type = "warning") {
  configElements.collectionConfigNotice.hidden = !message;
  configElements.collectionConfigNotice.className = `inline-config-notice ${type}`;
  configElements.collectionConfigNotice.textContent = message;
}

function collectionConfigCanEdit() {
  return !isConfigReadOnly() && configState.collectionConfig?.can_edit === true;
}

function collectionPathKey(machineId, index) {
  return `${machineId}\u0000${index}`;
}

function setCollectionConfigDirty(dirty = true) {
  if (isConfigReadOnly()) return;
  configState.collectionConfigDirty = dirty;
  configElements.collectionConfigDirtyBadge.hidden = !dirty;
  configElements.collectionConfigSaveState.textContent = dirty
    ? "搜索位置已修改，尚未保存"
    : "搜索位置已保存";
  setCollectionConfigNotice(dirty ? "保存后，下次下载更新才会使用新搜索位置。" : "");
  updateConfigActions();
}

function normalizeCollectionMachine(machine) {
  const paths = Array.isArray(machine?.paths) ? machine.paths : [];
  return {
    id: String(machine?.id || ""),
    name: String(machine?.name || machine?.hostname || machine?.id || "实验机"),
    hostname: String(machine?.hostname || ""),
    ip: String(machine?.ip || ""),
    path_count: Number(machine?.path_count ?? paths.length) || 0,
    paths: paths.map((entry, index) => ({
      label: String(entry?.label || `位置 ${index + 1}`),
      path: String(entry?.path || ""),
      enabled: entry?.enabled !== false,
    })),
  };
}

function updateCollectionConfigMeta() {
  const payload = configState.collectionConfig;
  configElements.collectionConfigRevision.textContent = payload?.revision == null
    ? "—"
    : `r${configFormatInteger(payload.revision)}`;
  configElements.collectionConfigUpdatedAt.textContent = configFormatTime(payload?.updated_utc);
}

function renderCollectionConfig() {
  const machines = configState.collectionConfigDraft;
  const canEdit = collectionConfigCanEdit()
    && !configJobBusy()
    && !configState.collectionConfigSaving
    && configState.checkingPaths.size === 0;
  const fragment = document.createDocumentFragment();
  for (const machine of machines) {
    const card = configElement("article", "collection-machine-card");
    card.dataset.machineId = machine.id;
    const heading = configElement("div", "collection-machine-heading");
    const identity = configElement("div", "machine-identity");
    identity.append(
      configElement("strong", "", machine.name),
      configElement("span", "", [machine.hostname, machine.ip].filter(Boolean).join(" · ")),
    );
    const count = machine.paths.length || machine.path_count;
    heading.append(identity, configElement("span", "config-meta-chip", `${configFormatInteger(count)} 个搜索位置`));
    card.append(heading);

    const list = configElement("div", "collection-path-list");
    if (!machine.paths.length && isConfigReadOnly() && machine.path_count) {
      list.append(configElement("p", "collection-path-private-note", `已配置 ${configFormatInteger(machine.path_count)} 个搜索位置；局域网只读入口不显示远程目录。`));
    }
    machine.paths.forEach((entry, index) => {
      const key = collectionPathKey(machine.id, index);
      const row = configElement("div", "collection-path-row");
      const enabledLabel = configElement("label", "collection-path-enabled");
      const enabled = document.createElement("input");
      enabled.type = "checkbox";
      enabled.checked = entry.enabled;
      enabled.disabled = !canEdit;
      enabled.addEventListener("change", () => {
        entry.enabled = enabled.checked;
        configState.pathCheckResults.delete(key);
        setCollectionConfigDirty(true);
      });
      enabledLabel.append(enabled, configElement("span", "", "启用"));
      const pathField = configElement("label", "collection-path-field");
      pathField.append(configElement("span", "collection-path-label", entry.label));
      const input = document.createElement("input");
      input.type = "text";
      input.value = entry.path;
      input.maxLength = 500;
      input.readOnly = !canEdit;
      input.spellcheck = false;
      input.placeholder = "例如 D:\\\\data\\\\XY";
      input.addEventListener("input", () => {
        entry.path = input.value;
        configState.pathCheckResults.delete(key);
        check.dataset.hasPath = String(Boolean(entry.path.trim()));
        setCollectionConfigDirty(true);
        row.querySelector(".collection-path-result")?.remove();
      });
      pathField.append(input);
      const result = configState.pathCheckResults.get(key);
      const check = configElement("button", "secondary-outline-button collection-path-check", configState.checkingPaths.has(key) ? "检查中…" : "检查路径");
      check.type = "button";
      check.dataset.pathKey = key;
      check.dataset.hasPath = String(Boolean(entry.path.trim()));
      check.disabled = !canEdit || !entry.path.trim() || configState.checkingPaths.has(key);
      check.addEventListener("click", () => checkCollectionPath(machine.id, index));
      const remove = configElement("button", "collection-path-remove", "删除");
      remove.type = "button";
      remove.disabled = !canEdit;
      remove.setAttribute("aria-label", `删除 ${machine.name} 的 ${entry.label}`);
      remove.addEventListener("click", () => {
        machine.paths.splice(index, 1);
        configState.pathCheckResults.clear();
        setCollectionConfigDirty(true);
        renderCollectionConfig();
      });
      row.append(enabledLabel, pathField, check, remove);
      if (result) {
        const resultNode = configElement("p", `collection-path-result ${result.status || "unchecked"}`, result.message || "检查完成");
        resultNode.title = resultNode.textContent;
        row.append(resultNode);
      }
      list.append(row);
    });
    card.append(list);
    if (canEdit) {
      const add = configElement("button", "secondary-outline-button collection-path-add local-write-only", "+ 添加搜索位置");
      add.type = "button";
      add.addEventListener("click", () => {
        const labels = new Set(machine.paths.map((entry) => entry.label));
        let suffix = 1;
        while (labels.has(`自定义位置 ${suffix}`)) suffix += 1;
        machine.paths.push({ label: `自定义位置 ${suffix}`, path: "", enabled: true });
        setCollectionConfigDirty(true);
        renderCollectionConfig();
      });
      card.append(add);
    }
    fragment.append(card);
  }
  configElements.collectionMachineList.replaceChildren(
    fragment.childNodes.length ? fragment : configElement("div", "start-stop-loading", "未找到可配置的实验机"),
  );
  configElements.collectionMachineList.setAttribute("aria-busy", "false");
  updateCollectionConfigMeta();
  updateConfigActions();
}

function validateCollectionConfig() {
  for (const machine of configState.collectionConfigDraft) {
    if (!machine.paths.length) return `${machine.name}至少需要一个搜索位置。`;
    const paths = machine.paths.map((entry) => entry.path.trim());
    if (paths.some((path) => !path)) return `${machine.name}存在空的搜索位置，请填写或删除。`;
    if (!machine.paths.some((entry) => entry.enabled)) return `${machine.name}至少需要启用一个搜索位置。`;
    const normalized = paths.map((path) => path.replaceAll("/", "\\").toLowerCase());
    if (new Set(normalized).size !== normalized.length) return `${machine.name}存在重复的搜索位置。`;
  }
  return "";
}

async function loadCollectionConfig({ discardDraft = false } = {}) {
  if (configState.collectionConfigDirty && !discardDraft) return;
  configElements.collectionMachineList.setAttribute("aria-busy", "true");
  try {
    const payload = await configRequest("/api/start-stop/collection-config");
    configState.collectionConfig = payload;
    configState.collectionConfigDraft = (payload.machines || []).map(normalizeCollectionMachine);
    configState.collectionConfigDirty = false;
    configElements.collectionConfigDirtyBadge.hidden = true;
    configElements.collectionConfigSaveState.textContent = "搜索位置已保存";
    configState.pathCheckResults.clear();
    setCollectionConfigNotice(isConfigReadOnly() ? "局域网只读入口只显示搜索位置数量，不公开远程目录。" : "");
    renderCollectionConfig();
  } catch (error) {
    setCollectionConfigNotice(`无法读取搜索位置：${error.message}`, "error");
    configElements.collectionMachineList.replaceChildren(configElement("div", "start-stop-loading", "搜索位置暂不可用"));
    configElements.collectionMachineList.setAttribute("aria-busy", "false");
  }
}

async function saveCollectionConfiguration() {
  if (!collectionConfigCanEdit() || !configState.collectionConfigDirty) return;
  const validationError = validateCollectionConfig();
  if (validationError) {
    setCollectionConfigNotice(validationError, "error");
    return;
  }
  configState.collectionConfigSaving = true;
  configElements.collectionConfigSaveState.textContent = "正在保存搜索位置…";
  updateConfigActions();
  try {
    const payload = await configRequest("/api/start-stop/collection-config", {
      method: "PUT",
      body: JSON.stringify({
        expected_revision: configState.collectionConfig.revision,
        machines: configState.collectionConfigDraft.map((machine) => ({
          id: machine.id,
          paths: machine.paths.map((entry) => ({
            label: entry.label,
            path: entry.path.trim(),
            enabled: Boolean(entry.enabled),
          })),
        })),
      }),
    });
    configState.collectionConfig = payload;
    configState.collectionConfigDraft = (payload.machines || []).map(normalizeCollectionMachine);
    configState.collectionConfigDirty = false;
    configElements.collectionConfigDirtyBadge.hidden = true;
    configElements.collectionConfigSaveState.textContent = "搜索位置已保存";
    configState.pathCheckResults.clear();
    setCollectionConfigNotice("搜索位置已保存，下次下载更新时生效。", "success");
    renderCollectionConfig();
  } catch (error) {
    setCollectionConfigNotice(error.status === 409
      ? "采集配置已变更或当前有任务在运行。请等待任务完成后重新读取。"
      : error.message, "error");
    configElements.collectionConfigSaveState.textContent = "保存失败，已存搜索位置未改变";
  } finally {
    configState.collectionConfigSaving = false;
    updateConfigActions();
  }
}

async function checkCollectionPath(machineId, index) {
  const machine = configState.collectionConfigDraft.find((item) => item.id === machineId);
  const entry = machine?.paths?.[index];
  if (!entry || !entry.path.trim() || isConfigReadOnly() || configJobBusy()) return;
  const key = collectionPathKey(machineId, index);
  configState.checkingPaths.add(key);
  renderCollectionConfig();
  try {
    const result = await configRequest("/api/start-stop/connectivity/path-check", {
      method: "POST",
      body: JSON.stringify({ machine_id: machineId, path: entry.path.trim() }),
    });
    configState.pathCheckResults.set(key, result);
  } catch (error) {
    configState.pathCheckResults.set(key, { status: "error", reachable: false, message: error.message });
  } finally {
    configState.checkingPaths.delete(key);
    renderCollectionConfig();
  }
}

function updateConfigActions() {
  const capability = configCapabilities();
  const activeJob = configState.status?.job || {};
  const jobIsBusy = ["queued", "running"].includes(activeJob.status);
  const busy = configJobBusy()
    || configState.uploading
    || configState.collectionConfigSaving
    || configState.checkingPaths.size > 0;
  const draftConflict = configState.collectionConfigDirty;
  configElements.refreshData.disabled = busy || draftConflict || !capability.canUpdate;
  configElements.renderAtlas.disabled = busy || !capability.canRender;
  configElements.renderDataMode.disabled = busy || !capability.canRender;
  configElements.renderMaterialScope.disabled = busy || !capability.canRender;
  configElements.refreshData.textContent = jobIsBusy && activeJob.action === "scan"
    ? "正在下载并更新…"
    : "从三台实验电脑下载并更新";
  configElements.renderAtlas.textContent = jobIsBusy && activeJob.action === "render"
    ? "正在更新平台分析…"
    : "按选择更新平台分析";
  configElements.refreshData.setAttribute("aria-busy", String(jobIsBusy && activeJob.action === "scan"));
  configElements.renderAtlas.setAttribute("aria-busy", String(jobIsBusy && activeJob.action === "render"));
  configElements.openUpload.hidden = capability.readOnly || !capability.repositoryMode;
  configElements.openUpload.disabled = busy || !capability.canUpload;
  configElements.refreshData.title = draftConflict ? "请先保存或放弃搜索位置修改" : "";
  configElements.saveCollectionConfig.disabled = busy || !collectionConfigCanEdit() || !draftConflict;
  configElements.reloadCollectionConfig.disabled = busy || !draftConflict;
  document.querySelectorAll(".collection-path-row input, .collection-path-row button, .collection-path-add").forEach((control) => {
    const checkDisabled = control.classList.contains("collection-path-check")
      && (control.dataset.hasPath !== "true" || configState.checkingPaths.has(control.dataset.pathKey));
    control.disabled = busy || !collectionConfigCanEdit() || checkDisabled;
  });
  renderAutoUpdate();
}

function updateConfigStatusView() {
  const payload = configState.status;
  applyConfigDeploymentProfile(payload);
  renderSafetyStatus(payload?.safety);
  const readOnly = isConfigReadOnly();
  document.body.classList.toggle("lan-read-only", readOnly);
  if (payload?.connectivity && configState.checkingMachineIds.size === 0) mergeMachineConnectivity(payload.connectivity);
  else renderMachineConnectivity();
  if (!payload?.available) {
    configElements.sidebarConfigState.textContent = "配置工作区不可用";
    updateCapabilityView(payload);
    updateJobView(payload?.job);
    updateJobProgressView(payload?.job);
    setConfigNotice(payload?.message || "无法读取启停运行配置。", "error");
    updateConfigActions();
    return;
  }
  configElements.sidebarConfigState.textContent = configState.collectionConfigDirty
    ? "搜索位置待保存"
    : payload.analysis_ready || (!payload.data_stale && !payload.configuration_stale)
      ? "平台分析已就绪"
      : "平台分析待更新";
  updateCapabilityView(payload);
  updateJobView(payload.job || {});
  updateJobProgressView(payload.job || {});
  applyUploadCapabilityLimits();
  if (readOnly) {
    setConfigNotice("当前是局域网只读入口：可查看平台分析；PDF 图集请前往材料库下载。上传、更新与保存仅限服务器本机。");
  } else if (configState.collectionConfigDirty) {
    setConfigNotice("实验机搜索位置已修改但尚未保存；保存前不能启动远程下载。");
  } else if (payload.job?.status === "interrupted") {
    setConfigNotice(
      payload.job.message || "服务重启导致上一次任务中断，可使用页面上方按钮重新执行。",
      "error",
    );
  } else if (payload.job?.status === "failed") {
    setConfigNotice(`上一次任务失败：${payload.job.message || "请重试。"}`, "error");
  } else if (payload.job?.status === "completed_with_warnings") {
    setConfigNotice(payload.job.message || "任务已完成，但有部分文件需要在下次更新时重试。");
  } else if (payload.job?.status === "completed") {
    const messages = {
      scan: "实验电脑数据已下载，材料库已刷新。",
      prepare_upload: "上传数据已入库，材料库已刷新；新材料不会自动进入图集。",
      render: payload.job.message || "平台分析已更新；本次未强制生成 PDF。",
    };
    setConfigNotice(messages[payload.job.action] || "任务已完成。", "success");
  } else if (payload.data_stale) {
    setConfigNotice("发现尚未刷新的数据，请使用页面上方的数据更新功能。");
  } else {
    setConfigNotice();
  }
  if (configState.collectionConfig) renderCollectionConfig();
  updateConfigActions();
}

async function startSavedConfigJob(action) {
  if (action === "scan" && configState.collectionConfigDirty) {
    setConfigNotice("请先保存或放弃未保存的搜索位置，再启动远程下载。", "error");
    return;
  }
  const capability = configCapabilities();
  if ((action === "scan" && !capability.canUpdate) || (action === "render" && !capability.canRender)) {
    setConfigNotice("当前环境尚未准备好，无法启动该任务。", "error");
    return;
  }
  try {
    const requestBody = { action };
    if (action === "render") {
      requestBody.render_data_mode = configElements.renderDataMode.value;
      requestBody.render_material_scope = configElements.renderMaterialScope.value;
    }
    const job = await configRequest("/api/start-stop/jobs", {
      method: "POST",
      body: JSON.stringify(requestBody),
    });
    configState.status = { ...(configState.status || {}), job };
    updateConfigStatusView();
    configElements.jobProgressPanel.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "nearest",
    });
    scheduleConfigJobPoll();
  } catch (error) {
    setConfigNotice(error.message, "error");
    await loadConfigWorkspace();
  }
}

function defaultUploadGroupName() {
  const now = new Date();
  const part = (value) => String(value).padStart(2, "0");
  return `网页上传-${now.getFullYear()}${part(now.getMonth() + 1)}${part(now.getDate())}-${part(now.getHours())}${part(now.getMinutes())}`;
}

function setUploadResult(message = "", type = "success") {
  configElements.uploadResult.hidden = !message;
  configElements.uploadResult.className = `upload-result ${type}`;
  configElements.uploadResult.textContent = message;
}

function uploadStatusLabel(entry) {
  const labels = {
    pending: "待上传",
    uploading: "上传中",
    uploaded: "已入库",
    duplicate: "已存在",
    failed: "失败",
  };
  return labels[entry.status] || entry.status;
}

function renderUploadQueue() {
  const totalBytes = configState.uploadQueue.reduce((sum, entry) => sum + entry.file.size, 0);
  configElements.uploadQueueSummary.textContent = `${configFormatInteger(configState.uploadQueue.length)} 个文件 · ${configFormatBytes(totalBytes)}`;
  if (!configState.uploadQueue.length) {
    configElements.uploadQueue.replaceChildren(configElement("p", "", "尚未选择文件。"));
  } else {
    const fragment = document.createDocumentFragment();
    const visibleEntries = configState.uploadQueue.slice(0, 250);
    for (const entry of visibleEntries) {
      const row = configElement("div", `upload-queue-row status-${entry.status}`);
      const copy = configElement("div", "upload-queue-copy");
      const name = configElement("strong", "", entry.relativePath);
      name.title = entry.relativePath;
      copy.append(
        name,
        configElement("small", "", `${configFormatBytes(entry.file.size)}${entry.message ? ` · ${entry.message}` : ""}`),
      );
      row.append(copy, configElement("span", "upload-file-status", uploadStatusLabel(entry)));
      fragment.append(row);
    }
    if (configState.uploadQueue.length > visibleEntries.length) {
      fragment.append(configElement(
        "p",
        "upload-queue-overflow",
        `另有 ${configFormatInteger(configState.uploadQueue.length - visibleEntries.length)} 个文件未在列表中展开。`,
      ));
    }
    configElements.uploadQueue.replaceChildren(fragment);
  }
  const capability = configCapabilities();
  const hasRetryable = configState.uploadQueue.some((entry) => (
    !entry.prepared && !["uploaded", "duplicate"].includes(entry.status)
  ));
  configElements.startUpload.disabled = configState.uploading
    || !configState.uploadQueue.length
    || !hasRetryable
    || !configElements.uploadGroupName.value.trim()
    || !capability.canUpload
    || !capability.canPrepareUpload
    || capability.busy;
  configElements.clearUploadQueue.disabled = configState.uploading || !configState.uploadQueue.length;
}

function addUploadCandidates(candidates) {
  const limits = uploadLimits();
  const known = new Set(configState.uploadQueue.map((entry) => entry.key));
  const ignored = [];
  for (const candidate of candidates) {
    const file = candidate.file || candidate;
    const relativePath = String(candidate.relativePath || file.webkitRelativePath || file.name)
      .replaceAll("\\", "/")
      .replace(/^\/+/, "");
    if (!file?.name || !relativePath || file.name === ".DS_Store") continue;
    const extensionIndex = file.name.lastIndexOf(".");
    const extension = extensionIndex >= 0 ? file.name.slice(extensionIndex).toLowerCase() : "";
    if (!limits.extensions.includes(extension)) {
      ignored.push(`${relativePath}：不支持 ${extension || "无扩展名"}`);
      continue;
    }
    if (file.size > limits.maxFileBytes) {
      ignored.push(`${relativePath}：超过 ${configFormatBytes(limits.maxFileBytes)}`);
      continue;
    }
    const key = `${relativePath}\u0000${file.size}\u0000${file.lastModified}`;
    if (known.has(key)) continue;
    if (configState.uploadQueue.length >= limits.maxFiles) {
      ignored.push(`已达到单批 ${configFormatInteger(limits.maxFiles)} 个文件的上限`);
      break;
    }
    known.add(key);
    configState.uploadQueue.push({
      key,
      file,
      relativePath,
      status: "pending",
      message: "",
      uploadId: "",
      prepared: false,
    });
  }
  configState.uploadIgnored.push(...ignored);
  if (ignored.length) {
    const sample = ignored.slice(0, 2).join("；");
    setUploadResult(`已忽略 ${configFormatInteger(ignored.length)} 个不可上传的文件：${sample}${ignored.length > 2 ? "…" : ""}`, "warning");
  } else {
    setUploadResult();
  }
  renderUploadQueue();
}

function readFileEntry(entry, relativePath) {
  return new Promise((resolve, reject) => {
    entry.file(
      (file) => resolve({ file, relativePath: relativePath || file.name }),
      reject,
    );
  });
}

function readDirectoryEntries(entry) {
  const reader = entry.createReader();
  const output = [];
  return new Promise((resolve, reject) => {
    const readNext = () => {
      reader.readEntries((entries) => {
        if (!entries.length) {
          resolve(output);
          return;
        }
        output.push(...entries);
        readNext();
      }, reject);
    };
    readNext();
  });
}

async function flattenDroppedEntry(entry, prefix = "") {
  const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) return [await readFileEntry(entry, relativePath)];
  if (!entry.isDirectory) return [];
  const children = await readDirectoryEntries(entry);
  const nested = await Promise.all(children.map((child) => flattenDroppedEntry(child, relativePath)));
  return nested.flat();
}

async function candidatesFromDrop(dataTransfer) {
  const items = [...(dataTransfer.items || [])];
  const entries = items.map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) {
    return [...(dataTransfer.files || [])].map((file) => ({
      file,
      relativePath: file.webkitRelativePath || file.name,
    }));
  }
  const nested = await Promise.all(entries.map((entry) => flattenDroppedEntry(entry)));
  return nested.flat();
}

function openUploadDialog() {
  const capability = configCapabilities();
  if (capability.readOnly || !capability.canUpload || configState.uploading) return;
  if (!configElements.uploadGroupName.value) configElements.uploadGroupName.value = defaultUploadGroupName();
  renderUploadQueue();
  configElements.uploadDialog.showModal();
  configElements.uploadGroupName.focus();
}

function closeUploadDialog() {
  if (configState.uploading) return;
  configElements.uploadDialog.close();
}

function updateUploadProgress(processedBytes, currentBytes, totalBytes, index, count) {
  const loaded = Math.min(totalBytes, processedBytes + currentBytes);
  const percentage = totalBytes ? Math.round((100 * loaded) / totalBytes) : 0;
  configElements.uploadProgress.value = percentage;
  configElements.uploadProgress.textContent = `${percentage}%`;
  configElements.uploadProgressValue.textContent = `${percentage}%`;
  configElements.uploadProgressLabel.textContent = `正在上传第 ${configFormatInteger(index)} / ${configFormatInteger(count)} 个文件`;
}

async function startUploadBatch() {
  const group = configElements.uploadGroupName.value.trim();
  const capability = configCapabilities();
  if (capability.readOnly || !capability.canUpload || !capability.canPrepareUpload) {
    setUploadResult("当前入口不允许上传数据。", "error");
    return;
  }
  if (!group) {
    setUploadResult("请先填写材料 / 分组文件夹名。", "error");
    configElements.uploadGroupName.focus();
    return;
  }
  const retryable = configState.uploadQueue.filter((entry) => (
    !entry.prepared && !["uploaded", "duplicate"].includes(entry.status)
  ));
  if (!retryable.length) return;
  configState.uploading = true;
  configElements.uploadDialog.setAttribute("aria-busy", "true");
  configElements.uploadProgressArea.hidden = false;
  setUploadResult();
  renderUploadQueue();
  updateConfigActions();
  const totalBytes = retryable.reduce((sum, entry) => sum + entry.file.size, 0);
  let processedBytes = 0;
  let failed = 0;
  for (const [index, entry] of retryable.entries()) {
    entry.status = "uploading";
    entry.message = "";
    renderUploadQueue();
    const query = new URLSearchParams({
      filename: entry.file.name,
      relative_path: entry.relativePath,
      group,
      last_modified: String(entry.file.lastModified || 0),
      size: String(entry.file.size),
    });
    try {
      const payload = await uploadRequest(
        `/api/start-stop/uploads?${query}`,
        entry.file,
        (loaded) => updateUploadProgress(processedBytes, loaded, totalBytes, index + 1, retryable.length),
      );
      const uploadId = payload.upload_id || payload.id || payload.file?.upload_id;
      const numericUploadId = Number(uploadId);
      if (!Number.isInteger(numericUploadId) || numericUploadId <= 0) {
        throw new Error("服务器未返回有效的 upload_id。");
      }
      entry.uploadId = numericUploadId;
      entry.status = payload.changed === false ? "duplicate" : "uploaded";
      entry.message = payload.changed === false ? "数据库中已有相同内容" : "原始字节已入库";
    } catch (error) {
      entry.status = "failed";
      entry.message = error.message;
      failed += 1;
    }
    processedBytes += entry.file.size;
    updateUploadProgress(processedBytes, 0, totalBytes, index + 1, retryable.length);
    renderUploadQueue();
  }
  if (failed) {
    setUploadResult(`有 ${configFormatInteger(failed)} 个文件上传失败。已成功的文件会保留；点击重试后再统一更新材料表。`, "error");
  } else {
    const uploadIds = [...new Set(configState.uploadQueue
      .filter((entry) => !entry.prepared)
      .map((entry) => entry.uploadId)
      .filter(Boolean))];
    try {
      configElements.uploadProgressLabel.textContent = "文件已入库，正在准备启停分析…";
      const job = await configRequest("/api/start-stop/jobs", {
        method: "POST",
        body: JSON.stringify({ action: "prepare_upload", upload_ids: uploadIds }),
      });
      configState.status = { ...(configState.status || {}), job };
      setUploadResult(`已接收 ${configFormatInteger(uploadIds.length)} 个文件，正在更新材料表。`, "processing");
      updateConfigStatusView();
      scheduleConfigJobPoll();
    } catch (error) {
      setUploadResult(`文件已入库，但启动材料表更新失败：${error.message}`, "error");
      for (const entry of configState.uploadQueue.filter((item) => !item.prepared)) {
        entry.status = "failed";
        entry.message = "准备分析失败；重试时会自动查重";
      }
    }
  }
  configState.uploading = false;
  configElements.uploadDialog.removeAttribute("aria-busy");
  renderUploadQueue();
  updateConfigActions();
}

function scheduleConfigJobPoll() {
  window.clearTimeout(configState.jobPollTimer);
  configState.jobPollTimer = window.setTimeout(async () => {
    try {
      configState.status = await configRequest("/api/start-stop/status");
      updateConfigStatusView();
      const jobStatus = configState.status?.job?.status;
      if (["queued", "running"].includes(jobStatus)) {
        scheduleConfigJobPoll();
        return;
      }
      const uploadJob = configState.status?.job?.action === "prepare_upload";
      await loadConfigWorkspace();
      if (uploadJob && ["completed", "completed_with_warnings"].includes(jobStatus)) {
        for (const entry of configState.uploadQueue.filter((item) => !item.prepared)) entry.prepared = true;
        setUploadResult(
          jobStatus === "completed"
            ? "上传数据已入库，材料表已更新。新材料不会自动进入总结图集。"
            : "材料表已更新，但部分文件有警告；请查看页面顶部提示。",
          jobStatus === "completed" ? "success" : "warning",
        );
        configElements.uploadProgressLabel.textContent = "上传与材料表更新已完成";
        renderUploadQueue();
      } else if (uploadJob && jobStatus === "failed") {
        const duplicatePlotName = configState.status?.job?.failure_code === "duplicate_material_name";
        for (const entry of configState.uploadQueue.filter((item) => !item.prepared)) {
          entry.status = duplicatePlotName ? "uploaded" : "failed";
          entry.message = duplicatePlotName
            ? "文件已入库；请先在材料库处理重名后重试更新"
            : "准备分析失败；重试时会自动查重";
        }
        setUploadResult(`文件已入库，但材料表更新失败：${configState.status?.job?.message || "请重试"}`, "error");
        renderUploadQueue();
      }
    } catch (error) {
      setConfigNotice(error.message, "error");
      scheduleConfigJobPoll();
    }
  }, 1800);
}

async function loadConfigWorkspace() {
  try {
    const status = await configRequest("/api/start-stop/status");
    configState.status = status;
    updateConfigStatusView();
    if (configJobBusy()) scheduleConfigJobPoll();
  } catch (error) {
    renderSafetyStatus();
    setConfigNotice(error.message, "error");
  }
}

function wireConfigEvents() {
  configElements.toggleAutoUpdate.addEventListener("click", () => {
    saveAutoUpdate(configState.autoUpdate?.enabled !== true);
  });
  configElements.autoUpdateInterval.addEventListener("change", () => {
    if (configState.autoUpdate?.enabled === true) {
      saveAutoUpdate(true);
    } else {
      configState.autoUpdate = {
        ...(configState.autoUpdate || { available: true, revision: 0, enabled: false }),
        interval_minutes: Number(configElements.autoUpdateInterval.value),
      };
      renderAutoUpdate();
    }
  });
  configElements.toggleLivePreview.addEventListener("click", () => {
    saveLivePreview(configState.livePreview?.enabled !== true);
  });
  configElements.plotLivePreview.addEventListener("click", () => {
    const itemCount = (configState.livePreview?.preview?.items || []).filter((item) => (
      item?.analysis?.work_step_key
      && (
        Array.isArray(item?.analysis?.cycle_points) && item.analysis.cycle_points.length
        || Array.isArray(item?.analysis?.overview_points) && item.analysis.overview_points.length
      )
    )).length;
    if (!itemCount) {
      setConfigNotice("尚未按正式启停规则计算出可绘图快照，请等待首次识别完成。", "warning");
      return;
    }
    window.location.assign("/start-stop/analysis?view=live");
  });
  configElements.reloadCollectionConfig.addEventListener("click", () => loadCollectionConfig({ discardDraft: true }));
  configElements.saveCollectionConfig.addEventListener("click", saveCollectionConfiguration);
  configElements.refreshData.addEventListener("click", () => startSavedConfigJob("scan"));
  configElements.renderAtlas.addEventListener("click", () => startSavedConfigJob("render"));
  configElements.openUpload.addEventListener("click", openUploadDialog);
  configElements.checkAllMachines.addEventListener("click", checkAllMachineConnectivity);
  configElements.machineConnectivityList.addEventListener("click", (event) => {
    const button = event.target.closest(".machine-check-button");
    if (!button) return;
    checkMachineConnectivity(button.dataset.machineId);
  });
  configElements.closeUpload.addEventListener("click", closeUploadDialog);
  configElements.cancelUpload.addEventListener("click", closeUploadDialog);
  configElements.chooseUploadFiles.addEventListener("click", () => configElements.uploadFiles.click());
  configElements.chooseUploadFolder.addEventListener("click", () => configElements.uploadFolder.click());
  configElements.uploadFiles.addEventListener("change", () => {
    addUploadCandidates([...configElements.uploadFiles.files].map((file) => ({
      file,
      relativePath: file.name,
    })));
    configElements.uploadFiles.value = "";
  });
  configElements.uploadFolder.addEventListener("change", () => {
    addUploadCandidates([...configElements.uploadFolder.files].map((file) => ({
      file,
      relativePath: file.webkitRelativePath || file.name,
    })));
    configElements.uploadFolder.value = "";
  });
  configElements.uploadGroupName.addEventListener("input", renderUploadQueue);
  configElements.clearUploadQueue.addEventListener("click", () => {
    if (configState.uploading) return;
    configState.uploadQueue = [];
    configState.uploadIgnored = [];
    configElements.uploadProgressArea.hidden = true;
    setUploadResult();
    renderUploadQueue();
  });
  configElements.startUpload.addEventListener("click", startUploadBatch);
  configElements.uploadDropZone.addEventListener("dragenter", (event) => {
    event.preventDefault();
    configElements.uploadDropZone.classList.add("is-dragging");
  });
  configElements.uploadDropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    configElements.uploadDropZone.classList.add("is-dragging");
  });
  configElements.uploadDropZone.addEventListener("dragleave", (event) => {
    if (!configElements.uploadDropZone.contains(event.relatedTarget)) {
      configElements.uploadDropZone.classList.remove("is-dragging");
    }
  });
  configElements.uploadDropZone.addEventListener("drop", async (event) => {
    event.preventDefault();
    configElements.uploadDropZone.classList.remove("is-dragging");
    try {
      addUploadCandidates(await candidatesFromDrop(event.dataTransfer));
    } catch (error) {
      setUploadResult(`无法读取拖入的文件：${error.message}`, "error");
    }
  });
  configElements.uploadDialog.addEventListener("cancel", (event) => {
    if (configState.uploading) event.preventDefault();
  });
  window.addEventListener("beforeunload", (event) => {
    if (!configState.collectionConfigDirty && !configState.uploading) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function initializeConfigPage() {
  wireConfigEvents();
  await loadConfigWorkspace();

  if (!configState.status) {
    await loadMachineConnectivity();
    return;
  }

  // The status response already contains the cached three-machine snapshot.
  // Wait for it before loading local-only settings so the LAN entry does not
  // download a full live curve payload or make requests that must return 403.
  if (isConfigReadOnly()) {
    configState.autoUpdate = { available: false, enabled: false };
    configState.livePreview = { available: false, enabled: false };
    renderAutoUpdate();
    renderLivePreview();
    setCollectionConfigNotice("局域网只读入口不读取实验机远程目录配置。");
    configElements.collectionMachineList.replaceChildren(
      configElement("div", "start-stop-loading", "搜索位置仅可在服务器本机查看与修改"),
    );
    configElements.collectionMachineList.setAttribute("aria-busy", "false");
    return;
  }

  await Promise.all([
    loadCollectionConfig(),
    loadAutoUpdate(),
    loadLivePreview(),
  ]);
}

initializeConfigPage();
