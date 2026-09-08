"use strict";

const lanbtsState = {
  payload: null,
  edits: new Map(),
  loading: false,
  saving: false,
  refreshTimer: null,
};

const lanbtsElements = Object.fromEntries(
  [
    "workbenchVersion",
    "lanbtsSidebarState",
    "refreshLanbts",
    "saveLanbtsConfiguration",
    "lanbtsNotice",
    "lanbtsModeNote",
    "lanbtsDischargingMetric",
    "lanbtsChargingMetric",
    "lanbtsCompletedMetric",
    "lanbtsIdleMetric",
    "lanbtsAttentionMetric",
    "lanbtsOverallState",
    "lanbtsUpdatedMetric",
    "lanbtsDeviceName",
    "lanbtsDeviceMeta",
    "lanbtsChannelGrid",
  ].map((id) => [id, document.querySelector(`#${id}`)]),
);

function lanbtsElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

async function lanbtsRequest(url, options = {}) {
  return StartStopClient.request(url, options);
}

function showLanbtsNotice(message = "", type = "info") {
  lanbtsElements.lanbtsNotice.hidden = !message;
  lanbtsElements.lanbtsNotice.className = `workspace-notice ${type}`;
  lanbtsElements.lanbtsNotice.textContent = message;
}

function formatInteger(value) {
  if (value === null || value === undefined || value === "") return "—";
  return Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN") : "—";
}

function formatNumber(value, digits = 3) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return parsed.toFixed(digits).replace(/\.?0+$/, "");
}

function formatSigned(value, digits = 3) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  const text = formatNumber(Math.abs(parsed), digits);
  if (parsed < 0) return `−${text}`;
  if (parsed > 0) return `+${text}`;
  return text;
}

function formatElapsed(value) {
  if (value === null || value === undefined || value === "") return "—";
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = Math.floor(seconds % 60);
  if (days) return `${days}天 ${hours}时`;
  if (hours) return `${hours}时 ${minutes}分`;
  if (minutes) return `${minutes}分 ${remaining}秒`;
  return `${remaining}秒`;
}

function formatTimestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value).replace("T", " ").slice(0, 19);
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function channelDisplayStatus(channel) {
  const explicit = String(channel?.status || "");
  if (["idle", "charging", "discharging", "completed", "attention"].includes(explicit)) return explicit;
  const state = String(channel?.state || "").toLowerCase();
  if (["finish", "complete", "stopped"].some((token) => state.includes(token)) || state === "stop") return "completed";
  if (state.includes("discharge")) return "discharging";
  if (state.includes("charge")) return "charging";
  return channel?.run_id ? "attention" : "idle";
}

function channelDisplayLabel(status) {
  return {
    idle: "空置",
    discharging: "放电",
    charging: "充电",
    completed: "测试完成",
    attention: "读取异常",
  }[status] || "状态未知";
}

function currentEdit(channel) {
  const edit = lanbtsState.edits.get(Number(channel.channel));
  if (edit && edit.run_id === channel.run_id) return edit;
  if (edit) lanbtsState.edits.delete(Number(channel.channel));
  return {
    run_id: channel.run_id || "",
    material_name: channel.configuration?.material_name || "",
    electrode_area_cm2: channel.configuration?.electrode_area_cm2 ?? "",
    notes: channel.configuration?.notes || "",
  };
}

function recordEdit(channel, field, value) {
  const existing = currentEdit(channel);
  lanbtsState.edits.set(Number(channel.channel), {
    ...existing,
    run_id: channel.run_id || "",
    [field]: value,
  });
  updateLanbtsActions();
}

function addCoreMetric(container, label, value, note = "") {
  const item = lanbtsElement("div", "lanbts-core-metric");
  item.append(lanbtsElement("span", "", label));
  item.append(lanbtsElement("strong", "", value));
  if (note) item.append(lanbtsElement("small", "", note));
  container.append(item);
}

