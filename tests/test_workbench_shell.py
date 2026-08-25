from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class PageStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.links: list[dict[str, str | None]] = []
        self.settings_links: list[dict[str, str | None]] = []
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self.sidebar_count = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        classes = set(str(values.get("class") or "").split())
        if "workbench-sidebar" in classes:
            self.sidebar_count += 1
        if tag == "a" and "workbench-nav-item" in classes:
            self.links.append(values)
        if tag == "a" and "workbench-settings-button" in classes:
            self.settings_links.append(values)
        if tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(str(values.get("href") or ""))
        if tag == "script" and values.get("src"):
            self.scripts.append(str(values.get("src") or ""))


class WorkbenchShellTests(unittest.TestCase):
    pages = {
        "index.html": "/",
        "protocol.html": "/steps",
        "analysis.html": "/analysis",
        "start-stop.html": "/start-stop/analysis",
        "start-stop-config.html": "/start-stop",
        "start-stop-workstations.html": "/start-stop/workstations",
        "start-stop-cv-eis.html": "/start-stop/cv-eis",
        "start-stop-materials.html": "/start-stop/materials",
        "monitor.html": None,
    }

    def parse_page(self, name: str) -> PageStructureParser:
        parser = PageStructureParser()
        parser.feed((STATIC / name).read_text(encoding="utf-8"))
        return parser

    def test_every_workspace_page_uses_the_shared_sidebar(self) -> None:
        for name, active_path in self.pages.items():
            with self.subTest(page=name):
                parser = self.parse_page(name)
                self.assertEqual(parser.sidebar_count, 1)
                self.assertIn("/static/workbench.css", parser.stylesheets)
                if name in {"start-stop.html", "start-stop-config.html", "start-stop-workstations.html", "start-stop-cv-eis.html", "start-stop-materials.html"}:
                    expected_links = ["/start-stop", "/start-stop/workstations", "/start-stop/analysis", "/start-stop/cv-eis", "/start-stop/materials"]
                    expected_settings_links = []
                else:
                    expected_links = ["/", "/steps", "/analysis"]
                    expected_settings_links = ["/environment"]
                self.assertEqual([link.get("href") for link in parser.links], expected_links)
                active = [
                    link for link in parser.links
                    if link.get("aria-current") == "page"
                ]
                if active_path:
                    self.assertEqual(len(active), 1)
                    self.assertEqual(active[0].get("href"), active_path)
                else:
                    self.assertEqual(active, [])
                self.assertEqual(
                    [link.get("href") for link in parser.settings_links],
                    expected_settings_links,
                )

    def test_environment_page_is_active_only_in_the_corner_settings_dock(self) -> None:
        parser = self.parse_page("monitor.html")
        self.assertEqual(len(parser.settings_links), 1)
        self.assertEqual(
            parser.settings_links[0].get("aria-current"),
            "page",
        )
        self.assertIn("/static/icons/gear.svg", (
            STATIC / "monitor.html"
        ).read_text(encoding="utf-8"))

    def test_dashboard_contains_only_the_three_requested_information_regions(self) -> None:
        dashboard = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="instrumentActivity"', dashboard)
        self.assertIn('class="system-metric-grid"', dashboard)
        self.assertIn('id="recentActivity"', dashboard)
        self.assertNotIn('id="curveChart"', dashboard)
        self.assertNotIn('id="metadataForm"', dashboard)
        self.assertNotIn('id="runList"', dashboard)

    def test_data_analysis_is_a_separate_module(self) -> None:
        parser = self.parse_page("analysis.html")
        self.assertIn('curveChart', parser.ids)
        self.assertIn('fileTree', parser.ids)
        self.assertIn('treeCount', parser.ids)
        self.assertNotIn('metadataForm', parser.ids)
        self.assertNotIn('auditList', parser.ids)
        self.assertNotIn('totalCount', parser.ids)
        self.assertNotIn('parsedCount', parser.ids)
        self.assertNotIn('integrityCount', parser.ids)
        self.assertNotIn('runList', parser.ids)
        self.assertNotIn('watchRoots', parser.ids)
        self.assertIn("/static/analysis.js", parser.scripts)

    def test_analysis_exposes_method_specific_eis_and_cv_workflows(self) -> None:
        parser = self.parse_page("analysis.html")
        page = (STATIC / "analysis.html").read_text(encoding="utf-8")
        script = (STATIC / "analysis.js").read_text(encoding="utf-8")
        for element_id in (
            "methodAnalysisPanel",
            "eisFields",
            "cvFields",
            "cvSolution",
            "cvSolutionCustom",
            "cvPh",
            "cvReference",
            "cvReferenceCustom",
            "cvReferenceOffset",
            "cvCompensation",
            "cvResistance",
            "cvScanBranch",
            "cvOnlineCompensation",
            "previewAnalysis",
            "saveAnalysis",
        ):
            self.assertIn(element_id, parser.ids)
        self.assertIn("所有条件会随结果保存为参数快照", page)
        self.assertIn("不确定（不允许离线补偿）", page)
        self.assertIn("/analyses/preview", script)
        self.assertIn("/api/runs/${runId}/analyses", script)
        self.assertIn("analysisRequestId", script)
        self.assertIn("analysisHistoryRequestId", script)
        self.assertIn("setAnalysisBusy(true)", script)
        self.assertIn("renderDetailLoading", script)
        self.assertIn("renderDetailFailure", script)
        self.assertIn("eis_resistance", script)
        self.assertIn("cv_overpotential", script)
        self.assertIn('<select id="cvSolution"', page)
        self.assertIn('data-ph="14.00"', page)
        self.assertIn('data-ph="13.00"', page)
        self.assertIn('data-offset="0.098"', page)
        self.assertIn('data-offset="0.197"', page)
        self.assertIn("25 ℃名义值", page)
        self.assertIn("normalizeSolutionLabel", script)
        self.assertIn("syncSolutionPreset", script)
        self.assertIn("selectedSolutionValue", script)
        self.assertIn("selectedReferenceValue", script)
        self.assertIn("syncReferenceOffset", script)
        self.assertIn("cvReferenceOffset.readOnly = !custom", script)
        self.assertNotIn("metadataRequestId", script)
        self.assertNotIn("/api/audit", script)
        self.assertNotIn("/metadata", script)

    def test_standalone_start_stop_analysis_contains_only_curve_inspection_tools(self) -> None:
        parser = self.parse_page("start-stop.html")
        page = (STATIC / "start-stop.html").read_text(encoding="utf-8")
        script = (STATIC / "start-stop.js").read_text(encoding="utf-8")
        for element_id in (
            "materialList",
            "materialSearch",
            "materialFilter",
            "materialFilterSummary",
            "clearMaterialFilters",
            "selectAllForChart",
            "clearAllForChart",
            "selectedSeriesMetric",
            "materialSort",
            "startStopChart",
            "compensationMode",
            "xAxisMode",
            "showAnomalyMarkers",
            "clearHighlights",
            "inspectMode",
            "zoomSelectMode",
            "panMode",
            "zoomIn",
            "zoomOut",
            "resetZoom",
            "zoomLevel",
            "zoomSelection",
            "printCurrentView",
            "liveComparisonBanner",
            "liveComparisonMeta",
            "liveComparisonState",
            "refreshLiveComparison",
        ):
            self.assertIn(element_id, parser.ids)
        for management_id in (
            "refreshData",
            "renderAtlas",
            "renderDataMode",
            "renderMaterialScope",
            "updateCapability",
            "lastDataUpdateMetric",
            "repositoryMetricTile",
            "openUpload",
            "uploadDialog",
            "downloadStandardPdf",
            "downloadWaterPdf",
        ):
            self.assertNotIn(management_id, parser.ids)
        self.assertIn("此处选择只用于当前检查", page)
        self.assertIn("全选检查”不会改变总结图集", page)
        self.assertIn("最低点 &lt;15 s 异常", page)
        self.assertIn("/api/start-stop/materials", script)
        self.assertIn("/api/start-stop/chart", script)
        self.assertIn('request("/api/start-stop/live-preview")', script)
        self.assertIn('get("view") === "live"', script)
        self.assertIn("seriesResult.error && !state.liveComparisonRequested", script)
        self.assertIn("正式图线待重绘，当前先显示实时快照", script)
        self.assertIn("mergeLiveComparisonChart", script)
        self.assertIn("formal_series_id", script)
        self.assertIn("按正式启停规则临时计算", script)
        self.assertIn("已按正式规则接续", script)
        self.assertIn("未稳定文件不会正式入库", script)
        self.assertIn("highlightedSeries", script)
        self.assertIn("MAX_CHART_SELECTION = 64", script)
        self.assertIn("MAX_HIGHLIGHTED_SERIES = 16", script)
        self.assertIn('const seriesLineStyles = ["", "9 4"]', script)
        self.assertIn("chartDasharray", script)
        self.assertIn("totalPointBudget", script)
        self.assertEqual(page.count("data-process-filter"), 2)
        self.assertEqual(page.count("data-element-filter"), 4)
        for label in ("脉冲", "恒流", "Ni", "Mo", "P", "Co"):
            self.assertIn(f"<span>{label}</span>", page)
        for label in (
            "收藏优先",
            "更新日期：最新优先",
            "更新日期：最早优先",
            "循环圈数：多到少",
            "循环圈数：少到多",
        ):
            self.assertIn(label, page)
        self.assertIn("latest_source_modified_at", script)
        self.assertIn("function sortVisibleMaterials(materials)", script)
        self.assertIn('filter === "favorite"', script)
        self.assertIn('sortMode === "favorites_first"', script)
        self.assertIn("material-favorite-badge", script)
        self.assertIn("anomalyMaterialKey", script)
        self.assertIn("function activeSeriesIds()", script)
        self.assertIn('chartCheck.type = anomalyMode ? "radio" : "checkbox"', script)
        self.assertIn('metric: state.metric', script)
        self.assertIn('mode: isAnomalyMode() ? "raw" : state.mode', script)
        self.assertIn("processFilters.size === 0", script)
        self.assertIn("some((tag) => processTags.has(tag))", script)
        self.assertIn("every((tag) => elementTags.has(tag))", script)
        self.assertIn("Control_Programs or pulse", script)
        self.assertIn("最多 16 条", page)
        self.assertIn("chart-highlight-halo", script)
        self.assertIn('access_mode === "lan_read_only"', script)
        self.assertIn("局域网只读入口", script)
        self.assertIn('deployment_profile === "start_stop_repository"', script)
        self.assertIn("sqlite_blob_repository", script)
        self.assertIn("zoomChartByFactor", script)
        self.assertIn("clampChartView", script)
        self.assertIn("chartDomainForSeries", script)
        self.assertIn('overlay.addEventListener("wheel"', script)
        self.assertIn('overlay.addEventListener("pointerdown"', script)
        self.assertIn("distanceToSegment", script)
        self.assertIn("start-stop-chart-plot-clip", script)
        self.assertIn("window.print()", script)
        self.assertIn("new URLSearchParams", script)
        self.assertIn('startStopNavNumber.textContent = "03"', script)
        self.assertNotIn('"Content-Type", "application/octet-stream"', script)
        self.assertNotIn('action: "prepare_upload"', script)
        self.assertIn("Start–stop Studio", page)
        self.assertNotIn("EchemPlatform", page)
        self.assertNotIn('href="/steps"', page)
        self.assertNotIn('href="/analysis"', page)
        self.assertNotIn("https://", page)

    def test_start_stop_sidebar_boots_in_its_final_repository_state(self) -> None:
        for filename in (
            "start-stop-config.html",
            "start-stop-workstations.html",
            "start-stop.html",
            "start-stop-cv-eis.html",
            "start-stop-materials.html",
        ):
            with self.subTest(filename=filename):
                page = (STATIC / filename).read_text(encoding="utf-8")
                self.assertIn(
                    '<body class="start-stop-app start-stop-repository-mode">',
                    page,
                )
                self.assertIn(
                    '<script src="/static/start-stop-shell.js"></script>',
                    page,
                )
                self.assertNotIn(
                    '<script src="/static/start-stop-shell.js" defer></script>',
                    page,
                )
                self.assertEqual(page.count("Docker 独立程序"), 1)
                self.assertEqual(page.count("原始字节入库 · SHA-256 可追溯"), 1)
        shell_script = (STATIC / "start-stop-shell.js").read_text(encoding="utf-8")
        self.assertLess(
            shell_script.index("root.dataset.startStopSidebar ="),
            shell_script.index("function initializeSidebar()"),
        )
        self.assertLess(
            shell_script.index('root.dataset.startStopSidebarReady = "true"'),
            shell_script.index("function initializeSidebar()"),
        )
        for filename in (
            "start-stop.js",
            "start-stop-config.js",
            "start-stop-workstations.js",
            "start-stop-cv-eis.js",
            "start-stop-materials.js",
        ):
            with self.subTest(filename=filename):
                script = (STATIC / filename).read_text(encoding="utf-8")
                self.assertNotIn(
                    'classList.toggle("start-stop-repository-mode"',
                    script,
                )

    def test_workstation_monitor_is_a_read_only_server_cached_dashboard(self) -> None:
        parser = self.parse_page("start-stop-workstations.html")
        page = (STATIC / "start-stop-workstations.html").read_text(encoding="utf-8")
        script = (STATIC / "start-stop-workstations.js").read_text(encoding="utf-8")
        css = (STATIC / "start-stop-workstations.css").read_text(encoding="utf-8")
        for element_id in (
            "monitorOverallState",
            "runningStationMetric",
            "monitorUpdatedMetric",
            "monitorFreshnessDetail",
            "workstationStationGrid",
            "workstationMachineList",
            "refreshWorkstations",
            "livePreviewStatus",
            "livePreviewGrid",
        ):
            self.assertIn(element_id, parser.ids)
        self.assertIn("/static/start-stop-workstations.css", parser.stylesheets)
        self.assertIn("/static/start-stop-workstations.js", parser.scripts)
        self.assertIn("六台工作站运行状态", page)
        self.assertIn("绿色正在运行，灰色空闲，红色离线", page)
        self.assertIn("运行状态来自文件连续写入", page)
        self.assertIn("尚未绑定具体 COM 口", page)
        self.assertIn("station-overview-card", script)
        self.assertIn("renderStationOverview", script)
        self.assertIn("@keyframes station-running-breathe", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn('workstationRequest("/api/start-stop/workstations")', script)
        self.assertIn('workstationRequest("/api/start-stop/live-preview")', script)
        self.assertIn("Hg/HgO 原始标尺", page)
        self.assertIn("renderLivePreviewChart", script)
        self.assertIn('method: "GET"', script)
        self.assertNotIn('method: "POST"', script)
        self.assertNotIn("innerHTML", script)

    def test_standalone_configuration_page_owns_runtime_and_collection_configuration(self) -> None:
        parser = self.parse_page("start-stop-config.html")
        page = (STATIC / "start-stop-config.html").read_text(encoding="utf-8")
        script = (STATIC / "start-stop-config.js").read_text(encoding="utf-8")
        for element_id in (
            "configAccessState",
            "updateCapability",
            "jobStatus",
            "refreshData",
            "renderAtlas",
            "openUpload",
            "toggleAutoUpdate",
            "autoUpdateInterval",
            "autoUpdatePanel",
            "autoUpdateState",
            "autoUpdateNextRun",
            "autoUpdateLastResult",
            "toggleLivePreview",
            "livePreviewPanel",
            "livePreviewState",
            "livePreviewNextRun",
            "livePreviewLastResult",
            "plotLivePreview",
            "livePreviewPlotHint",
            "jobProgressPanel",
            "jobProgressState",
            "jobProgressBar",
            "jobProgressPercent",
            "jobProgressPhase",
            "jobProgressCurrentLabel",
            "jobProgressCurrent",
            "jobProgressCountLabel",
            "jobProgressCount",
            "jobProgressElapsed",
            "jobProgressSteps",
            "jobProgressDetail",
            "jobProgressSummary",
            "jobProgressMachines",
            "jobProgressAnnouncement",
            "machineConnectivityList",
            "machineConnectivitySummary",
            "checkAllMachines",
            "collectionConfigRevision",
            "collectionConfigUpdatedAt",
            "collectionConfigDirtyBadge",
            "collectionConfigNotice",
            "collectionMachineList",
            "collectionConfigSaveState",
            "reloadCollectionConfig",
            "saveCollectionConfig",
            "plottingRulesTitle",
            "uploadDialog",
            "uploadGroupName",
            "chooseUploadFiles",
            "chooseUploadFolder",
            "uploadFiles",
            "uploadFolder",
            "uploadDropZone",
            "uploadQueue",
            "uploadProgress",
            "uploadResult",
            "startUpload",
        ):
            self.assertIn(element_id, parser.ids)
        self.assertIn("/static/start-stop-config.js", parser.scripts)
        self.assertIn("/static/start-stop-config.css", parser.stylesheets)
        self.assertIn("实验电脑搜索位置", page)
        self.assertIn("当前绘图规则", page)
        self.assertIn("原始实测电位 vs Hg/HgO（不做参比换算）", page)
        self.assertIn("实时数据预览", page)
        self.assertIn("在 03 页叠加正在测试数据", page)
        self.assertIn("按正式启停规则识别完整循环", page)
        self.assertIn('configRequest("/api/start-stop/live-preview")', script)
        self.assertIn('window.location.assign("/start-stop/analysis?view=live")', script)
        bootstrap = script[script.index("async function initializeConfigPage()") :]
        self.assertLess(
            bootstrap.index("await loadConfigWorkspace();"),
            bootstrap.index("if (isConfigReadOnly())"),
        )
        self.assertLess(
            bootstrap.index("if (isConfigReadOnly())"),
            bootstrap.index("loadCollectionConfig()"),
        )
        self.assertIn("局域网只读入口不读取实验机远程目录配置", bootstrap)
        self.assertIn('startStopNavNumber.textContent = "03"', script)
        for label in (
            "数据操作与实验机状态",
            "上传数据",
            "从三台实验电脑下载并更新",
            "按选择更新平台分析",
            "原始数据（未补偿）",
            "仅新增／数据已更新",
            "三台实验机连接状态",
            "检查全部连通性",
            "测试室 1",
            "测试室 2",
            "制备室",
        ):
            self.assertIn(label, page)
        for removed_metric_id in (
            "materialMetric",
            "selectedMetric",
            "cycleMetric",
            "anomalyMetric",
            "lastDataUpdateMetric",
            "repositoryMetricTile",
            "repositoryFileMetric",
            "repositoryStorageMetric",
            "renderedMetric",
        ):
            self.assertNotIn(removed_metric_id, parser.ids)
        self.assertEqual(page.count('class="machine-status-row"'), 3)
        self.assertEqual(page.count('class="machine-check-button'), 3)
        self.assertIn('configRequest("/api/start-stop/connectivity"', script)
        self.assertIn('configRequest("/api/start-stop/connectivity/check"', script)
        self.assertIn('configRequest("/api/start-stop/collection-config"', script)
        self.assertIn('configRequest("/api/start-stop/auto-update"', script)
        self.assertIn('method: "PUT"', script)
        self.assertIn("expected_revision", script)
        self.assertIn("AUTO_UPDATE_INTERVALS", script)
        self.assertIn("默认关闭", page)
        self.assertIn("不会自动重新绘图", page)
        self.assertIn("不会把新材料自动加入图集", page)
        self.assertIn("停止写入满 5 分钟后才会安全入库", page)
        self.assertIn('configRequest("/api/start-stop/connectivity/path-check"', script)
        self.assertIn('method: "PUT"', script)
        self.assertIn("expected_revision", script)
        self.assertIn("paths: machine.paths.map", script)
        self.assertIn("machine_id: machineId, path:", script)
        self.assertIn("checkAllMachineConnectivity", script)
        self.assertIn("checkingMachineIds", script)
        self.assertIn('connectivity?.can_check === true', script)
        self.assertIn("normalizeJobProgress", script)
        self.assertIn("progress.current_item", script)
        self.assertIn("RENDER_PROGRESS_STEPS", script)
        self.assertIn("function updateJobElapsed(job, isBusy)", script)
        self.assertIn("function renderJobProgressSteps(progress, isRender)", script)
        self.assertIn("scrollIntoView", script)
        self.assertIn('removeAttribute("value")', script)
        self.assertLess(
            page.index('id="jobProgressPanel"'),
            page.index('id="autoUpdatePanel"'),
        )
        self.assertLess(
            script.index("result.collection_ingested"),
            script.index("result.collection_downloaded"),
        )
        self.assertLess(
            script.index("result.collection_downloaded"),
            script.index("result.collection_copied"),
        )
        self.assertIn("当前文件", page)
        self.assertIn("正在扫描、计算或生成的内容", page)
        self.assertNotIn('configRequest("/api/start-stop/materials"', script)
        self.assertNotIn("plot_name", script)
        self.assertNotIn("include_in_summary_atlas", script)
        self.assertIn('access_mode === "lan_read_only"', script)
        self.assertIn("requestBody.render_data_mode", script)
        self.assertIn("requestBody.render_material_scope", script)
        self.assertIn("body: JSON.stringify(requestBody)", script)
        self.assertIn('"Content-Type", "application/octet-stream"', script)
        self.assertIn("new URLSearchParams", script)
        self.assertIn("webkitRelativePath", script)
        self.assertIn("webkitGetAsEntry", script)
        self.assertIn("upload_max_file_bytes", script)
        self.assertIn("upload_extensions", script)
        self.assertIn("upload_max_batch_files", script)
        self.assertIn('action: "prepare_upload"', script)
        self.assertIn("upload_ids: uploadIds", script)
        self.assertIn("payload.changed === false", script)
        self.assertIn("configState.collectionConfigDirty", script)
        self.assertIn("Start–stop Studio", page)
        self.assertNotIn("EchemPlatform", page)
        self.assertNotIn('href="/steps"', page)
        self.assertNotIn('href="/analysis"', page)
        self.assertNotIn("startStopChart", page)
        self.assertNotIn("selectedSeries", script)
        self.assertNotIn("highlightedSeries", script)

    def test_materials_library_owns_persistent_material_configuration(self) -> None:
        parser = self.parse_page("start-stop-materials.html")
        page = (STATIC / "start-stop-materials.html").read_text(encoding="utf-8")
        script = (STATIC / "start-stop-materials.js").read_text(encoding="utf-8")
        for element_id in (
            "materialsCount",
            "materialsFavoriteCount",
            "materialsIncludedCount",
            "materialsChangedCount",
            "materialsRevision",
            "materialsDirtyBadge",
            "materialsSearch",
            "materialsFilter",
            "materialsSort",
            "includeVisibleMaterials",
            "excludeVisibleMaterials",
            "materialsList",
            "materialsSaveState",
            "reloadMaterials",
            "saveMaterials",
            "saveMaterialsAndRender",
            "materialsExportPdf",
            "materialsExportMenu",
            "materialsDownloadStandardPdf",
            "materialsDownloadWaterPdf",
        ):
            self.assertIn(element_id, parser.ids)
        self.assertIn("/static/start-stop-materials.js", parser.scripts)
        self.assertIn("星标用于长期收藏关键材料", page)
        self.assertIn('materialsRequest("/api/start-stop/materials"', script)
        self.assertIn("dataset_fingerprint", script)
        self.assertIn("expected_revision", script)
        self.assertIn("plot_name", script)
        self.assertIn("include_in_summary_atlas", script)
        self.assertIn("favorite", script)
        self.assertIn("config-favorite-button", script)
        self.assertIn('filter === "favorite"', script)
        self.assertIn('sortMode === "favorites_first"', script)
        self.assertIn("notes", script)
        self.assertIn("ordered_source_files", script)
        self.assertIn("完整绘图名称不能为空", script)
        self.assertIn("完整绘图名称不能重复", script)
        self.assertIn("至少需要选择 1 种材料", script)
        self.assertIn("error.status === 409", script)
        self.assertIn('body: JSON.stringify({ action: "render" })', script)
        self.assertIn("生成 PDF 图集", page)
        self.assertIn("export_pdf: true", script)
        self.assertIn('render_data_mode: "both"', script)
        self.assertNotIn("exportMenuButton", (STATIC / "start-stop-config.html").read_text(encoding="utf-8"))
        self.assertIn("beforeunload", script)
        self.assertNotIn("selectedSeries", script)
        self.assertNotIn("highlightedSeries", script)

    def test_cv_eis_page_is_source_backed_and_exposes_ir_compensated_return_scan(self) -> None:
        parser = self.parse_page("start-stop-cv-eis.html")
        page = (STATIC / "start-stop-cv-eis.html").read_text(encoding="utf-8")
        script = (STATIC / "start-stop-cv-eis.js").read_text(encoding="utf-8")
        css = (STATIC / "start-stop-cv-eis.css").read_text(encoding="utf-8")
        for element_id in (
            "cvEisCvCount",
            "cvEisEisCount",
            "cvEisPairedCount",
            "cvEisReadyCount",
            "cvEisAttentionCount",
            "cvEisSearch",
            "cvEisStatusFilter",
            "cvEisSort",
            "cvEisMaterialList",
            "cvEisChart",
            "showCvRaw",
            "showCvIr",
            "cvEisTarget",
            "cvEisAutoScale",
            "cvEisOverpotentialCards",
            "cvEisEvidence",
        ):
            self.assertIn(element_id, parser.ids)
        self.assertIn("/static/start-stop-cv-eis.css", parser.stylesheets)
        self.assertIn("/static/start-stop-cv-eis.js", parser.scripts)
        self.assertIn("E<sub>corr</sub> = E<sub>meas</sub> − 0.90 × j × Rs", page)
        self.assertIn("E<sub>RHE</sub> = E<sub>Hg/HgO</sub> + 0.9268 V", page)
        self.assertIn('cvEisRequest("/api/start-stop/cv-eis")', script)
        self.assertIn("/api/start-stop/cv-eis/curve?analysis_id=", script)
        self.assertIn("raw_e_rhe_v", script)
        self.assertIn("ir90_e_rhe_v", script)
        self.assertIn("recordIsFavorite", script)
        self.assertIn('addEventListener("contextmenu"', script)
        self.assertIn('addEventListener("wheel"', script)
        self.assertIn("beginCvEisDrag", script)
        self.assertIn("resetCvEisZoom", script)
        self.assertIn("@media print", css)

    def test_start_stop_program_uses_readable_responsive_typography(self) -> None:
        shared_css = (STATIC / "start-stop.css").read_text(encoding="utf-8")
        config_css = (STATIC / "start-stop-config.css").read_text(encoding="utf-8")
        for page_name in ("start-stop.html", "start-stop-config.html", "start-stop-workstations.html", "start-stop-cv-eis.html", "start-stop-materials.html"):
            page = (STATIC / page_name).read_text(encoding="utf-8")
            self.assertIn('<body class="start-stop-app ', page)
        self.assertIn(".start-stop-app .workbench-page-heading h1", shared_css)
        self.assertIn(".start-stop-app :is(button, input, select, textarea)", shared_css)
        self.assertIn("@media (max-width: 1180px)", shared_css)
        self.assertIn("grid-template-columns: 1fr", shared_css)
        self.assertIn(".data-operations-heading h2", config_css)
        self.assertIn("font-size: 24px", config_css)
        self.assertIn("@media (max-width: 560px)", config_css)

    def test_start_stop_pages_show_the_live_service_version(self) -> None:
        shared_css = (STATIC / "start-stop.css").read_text(encoding="utf-8")
        for page_name, script_name in (
            ("start-stop-config.html", "start-stop-config.js"),
            ("start-stop-workstations.html", "start-stop-workstations.js"),
            ("start-stop.html", "start-stop.js"),
            ("start-stop-cv-eis.html", "start-stop-cv-eis.js"),
            ("start-stop-materials.html", "start-stop-materials.js"),
        ):
            parser = self.parse_page(page_name)
            script = (STATIC / script_name).read_text(encoding="utf-8")
            with self.subTest(page=page_name):
                self.assertIn("workbenchVersion", parser.ids)
                self.assertIn("版本读取中", (STATIC / page_name).read_text(encoding="utf-8"))
                self.assertIn("service?.version", script)
                self.assertIn("service?.image_reference", script)
                self.assertIn("版本未知", script)
        self.assertIn(".start-stop-app .workbench-version-badge", shared_css)

    def test_start_stop_program_has_its_own_five_page_navigation(self) -> None:
        for name, active_path in (
            ("start-stop-config.html", "/start-stop"),
            ("start-stop-workstations.html", "/start-stop/workstations"),
            ("start-stop.html", "/start-stop/analysis"),
            ("start-stop-cv-eis.html", "/start-stop/cv-eis"),
            ("start-stop-materials.html", "/start-stop/materials"),
        ):
            with self.subTest(page=name):
                parser = self.parse_page(name)
                self.assertEqual(
                    [link.get("href") for link in parser.links],
                    ["/start-stop", "/start-stop/workstations", "/start-stop/analysis", "/start-stop/cv-eis", "/start-stop/materials"],
                )
                active = [
                    link for link in parser.links
                    if link.get("aria-current") == "page"
                ]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0].get("href"), active_path)
                page = (STATIC / name).read_text(encoding="utf-8")
                self.assertIn('class="workbench-nav-number">01', page)
                self.assertIn('class="workbench-nav-number">02', page)
                self.assertIn('class="workbench-nav-number">03', page)
                self.assertIn('class="workbench-nav-number">04', page)
                self.assertIn('class="workbench-nav-number">05', page)
                self.assertNotIn("电化学测试工作台", page)

    def test_start_stop_pages_share_one_persistent_collapsible_sidebar(self) -> None:
        shell = (STATIC / "start-stop-shell.js").read_text(encoding="utf-8")
        for name in ("start-stop-config.html", "start-stop-workstations.html", "start-stop.html", "start-stop-cv-eis.html", "start-stop-materials.html"):
            with self.subTest(page=name):
                parser = self.parse_page(name)
                page = (STATIC / name).read_text(encoding="utf-8")
                self.assertEqual(parser.ids.count("workbenchSidebarToggle"), 1)
                self.assertEqual(parser.ids.count("workbenchNavigation"), 1)
                self.assertIn('aria-controls="workbenchNavigation"', page)
                self.assertIn("/static/start-stop-shell.js", parser.scripts)
        self.assertIn("start-stop.sidebar.collapsed.v1", shell)
        self.assertIn("window.matchMedia", shell)
        self.assertIn('event.key !== "Escape"', shell)
        self.assertIn('window.addEventListener("storage"', shell)
        self.assertIn("toggle.focus", shell)

    def test_start_stop_scripts_only_bind_ids_present_on_their_own_page(self) -> None:
        for html_name, script_name, declaration in (
            ("start-stop.html", "start-stop.js", "const elements = Object.fromEntries("),
            (
                "start-stop-config.html",
                "start-stop-config.js",
                "const configElements = Object.fromEntries(",
            ),
            (
                "start-stop-cv-eis.html",
                "start-stop-cv-eis.js",
                "const cvEisElements = Object.fromEntries(",
            ),
            (
                "start-stop-materials.html",
                "start-stop-materials.js",
                "const materialsElements = Object.fromEntries(",
            ),
        ):
            with self.subTest(page=html_name):
                parser = self.parse_page(html_name)
                script = (STATIC / script_name).read_text(encoding="utf-8")
                block = script.split(declaration, 1)[1].split("].map", 1)[0]
                declared_ids = set(re.findall(r'"([A-Za-z][A-Za-z0-9]*)"', block))
                self.assertTrue(declared_ids)
                self.assertEqual(declared_ids - set(parser.ids), set())

    def test_analysis_frontend_enforces_p0_data_and_scientific_safety_gates(self) -> None:
        parser = self.parse_page("analysis.html")
        page = (STATIC / "analysis.html").read_text(encoding="utf-8")
        script = (STATIC / "analysis.js").read_text(encoding="utf-8")
        css = (STATIC / "styles.css").read_text(encoding="utf-8")

        self.assertIn("fileScopeFilter", parser.ids)
        self.assertIn('<option value="experiment">实验数据</option>', page)
        self.assertIn('<option value="other">其他文件（控制 / 诊断 / 拒绝）</option>', page)
        self.assertIn("const folderParts = pathParts.slice(0, -1)", script)
        self.assertIn('"control_programs", "diagnostics", "rejected_data"', script)
        self.assertIn('part.startsWith("diag_")', script)
        self.assertIn('part.startsWith("diagnostic_")', script)
        self.assertIn("latestAnalyzableRun", script)
        self.assertIn('run.parse_status === "parsed"', script)
        self.assertIn("Number(run.point_count) > 0", script)
        self.assertIn("Boolean(analysisTypeForRun(run))", script)
        self.assertIn(
            'elements.fileScopeFilter.addEventListener("change", () => loadFileTree())',
            script,
        )
        self.assertIn(
            'elements.techniqueFilter.addEventListener("change", () => loadFileTree())',
            script,
        )
        self.assertNotIn("loadFileTree(false)", script)

        self.assertIn('<option value="unknown" selected>', page)
        self.assertIn('value="0" disabled required', page)
        self.assertNotIn('value="85"', page)
        self.assertIn("syncOfflineCompensation", script)
        self.assertIn("syncResistanceRequirement", script)
        self.assertIn("solution_resistance_ohm: solutionResistance", script)
        self.assertIn('<option value="auto">自动识别（预览后选择具体数据段）</option>', page)
        self.assertIn("cvBranchIdentifier", script)
        self.assertIn("potential_range_v", script)
        self.assertIn("电位递增", script)
        self.assertIn("请选择数据段", script)
        self.assertNotIn("<option value=\"forward\">正扫</option>", page)
        self.assertNotIn("<option value=\"reverse\">回扫</option>", page)

        self.assertIn('qualityLevel === "not_calculable"', script)
        self.assertIn("保存为筛查记录", script)
        self.assertIn("结果质量：", script)
        self.assertIn("resultQuality.reasons", script)
        self.assertIn("可定量", script)
        self.assertIn("仅筛查", script)
        self.assertIn("不可计算", script)
        self.assertIn(".analysis-quality.quality-screening", css)
        self.assertIn(".analysis-quality.quality-not_calculable", css)

    def test_analysis_page_omits_deferred_summary_and_context_regions(self) -> None:
        page = (STATIC / "analysis.html").read_text(encoding="utf-8")
        script = (STATIC / "analysis.js").read_text(encoding="utf-8")
        for text in (
            'class="hero-grid"',
            'id="metadataForm"',
            'id="auditList"',
            "样品信息",
            "最近活动",
            "搜索文件夹、文件或样品",
        ):
            self.assertNotIn(text, page)
        self.assertNotIn("file-sample", script)
        self.assertNotIn("loadStatus", script)
        self.assertNotIn("loadAudit", script)

    def test_data_folders_are_managed_from_environment_settings(self) -> None:
        analysis = (STATIC / "analysis.html").read_text(encoding="utf-8")
        monitor = (STATIC / "monitor.html").read_text(encoding="utf-8")
        script = (STATIC / "analysis.js").read_text(encoding="utf-8")
        self.assertNotIn("监控目录", analysis)
        self.assertNotIn('id="instrumentFilter"', analysis)
        self.assertIn('id="dataFolderList"', monitor)
        self.assertIn('id="dataFolderSummary"', monitor)
        self.assertIn("/api/files/tree", script)
        self.assertIn("parser-badge", script)

    def test_workspace_pages_do_not_duplicate_element_ids(self) -> None:
        for name in self.pages:
            with self.subTest(page=name):
                ids = self.parse_page(name).ids
                self.assertEqual(len(ids), len(set(ids)))

    def test_sidebar_is_desktop_sticky_and_content_uses_a_two_column_shell(self) -> None:
        css = (STATIC / "workbench.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: var(--workbench-sidebar)", css)
        self.assertIn("position: sticky", css)
        self.assertIn("height: 100vh", css)
        self.assertIn('aria-current="page"', (
            STATIC / "index.html"
        ).read_text(encoding="utf-8"))

    def test_environment_settings_reflects_the_control_lock(self) -> None:
        monitor = (STATIC / "monitor.html").read_text(encoding="utf-8")
        script = (STATIC / "monitor.js").read_text(encoding="utf-8")
        self.assertIn('id="sidebarControlState"', monitor)
        self.assertIn("elements.sidebarControlState.textContent", script)


if __name__ == "__main__":
    unittest.main()
