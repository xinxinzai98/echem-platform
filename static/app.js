const dashboardState = {
  status: null,
  instrumentRuns: {
    CHI: [],
    CorrTest: [],
  },
  activity: [],
  refreshTimer: null,
};

const dashboardElements = {
  scanButton: document.querySelector("#scanButton"),
  lastUpdated: document.querySelector("#lastUpdated"),
  controlPill: document.querySelector("#controlPill"),
  sidebarControlState: document.querySelector("#sidebarControlState"),
  instrumentActivity: document.querySelector("#instrumentActivity"),
  systemHealth: document.querySelector("#systemHealth"),
  serviceMetric: document.querySelector("#serviceMetric"),
  uptimeMetric: document.querySelector("#uptimeMetric"),
  parsedMetric: document.querySelector("#parsedMetric"),
  parsedMetricNote: document.querySelector("#parsedMetricNote"),
  sourceMetric: document.querySelector("#sourceMetric"),
  sourceMetricNote: document.querySelector("#sourceMetricNote"),
  controlMetric: document.querySelector("#controlMetric"),
  controlMetricNote: document.querySelector("#controlMetricNote"),
  activityCount: document.querySelector("#activityCount"),
  recentActivity: document.querySelector("#recentActivity"),
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
  if (!value) return "暂无记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatUptime(seconds) {
  if (!Number.isFinite(seconds)) return "等待心跳";
  if (seconds < 60) return `${seconds} 秒运行时间`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟运行时间`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`;
}

function showToast(message, isError = false) {
  dashboardElements.toast.textContent = message;
  dashboardElements.toast.style.background = isError ? "#9d3b2c" : "#17212b";
  dashboardElements.toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(
    () => dashboardElements.toast.classList.remove("show"),
    2600,
  );
}

function activityState(run) {
  if (!run?.modified_utc) {
    return { label: "无数据活动", className: "quiet" };
  }
  const ageSeconds = (Date.now() - new Date(run.modified_utc).getTime()) / 1000;
  if (Number.isFinite(ageSeconds) && ageSeconds <= 120) {
    return { label: "正在产生数据", className: "live" };
  }
  if (Number.isFinite(ageSeconds) && ageSeconds <= 3600) {
    return { label: "最近有活动", className: "recent" };
  }
  return { label: "待机", className: "idle" };
}

function renderInstrumentActivity() {
  const definitions = [
    { key: "CHI", name: "CHI760E", code: "CHI" },
    { key: "CorrTest", name: "CorrTest CS Studio", code: "CS" },
  ];
  dashboardElements.instrumentActivity.innerHTML = definitions
    .map((instrument) => {
      const latest = dashboardState.instrumentRuns[instrument.key][0];
      const current = activityState(latest);
      const detail = latest
        ? `${escapeHtml(latest.technique)} · ${escapeHtml(latest.source_name)}`
        : "尚未发现可用数据文件";
      const sourceState = latest && !latest.source_available
        ? '<span class="instrument-warning">源文件不可用</span>'
        : "";
      return `
        <article class="instrument-activity-row">
          <div class="instrument-code">${instrument.code}</div>
          <div class="instrument-activity-copy">
            <div class="instrument-name-line">
              <strong>${instrument.name}</strong>
              <span class="instrument-state ${current.className}">${current.label}</span>
            </div>
            <p>${detail}</p>
            <small>${latest ? `最近记录 ${formatDate(latest.modified_utc)}` : "等待首次数据扫描"} ${sourceState}</small>
          </div>
          <div class="instrument-control-note">
            <span>${dashboardState.status?.instrument_control ? "控制已启用" : "控制未接管"}</span>
            <small>${latest?.point_count ? `${latest.point_count.toLocaleString("zh-CN")} 点` : "只读观察"}</small>
          </div>
        </article>`;
    })
    .join("");
}