function addCompactMetric(container, label, value) {
  const item = lanbtsElement("div", "lanbts-compact-metric");
  item.append(lanbtsElement("span", "", label));
  item.append(lanbtsElement("strong", "", value));
  container.append(item);
}

function renderLanbtsChannel(channel) {
  const displayStatus = channelDisplayStatus(channel);
  const edit = currentEdit(channel);
  const area = Number(edit.electrode_area_cm2);
  const areaReady = Number.isFinite(area) && area > 0;
  const current = channel.current_ma === null || channel.current_ma === undefined
    ? Number.NaN
    : Number(channel.current_ma);
  const density = areaReady && Number.isFinite(current) ? current / area : null;
  const materialName = String(edit.material_name || "").trim();
  const configurationComplete = Boolean(materialName && areaReady);
  const cachedReading = displayStatus === "attention" || ["stale", "unavailable"].includes(lanbtsState.payload?.status);
  const card = lanbtsElement("article", `lanbts-channel-card ${displayStatus}`);
  card.dataset.channel = String(channel.channel);

  const heading = lanbtsElement("div", "lanbts-channel-heading");
  const headingMain = lanbtsElement("div", "lanbts-channel-heading-main");
  headingMain.append(lanbtsElement("span", "lanbts-channel-number", String(channel.channel).padStart(2, "0")));
  headingMain.append(lanbtsElement("span", "lanbts-channel-label", `通道 ${channel.channel}`));
  heading.append(headingMain);
  heading.append(lanbtsElement("span", `lanbts-channel-state ${displayStatus}`, channelDisplayLabel(displayStatus)));
  card.append(heading);

  if (!channel.run_id) {
    const empty = lanbtsElement("div", "lanbts-empty-state");
    const attention = displayStatus === "attention";
    empty.append(lanbtsElement("strong", "", attention ? "状态读取失败" : "等待新测试"));
    empty.append(lanbtsElement("span", "", attention ? "等待下一次只读检查" : "写入 BTS 文件后自动显示"));
    card.append(empty);
    return card;
  }

  const titleText = materialName || channel.display_name || `通道 ${channel.channel}`;
  const title = lanbtsElement("h2", "lanbts-material-name", titleText);
  title.title = titleText;
  card.append(title);

  const chips = lanbtsElement("div", "lanbts-chip-row");
  chips.append(lanbtsElement("span", `lanbts-protocol-chip ${channel.protocol?.kind || "unknown"}`, channel.protocol?.category || "方案待识别"));
  let configurationLabel = "材料与面积待填";
  if (configurationComplete) configurationLabel = "信息完整";
  else if (materialName) configurationLabel = "面积待填";
  else if (areaReady) configurationLabel = "材料待填";
  chips.append(lanbtsElement("span", `lanbts-config-chip ${configurationComplete ? "complete" : "missing"}`, configurationLabel));
  card.append(chips);

  card.append(lanbtsElement("p", "lanbts-protocol-label", channel.protocol?.label || "当前无测试方案"));
  const dataTime = channel.data_timestamp_local || channel.data_last_write_utc;
  card.append(lanbtsElement("p", `lanbts-reading-age${cachedReading ? " cached" : ""}`,
    `${cachedReading ? "缓存数值" : displayStatus === "completed" ? "结束时数据" : "数据时间"} · ${dataTime ? formatTimestamp(dataTime) : "时间未提供"}`));

  const coreMetrics = lanbtsElement("div", "lanbts-core-metrics");
  addCoreMetric(
    coreMetrics,
    cachedReading ? "缓存电流" : displayStatus === "completed" ? "结束时电流" : "当前电流",
    Number.isFinite(current) ? `${formatSigned(current, 2)} mA` : "—",
    areaReady && Number.isFinite(density)
      ? `${formatSigned(density, 2)} mA·cm⁻² · ${formatNumber(area, 3)} cm²`
      : "未设置电极面积",
  );
  addCoreMetric(
    coreMetrics,
    cachedReading ? "缓存电压" : displayStatus === "completed" ? "结束时电压" : "当前电压",
    channel.voltage_v !== null && channel.voltage_v !== undefined && Number.isFinite(Number(channel.voltage_v))
      ? `${formatSigned(channel.voltage_v, 4)} V`
      : "—",
    "参照待确认",
  );
  card.append(coreMetrics);

  const compactMetrics = lanbtsElement("div", "lanbts-compact-metrics");
  addCompactMetric(compactMetrics, "循环", `${formatInteger(channel.loop_count)} 圈`);
  addCompactMetric(compactMetrics, "工步", `${formatInteger(channel.step_no)} · ${channel.state_label || "未知"}`);
  addCompactMetric(compactMetrics, "已运行", formatElapsed(channel.elapsed_s));
  card.append(compactMetrics);
  const previewButton = lanbtsElement("button", "secondary-outline-button lanbts-live-open", "查看本次测试数据");
  previewButton.type = "button";
  previewButton.addEventListener("click", () => globalThis.openLanbtsLive(channel));
  card.append(previewButton);

  const details = lanbtsElement("details", "lanbts-card-details");
  details.append(lanbtsElement("summary", "", lanbtsState.payload?.can_edit ? "文件详情与材料配置" : "查看文件详情"));
  const detailsBody = lanbtsElement("div", "lanbts-card-details-body");
  const meta = lanbtsElement("div", "lanbts-run-meta");
  meta.append(lanbtsElement("p", "", `文件：${channel.data_file || "—"}`));
  meta.append(lanbtsElement("p", "", `开始：${formatTimestamp(channel.test_start_local)}`));
  meta.append(lanbtsElement("p", "", `本工步：${formatElapsed(channel.step_elapsed_s)}`));
  detailsBody.append(meta);

  const routineWarnings = ["电压参照尚未确认", "未填写电极面积", "同向负载循环不能与电流符号反转启停混合比较"];
  const warnings = (Array.isArray(channel.warnings) ? channel.warnings : [])
    .filter((warning) => !routineWarnings.some((text) => String(warning).includes(text)))
    .slice(0, 3);
  if (warnings.length) {
    const list = lanbtsElement("ul", "lanbts-warning-list");
    warnings.forEach((warning) => list.append(lanbtsElement("li", "", warning)));
    detailsBody.append(list);
  }

  if (lanbtsState.payload?.can_edit) {
    const form = lanbtsElement("div", "lanbts-config-grid");

    const materialField = lanbtsElement("label", "lanbts-field");
    materialField.append(lanbtsElement("span", "", "材料完整名称"));
    const materialInput = document.createElement("input");
    materialInput.type = "text";
    materialInput.maxLength = 120;
    materialInput.value = edit.material_name;
    materialInput.placeholder = channel.material_hint || "例如：NiMoP-85-恒流";
    materialInput.addEventListener("input", (event) => recordEdit(channel, "material_name", event.target.value));
    materialField.append(materialInput);
    form.append(materialField);

    const areaField = lanbtsElement("label", "lanbts-field");
    areaField.append(lanbtsElement("span", "", "本次电极有效面积 / cm²"));
    const areaInput = document.createElement("input");
    areaInput.type = "number";
    areaInput.min = "0.000001";
    areaInput.max = "10000";
    areaInput.step = "any";
    areaInput.inputMode = "decimal";
    areaInput.value = edit.electrode_area_cm2;
    areaInput.placeholder = "允许每个通道不同";
    areaInput.addEventListener("input", (event) => recordEdit(channel, "electrode_area_cm2", event.target.value));
    areaField.append(areaInput);
    form.append(areaField);

    const notesField = lanbtsElement("label", "lanbts-field");
    notesField.append(lanbtsElement("span", "", "备注"));
    const notesInput = document.createElement("textarea");
    notesInput.maxLength = 300;
    notesInput.value = edit.notes;
    notesInput.placeholder = "可记录参比电极、接线、批次等";
    notesInput.addEventListener("input", (event) => recordEdit(channel, "notes", event.target.value));
    notesField.append(notesInput);
    form.append(notesField);
    detailsBody.append(form);
    detailsBody.append(lanbtsElement("p", "lanbts-config-help", "配置只绑定当前 BTS 文件；开始新文件后需要重新确认。"));
  }

  details.append(detailsBody);
  card.append(details);
  return card;
}

