"""Reuse a source-bound reference fit and independently corrected material tables."""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from pathlib import Path

import pandas as pd

from material_result_cache import MaterialResultCache, identity, prune_computed_cache


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _load_model(path, key):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        model = saved["model"]
        if saved["key"] != key or saved["sha256"] != hashlib.sha256(_encoded(model)).hexdigest():
            return None
        for field in ("area_specific_resistance_drift_ohm_cm2_per_h", "reference_fit_slope_v_per_h",
                      "reference_fit_r_squared", "reference_fit_intercept_v"):
            if not math.isfinite(float(model[field])):
                return None
        if model["reference_fit_slope_v_per_h"] >= 0 or model["reference_fit_r_squared"] < 0.99:
            return None
        return model
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_model(path, key, model):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(_encoded({"key": key, "model": model,
                                       "sha256": hashlib.sha256(_encoded(model)).hexdigest()}))
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def compensate_with_cache(engine, specs, frames, root, algorithm):
    root = Path(root) / "water"
    if root.is_symlink():
        raise ValueError("补偿缓存不能是符号链接")
    references = [spec for spec in specs if spec["material_relative_path"] == engine.WATER_COMP_REFERENCE_KEY]
    if len(references) != 1:
        # Keep exactly the scientific routine's missing/ambiguous reference gate.
        engine.fit_water_compensation_model(frames[0], frames[3])
        raise RuntimeError("补偿参照来源与材料清单不一致")
    reference = references[0]
    model_key = identity(reference, algorithm)
    model_path = root / (model_key + ".json")
    model = _load_model(model_path, model_key)
    reused_model = model is not None
    if model is None:
        model = engine.fit_water_compensation_model(frames[0], frames[3])
        try:
            _save_model(model_path, model_key, model)
        except OSError:
            pass  # A valid analysis must not fail on an optional cache write.
    model = dict(model)
    model.update(reference_series_id=reference["series_id"],
                 reference_material=reference["material_display_name"],
                 reference_material_display_name=reference["material_display_name"],
                 reference_source_version=model_key)
    cache = MaterialResultCache(root / "materials", algorithm + ":" + model_key, frame_count=4)
    partitions = [[] for _ in range(4)]
    hits = 0
    for index, spec in enumerate(specs):
        engine.report_render_progress(
            phase="calculating_water", phase_label="增量水位补偿", phase_index=5,
            percent=80 + 2 * index / max(len(specs), 1), completed=index, total=len(specs),
            unit="materials", current_item=spec["series_display_name"],
            detail="按材料来源及参照模型版本复用补偿结果；参照变化会使全部补偿缓存失效",
        )
        result = cache.load(spec)
        if result is None:
            source = [frame[frame.series_id == spec["series_id"]].copy() for frame in frames]
            result = engine.apply_water_compensation(*source, model)
            cache.store(spec, result)
        else:
            hits += 1
        for bucket, frame in zip(partitions, result):
            bucket.append(frame)
    combined = tuple(pd.concat(parts, ignore_index=True) for parts in partitions)
    prune_computed_cache(root.parent)
    return model, combined, {"reference_model_reused": reused_model, "reference_source_version": model_key,
                             "series_cache_hits": hits, "series_recalculated": len(specs) - hits}
