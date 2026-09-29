"""JSON 导入批次的查看与确认撤回。

每次 :func:`app.importer.import_payload` 都形成一个批次，并把**实际写入**
的来源 / 事件 / 声明约束记入台账（``batch_sources`` / ``batch_events`` /
``batch_constraints``）。重复导入未写入的 ID 不进台账，因此撤回时不可能
被误删；``source_order`` 时序边是按来源全量派生的数据，不属于任何批次，
撤回后按受影响来源重建。

撤回安全原则
------------
撤回只删除“本批次实际写入、且当前仍存在”的记录，导入前数据原样保留。
出现以下**无法安全判断归属 / 会牵连批外数据**的情形时拒绝撤回并逐条说明：

1. 批次不存在或已撤回；
2. 批次登记的来源下仍存在**不属于任何批次**的事件（归属不可判断）；
3. 删除本批次事件会级联删掉**非本批次**的声明约束（审计员手工或别的
   批次建立的边）——要求审计员先手工处理，系统不替他删除。
``source_order`` 边不参与上述判定，它们在删除后统一重建。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import db
from .importer import _rebuild_source_edges


class BatchError(ValueError):
    """批次操作被拒绝（不存在 / 已撤回 / 归属不安全）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _marks(ids: list) -> str:
    """IN 占位符；空列表给出永假子查询，保持 SQL 形态统一。"""
    return ",".join("?" for _ in ids) if ids else "SELECT 0 WHERE 0"


# --------------------------------------------------------------- 读取

def list_batches() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT b.id, b.label, b.status, b.summary, b.created_at, b.retracted_at, "
            "(SELECT COUNT(*) FROM batch_sources bs WHERE bs.batch_id=b.id) AS n_sources, "
            "(SELECT COUNT(*) FROM batch_events be WHERE be.batch_id=b.id) AS n_events, "
            "(SELECT COUNT(*) FROM batch_constraints bc WHERE bc.batch_id=b.id) AS n_constraints "
            "FROM import_batches b ORDER BY b.id DESC").fetchall()
        return [{
            "id": r["id"],
            "label": r["label"],
            "status": r["status"],
            "created_at": r["created_at"],
            "retracted_at": r["retracted_at"],
            "summary": json.loads(r["summary"] or "{}"),
            "claimed": {"sources": r["n_sources"], "events": r["n_events"],
                        "constraints": r["n_constraints"]},
        } for r in rows]