function renderLanbtsChannels() {
  const channels = Array.isArray(lanbtsState.payload?.channels) ? lanbtsState.payload.channels : [];
  const fragment = document.createDocumentFragment();
  channels.forEach((channel) => fragment.append(renderLanbtsChannel(channel)));
  if (!channels.length) fragment.append(lanbtsElement("div", "start-stop-loading", "暂无八通道状态。"));
  lanbtsElements.lanbtsChannelGrid.replaceChildren(fragment);
  lanbtsElements.lanbtsChannelGrid.setAttribute("aria-busy", "false");
}

function updateLanbtsActions() {
  const payload = lanbtsState.payload;
  const dirty = lanbtsState.edits.size > 0;
  lanbtsElements.refreshLanbts.disabled = lanbtsState.loading || lanbtsState.saving;
  lanbtsElements.saveLanbtsConfiguration.hidden = !payload?.can_edit;
  lanbtsElements.saveLanbtsConfiguration.disabled = !payload?.can_edit || !dirty || lanbtsState.loading || lanbtsState.saving;
  lanbtsElements.saveLanbtsConfiguration.textContent = lanbtsState.saving ? "正在保存…" : "保存配置";
}

function renderLanbtsSummary() {
  const payload = lanbtsState.payload || {};
  const counts = payload.counts || {};
  const device = payload.device || {};
  const phases = { discharging: 0, charging: 0, completed: 0, idle: 0, attention: 0 };
  (Array.isArray(payload.channels) ? payload.channels : []).forEach((channel) => {
    const status = channelDisplayStatus(channel);
    if (Object.hasOwn(phases, status)) phases[status] += 1;
    else phases.attention += 1;
  });
  const phaseCount = (key) => Number.isFinite(Number(counts[`channels_${key}`]))
    ? Number(counts[`channels_${key}`])
    : phases[key];
  const discharging = phaseCount("discharging");
  const charging = phaseCount("charging");
  const completed = phaseCount("completed");
  const idle = phaseCount("idle");
  lanbtsElements.lanbtsDischargingMetric.textContent = formatInteger(discharging);
  lanbtsElements.lanbtsChargingMetric.textContent = formatInteger(charging);
  lanbtsElements.lanbtsCompletedMetric.textContent = formatInteger(completed);
  lanbtsElements.lanbtsIdleMetric.textContent = formatInteger(idle);
  lanbtsElements.lanbtsAttentionMetric.textContent = formatInteger(phases.attention);
  lanbtsElements.lanbtsOverallState.className = `lanbts-state-chip ${payload.status || "loading"}`;
  lanbtsElements.lanbtsOverallState.textContent = {
    ready: "设备在线",
    partial: "部分可用",
    stale: "缓存过期",
    unavailable: "仪器不可达",
    initializing: "首次读取中",
  }[payload.status] || "正在读取";
  lanbtsElements.lanbtsUpdatedMetric.textContent = payload.generated_at_utc ? `更新 ${formatTimestamp(payload.generated_at_utc)}` : "尚未更新";
  lanbtsElements.lanbtsDeviceName.textContent = device.hostname || device.name || "蓝博八通道";
  lanbtsElements.lanbtsDeviceMeta.textContent = [device.ip, device.software_version ? `LANBTS ${device.software_version}` : ""]
    .filter(Boolean)
    .join(" · ") || "设备信息待更新";
  lanbtsElements.lanbtsModeNote.textContent = `${payload.can_edit ? "本机可配置" : "只读监控"} · 不控制仪器 · 电压参照待确认 · 每 10 秒刷新显示`;
  lanbtsElements.lanbtsSidebarState.textContent = `${formatInteger(discharging)} 放电 · ${formatInteger(charging)} 充电`;
  updateLanbtsActions();
}

