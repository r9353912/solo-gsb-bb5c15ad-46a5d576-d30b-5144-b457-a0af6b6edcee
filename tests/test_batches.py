"""导入批次查看与安全撤回的纯标准库测试（无需 FastAPI / 网络）。

用法: python tests/test_batches.py

覆盖：
- 每次导入形成批次，账本如实登记 written / skipped；
- 撤回只删本批写入且仍存在的记录，导入前数据保留；
- 重复导入未写入的 ID 不被误删；
- 移除批次约束、重建受影响来源 source_order 边；
- 删除 / 保留 / 悬空三类统计；
- 来源被后续事件复用时保留；非本批约束变悬空逐条上报；
- 批次不存在 / 已撤回 / 未确认 / 归属无法安全判断 → 拒绝并说明；
- 撤回后引擎 / 可行性仍可在（可能含悬空约束的）库上运行。
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
from app import batches, engine  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)


def src(sid):
    return {"id": sid, "name": sid.upper()}


def ev(eid, sid, t, etype="t"):
    return {"id": eid, "source": sid, "time": t, "type": etype}


def cons(a, b, kind="causal"):
    return {"from": a, "to": b, "kind": kind}


class BatchImport(unittest.TestCase):
    def setUp(self):
        reset_all()

    def test_import_creates_batch_and_ledger(self):
        r = import_payload({"case": "C1", "sources": [src("s1")], "events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),
            ev("e2", "s1", "2026-01-01T00:00:10Z")],
            "constraints": [cons("e1", "e2")]})
        bid = r["batch_id"]
        b = batches.get_batch(bid)
        self.assertEqual(b["status"], "active")
        self.assertEqual(b["case_ref"], "C1")
        self.assertEqual(len(b["items"]["sources"]), 1)
        self.assertEqual(
            [i["status"] for i in b["items"]["events"]], ["written", "written"])
        self.assertEqual(b["items"]["constraints"][0]["status"], "written")
        # 行上带归属戳
        with db() as c:
            self.assertEqual(
                c.execute("SELECT import_batch_id FROM events WHERE id='e1'")
                .fetchone()[0], bid)
            self.assertEqual(
                c.execute("SELECT import_batch_id FROM sources WHERE id='s1'")
                .fetchone()[0], bid)

    def test_duplicate_id_recorded_skipped_not_written(self):
        r1 = import_payload({"sources": [src("s1")], "events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z")]})
        r2 = import_payload({"sources": [src("s2")], "events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),  # 重复
            ev("e9", "s2", "2026-01-02T00:00:00Z")]})
        self.assertEqual(r2["events_added"], 1)
        b2 = batches.get_batch(r2["batch_id"])
        skip = [i for i in b2["items"]["events"] if i["ref_id"] == "e1"]
        self.assertEqual(len(skip), 1)
        self.assertEqual(skip[0]["status"], "skipped")
        self.assertEqual(skip[0]["reason"], "duplicate")
        # 列表计数
        lst = {x["id"]: x for x in batches.list_batches()}
        self.assertEqual(lst[r2["batch_id"]]["counts"]["events_skipped"], 1)
        self.assertNotEqual(r1["batch_id"], r2["batch_id"])

    def test_dangling_constraint_recorded_skipped(self):
        r = import_payload({"sources": [src("s1")], "events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z")],
            "constraints": [cons("e1", "ghost")]})
        self.assertEqual(r["constraints_added"], 0)
        b = batches.get_batch(r["batch_id"])
        sc = b["items"]["constraints"][0]
        self.assertEqual(sc["status"], "skipped")
        self.assertEqual(sc["reason"], "dangling_endpoint")


class BatchRetract(unittest.TestCase):
    def setUp(self):
        reset_all()
        # 导入前基线批次
        self.b1 = import_payload({"case": "C1", "sources": [src("s1")], "events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),
            ev("e2", "s1", "2026-01-01T00:00:10Z")],
            "constraints": [cons("e1", "e2")]})["batch_id"]
        self.b2 = import_payload({"case": "C2", "sources": [src("s2")], "events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),  # 重复，未写
            ev("e3", "s2", "2026-01-01T00:00:20Z"),
            ev("e4", "s2", "2026-01-01T00:00:30Z")],
            "constraints": [
                cons("e2", "e3"),                    # 写入
                cons("e1", "e2"),                   # 重复，未写
            ]})["batch_id"]

    def _state(self):
        with db() as c:
            evs = sorted(r["id"] for r in c.execute("SELECT id FROM events"))
            ss = sorted(r["id"] for r in c.execute("SELECT id FROM sources"))
            edges = sorted((r["from_event"], r["to_event"], r["kind"])
                           for r in c.execute("SELECT * FROM constraints"))
        return evs, ss, edges

    def test_retract_removes_only_batch2_and_rebuilds_edges(self):
        res = batches.retract_batch(self.b2, confirm=True)
        st = res["stats"]
        self.assertEqual(st["deleted"]["events"], 2)       # e3,e4
        self.assertEqual(st["deleted"]["sources"], 1)      # s2
        self.assertEqual(st["deleted"]["constraints"], 1)  # e2->e3
        self.assertGreaterEqual(st["deleted"]["source_order_edges_removed"], 1)
        evs, ss, edges = self._state()
        # 导入前数据 e1,e2,s1 与其人工约束保留；重复的 e1 未被误删
        self.assertEqual(evs, ["e1", "e2"])
        self.assertEqual(ss, ["s1"])
        self.assertIn(("e1", "e2", "causal"), edges)
        # s1 仍有 2 个留存事件 → 重建出 source_order e1->e2
        self.assertIn(("e1", "e2", "source_order"), edges)
        # 批次2的事件/来源/约束全部消失
        self.assertNotIn("e3", evs)
        self.assertNotIn(("e2", "e3", "causal"), edges)
        self.assertTrue(res["retracted"])
        # 批次状态翻转
        self.assertEqual(batches.get_batch(self.b2)["status"], "retracted")

    def test_duplicate_untouched_after_retract(self):
        batches.retract_batch(self.b2, confirm=True)
        with db() as c:
            row = c.execute("SELECT id, source_id FROM events WHERE id='e1'").fetchone()
        self.assertIsNotNone(row)          # 重复导入的 e1 仍在
        self.assertEqual(row["source_id"], "s1")  # 归属未被批次2改写

    def test_retract_restores_prior_source_order_stamp(self):
        # 导入 b2 的全局重建把 s1 的系统边也盖戳 b2；撤回必须去污为 NULL。
        batches.retract_batch(self.b2, confirm=True)
        with db() as c:
            stamps = [r[0] for r in c.execute(
                "SELECT import_batch_id FROM constraints WHERE kind='source_order'")]
        self.assertTrue(stamps)
        self.assertTrue(all(s is None for s in stamps))

    def test_double_retract_rejected(self):
        batches.retract_batch(self.b2, confirm=True)
        with self.assertRaises(batches.BatchConflict):
            batches.retract_batch(self.b2, confirm=True)

    def test_missing_batch_404(self):
        with self.assertRaises(batches.BatchNotFound):
            batches.retract_batch(9999, confirm=True)
        with self.assertRaises(batches.BatchNotFound):
            batches.get_batch(9999)

    def test_confirm_required(self):
        with self.assertRaises(batches.BatchError):
            batches.retract_batch(self.b2, confirm=False)
        # 拒绝后状态不变、数据未动
        self.assertEqual(batches.get_batch(self.b2)["status"], "active")

    def test_ownership_conflict_rejects_without_deleting(self):
        # 外部把本批事件归属戳改成别的批次 → 无法安全判断归属，拒绝。
        with db() as c:
            c.execute("UPDATE events SET import_batch_id=777 WHERE id='e3'")
        with self.assertRaises(batches.BatchConflict) as ctx:
            batches.retract_batch(self.b2, confirm=True)
        self.assertIn("无法安全判断归属", str(ctx.exception))
        with db() as c:
            self.assertIsNotNone(
                c.execute("SELECT 1 FROM events WHERE id='e3'").fetchone())
        self.assertEqual(batches.get_batch(self.b2)["status"], "active")

    def test_ownership_null_stamp_rejects(self):
        with db() as c:
            c.execute("UPDATE events SET import_batch_id=NULL WHERE id='e3'")
        with self.assertRaises(batches.BatchConflict) as ctx:
            batches.retract_batch(self.b2, confirm=True)
        self.assertIn("归属戳", str(ctx.exception))

    def test_reused_source_is_retained(self):
        # b3 复用 s1（已存在），只新增 e5；撤回 b1 时 s1 仍被 e3,e4,e5 等使用
        # → s1 保留。这里直接测：撤回引入 s1 的 b1 之前先让 b2 复用 s1。
        reset_all()
        b1 = import_payload({"sources": [src("s1")], "events": [
            ev("x1", "s1", "2026-01-01T00:00:00Z")]})["batch_id"]
        b2 = import_payload({"events": [
            ev("x2", "s1", "2026-01-01T00:00:05Z")]})["batch_id"]
        res = batches.retract_batch(b1, confirm=True)
        self.assertEqual(res["stats"]["retained"]["sources"], 1)
        self.assertEqual(res["retained"]["sources"][0]["id"], "s1")
        with db() as c:
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM sources WHERE id='s1'").fetchone())
            self.assertEqual(
                c.execute("SELECT id FROM events").fetchall()[0]["id"], "x2")
        # 撤回 b2 时 s1 在其账本里是 skipped，不会被删
        batches.retract_batch(b2, confirm=True)
        with db() as c:
            self.assertIsNone(c.execute(
                "SELECT 1 FROM events WHERE id='x2'").fetchone())
            # s1 账本属于 b1（已撤回），b2 无权删除 → s1 保留为空来源
            self.assertIsNotNone(c.execute(
                "SELECT 1 FROM sources WHERE id='s1'").fetchone())

    def test_foreign_manual_constraint_becomes_dangling(self):
        # 人工约束 e1 -> e3（跨批次1/2）；撤回 b2 后 e3 删除，约束保留为悬空。
        with db() as c:
            cur = c.execute(
                "INSERT INTO constraints(from_event,to_event,kind,note,created_at,"
                "import_batch_id) VALUES('e1','e3','claimed','manual','t',NULL)")
            cid = cur.lastrowid
        res = batches.retract_batch(self.b2, confirm=True)
        self.assertEqual(res["stats"]["dangling"]["foreign_constraints"], 1)
        item = res["dangling"]["foreign_constraints"][0]
        self.assertEqual(item["id"], cid)
        self.assertEqual((item["from"], item["to"]), ("e1", "e3"))
        with db() as c:
            self.assertIsNotNone(
                c.execute("SELECT 1 FROM constraints WHERE id=?", (cid,)).fetchone())
        # 引擎仍能正常计算（悬空边被跳过），不报错
        r = engine.compute()
        self.assertGreaterEqual(r["stats"]["event_count"], 2)

    def test_externally_deleted_event_counts_dangling_not_deleted(self):
        with db() as c:
            c.execute("DELETE FROM events WHERE id='e3'")
        res = batches.retract_batch(self.b2, confirm=True)
        self.assertIn("e3", [d["ref_id"] for d in res["dangling"]["events"]])
        self.assertEqual(res["stats"]["deleted"]["events"], 1)  # 只剩 e4 真删

    def test_totals_count_entities_not_derived_edges(self):
        res = batches.retract_batch(self.b2, confirm=True)
        t = res["stats"]["totals"]
        # 实体：2 事件 + 1 来源 + 1 人工约束 = 4；source_order 边单列不计入。
        self.assertEqual(t["deleted"], 4)
        self.assertEqual(res["stats"]["deleted"]["source_order_edges_removed"], 1)
        self.assertEqual(t["retained"], 0)
        self.assertEqual(t["dangling"], 0)

    def test_retract_all_batches_leaves_pre_import_state_empty(self):
        # self.b2 已在上一用例撤回？不——用例相互独立（setUp 重建），这里显式撤回：
        reset_all()
        b1 = import_payload({"case": "C1", "sources": [src("s1")], "events": [
            ev("e1", "s1", "2026-01-01T00:00:00Z"),
            ev("e2", "s1", "2026-01-01T00:00:10Z")],
            "constraints": [cons("e1", "e2")]})["batch_id"]
        b2 = import_payload({"case": "C2", "sources": [src("s2")], "events": [
            ev("e3", "s2", "2026-01-01T00:00:20Z"),
            ev("e4", "s2", "2026-01-01T00:00:30Z")]})["batch_id"]
        batches.retract_batch(b2, confirm=True)
        batches.retract_batch(b1, confirm=True)
        with db() as c:
            for t in ("events", "sources"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"], 0)
            # 人工/导入因果约束清空；source_order 无事件自然为空
            self.assertEqual(
                c.execute("SELECT COUNT(*) n FROM constraints "
                          "WHERE kind!='source_order'").fetchone()["n"], 0)
            self.assertEqual(
                c.execute("SELECT COUNT(*) n FROM constraints").fetchone()["n"], 0)


class LegacyAndReimport(unittest.TestCase):
    def test_sketched_reimport_after_retract(self):
        reset_all()
        b1 = import_payload({"sources": [src("s1")], "events": [
            ev("k1", "s1", "2026-01-01T00:00:00Z")]})["batch_id"]
        batches.retract_batch(b1, confirm=True)
        # 撤回后同 ID 可再次导入（新批次）
        b2 = import_payload({"sources": [src("s1")], "events": [
            ev("k1", "s1", "2026-01-01T00:00:00Z")]})["batch_id"]
        self.assertNotEqual(b1, b2)
        with db() as c:
            stamp = c.execute(
                "SELECT import_batch_id FROM events WHERE id='k1'").fetchone()[0]
        self.assertEqual(stamp, b2)
        # 新批次可独立撤回
        res = batches.retract_batch(b2, confirm=True)
        self.assertEqual(res["stats"]["deleted"]["events"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
