"""全局时序可行性求解的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_feasibility.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-feas-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import db, init_db  # noqa: E402
from app.feasibility import check_feasibility  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")


def load_sample():
    reset_all()
    with open(EXAMPLE, encoding="utf-8") as f:
        import_payload(json.load(f))


class SampleCase(unittest.TestCase):
    """内置示例案件：默认假设下无解，校正假设后全局可行。"""

    def setUp(self):
        load_sample()

    def test_default_assumptions_infeasible(self):
        r = check_feasibility()
        self.assertFalse(r["feasible"])
        self.assertEqual(r["check_type"], "global-temporal-feasibility")
        w = r["witness"]
        # a1(登录提交) → m1(MFA 推送) 单独即不可能：lo(a1) > hi(m1)
        self.assertEqual(w["head"]["event"], "a1")
        self.assertEqual(w["head"]["bound"], "lo")
        self.assertEqual(w["tail"]["event"], "m1")
        self.assertEqual(w["tail"]["bound"], "hi")
        self.assertGreater(w["head"]["value"], w["tail"]["value"])  # 两端界限冲突
        self.assertEqual(w["events"], ["a1", "m1"])
        self.assertEqual(len(w["steps"]), 1)
        step = w["steps"][0]
        self.assertEqual(step["from"], "a1")
        self.assertEqual(step["to"], "m1")
        self.assertIsInstance(step["constraint_id"], int)  # 带约束 ID
        self.assertFalse(step["individually_feasible"])
        self.assertFalse(w["all_edges_individually_feasible"])
        self.assertGreater(w["overshoot"], 0)
        # 统计：6 条声明约束 + 5 条 source_order，全部参与联立
        self.assertEqual(r["stats"]["event_count"], 8)
        self.assertEqual(r["stats"]["constraint_count"], 11)
        self.assertEqual(r["stats"]["skipped_constraints"], 0)

    def test_corrected_offsets_still_expose_fraud(self):
        # 修正时钟后 a1→m1 消除，但 g3→a3 的合规造假仍使全局无解
        r = check_feasibility({"app-server": {"clock_offset": 60},
                               "mobile-app": {"clock_offset": -30,
                                              "uncertainty": 1}})
        self.assertFalse(r["feasible"])
        w = r["witness"]
        self.assertEqual(w["head"]["event"], "g3")
        self.assertEqual(w["tail"]["event"], "a3")
        self.assertEqual(w["events"], ["g3", "a3"])
        self.assertEqual(w["steps"][0]["kind"], "claimed")
        self.assertFalse(w["all_edges_individually_feasible"])

    def test_corrected_assumptions_feasible_witness(self):
        # app +50s / mobile -30s：全部边相容且无环 → 全局可行，返回最早可行时刻
        r = check_feasibility({"app-server": {"clock_offset": 50},
                               "mobile-app": {"clock_offset": -30}})
        self.assertTrue(r["feasible"])
        self.assertEqual(len(r["events"]), 8)
        by_id = {e["id"]: e for e in r["events"]}
        # 每个事件的最早可行时刻都在其闭区间内
        for e in r["events"]:
            self.assertLessEqual(e["lo"], e["earliest"] + 1e-6)
            self.assertLessEqual(e["earliest"], e["hi"] + 1e-6)
            self.assertAlmostEqual(e["hi"] - e["earliest"], e["slack"], places=4)
            self.assertTrue(e["earliest_text"].endswith("Z"))
        # 约束被满足：g1→a1、a1→m1、m2→g2、g3→a3
        self.assertLessEqual(by_id["g1"]["earliest"], by_id["a1"]["earliest"])
        self.assertLessEqual(by_id["a1"]["earliest"], by_id["m1"]["earliest"])
        self.assertLessEqual(by_id["m2"]["earliest"], by_id["g2"]["earliest"])
        self.assertLessEqual(by_id["g3"]["earliest"], by_id["a3"]["earliest"])
        # 无环
        self.assertEqual(r["stats"]["cycle_count"], 0)
        self.assertFalse(any(e["in_cycle"] for e in r["events"]))

    def test_read_only_and_filter_free(self):
        # 检查不改写事件或假设；且不接受筛选（签名里就没有筛选参数）
        with db() as conn:
            before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                      for t in ("events", "sources", "constraints")}
            stored_before = conn.execute(
                "SELECT id, clock_offset, uncertainty FROM sources").fetchall()
            stored_before = [(r["id"], r["clock_offset"], r["uncertainty"])
                             for r in stored_before]
        check_feasibility({"app-server": {"clock_offset": 50}})
        with db() as conn:
            after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                     for t in ("events", "sources", "constraints")}
            stored_after = conn.execute(
                "SELECT id, clock_offset, uncertainty FROM sources").fetchall()
            stored_after = [(r["id"], r["clock_offset"], r["uncertainty"])
                            for r in stored_after]
        self.assertEqual(before, after)
        self.assertEqual(stored_before, stored_after)  # overrides 未持久化


class SyntheticCases(unittest.TestCase):
    """合成数据：DAG 传播、环交集、单不可能边、逐边可行但整体无解。"""

    def test_dag_longest_path(self):
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 1},
                        {"id": "s2", "uncertainty": 2}],
            "events": [
                {"id": "A", "source": "s1", "time": 100},
                {"id": "B", "source": "s1", "time": 200},
                {"id": "C", "source": "s2", "time": 250},
            ],
            "constraints": [{"from": "B", "to": "C"}, {"from": "A", "to": "C"}],
        })
        r = check_feasibility()
        self.assertTrue(r["feasible"])
        by_id = {e["id"]: e for e in r["events"]}
        # 最早可行时刻 = max(自身 lo, 所有前驱的最早可行时刻)
        self.assertAlmostEqual(by_id["A"]["earliest"], 99.0, places=4)
        self.assertAlmostEqual(by_id["B"]["earliest"], 199.0, places=4)
        self.assertAlmostEqual(by_id["C"]["earliest"], 248.0, places=4)
        # 事件按最早可行时刻排序返回
        self.assertEqual([e["id"] for e in r["events"]], ["A", "B", "C"])

    def test_cycle_with_common_intersection_feasible(self):
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 10},
                        {"id": "s2", "uncertainty": 10}],
            "events": [
                {"id": "x", "source": "s1", "time": 100},   # [90, 110]
                {"id": "y", "source": "s2", "time": 105},   # [95, 115]
            ],
            "constraints": [{"from": "x", "to": "y"}, {"from": "y", "to": "x"}],
        })
        r = check_feasibility()
        self.assertTrue(r["feasible"])  # 公共交集 [95,110] 非空，环允许同刻
        self.assertEqual(r["stats"]["cycle_count"], 1)
        self.assertEqual(r["stats"]["events_in_cycles"], 2)
        by_id = {e["id"]: e for e in r["events"]}
        # 环内事件被钉在同一时刻：公共下界 max(lo) = 95
        self.assertAlmostEqual(by_id["x"]["earliest"], 95.0, places=4)
        self.assertAlmostEqual(by_id["y"]["earliest"], 95.0, places=4)
        self.assertTrue(all(e["in_cycle"] for e in r["events"]))

    def test_cycle_without_common_intersection_infeasible(self):
        # 每条边单独可行，但环内区间无公共交集 → 整条路径无解（核心覆盖场景）
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 2},
                        {"id": "s2", "uncertainty": 0.5},
                        {"id": "s3", "uncertainty": 1}],
            "events": [
                {"id": "x", "source": "s1", "time": 102},    # [100, 104]
                {"id": "y", "source": "s2", "time": 101.5},  # [101, 102]
                {"id": "z", "source": "s3", "time": 104},    # [103, 105]
            ],
            "constraints": [{"from": "x", "to": "y"},
                            {"from": "y", "to": "z"},
                            {"from": "z", "to": "x"}],
        })
        r = check_feasibility()
        self.assertFalse(r["feasible"])
        w = r["witness"]
        # 两端冲突界限：max(lo)=z.lo=103 vs min(hi)=y.hi=102
        self.assertEqual(w["head"]["event"], "z")
        self.assertEqual(w["tail"]["event"], "y")
        self.assertAlmostEqual(w["head"]["value"], 103.0, places=4)
        self.assertAlmostEqual(w["tail"]["value"], 102.0, places=4)
        self.assertAlmostEqual(w["overshoot"], 1.0, places=4)
        # 矛盾路径 z→x→y 在环内闭合，每步都带约束 ID 且单独可行
        self.assertEqual(w["events"], ["z", "x", "y"])
        self.assertEqual([(s["from"], s["to"]) for s in w["steps"]],
                         [("z", "x"), ("x", "y")])
        self.assertTrue(w["all_edges_individually_feasible"])
        for s in w["steps"]:
            self.assertIsInstance(s["constraint_id"], int)
            self.assertTrue(s["individually_feasible"])
        self.assertIn("公共交集", w["explanation"])

    def test_single_impossible_edge(self):
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 1},
                        {"id": "s2", "uncertainty": 1}],
            "events": [
                {"id": "a", "source": "s1", "time": 100},   # [99, 101]
                {"id": "b", "source": "s2", "time": 105},   # [104, 106]
            ],
            "constraints": [{"from": "b", "to": "a"}],      # lo(b)=104 > hi(a)=101
        })
        r = check_feasibility()
        self.assertFalse(r["feasible"])
        w = r["witness"]
        self.assertEqual(w["head"]["event"], "b")
        self.assertEqual(w["tail"]["event"], "a")
        self.assertEqual(w["events"], ["b", "a"])
        self.assertFalse(w["steps"][0]["individually_feasible"])
        self.assertFalse(w["all_edges_individually_feasible"])
        self.assertAlmostEqual(w["overshoot"], 3.0, places=4)

    def test_empty_database_feasible(self):
        reset_all()
        r = check_feasibility()
        self.assertTrue(r["feasible"])
        self.assertEqual(r["events"], [])
        self.assertEqual(r["stats"]["event_count"], 0)


class ScaleAndRecursion(unittest.TestCase):
    """5000 事件规模：性能与无递归（深图不爆栈）。"""

    def test_5000_events_chain_feasible(self):
        reset_all()
        n = 5000
        import_payload({
            "sources": [{"id": "big", "uncertainty": 0.4}],
            "events": [{"id": f"e{i}", "source": "big", "time": 1_000_000 + i}
                       for i in range(n)],
        })  # 自动生成 4999 条 source_order 严格时序边
        t0 = time.monotonic()
        r = check_feasibility()
        elapsed = time.monotonic() - t0
        self.assertTrue(r["feasible"])
        self.assertEqual(len(r["events"]), n)
        self.assertEqual(r["stats"]["constraint_count"], n - 1)
        by_id = {e["id"]: e for e in r["events"]}
        # 链式传播：每个事件的最早可行时刻 = 自身 lo（间隔 1s > 2×0.4 半径）
        self.assertAlmostEqual(by_id["e0"]["earliest"], 1_000_000 - 0.4, places=4)
        self.assertAlmostEqual(by_id[f"e{n-1}"]["earliest"],
                               1_000_000 + n - 1 - 0.4, places=4)
        self.assertLess(elapsed, 20)  # 交互量级（实测远低于此）

    def test_5000_cycle_infeasible_deep_witness(self):
        # 5000 节点环：每条边单独可行、无公共交集；矛盾路径长 4999 步，
        # 递归实现必然爆栈，迭代实现必须正常返回。
        reset_all()
        n = 5000
        events = [{"id": "e0", "source": "s0", "time": 1_002_500}]  # max(lo)
        events += [{"id": f"e{i}", "source": f"s{i}", "time": 1_002_499}
                   for i in range(1, n - 1)]
        events.append({"id": f"e{n-1}", "source": f"s{n-1}", "time": 1_002_498})
        constraints = ([{"from": f"e{i}", "to": f"e{i+1}"} for i in range(n - 1)]
                       + [{"from": f"e{n-1}", "to": "e0"}])
        import_payload({
            "sources": [{"id": f"s{i}", "uncertainty": 0.6} for i in range(n)],
            "events": events,
            "constraints": constraints,
        })
        r = check_feasibility()
        self.assertFalse(r["feasible"])
        w = r["witness"]
        # 两端冲突界限：e0.lo = 1002499.4 > e4999.hi = 1002498.6
        self.assertEqual(w["head"]["event"], "e0")
        self.assertEqual(w["tail"]["event"], f"e{n-1}")
        self.assertAlmostEqual(w["overshoot"], 0.8, places=3)
        # 深路径：e0 → e1 → … → e4999，4999 步、每步带约束 ID 且单独可行
        self.assertEqual(len(w["events"]), n)
        self.assertEqual(len(w["steps"]), n - 1)
        self.assertTrue(w["all_edges_individually_feasible"])
        self.assertTrue(all(s["individually_feasible"] for s in w["steps"]))
        self.assertTrue(all(isinstance(s["constraint_id"], int)
                            for s in w["steps"]))
        self.assertEqual(r["stats"]["cycle_count"], 1)
        self.assertEqual(r["stats"]["events_in_cycles"], n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