async function loadLanbts({ quiet = false } = {}) {
  if (lanbtsState.loading) return;
  lanbtsState.loading = true;
  updateLanbtsActions();
  try {
    const payload = await lanbtsRequest("/api/start-stop/lanbts");
    lanbtsState.payload = payload;
    renderLanbtsSummary();
    if (!lanbtsState.edits.size) renderLanbtsChannels();
    if (!quiet && payload.status !== "ready" && payload.message) showLanbtsNotice(payload.message, "info");
    else if (!quiet) showLanbtsNotice("");
  } catch (error) {
    showLanbtsNotice(`蓝博状态读取失败：${error.message}；保留的数值是上次缓存，不代表当前读数。`, "error");
    if (lanbtsState.payload) {
      lanbtsState.payload = {...lanbtsState.payload, status:"stale"};
      if (!lanbtsState.edits.size) renderLanbtsChannels();
    }
    lanbtsElements.lanbtsOverallState.className = "lanbts-state-chip unavailable";
    lanbtsElements.lanbtsOverallState.textContent = "读取失败";
    lanbtsElements.lanbtsSidebarState.textContent = "蓝博状态不可用";
  } finally {
    lanbtsState.loading = false;
    updateLanbtsActions();
  }
}

async function saveLanbtsConfiguration() {
  if (!lanbtsState.payload?.can_edit || !lanbtsState.edits.size || lanbtsState.saving) return;
  lanbtsState.saving = true;
  updateLanbtsActions();
  try {
    const channels = (lanbtsState.payload.channels || []).map((channel) => {
      const edit = currentEdit(channel);
      const rawArea = String(edit.electrode_area_cm2 ?? "").trim();
      return {
        channel: Number(channel.channel),
        run_id: channel.run_id || "",
        material_name: String(edit.material_name || "").trim(),
        electrode_area_cm2: rawArea === "" ? null : Number(rawArea),
        notes: String(edit.notes || "").trim(),
      };
    });
    await lanbtsRequest("/api/start-stop/lanbts", {
      method: "PUT",
      body: JSON.stringify({
        expected_revision: Number(lanbtsState.payload.configuration?.revision || 0),
        channels,
      }),
    });
    lanbtsState.edits.clear();
    await loadLanbts({ quiet: true });
    showLanbtsNotice("蓝博材料名称与本次电极面积已保存。", "success");
  } catch (error) {
    showLanbtsNotice(`保存失败：${error.message}`, "error");
  } finally {
    lanbtsState.saving = false;
    updateLanbtsActions();
  }
}

