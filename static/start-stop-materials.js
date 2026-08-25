const materialsState = {
  status: null,
  payload: null,
  materials: [],
  dirty: false,
  saving: false,
  polling: false,
  pollTimer: null,
  pendingPdfExportId: "",
};

const materialsElements = Object.fromEntries(
  [
    "materialsWorkbenchBrand", "materialsEnvironmentTitle", "materialsEnvironmentNote", "workbenchVersion",
    "sidebarMaterialsState", "materialsNotice", "materialsCount",
    "materialsFavoriteCount", "materialsIncludedCount", "materialsChangedCount", "materialsRevision",
    "materialsUpdatedAt", "materialsDirtyBadge", "materialsSearch", "materialsFilter", "materialsSort",
    "includeVisibleMaterials", "excludeVisibleMaterials", "visibleMaterialsCount",
    "materialsList", "materialsSaveState", "reloadMaterials", "saveMaterials",
    "saveMaterialsAndRender", "materialsExportPdf", "materialsExportMenu",
    "materialsDownloadStandardPdf", "materialsDownloadWaterPdf",
  ].map((id) => [id, document.querySelector(`#${id}`)]),
);

class MaterialsRequestError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function materialsRequest(url, options = {}) {
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
    throw new MaterialsRequestError(payload?.error || `请求失败（${response.status}）`, response.status);
  }
  return payload;
}

function materialsElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

async function copyMaterialsText(text, button) {
  const original = button.textContent;
  let fallback = null;
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "已复制";
        window.setTimeout(() => { button.textContent = original; }, 1600);
        return;
      } catch (_clipboardError) {
        // Browser permission policies can block Clipboard API even on localhost.
      }
    }
    fallback = document.createElement("textarea");
    fallback.value = text;
    fallback.setAttribute("readonly", "");
    fallback.className = "materials-copy-fallback";
    document.body.append(fallback);
    fallback.select();
    if (!document.execCommand("copy")) throw new Error("复制失败");
    button.textContent = "已复制";
  } catch (_error) {
    button.textContent = "复制失败";
  } finally {
    fallback?.remove();
  }
  window.setTimeout(() => { button.textContent = original; }, 1600);
}

