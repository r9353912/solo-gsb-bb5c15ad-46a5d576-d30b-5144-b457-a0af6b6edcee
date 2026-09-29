"""JSON 导入批次查看与确认撤回的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_batches.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-batch-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import db, init_db  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402
from app.batches import (  # noqa: E402
    BatchError, get_batch, list_batches, preview_retraction, retract_batch,
)

init_db(load_sample=False)


def ev(eid, source, t, etype="t"):
    return {"id": eid, "source": source, "time": t, "type": etype}


def table_count(conn, table, where="", params=()):
    return conn.execute(f"SELECT COUNT(*) c FROM {table} {where}", params).fetchone()["c"]


def insert_manual_event(eid, source, ts=500.0):
    """模拟导入前 / 审计员手工录入：直接写库，不挂任何批次台账。"""
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO sources(id,name,clock_offset,drift_ppm,uncertainty,"
            "reliability,kind,created_at,updated_at) "
            "VALUES(?,?,0,0,1,0.8,'external','t','t')", (source, source))
        c.execute(
            "INSERT INTO events(id,source_id,seq,local_ts,ts_text,event_type,actor,"
            "summary,raw,imported_at) VALUES(?,?,NULL,?,'','t',NULL,'','{}','t')",
            (eid, source, ts))


def insert_manual_constraint(frm, to, kind="claimed"):
    with db() as c:
        c.execute(
            "INSERT INTO constraints(from_event,to_event,kind,note,created_at) "
            "VALUES(?,?,?,?,'t')", (frm, to, kind, "manual"))
        return c.execute("SELECT last_insert_rowid() i").fetchone()["i"]


class BatchBasics(unittest.TestCase):

    def setUp(self):
        reset_all()

    def test_import_creates_batch_with_actual_writes(self):
        r = import_payload({
            "sources": [{"id": "s1", "name": "S1"}],
            "events": [ev("e1", "s1", "2026-01-01T00:00:00Z"),
                       ev("e2", "s1", "2026-01-01T00:00:10Z")],
            "constraints": [{"from": "e1", "to": "e2", "kind": "causal"}],
        }, label="f.json")
        bid = r["batch_id"]
        detail = get_batch(bid)
        self.assertEqual(detail["label"], "f.json")
        self.assertEqual(detail["status"], "active")
        self.assertEqual([s["id"] for s in detail["sources"]], ["s1"])
        self.assertEqual([e["id"] for e in detail["events"]], ["e1", "e2"])
        self.assertEqual(len(detail["constraints"]), 1)
        self.assertTrue(all(x["present"] for x in detail["sources"]))
        self.assertTrue(all(x["present"] for x in detail["events"]))
        # source_order 派生物不计入批次约束台账
        self.assertTrue(all(c["kind"] != "source_order" for c in detail["constraints"]))
        # 列表最新在前
        self.assertEqual(list_batches()[0]["id"], bid)

    def test_duplicate_ids_not_in_ledger_and_survive_retraction(self):
        payload = {"sources": [{"id": "s1"}],
                   "events": [ev("e1", "s1", "2026-01-01T00:00:00Z")]}
        first = import_payload(payload, label="a")
        second = import_payload(payload, label="dup")
        self.assertEqual(second["events_added"], 0)
        self.assertEqual(second["duplicate_event_ids"], ["e1"])
        # 第二个批次台账为空：没有任何事件归属
        detail = get_batch(second["batch_id"])
        self.assertEqual(detail["events"], [])
        # 撤回第二个（空）批次不得删掉第一次写入的 e1 / s1
        res = retract_batch(second["batch_id"], confirm=True)
        self.assertEqual(res["stats"]["deleted"]["events"], 0)
        with db() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM events WHERE id='e1'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM sources WHERE id='s1'").fetchone())
        # 撤回第一个批次仍可正常删除
        res1 = retract_batch(first["batch_id"], confirm=True)
        self.assertEqual(res1["deleted"]["events"], ["e1"])

    def test_duplicate_declared_constraint_not_claimed(self):
        payload = {"events": [ev("e1", "s1", "2026-01-01T00:00:00Z"),
                              ev("e2", "s1", "2026-01-01T00:00:10Z")],
                   "constraints": [{"from": "e1", "to": "e2", "kind": "causal"}]}
        first = import_payload(payload)
        second = import_payload(payload)
        # 约束 UNIQUE(from,to,kind) 冲突，第二次未写入 → 台账为空
        self.assertEqual(get_batch(second["batch_id"])["constraints"], [])
        res = retract_batch(second["batch_id"], confirm=True)
        self.assertEqual(res["stats"]["deleted"]["constraints"], 0)
        with db() as c:
            self.assertEqual(table_count(c, "constraints",
                                         "WHERE kind='causal'"), 1)

    def test_retraction_deletes_only_batch_records_and_rebuilds_edges(self):
        bid = import_payload({"events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),
            ev("e2", "s1", "2026-01-01T00:00:10Z"),
            ev("e3", "s1", "2026-01-01T00:00:20Z")],
            "constraints": [{"from": "e1", "to": "e3", "kind": "claimed"}]},
            label="b1")["batch_id"]
        res = retract_batch(bid, confirm=True)
        self.assertEqual(res["status"], "retracted")
        self.assertEqual(sorted(res["deleted"]["events"]), ["e1", "e2", "e3"])
        self.assertEqual(res["deleted"]["sources"], ["s1"])
        self.assertEqual(len(res["deleted"]["constraints"]), 1)
        with db() as c:
            self.assertEqual(table_count(c, "events"), 0)
            self.assertEqual(table_count(c, "sources"), 0)
            self.assertEqual(table_count(c, "constraints"), 0)
        # 台账行保留（审计追溯）
        self.assertEqual(get_batch(bid)["status"], "retracted")

    def test_double_retraction_refused(self):
        bid = import_payload({"events": [ev("e1", "s1", "2026-01-01T00:00:00Z")]})["batch_id"]
        retract_batch(bid, confirm=True)
        with self.assertRaises(BatchError):
            retract_batch(bid, confirm=True)
        with self.assertRaises(BatchError):
            preview_retraction(bid)

    def test_missing_batch_refused(self):
        with self.assertRaises(BatchError):
            preview_retraction(424242)
        with self.assertRaises(BatchError):
            retract_batch(424242, confirm=True)

    def test_confirm_required(self):
        bid = import_payload({"events": [ev("e1", "s1", "2026-01-01T00:00:00Z")]})["batch_id"]
        res = retract_batch(bid, confirm=False)
        self.assertEqual(res["action"], "preview")
        self.assertEqual(get_batch(bid)["status"], "active")  # 未执行
        with db() as c:
            self.assertEqual(table_count(c, "events"), 1)


class BatchSafety(unittest.TestCase):

    def setUp(self):
        reset_all()

    def test_preexisting_data_preserved_and_source_retained(self):
        # 导入前已有来源 old 与事件 o1（手工，无批次）
        insert_manual_event("o1", "old", 100.0)
        bid = import_payload({
            "events": [
                ev("n1", "old", "2026-01-01T00:00:20Z"),
                ev("n2", "new", "2026-01-01T00:00:30Z")]})["batch_id"]
        plan = preview_retraction(bid)
        self.assertTrue(plan["safe"])
        # old 不归属本批次（导入前已存在），不会被删；new 为空后删除
        self.assertEqual(plan["delete"]["sources"], ["new"])
        self.assertNotIn("old", plan["delete"]["sources"])
        res = retract_batch(bid, confirm=True)
        self.assertEqual(res["stats"]["deleted"]["events"], 2)
        with db() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM events WHERE id='o1'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM sources WHERE id='old'").fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM sources WHERE id='new'").fetchone())

    def test_unowned_event_on_batch_source_blocks(self):
        bid = import_payload({"events": [
            ev("m1", "ms", "2026-01-01T00:00:40Z"),
            ev("m2", "ms", "2026-01-01T00:00:50Z")]})["batch_id"]
        # 导入后审计员手工往批次来源里加了无批次事件
        insert_manual_event("manual", "ms", 999.0)
        plan = preview_retraction(bid)
        self.assertFalse(plan["safe"])
        self.assertEqual([b["reason"] for b in plan["blockers"]], ["unowned_event"])
        with self.assertRaises(BatchError):
            retract_batch(bid, confirm=True)
        # 拒绝后数据原样
        with db() as c:
            self.assertEqual(table_count(c, "events"), 3)

    def test_foreign_constraint_blocks_cascade_delete(self):
        bid = import_payload({"events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),
            ev("e2", "s2", "2026-01-01T00:00:01Z")]})["batch_id"]
        # 审计员手工建了一条引用批次事件的约束（非本批次）
        insert_manual_constraint("e1", "e2")
        plan = preview_retraction(bid)
        self.assertFalse(plan["safe"])
        self.assertIn("foreign_constraint",
                      [b["reason"] for b in plan["blockers"]])
        with self.assertRaises(BatchError):
            retract_batch(bid, confirm=True)

    def test_other_batch_referencing_constraint_blocks(self):
        b1 = import_payload({"events": [ev("e1", "s1", "2026-01-01T00:00:00Z"),
                                        ev("e2", "s2", "2026-01-01T00:00:01Z")]})["batch_id"]
        b2 = import_payload({"constraints": [
            {"from": "e1", "to": "e2", "kind": "causal"}]})["batch_id"]
        # 撤回 b1 会级联删掉 b2 写入的约束 → 拒绝
        plan = preview_retraction(b1)
        self.assertFalse(plan["safe"])
        self.assertEqual([b["reason"] for b in plan["blockers"]], ["foreign_constraint"])
        # 先撤回 b2（它不删事件），再撤回 b1 即安全
        r2 = retract_batch(b2, confirm=True)
        self.assertEqual(r2["stats"]["deleted"]["constraints"], 1)
        r1 = retract_batch(b1, confirm=True)
        self.assertTrue(r1["safe"])

    def test_other_batch_events_on_same_source_kept_and_source_retained(self):
        b1 = import_payload({"events": [ev("p1", "s", "2026-01-01T00:00:10Z"),
                                        ev("p2", "s", "2026-01-01T00:00:20Z")]})["batch_id"]
        b2 = import_payload({"events": [ev("q1", "s", "2026-01-01T00:00:30Z")]})["batch_id"]
        # 撤回较早批次 b1：来源仍被 b2 的 q1 占用 → 保留来源
        res = retract_batch(b1, confirm=True)
        self.assertEqual(res["deleted"]["events"], ["p1", "p2"])
        self.assertEqual(res["retained"]["sources"], ["s"])
        self.assertIn("s", res["rebuilt_source_order_sources"])
        with db() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM sources WHERE id='s'").fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM events WHERE id='q1'").fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM events WHERE id='p1'").fetchone())

    def test_dangling_records_counted_not_deleted(self):
        bid = import_payload({"events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),
            ev("e2", "s1", "2026-01-01T00:00:10Z")]})["batch_id"]
        # 审计员在撤回前手工删掉了 e2（连同其 source_order 边）
        with db() as c:
            c.execute("DELETE FROM events WHERE id='e2'")
        plan = preview_retraction(bid)
        self.assertTrue(plan["safe"])
        self.assertEqual(plan["dangling"]["events"], ["e2"])
        self.assertEqual(plan["delete"]["events"], ["e1"])
        res = retract_batch(bid, confirm=True)
        self.assertEqual(res["stats"]["deleted"]["events"], 1)
        self.assertEqual(res["stats"]["dangling"]["events"], 1)

    def test_source_order_edges_rebuilt_for_affected_source(self):
        # 导入前：来源 s 有 a、c（c 晚于 a），带 source_order a→c
        insert_manual_event("a", "s", 100.0)
        insert_manual_event("c", "s", 300.0)
        with db() as c:
            c.execute("INSERT INTO constraints(from_event,to_event,kind,note,created_at) "
                      "VALUES('a','c','source_order','x','t')")
        # 新事件 b 落在 a、c 之间（200 秒）；直接插库后手动触发与导入时相同的全量重建
        from app.importer import _rebuild_source_edges
        with db() as c:
            c.execute(
                "INSERT INTO events(id,source_id,seq,local_ts,ts_text,event_type,actor,"
                "summary,raw,imported_at) VALUES('b','s',NULL,200.0,'','t',NULL,'','{}','t')")
            _rebuild_source_edges(c, "t")
        with db() as c:
            edges = [(r["from_event"], r["to_event"]) for r in c.execute(
                "SELECT from_event,to_event FROM constraints WHERE kind='source_order'")]
        self.assertIn(("a", "b"), edges)
        self.assertIn(("b", "c"), edges)
        # 给 b 补批次台账后撤回（模拟该事件确为批次写入）
        with db() as c:
            c.execute("INSERT INTO import_batches(label,status,summary,created_at) "
                      "VALUES('','active','{}','t')")
            bid = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            c.execute("INSERT INTO batch_events(batch_id,event_id) VALUES(?,'b')", (bid,))
        retract_batch(bid, confirm=True)
        with db() as c:
            edges = [(r["from_event"], r["to_event"]) for r in c.execute(
                "SELECT from_event,to_event FROM constraints WHERE kind='source_order'")]
        self.assertEqual(edges, [("a", "c")])  # 恢复为导入前形态

    def test_other_sources_edges_untouched_on_retract(self):
        # 两个批次各管一个来源；撤回一个不得改动另一个来源的 source_order 边
        b1 = import_payload({"events": [ev("x1", "x", "2026-01-01T00:00:00Z"),
                                        ev("x2", "x", "2026-01-01T00:00:10Z")]})["batch_id"]
        import_payload({"events": [ev("y1", "y", "2026-01-01T00:00:00Z"),
                                   ev("y2", "y", "2026-01-01T00:00:10Z")]})
        retract_batch(b1, confirm=True)  # 撤回 x 批次
        with db() as c:
            y_edges = {(r["from_event"], r["to_event"]) for r in c.execute(
                "SELECT from_event,to_event FROM constraints WHERE kind='source_order'")}
        self.assertEqual(y_edges, {("y1", "y2")})

    def test_batch_own_constraint_deleted_even_when_other_events_remain(self):
        # 批次往“导入前已存在”的来源加事件 + 一条声明约束；撤回删约束与事件，
        # 但保留来源。
        insert_manual_event("o1", "old", 100.0)
        bid = import_payload({
            "events": [ev("n1", "old", "2026-01-01T00:00:20Z")],
            "constraints": [{"from": "o1", "to": "n1", "kind": "claimed"}]})["batch_id"]
        res = retract_batch(bid, confirm=True)
        self.assertEqual(res["stats"]["deleted"]["constraints"], 1)
        self.assertEqual(res["stats"]["deleted"]["events"], 1)
        self.assertEqual(res["retained"]["sources"], ["old"])
        with db() as c:
            self.assertEqual(table_count(c, "constraints",
                                         "WHERE kind='claimed'"), 0)
            self.assertIsNotNone(c.execute("SELECT 1 FROM events WHERE id='o1'").fetchone())

    def test_retraction_is_idempotent_and_atomic_on_refusal(self):
        # 拒绝撤回事务回滚：批次仍 active，全部数据不变
        bid = import_payload({"events": [ev("e1", "s1", "2026-01-01T00:00:00Z"),
                                         ev("e2", "s2", "2026-01-01T00:00:01Z")]})["batch_id"]
        insert_manual_constraint("e1", "e2")
        with db() as c:
            before = {t: table_count(c, t) for t in
                      ("events", "sources", "constraints")}
        with self.assertRaises(BatchError):
            retract_batch(bid, confirm=True)
        with db() as c:
            after = {t: table_count(c, t) for t in
                     ("events", "sources", "constraints")}
        self.assertEqual(before, after)
        self.assertEqual(get_batch(bid)["status"], "active")


if __name__ == "__main__":
    unittest.main(verbosity=2)
