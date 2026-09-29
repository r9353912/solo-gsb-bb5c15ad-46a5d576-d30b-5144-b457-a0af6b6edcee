"""导入批次查看与安全撤回。

撤回的安全原则
==============
一次撤回**只能删除该批次实际写入、且仍存在、且归属仍可被安全证明**的记录：

1. **以批次账本（batch_items）为准**：只有 ``written`` 项参与撤回；
   ``skipped``（重复未写入 / 端点悬空 / 解析拒绝）从不删除——因此重复导入
   的事件 ID 不会被误删。
2. **用行上的 ``import_batch_id`` 归属戳交叉验证**：账本说“这行是批次 B 写的”，
   行上的戳必须确实等于 B。戳为 NULL（批次功能上线前的存量数据 / 页面手工
   数据）或指向别的批次（归属被改动、无法安全判断）时**拒绝撤回并逐条说明**，
   绝不猜测删除。
3. **保留导入前数据**：非本批次写入的事件 / 来源 / 人工约束一律不删。
   批次来源若仍被其它事件使用则保留（计入 retained）；非本批次约束引用了
   被删事件时保留该约束（其端点随后不存在，成为悬空约束，计入
   dangling 并逐条列出，由审计员后续处理）。
4. source_order 是**系统派生边**（不属于任何批次，归属戳恒为 NULL）：
   撤回时移除触及本批事件的 source_order 边，然后对所有受影响来源按其
   留存事件**重建**，恢复到“导入前数据自己生成的边”。

统计返回 deleted / retained / dangling 三类计数与明细。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import db
from .importer import _rebuild_source_edges


class BatchError(ValueError):
    """可映射为 4xx 的批次操作错误。"""


class BatchNotFound(BatchError):
    """批次不存在（映射 404）。"""


class BatchConflict(BatchError):
    """批次状态或归属不允许撤回（映射 409）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------ 列表与详情

def list_batches(limit: int = 200) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM import_batches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            b = dict(r)
            b["counts"] = _counts(conn, r["id"])
            out.append(b)
    return out


def _counts(conn, batch_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT item_type, status, COUNT(*) n FROM batch_items WHERE batch_id=? "
        "GROUP BY item_type, status", (batch_id,)).fetchall()
    c = {"sources_written": 0, "events_written": 0, "constraints_written": 0,
         "events_skipped": 0, "constraints_skipped": 0, "sources_skipped": 0}
    for r in rows:
        key = f"{r['item_type']}s_{r['status']}"
        c[key] = r["n"]
    return c


