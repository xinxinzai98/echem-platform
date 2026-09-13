"""PDF rendering from sealed analysis tables, without parsing or recalculating sources."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd


def read_table(root, schemas, relative):
    columns = schemas[relative]
    na = {name:[""] for name, dtype in columns.items() if dtype != "object"}
    return pd.read_csv(root / relative, dtype=columns, keep_default_na=False,
                       na_values=na, float_precision="round_trip", encoding="utf-8-sig")


def export_existing_analysis(engine, data_mode):
    root = engine.OUTPUT_DIR
    schemas = json.loads((root/"analysis_table_schema.json").read_text(encoding="utf-8"))
    summary = json.loads((root/"analysis_summary.json").read_text(encoding="utf-8"))
    read = lambda name: read_table(root, schemas, name)
    series = read("series_summary_raw.csv")
    chosen = series[series["include_in_summary_atlas"]].copy()
    ids = set(chosen.series_id)
    selected = lambda frame: frame[frame.series_id.isin(ids)].copy()
    material = read("material_summary_raw.csv")
    segments = read("segment_summary_raw.csv")
    engine.configure_static_figure_export(False)
    if data_mode in {"raw","both"}:
        engine.render_all_figures(
            selected(read("overview_downsampled_raw.csv")), selected(read("cycle_summary_raw.csv")),
            selected(read("representative_cycles_raw.csv")), chosen,
            material[material["include_in_summary_atlas"]].copy(), selected(segments),
            selected(read("segment_boundaries.csv")),
        )
    if data_mode in {"water","both"}:
        model=json.loads((root/"water_compensation/water_compensation_model.json").read_text(encoding="utf-8"))
        cycles=read("water_compensation/cycle_summary_water_compensated.csv")
        water_series=selected(read("water_compensation/series_summary_water_compensated.csv"))
        selected_cycles=selected(cycles)
        engine.configure_matplotlib()
        engine.begin_figure_progress(phase="rendering_water",phase_label="导出已有水位补偿结果",phase_index=5,
                                     total=4+len(water_series),percent_start=80,percent_end=94,
                                     detail="直接从已完成分析生成 PDF，不重新拟合补偿模型")
        with engine.PdfPages(root/"启停数据_水位补偿图集.pdf") as pdf:
            engine.plot_water_compensation_model(cycles,model,pdf)
            engine.plot_water_compensation_facets(selected_cycles,water_series,model,pdf)
            engine.plot_water_compensation_overlay(selected_cycles,water_series,model,pdf)
            engine.plot_water_compensation_shift_comparison(water_series,pdf)
            engine.plot_water_compensation_series_details(selected_cycles,water_series,segments,model,pdf)
    summary["pdf_generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    summary.setdefault("render_options",{})["export_pdf"] = True
    summary["export_mode"] = "existing_analysis"
    (root/"analysis_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"stage":"PDF 已从已完成分析结果生成", "pdf_exported":True, "analysis_reused":True,
            "materials_analyzed":0,"materials_skipped_unchanged":len(material),
            "analysis_series":len(series),"materials_available":len(material)}
