"""Read-only retention preview for regenerable analysis generations.

SQLite provenance is immutable. This module produces an archival plan, never
deletes rows or promises that logical generation sizes equal reclaimable space.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Iterable


def artifact_retention_plan(connection, *, keep_render: int = 3, keep_scan: int = 2,
                            keep_days: int = 7, pinned_ids: Iterable[int] = (),
                            now: dt.datetime | None = None) -> dict[str, Any]:
    for value in (keep_render, keep_scan):
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError("保留代数必须为 1–100。")
    if type(keep_days) is not int or not 0 <= keep_days <= 3650:
        raise ValueError("保留天数必须为 0–3650。")
    pins = set(pinned_ids)
    if any(type(value) is not int or value <= 0 for value in pins) or len(pins) > 100:
        raise ValueError("回退版本编号无效，最多固定 100 个版本。")
    current_time = now or dt.datetime.now(dt.timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("保留计划时间必须包含时区。")
    generations = [dict(row) for row in connection.execute(
        "SELECT id,kind,snapshot_id,config_revision,created_utc,file_count,total_bytes FROM artifact_generations ORDER BY id DESC"
    )]
    by_id = {row["id"]: row for row in generations}
    if pins - set(by_id):
        raise ValueError("指定的回退版本不存在。")
    reasons: dict[int, list[str]] = defaultdict(list)
    for row in connection.execute("SELECT s.generation_id FROM artifact_current c JOIN artifact_selections s ON s.id=c.selection_id"):
        reasons[int(row[0])].append("current")
    for identifier in pins:
        reasons[identifier].append("pinned")
    kind_seen: dict[str, int] = defaultdict(int)
    for row in generations:
        identifier, kind = int(row["id"]), str(row["kind"])
        kind_seen[kind] += 1
        limit = keep_render if kind == "render" else keep_scan
        if kind_seen[kind] <= limit:
            reasons[identifier].append("recent_generation")
        try:
            created = dt.datetime.fromisoformat(row["created_utc"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise ValueError("timezone missing")
            if current_time - created <= dt.timedelta(days=keep_days):
                reasons[identifier].append("recent_date")
        except (TypeError, ValueError):
            reasons[identifier].append("unverified_date")
    candidates = {identifier for identifier in by_id if not reasons[identifier]}
    source_blobs = {int(row[0]) for row in connection.execute("SELECT DISTINCT blob_id FROM source_versions")}
    references: dict[int, set[int]] = defaultdict(set)
    sizes: dict[int, int] = {}
    for row in connection.execute("SELECT a.blob_id,a.generation_id,b.size_bytes FROM artifact_entries a JOIN content_blobs b ON b.id=a.blob_id"):
        references[int(row[0])].add(int(row[1]))
        sizes[int(row[0])] = int(row[2])
    exclusive = {blob for blob, refs in references.items() if refs <= candidates and blob not in source_blobs}
    return {
        "schema_version": 1, "mode": "archive_preview", "read_only": True,
        "generated_utc": current_time.isoformat(timespec="seconds"),
        "policy": {"keep_render":keep_render,"keep_scan":keep_scan,"keep_days":keep_days,"pinned_ids":sorted(pins)},
        "generation_count":len(generations),"protected_count":len(generations)-len(candidates),
        "candidate_count":len(candidates),
        "candidate_logical_bytes":sum(by_id[i]["total_bytes"] for i in candidates),
        "candidate_exclusive_blob_bytes":sum(sizes[i] for i in exclusive),
        "direct_delete_allowed":False,
        "message":"这是只读保留计划。当前结果、指定回退版本和所有原始来源均受保护；历史产物有不可变溯源引用，需确认归档方案后才能回收物理空间。",
        "generations":[{**row,"protected":bool(reasons[row["id"]]),"reasons":reasons[row["id"]]} for row in generations],
    }