function renderSystemMetrics() {
  const status = dashboardState.status;
  if (!status) return;
  const availableRoots = status.watch_roots.filter((root) => root.available).length;
  const parsedPercent = status.total
    ? Math.round((status.parsed / status.total) * 100)
    : 0;
  const healthy = availableRoots === status.watch_roots.length
    && (status.unavailable_sources || 0) === 0;

  dashboardElements.systemHealth.textContent = healthy ? "运行正常" : "需要检查";
  dashboardElements.systemHealth.classList.toggle("warning", !healthy);
  dashboardElements.serviceMetric.textContent = "在线";
  dashboardElements.uptimeMetric.textContent = formatUptime(status.uptime_seconds);
  dashboardElements.parsedMetric.textContent = `${parsedPercent}%`;
  dashboardElements.parsedMetricNote.textContent = `${status.parsed}/${status.total} 条可绘制`;
  dashboardElements.sourceMetric.textContent = `${availableRoots}/${status.watch_roots.length}`;
  dashboardElements.sourceMetricNote.textContent = status.unavailable_sources
    ? `${status.unavailable_sources} 条源文件不可用`
    : "监控目录与源文件可用";
  dashboardElements.controlMetric.textContent = status.instrument_control ? "已启用" : "锁定";
  dashboardElements.controlMetric.classList.toggle("metric-warning", status.instrument_control);
  dashboardElements.controlMetricNote.textContent = status.instrument_control
    ? "进入环境设置复核授权"
    : "COM3 / COM4 未访问";

  dashboardElements.controlPill.lastChild.textContent = status.instrument_control
    ? "仪器控制已启用"
    : "仪器控制锁定";
  dashboardElements.sidebarControlState.textContent = status.instrument_control
    ? "控制已启用"
    : "控制默认锁定";
}

function renderRecentActivity() {
  const labels = {
    imported: "导入记录",
    updated: "源文件更新",
    metadata_updated: "样品信息更新",
    deferred: "暂缓读取",
    read_error: "读取异常",
  };
  dashboardElements.activityCount.textContent = `${dashboardState.activity.length} 条`;
  if (!dashboardState.activity.length) {
    dashboardElements.recentActivity.innerHTML =
      '<div class="dashboard-empty">尚无平台活动记录</div>';
    return;
  }
  dashboardElements.recentActivity.innerHTML = dashboardState.activity
    .map((item) => `
      <article class="dashboard-activity-item">
        <span class="dashboard-activity-dot"></span>
        <div>
          <strong>${escapeHtml(labels[item.action] || item.action)}</strong>
          <p>${escapeHtml(item.target)} · ${escapeHtml(item.detail)}</p>
        </div>
        <time>${formatDate(item.created_utc)}</time>
      </article>`)
    .join("");
}

async function refreshDashboard() {
  try {
    const [status, chiRuns, corrTestRuns, activity] = await Promise.all([
      request("/api/status"),
      request("/api/runs?instrument=CHI"),
      request("/api/runs?instrument=CorrTest"),
      request("/api/audit?limit=8"),
    ]);
    dashboardState.status = status;
    dashboardState.instrumentRuns.CHI = chiRuns;
    dashboardState.instrumentRuns.CorrTest = corrTestRuns;
    dashboardState.activity = activity;
    renderInstrumentActivity();
    renderSystemMetrics();
    renderRecentActivity();
    dashboardElements.lastUpdated.textContent =
      `更新于 ${new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(new Date())}`;
  } catch (error) {
    dashboardElements.systemHealth.textContent = "连接异常";
    dashboardElements.systemHealth.classList.add("warning");
    showToast(error.message, true);
  }
}

dashboardElements.scanButton.addEventListener("click", async () => {
  dashboardElements.scanButton.disabled = true;
  dashboardElements.scanButton.textContent = "刷新中…";
  try {
    const result = await request("/api/scan", { method: "POST", body: "{}" });
    if (result.status === "busy") {
      showToast("后台扫描正在进行");
    } else {
      showToast(`数据刷新完成：新增 ${result.imported}，更新 ${result.updated}`);
    }
    await refreshDashboard();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    dashboardElements.scanButton.disabled = false;
    dashboardElements.scanButton.textContent = "刷新数据";
  }
});

refreshDashboard();
dashboardState.refreshTimer = window.setInterval(refreshDashboard, 10000);