function wireLanbtsRoutes() {
  StartStopClient.applyRoutes();
}

async function loadLanbtsVersion() {
  try {
    const status = await lanbtsRequest("/api/start-stop/status");
    const version = status.service?.version || "未知";
    const imageReference = status.service?.image_reference || "";
    lanbtsElements.workbenchVersion.textContent = `版本 ${version}`;
    lanbtsElements.workbenchVersion.title = imageReference || "版本未知";
  } catch (_error) {
    lanbtsElements.workbenchVersion.textContent = "版本读取失败";
  }
}

function initializeLanbtsPage() {
  wireLanbtsRoutes();
  lanbtsElements.refreshLanbts.addEventListener("click", () => loadLanbts());
  lanbtsElements.saveLanbtsConfiguration.addEventListener("click", saveLanbtsConfiguration);
  loadLanbtsVersion();
  loadLanbts();
  const scheduleRefresh = () => {
    lanbtsState.refreshTimer = StartStopClient.visibleTimeout(async () => {
      await loadLanbts({ quiet: true });
      scheduleRefresh();
    }, 10_000);
  };
  scheduleRefresh();
  window.addEventListener("beforeunload", () => {
    StartStopClient.clearVisibleTimeout(lanbtsState.refreshTimer);
  }, { once: true });
}

initializeLanbtsPage();