def get_batch(batch_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise BatchNotFound(f"批次不存在: #{batch_id}")
        b = dict(row)
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY id", (batch_id,))]

    grouped: dict[str, list[dict]] = {
        "sources": [], "events": [], "constraints": [],
    }
    for it in items:
        try:
            it["detail"] = json.loads(it.get("detail") or "{}")
        except (ValueError, TypeError):
            it["detail"] = {}
        grouped[f"{it['item_type']}s" if it["item_type"] != "constraints"
                else "constraints"].append(it)
    b["items"] = grouped
    b["counts"] = {
        "sources": len(grouped["sources"]),
        "events": len(grouped["events"]),
        "constraints": len(grouped["constraints"]),
    }
    return b


# ------------------------------------------------------------ 归属预检

def _ownership_problems(conn, batch_id: int, written: list[dict]) -> list[dict]:
    """账本 written 项与库内行的归属戳交叉验证；返回无法安全判断归属的明细。"""
    problems: list[dict] = []
    for it in written:
        t, ref = it["item_type"], it["ref_id"]
        if t == "source":
            row = conn.execute(
                "SELECT id, import_batch_id FROM sources WHERE id=?", (ref,)).fetchone()
        elif t == "event":
            row = conn.execute(
                "SELECT id, import_batch_id FROM events WHERE id=?", (ref,)).fetchone()
        else:
            try:
                cid = int(ref)
            except ValueError:
                problems.append({"item_type": t, "ref_id": ref,
                                 "reason": "账本引用不是有效约束 id"})
                continue
            row = conn.execute(
                "SELECT id, import_batch_id FROM constraints WHERE id=?", (cid,)).fetchone()
        if row is None:
            continue  # 行已不存在：记为 dangling，不阻止撤回
        stamp = row["import_batch_id"]
        if stamp != batch_id:
            if stamp is None:
                reason = "行仍存在但没有批次归属戳（可能是批次功能上线前的存量数据或手工数据），无法证明由本批次写入"
            else:
                reason = f"行仍存在但归属戳指向批次 #{stamp}，与账本记录（本批次 #{batch_id}）冲突"
            problems.append({"item_type": t, "ref_id": ref, "reason": reason})
    return problems


# ------------------------------------------------------------ 撤回

def retract_batch(batch_id: int, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise BatchError("撤回必须显式确认（confirm=true）；请先查看批次明细再确认。")

    with db() as conn:
        # PRAGMA foreign_keys 不能在事务内切换；with db() 在 yield 时已开启
        # 事务（sqlite3 默认 begin deferred），此处先提交空事务再关闭级联，
        # 随后的删除全部在同一事务内完成，异常仍整体回滚。
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")

        row = conn.execute(
            "SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise BatchNotFound(f"批次不存在: #{batch_id}")
        if row["status"] == "retracted":
            raise BatchConflict(f"批次 #{batch_id} 已撤回（{row['retracted_at']}），不能重复撤回")
        if row["status"] != "active":
            raise BatchConflict(f"批次 #{batch_id} 状态为 {row['status']}，不可撤回")

        written = [dict(r) for r in conn.execute(
            "SELECT * FROM batch_items WHERE batch_id=? AND status='written' ORDER BY id",
            (batch_id,))]

        # 1) 归属预检：戳不一致即拒绝，不删任何数据。
        problems = _ownership_problems(conn, batch_id, written)
        if problems:
            raise BatchConflict(
                f"批次 #{batch_id} 含 {len(problems)} 条无法安全判断归属的记录，"
                "为避免误删已拒绝撤回：" + "；".join(
                    f"[{p['item_type']}] {p['ref_id']}：{p['reason']}"
                    for p in problems[:5])
                + ("……" if len(problems) > 5 else ""),
            )

        # 2) 分类账本 written 项：行仍在 → 删除/保留候选；行缺失 → dangling。
        src_items = [w for w in written if w["item_type"] == "source"]
        ev_items = [w for w in written if w["item_type"] == "event"]
        con_items = [w for w in written if w["item_type"] == "constraint"]

        deleted = {"sources": [], "events": [], "constraints": [], "source_order_edges": 0}
        retained = {"sources": [], "events": [], "constraints": []}
        dangling = {"sources": [], "events": [], "constraints": [],
                    "foreign_constraints": []}

        event_ids_to_delete: set[str] = set()
        for it in ev_items:
            r = conn.execute(
                "SELECT id FROM events WHERE id=?", (it["ref_id"],)).fetchone()
            if r is not None:
                event_ids_to_delete.add(it["ref_id"])
            else:
                dangling["events"].append(_dangling_item(it, "事件已在批次外被删除"))

        # 3) 非本批次、且非系统派生（source_order）的约束若触及待删事件：
        #    不删约束、不阻止撤回；删后它们成为悬空约束（引擎与可行性检查本就
        #    跳过悬空边），逐条上报由审计员后续处理。
        if event_ids_to_delete:
            ph = ",".join("?" for _ in event_ids_to_delete)
            touch = conn.execute(
                f"SELECT id, from_event, to_event, kind, note, import_batch_id "
                f"FROM constraints WHERE (from_event IN ({ph}) OR to_event IN ({ph})) "
                f"AND kind != 'source_order' AND COALESCE(import_batch_id, -1) != ?",
                (*event_ids_to_delete, *event_ids_to_delete, batch_id),
            ).fetchall()
            for r in touch:
                dangling["foreign_constraints"].append({
                    "id": r["id"], "from": r["from_event"], "to": r["to_event"],
                    "kind": r["kind"], "note": r["note"],
                    "reason": "引用的事件由本批次撤回删除，该约束非本批次写入，已保留（成为悬空约束）",
                })

        # 4) 系统派生 source_order 边（不属于任何批次，归属戳恒为 NULL）：
        #    先在事件仍在库时按受影响来源定位并移除旧边，删除事件后再按各来源
        #    留存事件重建，恢复为“导入前数据自己生成的边”。
        affected_sources: set[str] = set()
        for it in ev_items:
            src = (json.loads(it.get("detail") or "{}")).get("source_id")
            if src:
                affected_sources.add(src)
        so_removed = _remove_source_edges(conn, affected_sources)

        # 5) 删除本批次写入的人工/导入约束（source_order 已在第 4 步处理）。
        for it in con_items:
            cid = int(it["ref_id"])
            r = conn.execute(
                "SELECT id, from_event, to_event, kind FROM constraints WHERE id=?",
                (cid,)).fetchone()
            if r is None:
                dangling["constraints"].append(
                    _dangling_item(it, "约束已在批次外被删除"))
                continue
            if r["kind"] == "source_order":
                # 正常流程不会出现（派生边不进账本）；保险起见不重复计数。
                continue
            conn.execute("DELETE FROM constraints WHERE id=?", (cid,))
            deleted["constraints"].append({
                "id": cid, "from": r["from_event"], "to": r["to_event"],
                "kind": r["kind"],
            })

        # 6) 删除本批次事件。
        for eid in sorted(event_ids_to_delete):
            conn.execute("DELETE FROM events WHERE id=?", (eid,))
            deleted["events"].append(eid)

        # 7) 本批次来源：仍被其它（非本批）事件使用则保留，否则删除。
        for it in src_items:
            sid = it["ref_id"]
            r = conn.execute(
                "SELECT id FROM sources WHERE id=?", (sid,)).fetchone()
            if r is None:
                dangling["sources"].append(
                    _dangling_item(it, "来源已在批次外被删除"))
                continue
            users = conn.execute(
                "SELECT COUNT(*) n FROM events WHERE source_id=?", (sid,)).fetchone()["n"]
            if users > 0:
                retained["sources"].append({
                    "id": sid, "remaining_events": users,
                    "reason": "来源仍被导入后新增/导入前已有的事件使用，删除会连带破坏非本批次数据",
                })
            else:
                conn.execute("DELETE FROM sources WHERE id=?", (sid,))
                deleted["sources"].append(sid)

        # 8) 按受影响来源的留存事件重建 source_order 边（无留存事件则不产边）。
        rebuilt = _rebuild_source_edges(conn, _now(), batch_id=None,
                                        only_sources=affected_sources)
        deleted["source_order_edges"] = so_removed
        deleted["source_order_edges_rebuilt"] = rebuilt

        # 9) 归属去污：导入时的全局重建会把“仅含既有事件的来源”的系统边也盖上
        #    本批次戳。这些来源的事件集本次并未变化，撤回后其仍存在的
        #    source_order 边必须恢复成系统派生（戳置 NULL），否则会污染后续
        #    其它批次的归属判断。
        conn.execute(
            "UPDATE constraints SET import_batch_id=NULL WHERE kind='source_order' "
            "AND import_batch_id=? AND id NOT IN "
            "(SELECT c.id FROM constraints c JOIN events e "
            " ON c.from_event=e.id WHERE e.import_batch_id=?) "
            "AND id NOT IN "
            "(SELECT c.id FROM constraints c JOIN events e "
            " ON c.to_event=e.id WHERE e.import_batch_id=?)",
            (batch_id, batch_id, batch_id),
        )

        # 10) 标记批次已撤回（账本保留作为审计轨迹）。
        ts = _now()
        conn.execute(
            "UPDATE import_batches SET status='retracted', retracted_at=? WHERE id=?",
            (ts, batch_id))

        stats = {
            "deleted": {
                "sources": len(deleted["sources"]),
                "events": len(deleted["events"]),
                "constraints": len(deleted["constraints"]),
                "source_order_edges_removed": deleted["source_order_edges"],
                "source_order_edges_rebuilt": rebuilt,
            },
            "retained": {
                "sources": len(retained["sources"]),
                "events": len(retained["events"]),
                "constraints": len(retained["constraints"]),
            },
            "dangling": {
                "sources": len(dangling["sources"]),
                "events": len(dangling["events"]),
                "constraints": len(dangling["constraints"]),
                "foreign_constraints": len(dangling["foreign_constraints"]),
            },
        }
        stats["totals"] = {
            # 实体记录总数（source_order 为系统派生边，单列在 deleted 内，不计入，
            # 避免“实体 + 派生边”重复计数）。
            "deleted": (stats["deleted"]["sources"] + stats["deleted"]["events"]
                        + stats["deleted"]["constraints"]),
            "retained": _stats_total(stats["retained"]),
            "dangling": _stats_total(stats["dangling"]),
        }
        result = {
            "retracted": True,
            "batch_id": batch_id,
            "retracted_at": ts,
            "summary": (
                f"批次 #{batch_id} 已撤回：删除 {stats['totals']['deleted']} 条"
                f"（含 {stats['deleted']['source_order_edges_removed']} 条系统时序边，"
                f"重建 {rebuilt} 条），保留 {stats['totals']['retained']} 条，"
                f"悬空 {stats['totals']['dangling']} 条；导入前数据未被删除。"
            ),
            "stats": stats,
            "deleted": deleted,
            "retained": retained,
            "dangling": dangling,
        }
        # 提交点：with db() 退出时再 commit；提前准备好返回值即可。
    return result


def _stats_total(section: dict) -> int:
    return sum(section.values())


def _remove_source_edges(conn, sources: set[str]) -> int:
    """删除任一端点事件属于给定来源的全部 source_order 边，返回删除条数。

    必须在事件行仍存在时调用（撤回流程在删事件之前调用），以便通过
    events.source_id 子查询定位这些系统派生边。
    """
    if not sources:
        return 0
    ph = ",".join("?" for _ in sources)
    cur = conn.execute(
        "DELETE FROM constraints WHERE kind='source_order' AND "
        f"(from_event IN (SELECT id FROM events WHERE source_id IN ({ph})) "
        f"OR to_event IN (SELECT id FROM events WHERE source_id IN ({ph})))",
        (*sources, *sources),
    )
    return cur.rowcount


def _dangling_item(item: dict, reason: str) -> dict:
    detail = {}
    try:
        detail = json.loads(item.get("detail") or "{}")
    except (ValueError, TypeError):
        pass
    return {"ref_id": item["ref_id"], "reason": reason, "detail": detail}
