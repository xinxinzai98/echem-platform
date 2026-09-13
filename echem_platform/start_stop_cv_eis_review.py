"""CV/EIS pairing policy and immutable local review decisions.

Uses the existing append-only audit log; no schema migration or experimental
source mutation is needed. A decision is bound to immutable source version IDs.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Iterable

REVIEW_ACTION = "start_stop_cv_eis_review"
MAX_PAIR_AGE = dt.timedelta(hours=24)
MIN_PAIR_SCORE = 300
PAIR_SCORE_MARGIN = 20
EXPLICIT_STAGES = frozenset({"pre", "post", "her", "sequence"})


def incompatible_stages(cv: Any, eis: Any) -> bool:
    return cv.stage in EXPLICIT_STAGES and eis.stage in EXPLICIT_STAGES and cv.stage != eis.stage


def pair_time_known_and_close(cv: Any, eis: Any) -> bool:
    try:
        left = dt.datetime.fromisoformat(cv.source_modified_utc.replace("Z", "+00:00"))
        right = dt.datetime.fromisoformat(eis.source_modified_utc.replace("Z", "+00:00"))
        return left.tzinfo is not None and right.tzinfo is not None and abs(right - left) <= MAX_PAIR_AGE
    except (ValueError, TypeError):
        return False


def select_eis(cv: Any, candidates: Iterable[Any], score: Any, reviewed_id: int | None = None):
    candidates = list(candidates)
    eligible = [item for item in candidates if not incompatible_stages(cv, item)]
    if reviewed_id is not None:
        selected = next((item for item in eligible if item.source_version_id == reviewed_id), None)
        if selected is not None:
            return selected, "reviewed", ""
        return None, "pairing_required", "已确认的 EIS 不再可用或与 CV 测试阶段冲突，请重新确认。"
    if not candidates:
        return None, "missing_eis", "同一材料目录没有受支持的 EIS 文本。"
    ranked = sorted(eligible, key=lambda item: (-score(cv, item), item.source_version_id))
    if not ranked:
        return None, "pairing_required", "EIS 与 CV 的前测、后测或测试阶段不相容，不能自动配对。"
    best = ranked[0]
    if not pair_time_known_and_close(cv, best):
        return None, "pairing_required", "CV/EIS 测试时间缺失或间隔超过 24 小时，请确认 EIS 来源。"
    if score(cv, best) < MIN_PAIR_SCORE:
        return None, "pairing_required", "CV/EIS 文件名与阶段配对证据不足，请确认 EIS 来源。"
    if len(ranked) > 1 and score(cv, best) - score(cv, ranked[1]) < PAIR_SCORE_MARGIN:
        return None, "pairing_required", "存在多个相近的 EIS 候选，请选择实际对应的测试。"
    return best, "automatic", ""


def read_reviews(connection) -> dict[str, Any]:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone():
        return {"revision": 0, "reviews": {}}
    rows = connection.execute(
        "SELECT id,detail_json FROM audit_log WHERE action=? ORDER BY id", (REVIEW_ACTION,)
    )
    result: dict[str, Any] = {"revision": 0, "reviews": {}}
    for row in rows:
        payload = json.loads(row[1])
        result["revision"] = int(row[0])
        result["reviews"][str(payload["cv_source_version_id"])] = payload
    return result


def save_review(connection, *, expected_revision: int, payload: dict[str, Any]) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    current = read_reviews(connection)
    if current["revision"] != expected_revision:
        connection.rollback()
        raise ValueError("CV/EIS 确认记录已更新，请刷新后重试。")
    required_ids = [payload["cv_source_version_id"]]
    if payload.get("eis_source_version_id") is not None:
        required_ids.append(payload["eis_source_version_id"])
    for identifier in required_ids:
        if not connection.execute(
            """SELECT 1 FROM source_current c JOIN source_selections s ON s.id=c.selection_id
            WHERE s.source_version_id=?""", (identifier,)
        ).fetchone():
            connection.rollback()
            raise ValueError("CV/EIS 来源在确认期间已更新，请重新读取后确认。")
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    recorded = {**payload, "confirmed_utc": timestamp}
    connection.execute(
        "INSERT INTO audit_log(created_utc,action,target,detail_json) VALUES(?,?,?,?)",
        (timestamp, REVIEW_ACTION, str(payload["cv_source_version_id"]),
         json.dumps(recorded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )
    connection.commit()
    return read_reviews(connection)