function formatMaterialsInteger(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function formatMaterialsTime(value) {
  if (!value) return "尚未保存";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function materialsReadOnly() {
  return materialsState.status?.access_mode === "lan_read_only";
}

function materialsJobBusy() {
  return ["queued", "running"].includes(materialsState.status?.job?.status);
}

function canSaveMaterials() {
  if (materialsReadOnly()) return false;
  const capabilities = materialsState.status?.capabilities;
  return capabilities
    ? capabilities.can_save_configuration === true
    : Boolean(materialsState.status?.available);
}

function canRenderMaterials() {
  if (materialsReadOnly()) return false;
  const capabilities = materialsState.status?.capabilities;
  return capabilities
    ? capabilities.can_render_atlas === true
    : Boolean(materialsState.status?.available && materialsState.status?.execution?.render_ready);
}

function canExportMaterialsPdf() {
  if (materialsReadOnly()) return false;
  const capabilities = materialsState.status?.capabilities;
  if (capabilities) {
    return Object.prototype.hasOwnProperty.call(capabilities, "can_export_pdf")
      ? capabilities.can_export_pdf === true
      : capabilities.can_render_atlas === true;
  }
  return Boolean(materialsState.status?.available && materialsState.status?.execution?.render_ready);
}

function materialsPdfState() {
  const status = materialsState.status || {};
  const analysisReady = !materialsState.dirty && (
    status.analysis_ready === true
    || (!status.data_stale && !status.configuration_stale)
  );
  return {
    standard: Boolean(analysisReady && status.pdf?.standard),
    water: Boolean(analysisReady && status.pdf?.water),
  };
}

function setMaterialsNotice(message = "", type = "warning") {
  materialsElements.materialsNotice.hidden = !message;
  materialsElements.materialsNotice.classList.toggle("error", type === "error");
  materialsElements.materialsNotice.classList.toggle("success", type === "success");
  materialsElements.materialsNotice.textContent = message;
}

function applyMaterialsDeploymentProfile(payload) {
  const repositoryMode = payload?.deployment_profile === "start_stop_repository"
    || payload?.repository?.storage_mode === "sqlite_blob_repository";
  document.querySelectorAll("[data-config-route]").forEach((link) => { link.href = "/start-stop"; });
  document.querySelectorAll("[data-workstations-route]").forEach((link) => { link.href = "/start-stop/workstations"; });
  document.querySelectorAll("[data-analysis-route]").forEach((link) => { link.href = "/start-stop/analysis"; });
  document.querySelectorAll("[data-cv-eis-route]").forEach((link) => { link.href = "/start-stop/cv-eis"; });
  document.querySelectorAll("[data-materials-route]").forEach((link) => { link.href = "/start-stop/materials"; });
  materialsElements.materialsWorkbenchBrand.href = "/start-stop";
  const serviceVersion = payload?.service?.version;
  materialsElements.workbenchVersion.textContent = serviceVersion && serviceVersion !== "unknown"
    ? `版本 ${serviceVersion}`
    : "版本未知";
  materialsElements.workbenchVersion.title = payload?.service?.image_reference || "当前运行服务版本";
}

function updateMaterialsStatus() {
  const status = materialsState.status || {};
  applyMaterialsDeploymentProfile(status);
  const readOnly = materialsReadOnly();
  document.body.classList.toggle("lan-read-only", readOnly);
  if (!status.available) {
    materialsElements.sidebarMaterialsState.textContent = "材料库不可用";
    setMaterialsNotice(status.message || "无法读取材料库。", "error");
    updateMaterialsActions();
    return;
  }
  materialsElements.sidebarMaterialsState.textContent = materialsState.dirty
    ? "材料配置待保存"
    : `已读取 ${formatMaterialsInteger(materialsState.materials.length)} 种材料`;
  if (readOnly) {
    const pdf = materialsPdfState();
    setMaterialsNotice(
      pdf.standard || pdf.water
        ? "当前是局域网只读入口：可查看材料并下载已经生成的 PDF 图集；生成和修改请在服务器本机进行。"
        : "当前是局域网只读入口：可查看材料；PDF 尚未生成，生成和修改请在服务器本机进行。",
    );
  } else if (materialsJobBusy()) {
    const progress = status.job?.progress || {};
    const percent = Number(progress.percent);
    const progressText = Number.isFinite(percent) ? ` · ${Math.round(percent)}%` : "";
    const current = progress.current_item ? ` · ${progress.current_item}` : "";
    setMaterialsNotice(`${progress.phase_label || status.job?.message || "任务正在进行"}${progressText}${current}`);
  } else if (materialsState.dirty) {
    setMaterialsNotice("当前有未保存的材料修改。离开页面或重新读取前请先保存。");
  } else if (status.job?.status === "interrupted") {
    setMaterialsNotice(
      status.job.message || "服务重启导致上一次任务中断，可回到运行配置页重新执行。",
      "error",
    );
  } else if (status.job?.status === "failed") {
    setMaterialsNotice(`上一次任务失败：${status.job.message || "请重试。"}`, "error");
  } else if (status.job?.status === "completed_with_warnings") {
    setMaterialsNotice(status.job.message || "任务已完成，但有部分警告。");
  } else if (status.job?.status === "completed" && status.job?.action === "render") {
    setMaterialsNotice(
      status.job.message || "平台分析已更新；PDF 图集仅在主动导出时生成。",
      "success",
    );
  } else if (!materialsState.dirty) {
    setMaterialsNotice();
  }
  updateMaterialsActions();
}

function setMaterialsDirty(dirty = true) {
  if (materialsReadOnly()) return;
  materialsState.dirty = dirty;
  materialsElements.materialsDirtyBadge.hidden = !dirty;
  materialsElements.materialsSaveState.textContent = dirty
    ? "材料配置已修改，尚未保存"
    : "材料配置已保存";
  updateMaterialsStatus();
}

function updateMaterialsMetrics() {
  const favorites = materialsState.materials.filter((item) => item.favorite).length;
  const included = materialsState.materials.filter((item) => item.include_in_summary_atlas).length;
  const changed = materialsState.materials.filter((item) => item.status !== "未变化").length;
  materialsElements.materialsCount.textContent = formatMaterialsInteger(materialsState.materials.length);
  materialsElements.materialsFavoriteCount.textContent = formatMaterialsInteger(favorites);
  materialsElements.materialsIncludedCount.textContent = formatMaterialsInteger(included);
  materialsElements.materialsChangedCount.textContent = formatMaterialsInteger(changed);
  materialsElements.materialsRevision.textContent = `r${formatMaterialsInteger(materialsState.payload?.revision)}`;
  materialsElements.materialsUpdatedAt.textContent = formatMaterialsTime(materialsState.payload?.updated_utc);
}

function materialMatchesFilters(material) {
  const query = materialsElements.materialsSearch.value.trim().toLowerCase();
  const filter = materialsElements.materialsFilter.value;
  const haystack = [
    material.plot_name,
    material.auto_name,
    material.notes,
    material.key,
    ...(material.ordered_source_files || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return (!query || haystack.includes(query)) && (
    filter === "all"
    || (filter === "favorite" && material.favorite)
    || (filter === "included" && material.include_in_summary_atlas)
    || (filter === "excluded" && !material.include_in_summary_atlas)
    || (filter === "changed" && material.status !== "未变化")
  );
}

function visibleMaterials() {
  const visible = materialsState.materials.filter(materialMatchesFilters);
  const sortMode = materialsElements.materialsSort?.value || "favorites_first";
  if (sortMode === "source_order") return visible;
  return visible
    .map((material, index) => ({ material, index }))
    .sort((left, right) => {
      if (sortMode === "favorites_first") {
        const favoriteOrder = Number(Boolean(right.material.favorite))
          - Number(Boolean(left.material.favorite));
        if (favoriteOrder) return favoriteOrder;
      } else if (sortMode === "name_asc") {
        const leftName = left.material.plot_name || left.material.auto_name || left.material.key;
        const rightName = right.material.plot_name || right.material.auto_name || right.material.key;
        const nameOrder = leftName.localeCompare(rightName, "zh-CN", { numeric: true });
        if (nameOrder) return nameOrder;
      }
      return left.index - right.index;
    })
    .map(({ material }) => material);
}

function renderMaterials() {
  const visible = visibleMaterials();
  const locked = materialsReadOnly() || materialsJobBusy() || !canSaveMaterials();
  const fragment = document.createDocumentFragment();
  for (const material of visible) {
    const row = materialsElement("article", "config-material-row");
    row.dataset.materialKey = material.key;
    row.classList.toggle("is-favorite", Boolean(material.favorite));

    const sourceCell = materialsElement("div", "config-source-cell");
    const sourceHeading = materialsElement("div", "config-source-heading");
    const favoriteButton = materialsElement(
      "button",
      "config-favorite-button",
      material.favorite ? "★" : "☆",
    );
    favoriteButton.type = "button";
    favoriteButton.disabled = locked;
    favoriteButton.setAttribute("aria-pressed", String(Boolean(material.favorite)));
    const updateFavoriteButtonLabel = () => {
      const materialName = material.plot_name || material.auto_name || material.key;
      const action = material.favorite ? "取消收藏" : "收藏";
      favoriteButton.setAttribute("aria-label", `${action} ${materialName}`);
      favoriteButton.title = `${action} ${materialName}`;
    };
    updateFavoriteButtonLabel();
    favoriteButton.addEventListener("click", () => {
      material.favorite = !material.favorite;
      setMaterialsDirty(true);
      renderMaterials();
      window.requestAnimationFrame(() => {
        const updatedRow = [...document.querySelectorAll(".config-material-row")]
          .find((item) => item.dataset.materialKey === material.key);
        updatedRow?.querySelector(".config-favorite-button")?.focus();
      });
    });
    const autoName = materialsElement("strong", "", material.auto_name || material.key);
    autoName.title = material.auto_name || material.key;
    sourceHeading.append(favoriteButton, autoName);
    const sourceMeta = materialsElement("div", "config-source-meta");
    sourceMeta.append(
      materialsElement("span", `material-status${material.status === "未变化" ? "" : " changed"}`, material.status || "未知"),
      materialsElement("span", "", material.test_types || "启停"),
    );
    const orderedSources = material.ordered_source_files?.length
      ? material.ordered_source_files
      : [material.key];
    const sourceOrder = orderedSources.join(" → ");
    const sourceFiles = materialsElement("div", "config-source-files", sourceOrder);
    sourceFiles.title = orderedSources.join("\n");
    const sourceActions = materialsElement("div", "config-source-actions");
    const sourceDetails = materialsElement("details", "config-source-details");
    const sourceSummary = materialsElement(
      "summary",
      "",
      `查看完整来源与接续（${formatMaterialsInteger(orderedSources.length)} 个文件）`,
    );
    const sourceList = materialsElement("ol", "config-source-full-list");
    orderedSources.forEach((source) => {
      const item = materialsElement("li", "", source);
      item.title = source;
      sourceList.append(item);
    });
    sourceDetails.append(sourceSummary, sourceList);
    const copySources = materialsElement("button", "config-copy-source", "复制来源");
    copySources.type = "button";
    copySources.setAttribute("aria-label", `复制 ${material.auto_name || material.key} 的完整来源与接续顺序`);
    copySources.addEventListener("click", () => copyMaterialsText(orderedSources.join("\n"), copySources));
    sourceActions.append(sourceDetails, copySources);
    sourceCell.append(sourceHeading, sourceMeta, sourceFiles, sourceActions);

    const nameCell = materialsElement("label", "config-field-cell");
    nameCell.append(materialsElement("span", "sr-only", `绘图名称：${material.auto_name || material.key}`));
    const nameInput = document.createElement("input");
    nameInput.className = "config-name-input";
    nameInput.type = "text";
    nameInput.value = material.plot_name || "";
    nameInput.maxLength = 180;
    nameInput.readOnly = locked;
    nameInput.addEventListener("input", () => {
      material.plot_name = nameInput.value;
      includeInput.setAttribute("aria-label", `${nameInput.value.trim() || material.auto_name}进入总结图集`);
      updateFavoriteButtonLabel();
      clearMaterialFieldValidation(nameInput, row);
      setMaterialsDirty(true);
    });
    nameCell.append(nameInput);

    const notesCell = materialsElement("label", "config-field-cell");
    notesCell.append(materialsElement("span", "sr-only", `备注：${material.plot_name || material.auto_name}`));
    const notesInput = document.createElement("textarea");
    notesInput.className = "config-notes-input";
    notesInput.value = material.notes || "";
    notesInput.maxLength = 1000;
    notesInput.rows = 2;
    notesInput.readOnly = locked;
    notesInput.placeholder = "可选：样品条件或特殊接续说明";
    notesInput.addEventListener("input", () => {
      material.notes = notesInput.value;
      setMaterialsDirty(true);
    });
    notesCell.append(notesInput);

    const includeCell = materialsElement("label", "config-include-cell");
    const includeInput = document.createElement("input");
    includeInput.type = "checkbox";
    includeInput.checked = Boolean(material.include_in_summary_atlas);
    includeInput.disabled = locked;
    includeInput.setAttribute("aria-label", `${material.plot_name || material.auto_name}进入总结图集`);
    includeInput.addEventListener("change", () => {
      material.include_in_summary_atlas = includeInput.checked;
      clearMaterialFieldValidation(includeInput, row);
      setMaterialsDirty(true);
      updateMaterialsMetrics();
    });
    includeCell.append(includeInput, materialsElement("span", "", "进入图集"));
    row.append(sourceCell, nameCell, notesCell, includeCell);
    fragment.append(row);
  }
  materialsElements.materialsList.replaceChildren(
    fragment.childNodes.length ? fragment : materialsElement("div", "start-stop-loading", "没有符合筛选条件的材料"),
  );
  materialsElements.materialsList.setAttribute("aria-busy", "false");
  materialsElements.visibleMaterialsCount.textContent = `当前可见 ${formatMaterialsInteger(visible.length)} / ${formatMaterialsInteger(materialsState.materials.length)}`;
  updateMaterialsMetrics();
  updateMaterialsActions();
}

function updateMaterialsActions() {
  const locked = materialsJobBusy() || materialsState.saving;
  const saveAllowed = canSaveMaterials();
  materialsElements.saveMaterials.disabled = locked || !saveAllowed || !materialsState.dirty;
  materialsElements.saveMaterialsAndRender.disabled = locked || !saveAllowed || !canRenderMaterials();
  materialsElements.reloadMaterials.disabled = locked || !materialsState.dirty;
  materialsElements.includeVisibleMaterials.disabled = locked || !saveAllowed;
  materialsElements.excludeVisibleMaterials.disabled = locked || !saveAllowed;
  document.querySelectorAll(".config-name-input, .config-notes-input").forEach((input) => {
    input.readOnly = locked || !saveAllowed;
  });
  document.querySelectorAll(".config-include-cell input").forEach((input) => {
    input.disabled = locked || !saveAllowed;
  });
  document.querySelectorAll(".config-favorite-button").forEach((button) => {
    button.disabled = locked || !saveAllowed;
  });
  updateMaterialsPdfActions();
}

function updateMaterialsPdfActions() {
  const pdf = materialsPdfState();
  const downloadable = pdf.standard || pdf.water;
  const busy = materialsJobBusy();
  materialsElements.materialsDownloadStandardPdf.setAttribute("aria-disabled", String(!pdf.standard));
  materialsElements.materialsDownloadStandardPdf.title = pdf.standard
    ? ""
    : "当前没有可下载的原始数据 PDF";
  materialsElements.materialsDownloadWaterPdf.setAttribute("aria-disabled", String(!pdf.water));
  materialsElements.materialsDownloadWaterPdf.title = pdf.water
    ? ""
    : "当前没有可下载的水位补偿 PDF";
  if (busy) {
    materialsElements.materialsExportPdf.disabled = true;
    materialsElements.materialsExportPdf.textContent = materialsState.status?.job?.message || "任务进行中";
    closeMaterialsExportMenu();
  } else if (downloadable) {
    materialsElements.materialsExportPdf.disabled = false;
    materialsElements.materialsExportPdf.textContent = "下载 PDF 图集";
    materialsElements.materialsExportPdf.title = "下载当前分析对应的 PDF 图集";
  } else if (materialsReadOnly()) {
    materialsElements.materialsExportPdf.disabled = true;
    materialsElements.materialsExportPdf.textContent = "PDF 尚未生成";
    materialsElements.materialsExportPdf.title = "请在服务器本机的材料库生成 PDF 图集";
    closeMaterialsExportMenu();
  } else {
    materialsElements.materialsExportPdf.disabled = materialsState.saving || !canExportMaterialsPdf();
    materialsElements.materialsExportPdf.textContent = "生成 PDF 图集";
    materialsElements.materialsExportPdf.title = "按当前材料配置生成原始数据和水位补偿 PDF 图集";
    closeMaterialsExportMenu();
  }
}

function clearMaterialFieldValidation(field, row) {
  field.removeAttribute("aria-invalid");
  field.removeAttribute("aria-describedby");
  if (!row.querySelector('[aria-invalid="true"]')) {
    row.removeAttribute("aria-invalid");
    row.classList.remove("validation-error");
  }
}

function clearMaterialsValidation() {
  document.querySelectorAll('.config-material-row[aria-invalid="true"], .config-material-row.validation-error')
    .forEach((row) => {
      row.removeAttribute("aria-invalid");
      row.classList.remove("validation-error");
    });
  document.querySelectorAll('.config-material-row [aria-invalid="true"]')
    .forEach((field) => {
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
    });
}

function validateMaterials() {
  const names = materialsState.materials.map((item) => (item.plot_name || "").trim());
  const blankIndex = names.findIndex((name) => !name);
  if (blankIndex >= 0) {
    return {
      message: "完整绘图名称不能为空。已定位到需要修改的材料。",
      materialKey: materialsState.materials[blankIndex].key,
      fieldSelector: ".config-name-input",
    };
  }
  const firstIndexForName = new Map();
  for (let index = 0; index < names.length; index += 1) {
    if (firstIndexForName.has(names[index])) {
      return {
        message: `完整绘图名称不能重复：“${names[index]}”。已定位到后一个重复项。`,
        materialKey: materialsState.materials[index].key,
        fieldSelector: ".config-name-input",
      };
    }
    firstIndexForName.set(names[index], index);
  }
  if (!materialsState.materials.some((item) => item.include_in_summary_atlas)) {
    return {
      message: "至少需要选择 1 种材料进入总结图集。已定位到第一个“进入图集”选项。",
      materialKey: materialsState.materials[0]?.key,
      fieldSelector: '.config-include-cell input',
    };
  }
  return null;
}

function showMaterialsValidationError(validation) {
  clearMaterialsValidation();
  setMaterialsNotice(validation.message, "error");
  const material = materialsState.materials.find((item) => item.key === validation.materialKey);
  if (material && !materialMatchesFilters(material)) {
    materialsElements.materialsSearch.value = "";
    materialsElements.materialsFilter.value = "all";
    renderMaterials();
  }
  const row = [...document.querySelectorAll(".config-material-row")]
    .find((item) => item.dataset.materialKey === validation.materialKey);
  const field = row?.querySelector(validation.fieldSelector);
  if (!row || !field) return;
  row.classList.add("validation-error");
  row.setAttribute("aria-invalid", "true");
  field.setAttribute("aria-invalid", "true");
  field.setAttribute("aria-describedby", "materialsNotice");
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  field.focus({ preventScroll: true });
}

async function saveMaterialLibrary() {
  if (!materialsState.payload || !canSaveMaterials()) return false;
  const validationError = validateMaterials();
  if (validationError) {
    showMaterialsValidationError(validationError);
    return false;
  }
  clearMaterialsValidation();
  if (!materialsState.dirty) return true;
  materialsState.saving = true;
  materialsElements.materialsSaveState.textContent = "正在保存材料配置…";
  updateMaterialsActions();
  try {
    const payload = await materialsRequest("/api/start-stop/materials", {
      method: "POST",
      body: JSON.stringify({
        dataset_fingerprint: materialsState.payload.dataset_fingerprint,
        expected_revision: materialsState.payload.revision,
        materials: materialsState.materials.map((item) => ({
          key: item.key,
          plot_name: item.plot_name.trim(),
          include_in_summary_atlas: Boolean(item.include_in_summary_atlas),
          favorite: Boolean(item.favorite),
          notes: (item.notes || "").trim(),
        })),
      }),
    });
    materialsState.payload = payload;
    materialsState.materials = payload.materials || [];
    materialsState.dirty = false;
    materialsElements.materialsDirtyBadge.hidden = true;
    materialsElements.materialsSaveState.textContent = "材料配置已保存";
    renderMaterials();
    setMaterialsNotice("材料配置已保存到独立数据库。", "success");
    return true;
  } catch (error) {
    const message = error.status === 409
      ? "材料库已被其他页面或更新任务修改。请放弃未保存修改后重新读取。"
      : error.message;
    setMaterialsNotice(message, "error");
    materialsElements.materialsSaveState.textContent = "保存失败，已存配置未改变";
    return false;
  } finally {
    materialsState.saving = false;
    updateMaterialsActions();
  }
}

async function saveAndRenderMaterialLibrary() {
  if (!(await saveMaterialLibrary())) return;
  if (!canRenderMaterials()) {
    setMaterialsNotice("材料配置已保存，但当前分析环境尚未就绪，暂时不能更新平台分析。", "error");
    return;
  }
  try {
    const job = await materialsRequest("/api/start-stop/jobs", {
      method: "POST",
      body: JSON.stringify({ action: "render" }),
    });
    materialsState.status = { ...(materialsState.status || {}), job };
    setMaterialsNotice("材料配置已保存，正在更新平台分析；本次不会生成 PDF。", "success");
    scheduleMaterialsPoll();
    updateMaterialsActions();
  } catch (error) {
    setMaterialsNotice(`材料配置已保存，但无法启动平台分析：${error.message}`, "error");
  }
}

function materialsExportMenuItems() {
  return [
    materialsElements.materialsDownloadStandardPdf,
    materialsElements.materialsDownloadWaterPdf,
  ].filter((item) => item.getAttribute("aria-disabled") !== "true");
}

function closeMaterialsExportMenu({ restoreFocus = false } = {}) {
  if (materialsElements.materialsExportMenu.hidden) return;
  materialsElements.materialsExportMenu.hidden = true;
  materialsElements.materialsExportPdf.setAttribute("aria-expanded", "false");
  if (restoreFocus) {
    materialsElements.materialsExportPdf.focus({ preventScroll: true });
  }
}

function openMaterialsExportMenu() {
  const items = materialsExportMenuItems();
  if (!items.length) return;
  materialsElements.materialsExportMenu.hidden = false;
  materialsElements.materialsExportPdf.setAttribute("aria-expanded", "true");
  items[0].focus({ preventScroll: true });
}

async function startMaterialsPdfExport() {
  if (materialsJobBusy() || materialsReadOnly()) return;
  if (!(await saveMaterialLibrary())) return;
  if (!canExportMaterialsPdf()) {
    setMaterialsNotice("当前分析环境尚未就绪，暂时不能生成 PDF 图集。", "error");
    return;
  }
  try {
    const job = await materialsRequest("/api/start-stop/jobs", {
      method: "POST",
      body: JSON.stringify({
        action: "render",
        render_data_mode: "both",
        render_material_scope: "all",
        export_pdf: true,
      }),
    });
    materialsState.pendingPdfExportId = job.id || "";
    materialsState.status = { ...(materialsState.status || {}), job };
    closeMaterialsExportMenu();
    setMaterialsNotice("正在生成原始数据和水位补偿 PDF 图集。");
    scheduleMaterialsPoll();
    updateMaterialsActions();
  } catch (error) {
    setMaterialsNotice(`无法启动 PDF 导出：${error.message}`, "error");
  }
}

function handleMaterialsPdfButton() {
  if (materialsPdfState().standard || materialsPdfState().water) {
    if (materialsElements.materialsExportMenu.hidden) openMaterialsExportMenu();
    else closeMaterialsExportMenu({ restoreFocus: true });
    return;
  }
  startMaterialsPdfExport();
}

function scheduleMaterialsPoll() {
  window.clearTimeout(materialsState.pollTimer);
  materialsState.pollTimer = window.setTimeout(async () => {
    try {
      materialsState.status = await materialsRequest("/api/start-stop/status");
      updateMaterialsStatus();
      if (materialsJobBusy()) {
        scheduleMaterialsPoll();
      } else if (materialsState.status?.job?.status === "completed") {
        const exported = materialsState.pendingPdfExportId
          && materialsState.status.job.id === materialsState.pendingPdfExportId;
        setMaterialsNotice(
          exported
            ? "PDF 图集已生成。点击右上角“下载 PDF 图集”选择文件。"
            : materialsState.status.job.message || "平台分析已更新；本次未生成 PDF。",
          "success",
        );
        materialsState.pendingPdfExportId = "";
        updateMaterialsActions();
      }
    } catch (error) {
      setMaterialsNotice(error.message, "error");
      scheduleMaterialsPoll();
    }
  }, 1800);
}

async function loadMaterialLibrary({ discardDraft = false } = {}) {
  if (materialsState.dirty && !discardDraft) return;
  materialsElements.materialsList.setAttribute("aria-busy", "true");
  try {
    const [status, payload] = await Promise.all([
      materialsRequest("/api/start-stop/status"),
      materialsRequest("/api/start-stop/materials"),
    ]);
    materialsState.status = status;
    materialsState.payload = payload;
    materialsState.materials = payload.materials || [];
    materialsState.dirty = false;
    materialsElements.materialsDirtyBadge.hidden = true;
    materialsElements.materialsSaveState.textContent = "材料配置已保存";
    updateMaterialsStatus();
    renderMaterials();
    if (materialsJobBusy()) scheduleMaterialsPoll();
  } catch (error) {
    setMaterialsNotice(error.message, "error");
    materialsElements.materialsList.replaceChildren(
      materialsElement("div", "start-stop-loading", "无法读取材料库"),
    );
    materialsElements.materialsList.setAttribute("aria-busy", "false");
  }
}

function setVisibleMaterialsIncluded(include) {
  if (!canSaveMaterials()) return;
  for (const material of visibleMaterials()) material.include_in_summary_atlas = include;
  setMaterialsDirty(true);
  renderMaterials();
}

materialsElements.materialsSearch.addEventListener("input", renderMaterials);
materialsElements.materialsFilter.addEventListener("change", renderMaterials);
materialsElements.materialsSort.addEventListener("change", renderMaterials);
materialsElements.includeVisibleMaterials.addEventListener("click", () => setVisibleMaterialsIncluded(true));
materialsElements.excludeVisibleMaterials.addEventListener("click", () => setVisibleMaterialsIncluded(false));
materialsElements.reloadMaterials.addEventListener("click", () => loadMaterialLibrary({ discardDraft: true }));
materialsElements.saveMaterials.addEventListener("click", saveMaterialLibrary);
materialsElements.saveMaterialsAndRender.addEventListener("click", saveAndRenderMaterialLibrary);
materialsElements.materialsExportPdf.addEventListener("click", handleMaterialsPdfButton);
materialsElements.materialsExportMenu.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeMaterialsExportMenu({ restoreFocus: true });
    return;
  }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const items = materialsExportMenuItems();
  if (!items.length) return;
  event.preventDefault();
  const currentIndex = items.indexOf(document.activeElement);
  let nextIndex = currentIndex;
  if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = items.length - 1;
  else if (event.key === "ArrowDown") nextIndex = (currentIndex + 1 + items.length) % items.length;
  else nextIndex = (currentIndex - 1 + items.length) % items.length;
  items[nextIndex].focus({ preventScroll: true });
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".materials-export-menu")) closeMaterialsExportMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || materialsElements.materialsExportMenu.hidden) return;
  event.preventDefault();
  closeMaterialsExportMenu({ restoreFocus: true });
});
for (const link of [
  materialsElements.materialsDownloadStandardPdf,
  materialsElements.materialsDownloadWaterPdf,
]) {
  link.addEventListener("click", (event) => {
    if (link.getAttribute("aria-disabled") === "true") event.preventDefault();
    closeMaterialsExportMenu();
  });
}
window.addEventListener("beforeunload", (event) => {
  if (!materialsState.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

loadMaterialLibrary();
