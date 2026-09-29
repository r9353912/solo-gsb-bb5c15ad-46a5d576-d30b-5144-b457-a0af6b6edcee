"""多来源 JSON 事件记录导入器。

接受两种顶层形态：
1. {"sources": [...], "events": [...], "constraints": [...]}
2. 顶层事件数组 [ {...}, {...} ]

字段名做宽松别名映射（time/ts/timestamp、source/src、type/kind ...），
无法解析的时间戳会被拒绝并在 summary.rejected 中说明，不会静默入库。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .db import db
from .timeutil import to_unix_seconds

MAX_EVENTS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _as_float(v, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- sources

def _normalize_source(raw: dict) -> dict:
    sid = str(_pick(raw, "id", "source_id", "name", default="")).strip()
    return {
        "id": sid,
        "name": str(_pick(raw, "name", "label", default=sid)),
        "clock_offset": _as_float(_pick(raw, "clock_offset", "offset", "skew"), 0.0),
        "drift_ppm": _as_float(_pick(raw, "drift_ppm", "drift"), 0.0),
        "uncertainty": max(0.0, _as_float(_pick(raw, "uncertainty", "jitter", "noise"), 1.0)),
        "reliability": min(1.0, max(0.0, _as_float(_pick(raw, "reliability", "trust", "weight"), 0.8))),
        "kind": str(_pick(raw, "kind", "type", default="external")),
    }


def _upsert_source(conn, s: dict, warnings: list[str]) -> None:
    row = conn.execute("SELECT id, name FROM sources WHERE id=?", (s["id"],)).fetchone()
    now = _now()
    if row is None:
        conn.execute(
            "INSERT INTO sources(id,name,clock_offset,drift_ppm,uncertainty,reliability,kind,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (s["id"], s["name"], s["clock_offset"], s["drift_ppm"], s["uncertainty"],
             s["reliability"], s["kind"], now, now),
        )
    else:
        # 已存在的来源（审计员可能已调整偏差假设）：不覆盖假设，只补名称。
        if not row["name"] and s["name"]:
            conn.execute("UPDATE sources SET name=?, updated_at=? WHERE id=?",
                         (s["name"], now, s["id"]))


# ----------------------------------------------------------------- events

def _normalize_event(raw: dict, source_index: dict) -> tuple[dict | None, str | None]:
    local_ts = to_unix_seconds(_pick(raw, "time", "ts", "timestamp", "local_time",
                                    "local_ts", "datetime", "occurred_at", "@timestamp"))
    if local_ts is None:
        return None, f"记录 {raw.get('id', raw.get('event_id', '?'))} 缺少无法解析的时间戳"

    source_ref = _pick(raw, "source", "source_id", "src", "host", "node", "origin")
    source_id = str(source_ref) if source_ref is not None else "unknown"
    # 允许用 {"id":..., "name":...} 形式内联来源
    if isinstance(source_ref, dict):
        source_id = str(_pick(source_ref, "id", "name", default="unknown"))

    eid = _pick(raw, "id", "event_id", "eid", "uuid")
    if eid is None:
        eid = _stable_id("evt", source_id, local_ts,
                         _pick(raw, "type", "event_type", "kind", default="event"),
                         json.dumps(raw, sort_keys=True, ensure_ascii=False))
    eid = str(eid)

    seq = _pick(raw, "seq", "sequence", "index", "counter", "log_offset")
    seq = int(seq) if isinstance(seq, (int, float)) and not isinstance(seq, bool) else None

    return {
        "id": eid,
        "source_id": source_id,
        "seq": seq,
        "local_ts": local_ts,
        "ts_text": str(_pick(raw, "time", "ts", "timestamp", default="")),
        "event_type": str(_pick(raw, "type", "event_type", "kind", "action", default="event")),
        "actor": _as_str(_pick(raw, "actor", "user", "subject")),
        "summary": str(_pick(raw, "summary", "message", "msg", "description", "title",
                             default="")),
        "raw": raw,
    }, None


def _as_str(v) -> str | None:
    return None if v is None else str(v)


# ------------------------------------------------------------ constraints

def _normalize_constraint(raw: dict) -> tuple[tuple | None, str | None]:
    frm = _pick(raw, "from", "from_event", "before", "earlier", "cause")
    to = _pick(raw, "to", "to_event", "after", "later", "effect")
    if not frm or not to:
        return None, f"约束缺少端点: {raw}"
    kind = str(_pick(raw, "kind", "type", default="causal"))
    return (str(frm), str(to), kind, str(_pick(raw, "note", "reason", default=""))), None


# ------------------------------------------------------------------ 入口

def import_payload(payload: Any) -> dict[str, Any]:
    """导入一份 JSON 文档，返回 {sources_added, events_imported, rejected, warnings}。"""
    warnings: list[str] = []
    rejected: list[dict] = []

    if isinstance(payload, list):
        payload = {"events": payload}
    if not isinstance(payload, dict):
        raise ValueError("顶层必须是对象或事件数组")

    raw_sources = payload.get("sources", []) or []
    if isinstance(raw_sources, dict):  # 允许 {"gateway": {...}}
        raw_sources = [{"id": k, **(v if isinstance(v, dict) else {})}
                       for k, v in raw_sources.items()]
    raw_events = payload.get("events", payload.get("records", [])) or []
    raw_constraints = payload.get("constraints", payload.get("links", [])) or []

    sources = [_normalize_source(s) for s in raw_sources if isinstance(s, dict)]

    events: list[dict] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            rejected.append({"raw": str(raw)[:200], "reason": "不是对象"})
            continue
        ev, err = _normalize_event(raw, {})
        if ev is None:
            rejected.append({"raw": str(raw)[:200], "reason": err})
            continue
        events.append(ev)

    constraints: list[tuple] = []
    for raw in raw_constraints:
        if not isinstance(raw, dict):
            continue
        c, err = _normalize_constraint(raw)
        if c is None:
            warnings.append(err or "约束被忽略")
        else:
            constraints.append(c)

    if len(events) > MAX_EVENTS:
        raise ValueError(f"事件数量超过上限 {MAX_EVENTS}")

    now = _now()
    with db() as conn:
        known_before = {r["id"] for r in conn.execute("SELECT id FROM sources")}
        known_events = {r["id"] for r in conn.execute("SELECT id FROM events")}

        # 事件里出现但未声明的来源：自动登记，偏差假设留待审计员修正。
        declared = {s["id"] for s in sources}
        for ev in events:
            if ev["source_id"] not in declared:
                sources.append({
                    "id": ev["source_id"], "name": ev["source_id"],
                    "clock_offset": 0.0, "drift_ppm": 0.0,
                    "uncertainty": 1.0, "reliability": 0.8, "kind": "inferred",
                })
                declared.add(ev["source_id"])

        sources_added = 0
        for s in sources:
            if s["id"] not in known_before:
                sources_added += 1
            _upsert_source(conn, s, warnings)

        events_added = 0
        for ev in events:
            if ev["id"] in known_events:
                continue  # 幂等：重复导入不覆盖
            conn.execute(
                "INSERT INTO events(id,source_id,seq,local_ts,ts_text,event_type,actor,summary,raw,"
                "imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ev["id"], ev["source_id"], ev["seq"], ev["local_ts"], ev["ts_text"],
                 ev["event_type"], ev["actor"], ev["summary"],
                 json.dumps(ev["raw"], ensure_ascii=False), now),
            )
            events_added += 1

        # 重建“同来源时序边”：同来源事件天然按本地时钟严格有序。
        _rebuild_source_edges(conn, now)

        # 用户/外部声明的约束（端点必须存在，悬空则报警）。
        constraints_added = 0
        for frm, to, kind, note in constraints:
            if frm not in known_events and not any(e["id"] == frm for e in events):
                warnings.append(f"约束引用了不存在的事件 {frm}")
                continue
            if to not in known_events and not any(e["id"] == to for e in events):
                warnings.append(f"约束引用了不存在的事件 {to}")
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO constraints(from_event,to_event,kind,note,created_at) "
                "VALUES(?,?,?,?,?)", (frm, to, kind, note, now))
            constraints_added += cur.rowcount

    return {
        "sources_total": len(sources),
        "sources_added": sources_added,
        "events_total": len(events),
        "events_added": events_added,
        "constraints_added": constraints_added,
        "rejected": rejected,
        "warnings": warnings,
    }


def _rebuild_source_edges(conn, now: str) -> None:
    """为每个来源按 (seq 优先, 否则 local_ts) 生成严格时序边。"""
    conn.execute("DELETE FROM constraints WHERE kind='source_order'")
    rows = conn.execute("SELECT id, source_id, seq, local_ts FROM events").fetchall()
    by_source: dict[str, list] = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append(r)
    for evs in by_source.values():
        evs.sort(key=lambda r: (r["seq"] if r["seq"] is not None else 1 << 62, r["local_ts"]))
        for a, b in zip(evs, evs[1:]):
            if a["local_ts"] == b["local_ts"]:
                continue  # 同刻戳无法声明严格顺序
            conn.execute(
                "INSERT OR IGNORE INTO constraints(from_event,to_event,kind,note,created_at) "
                "VALUES(?,?,?,?,?)",
                (a["id"], b["id"], "source_order", "同一来源本地时钟严格有序", now),
            )


def reset_all() -> None:
    with db() as conn:
        conn.execute("DELETE FROM constraints")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM scenarios")