def _get_batch_row(conn, batch_id: int):
    row = conn.execute(
        "SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise BatchError(f"批次不存在: #{batch_id}")
    return row


def get_batch(batch_id: int) -> dict[str, Any]:
    """批次详情：台账声明的来源 / 事件 / 约束，逐项标注当前是否仍存在。"""
    with db() as conn:
        row = _get_batch_row(conn, batch_id)

        sources = [dict(r) for r in conn.execute(
            "SELECT bs.source_id AS id, (s.id IS NOT NULL) AS present, s.name AS name, "
            "(SELECT COUNT(*) FROM events e WHERE e.source_id=bs.source_id) AS event_count "
            "FROM batch_sources bs LEFT JOIN sources s ON s.id=bs.source_id "
            "WHERE bs.batch_id=? ORDER BY bs.source_id", (batch_id,))]
        events = [dict(r) for r in conn.execute(
            "SELECT be.event_id AS id, (e.id IS NOT NULL) AS present, "
            "e.source_id AS source_id, e.event_type AS event_type, e.actor AS actor, "
            "e.summary AS summary "
            "FROM batch_events be LEFT JOIN events e ON e.id=be.event_id "
            "WHERE be.batch_id=? ORDER BY be.event_id", (batch_id,))]
        constraints = [dict(r) for r in conn.execute(
            "SELECT bc.constraint_id AS id, (c.id IS NOT NULL) AS present, "
            "c.from_event AS from_event, c.to_event AS to_event, c.kind AS kind, "
            "c.note AS note "
            "FROM batch_constraints bc LEFT JOIN constraints c ON c.id=bc.constraint_id "
            "WHERE bc.batch_id=? ORDER BY bc.constraint_id", (batch_id,))]

    return {
        "id": row["id"],
        "label": row["label"],
        "status": row["status"],
        "created_at": row["created_at"],
        "retracted_at": row["retracted_at"],
        "summary": json.loads(row["summary"] or "{}"),
        "sources": sources,
        "events": events,
        "constraints": constraints,
    }


# --------------------------------------------------------------- 撤回预案

def _build_plan(conn, batch_id: int) -> dict[str, Any]:
    """在当前数据库状态上计算撤回预案（不写库）：删除 / 保留 / 悬空 + 阻断项。"""
    claimed_sources = [r["source_id"] for r in conn.execute(
        "SELECT source_id FROM batch_sources WHERE batch_id=? ORDER BY source_id",
        (batch_id,))]
    claimed_events = [r["event_id"] for r in conn.execute(
        "SELECT event_id FROM batch_events WHERE batch_id=? ORDER BY event_id",
        (batch_id,))]
    claimed_constraints = [r["constraint_id"] for r in conn.execute(
        "SELECT constraint_id FROM batch_constraints WHERE batch_id=? ORDER BY constraint_id",
        (batch_id,))]

    present_sources = {r["id"] for r in conn.execute(
        f"SELECT id FROM sources WHERE id IN ({_marks(claimed_sources)})",
        claimed_sources)} if claimed_sources else set()
    present_events = {r["id"] for r in conn.execute(
        f"SELECT id FROM events WHERE id IN ({_marks(claimed_events)})",
        claimed_events)} if claimed_events else set()
    present_constraint_rows = conn.execute(
        f"SELECT id, from_event, to_event, kind, note FROM constraints "
        f"WHERE id IN ({_marks(claimed_constraints)}) ORDER BY id",
        claimed_constraints).fetchall() if claimed_constraints else []
    present_constraints = {r["id"] for r in present_constraint_rows}

    dangling = {
        "sources": [s for s in claimed_sources if s not in present_sources],
        "events": [e for e in claimed_events if e not in present_events],
        "constraints": [c for c in claimed_constraints if c not in present_constraints],
    }

    blockers: list[dict[str, Any]] = []

    # 阻断项 1：本批次来源下，删除本批次事件后仍留下、且**不属于任何批次**
    # 的事件（导入前数据 / 手工录入）——归属不可判断，拒绝删来源。
    # 归属其它活跃批次的事件安全，只需保留来源。
    unowned_events: list[dict[str, str]] = []
    foreign_events: list[dict[str, Any]] = []
    if claimed_sources:
        rows = conn.execute(
            f"SELECT e.id, e.source_id, "
            f"(SELECT be.batch_id FROM batch_events be WHERE be.event_id=e.id) AS owner_batch "
            f"FROM events e WHERE e.source_id IN ({_marks(claimed_sources)}) ORDER BY e.id",
            claimed_sources).fetchall()
        active_other = {r["id"] for r in conn.execute(
            "SELECT id FROM import_batches WHERE status='active' AND id<>?", (batch_id,))}
        for r in rows:
            if r["id"] in present_events:
                continue  # 本批次自己的事件，将被删除
            owner = r["owner_batch"]
            if owner is None or owner == batch_id:
                unowned_events.append({"id": r["id"], "source_id": r["source_id"]})
            elif owner in active_other:
                foreign_events.append(
                    {"id": r["id"], "source_id": r["source_id"], "batch_id": owner})
        for e in unowned_events:
            blockers.append({
                "reason": "unowned_event",
                "message": f"来源 {e['source_id']} 下的事件 {e['id']} 不属于任何导入批次"
                           f"（导入前数据或手工录入），撤回该来源会误删；"
                           f"请先手工处理该事件后再撤回",
                "event_id": e["id"], "source_id": e["source_id"]})

    # 阻断项 2：删掉本批次事件会级联删除非本批次的声明约束（source_order
    # 除外——它是派生物，撤回后统一重建）。
    foreign_edges: list[dict[str, Any]] = []
    if present_events:
        pe = list(present_events)
        rows = conn.execute(
            f"SELECT c.id, c.from_event, c.to_event, c.kind, c.note, "
            f"(SELECT bc.batch_id FROM batch_constraints bc WHERE bc.constraint_id=c.id) "
            f"AS owner_batch "
            f"FROM constraints c WHERE c.kind<>'source_order' AND "
            f"(c.from_event IN ({_marks(pe)}) OR c.to_event IN ({_marks(pe)})) ORDER BY c.id",
            pe + pe).fetchall()
        active_other = {r["id"] for r in conn.execute(
            "SELECT id FROM import_batches WHERE status='active' AND id<>?", (batch_id,))}
        for r in rows:
            if r["id"] in present_constraints:
                continue  # 本批次自己的约束，将一并删除
            owner = r["owner_batch"]
            edge = {"id": r["id"], "from": r["from_event"], "to": r["to_event"],
                    "kind": r["kind"], "note": r["note"],
                    "owner_batch": owner if owner in active_other else None}
            foreign_edges.append(edge)
            blockers.append({
                "reason": "foreign_constraint",
                "message": f"约束 #{r['id']}（{r['from_event']} → {r['to_event']}，"
                           f"{r['kind']}）不是本批次写入的，却引用了本批次事件；"
                           f"撤回会级联删除它，请先手工删除该约束后再撤回",
                "constraint_id": r["id"],
                "from_event": r["from_event"], "to_event": r["to_event"]})

    delete_events = sorted(present_events)
    delete_constraint_rows = sorted(present_constraint_rows, key=lambda r: r["id"])
    delete_constraints = [r["id"] for r in delete_constraint_rows]

    # 被删事件当前所属、且撤回后仍存在的来源都需要重建 source_order 边；
    # 其中既包括台账声明的本批次来源，也包括“导入前已存在”的老来源。
    deleted_event_sources = {r["source_id"] for r in conn.execute(
        f"SELECT DISTINCT source_id FROM events WHERE id IN ({_marks(delete_events)})",
        delete_events).fetchall()} if delete_events else set()

    # 来源：删除事件后该来源不再留下任何事件、且来源仍存在 → 一并删除；否则保留。
    retained_sources: list[str] = []
    delete_sources: list[str] = []
    for sid in claimed_sources:
        if sid not in present_sources:
            continue  # 已不存在（悬空）
        remaining = conn.execute(
            f"SELECT COUNT(*) c FROM events WHERE source_id=? "
            f"AND id NOT IN ({_marks(delete_events)})",
            [sid] + delete_events).fetchone()["c"]
        (retained_sources if remaining else delete_sources).append(sid)

    # 导入前已存在、未被本批次登记的来源：本批次只是往里面写了事件，
    # 撤回后来源与导入前事件一律保留（并重建其 source_order 边）。
    unclaimed_sources = sorted(
        sid for sid in deleted_event_sources
        if sid not in set(claimed_sources) and sid in {
            r["id"] for r in conn.execute("SELECT id FROM sources")})
    retained_sources = sorted(set(retained_sources) | set(unclaimed_sources))

    # 受影响来源（撤回后仍存在）：重建其 source_order 边。
    affected = sorted(deleted_event_sources - set(delete_sources))

    return {
        "safe": not blockers,
        "blockers": blockers,
        "delete": {
            "sources": sorted(delete_sources),
            "events": delete_events,
            "constraints": delete_constraints,
            "constraint_details": [
                {"id": r["id"], "from": r["from_event"], "to": r["to_event"],
                 "kind": r["kind"], "note": r["note"]}
                for r in delete_constraint_rows],
        },
        "retained": {
            "sources": sorted(retained_sources),
            "unclaimed_sources": unclaimed_sources,
            "foreign_batched_events": foreign_events,
            "foreign_constraints": foreign_edges,
        },
        "dangling": dangling,
        "affected_source_order_sources": affected,
        "stats": {
            "deleted": {"sources": len(delete_sources),
                        "events": len(delete_events),
                        "constraints": len(delete_constraints)},
            "retained": {"sources": len(retained_sources),
                         "events": len(foreign_events),
                         "constraints": len(foreign_edges)},
            "dangling": {k: len(v) for k, v in dangling.items()},
        },
    }


# --------------------------------------------------------------- 撤回

def preview_retraction(batch_id: int) -> dict[str, Any]:
    """只读撤回预案（不写库），供页面确认前展示与安全检查。"""
    with db() as conn:
        row = _get_batch_row(conn, batch_id)
        if row["status"] == "retracted":
            raise BatchError(f"批次 #{batch_id} 已撤回（{row['retracted_at']}），不能重复撤回")
        plan = _build_plan(conn, batch_id)
    return {"batch_id": batch_id, "status": row["status"], "action": "preview", **plan}


def retract_batch(batch_id: int, confirm: bool = False) -> dict[str, Any]:
    """确认撤回批次。

    confirm=False 时只返回预案；confirm=True 且预案安全才执行删除。
    只删除本批次实际写入且仍存在的记录；导入前数据原样保留。
    """
    if not confirm:
        return preview_retraction(batch_id)

    with db() as conn:
        row = _get_batch_row(conn, batch_id)
        if row["status"] == "retracted":
            raise BatchError(f"批次 #{batch_id} 已撤回（{row['retracted_at']}），不能重复撤回")

        plan = _build_plan(conn, batch_id)
        if not plan["safe"]:
            reasons = "；".join(b["message"] for b in plan["blockers"])
            raise BatchError(f"批次 #{batch_id} 无法安全撤回：{reasons}")

        # 1) 删除本批次写入的声明约束（source_order 边随后统一重建）。
        for cid in plan["delete"]["constraints"]:
            conn.execute("DELETE FROM constraints WHERE id=?", (cid,))

        # 2) 删除本批次写入且仍存在的事件（外键级联只可能命中 source_order 边；
        #    非本批次声明约束已在安全检查中拦截）。
        for eid in plan["delete"]["events"]:
            conn.execute("DELETE FROM events WHERE id=?", (eid,))

        # 3) 删除不再留下任何事件的本批次来源；其余来源保留。
        for sid in plan["delete"]["sources"]:
            conn.execute("DELETE FROM sources WHERE id=?", (sid,))

        # 4) 重建受影响来源的 source_order 边，保留其它来源原有时序边。
        _rebuild_source_edges(conn, _now(),
                              set(plan["affected_source_order_sources"]))

        # 5) 台账行保留（状态置为撤回），便于审计追溯。
        ts = _now()
        conn.execute(
            "UPDATE import_batches SET status='retracted', retracted_at=? WHERE id=?",
            (ts, batch_id))

        stats = plan["stats"]
        result = {
            "batch_id": batch_id,
            "status": "retracted",
            "action": "retracted",
            "retracted_at": ts,
            "safe": True,
            "blockers": [],
            "deleted": plan["delete"],
            "retained": plan["retained"],
            "dangling": plan["dangling"],
            "rebuilt_source_order_sources": plan["affected_source_order_sources"],
            "stats": stats,
        }
    return result
