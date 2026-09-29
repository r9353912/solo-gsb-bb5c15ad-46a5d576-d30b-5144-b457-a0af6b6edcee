"""约束撤销建议的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_retraction.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-retract-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import db, init_db  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402
from app.retraction import suggest_retractions  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")


def load_sample():
    reset_all()
    with open(EXAMPLE, encoding="utf-8") as f:
        import_payload(json.load(f))


def db_snapshot():
    with db() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("events", "sources", "constraints")}
        assumptions = [(r["id"], r["clock_offset"], r["uncertainty"])
                       for r in conn.execute(
                           "SELECT id, clock_offset, uncertainty FROM sources ORDER BY id")]
        constraint_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM constraints ORDER BY id")]
    return counts, assumptions, constraint_ids


class SampleCase(unittest.TestCase):
    """内置示例案件：默认假设下撤销 a1→m1 一条即可恢复可行。"""

    def setUp(self):
        load_sample()

    def test_default_assumptions_single_retraction(self):
        r = suggest_retractions()
        self.assertEqual(r["suggestion_type"], "constraint-retraction")
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["complete"])
        self.assertFalse(r["solutions_truncated"])
        self.assertEqual(r["min_retractions"], 1)
        # 统计：6 条人工/导入约束可撤销，5 条 source_order 受保护
        self.assertEqual(r["stats"]["retractable_constraints"], 6)
        self.assertEqual(r["stats"]["protected_constraints"], 5)
        self.assertLessEqual(r["stats"]["solves_used"], r["limits"]["max_solves"])
        # 唯一的最少方案：撤销 #7（a1→m1 causal）
        self.assertEqual(len(r["solutions"]), 1)
        sol = r["solutions"][0]
        self.assertEqual(len(sol["constraints"]), 1)
        c = sol["constraints"][0]
        self.assertEqual((c["from"], c["to"], c["kind"]), ("a1", "m1", "causal"))
        self.assertIsInstance(c["id"], int)
        self.assertNotEqual(c["kind"], "source_order")
        # 撤销后全量可行见证：8 个事件，每个最早可行时刻都在闭区间内
        w = sol["witness"]
        self.assertEqual(len(w["events"]), 8)
        for e in w["events"]:
            self.assertLessEqual(e["lo"], e["earliest"] + 1e-6)
            self.assertLessEqual(e["earliest"], e["hi"] + 1e-6)

    def test_unsaved_overrides_participate_already_feasible(self):
        # 页面未保存的校正假设参与计算：该假设下已可行 → 无需撤销
        r = suggest_retractions({"app-server": {"clock_offset": 50},
                                 "mobile-app": {"clock_offset": -30}})
        self.assertEqual(r["status"], "already_feasible")
        self.assertEqual(r["solutions"], [])
        self.assertIsNone(r["min_retractions"])

    def test_unsaved_overrides_participate_fraud(self):
        # 另一组未保存假设下，矛盾转移到 g3→a3（合规造假），建议撤销该声明约束
        r = suggest_retractions({"app-server": {"clock_offset": 60},
                                 "mobile-app": {"clock_offset": -30,
                                                "uncertainty": 1}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["min_retractions"], 1)
        self.assertEqual(len(r["solutions"]), 1)
        c = r["solutions"][0]["constraints"][0]
        self.assertEqual((c["from"], c["to"], c["kind"]), ("g3", "a3", "claimed"))

    def test_read_only_preview(self):
        before = db_snapshot()
        suggest_retractions()
        suggest_retractions({"app-server": {"clock_offset": 60}})
        self.assertEqual(before, db_snapshot())  # 预览不写入数据库


class SyntheticCases(unittest.TestCase):
    """合成数据：多解、多条撤销、平行边、受保护边与预算/深度限制。"""

    def test_two_equally_minimal_solutions(self):
        # 各边单独可行而整条路径无解：撤掉路径上任一条都可修复 → 2 个同样最少
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0.5},
                        {"id": "s2", "uncertainty": 0.75},
                        {"id": "s3", "uncertainty": 0.75}],
            "events": [
                {"id": "x", "source": "s1", "time": 100.5},   # [100, 101]
                {"id": "y", "source": "s2", "time": 99.75},   # [99, 100.5]
                {"id": "z", "source": "s3", "time": 98.75},   # [98, 99.5]
            ],
            "constraints": [{"from": "x", "to": "y"},
                            {"from": "y", "to": "z"}],
        })
        r = suggest_retractions()
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_retractions"], 1)
        self.assertEqual(len(r["solutions"]), 2)
        pairs = {tuple(sorted((c["from"], c["to"]) for c in s["constraints"]))
                 for s in r["solutions"]}
        self.assertEqual(pairs, {(("x", "y"),), (("y", "z"),)})
        for s in r["solutions"]:
            self.assertEqual(len(s["witness"]["events"]), 3)

    def test_pair_needed_depth_two(self):
        # 两条互不相干的不可能边：必须同时撤销 2 条
        reset_all()
        import_payload({
            "sources": [{"id": f"s{i}", "uncertainty": 0.5} for i in range(4)],
            "events": [
                {"id": "a1", "source": "s0", "time": 100},   # [99.5, 100.5]
                {"id": "b1", "source": "s1", "time": 90},    # [89.5, 90.5]
                {"id": "a2", "source": "s2", "time": 100},
                {"id": "b2", "source": "s3", "time": 90},
            ],
            "constraints": [{"from": "a1", "to": "b1"},
                            {"from": "a2", "to": "b2"}],
        })
        r = suggest_retractions()
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_retractions"], 2)
        self.assertEqual(len(r["solutions"]), 1)
        got = {(c["from"], c["to"]) for c in r["solutions"][0]["constraints"]}
        self.assertEqual(got, {("a1", "b1"), ("a2", "b2")})

    def test_parallel_edges_same_hop(self):
        # 同一对端点上两条人工约束（causal + claimed）：必须两条都撤才能打断这一跳
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0.5},
                        {"id": "s2", "uncertainty": 0.5}],
            "events": [
                {"id": "a", "source": "s1", "time": 100},   # [99.5, 100.5]
                {"id": "b", "source": "s2", "time": 90},    # [89.5, 90.5]
            ],
            "constraints": [{"from": "a", "to": "b", "kind": "causal"},
                            {"from": "a", "to": "b", "kind": "claimed"}],
        })
        r = suggest_retractions()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["min_retractions"], 2)
        self.assertEqual(len(r["solutions"]), 1)
        kinds = sorted(c["kind"] for c in r["solutions"][0]["constraints"])
        self.assertEqual(kinds, ["causal", "claimed"])

    def test_no_retraction_fix(self):
        # 极端漂移使 source_order 边自身与区间矛盾：撤销全部人工约束仍无解
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "drift_ppm": 2000000, "uncertainty": 0.1}],
            "events": [
                {"id": "p", "source": "s1", "time": 1000},
                {"id": "q", "source": "s1", "time": 1100},
            ],
            "constraints": [{"from": "p", "to": "q", "kind": "causal"}],
        })
        r = suggest_retractions()
        self.assertEqual(r["status"], "no_retraction_fix")
        self.assertTrue(r["complete"])
        self.assertEqual(r["solutions"], [])
        self.assertIsNone(r["min_retractions"])
        self.assertIn("没有仅靠撤销约束的修复方案", r["summary"])
        # 全部撤销后依然存在的矛盾路径（p→q，由受保护的 source_order 构成）
        w = r["remaining_witness"]
        self.assertIsNotNone(w)
        self.assertEqual(w["events"], ["p", "q"])

    def test_cap_five_solutions(self):
        # 6 跳长链、各边单独可行：6 个单条撤销方案，按上限只展示 5 个
        reset_all()
        events = [{"id": "e0", "source": "s0", "time": 100.5}]          # [100, 101]
        events += [{"id": f"e{i}", "source": f"s{i}", "time": 100}
                   for i in range(1, 6)]                                # [98, 102]
        events.append({"id": "e6", "source": "s6", "time": 98})         # [97, 99]
        import_payload({
            "sources": ([{"id": "s0", "uncertainty": 0.5}]
                        + [{"id": f"s{i}", "uncertainty": 2} for i in range(1, 6)]
                        + [{"id": "s6", "uncertainty": 1}]),
            "events": events,
            "constraints": [{"from": f"e{i}", "to": f"e{i+1}"} for i in range(6)],
        })
        r = suggest_retractions()
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_retractions"], 1)
        self.assertEqual(len(r["solutions"]), 5)   # 至多 5 个同样最少的方案
        self.assertTrue(r["solutions_truncated"])
        for s in r["solutions"]:
            self.assertEqual(len(s["constraints"]), 1)
            self.assertEqual(len(s["witness"]["events"]), 7)

    def test_depth_limit(self):
        # 4 条互不相干的不可能边：默认最多撤 3 条 → 限制内无解；放宽到 4 才找到
        reset_all()
        import_payload({
            "sources": [{"id": f"s{i}", "uncertainty": 0.5} for i in range(8)],
            "events": [
                *[{"id": f"a{i}", "source": f"s{i}", "time": 100} for i in range(4)],
                *[{"id": f"b{i}", "source": f"s{i+4}", "time": 90} for i in range(4)],
            ],
            "constraints": [{"from": f"a{i}", "to": f"b{i}"} for i in range(4)],
        })
        r = suggest_retractions()
        self.assertEqual(r["status"], "none_within_limits")
        self.assertTrue(r["complete"])          # 深度空间已穷尽，不是触限
        self.assertEqual(r["solutions"], [])
        r4 = suggest_retractions(max_depth=4)
        self.assertEqual(r4["status"], "ok")
        self.assertEqual(r4["min_retractions"], 4)
        self.assertEqual(len(r4["solutions"]), 1)

    def test_solve_budget_incomplete(self):
        # 求解次数预算触限：明确标为未完成，不给结论性判断
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0.5},
                        {"id": "s2", "uncertainty": 0.75},
                        {"id": "s3", "uncertainty": 0.75}],
            "events": [
                {"id": "x", "source": "s1", "time": 100.5},
                {"id": "y", "source": "s2", "time": 99.75},
                {"id": "z", "source": "s3", "time": 98.75},
            ],
            "constraints": [{"from": "x", "to": "y"},
                            {"from": "y", "to": "z"}],
        })
        # 预算只够初始检查 + 全撤检查：一个候选都复核不了
        r = suggest_retractions(max_solves=2)
        self.assertEqual(r["status"], "none_within_limits")
        self.assertFalse(r["complete"])
        self.assertEqual(r["solutions"], [])
        self.assertIn("未完成", r["summary"])
        # 预算够复核一个候选：找到 1 个方案但同层未穷尽 → 仍是未完成
        r2 = suggest_retractions(max_solves=3)
        self.assertEqual(r2["status"], "ok")
        self.assertFalse(r2["complete"])
        self.assertEqual(len(r2["solutions"]), 1)
        self.assertIn("未完成", r2["summary"])
        self.assertNotIn("共找到", r2["summary"])  # 触限时不得宣称已穷尽/最优

    def test_source_order_never_suggested(self):
        # 全部用例中，建议撤销的约束都不含 source_order 受保护边
        load_sample()
        r = suggest_retractions()
        self.assertEqual(r["status"], "ok")
        for s in r["solutions"]:
            for c in s["constraints"]:
                self.assertNotEqual(c["kind"], "source_order")


if __name__ == "__main__":
    unittest.main(verbosity=2)
