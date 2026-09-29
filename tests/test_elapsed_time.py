"""两事件耗时精确界审计的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_elapsed_time.py
"""
import json
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-elapsed-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import db, init_db  # noqa: E402
from app.elapsed_time import ElapsedError, audit_elapsed  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")


def load_sample():
    reset_all()
    with open(EXAMPLE, encoding="utf-8") as f:
        import_payload(json.load(f))


def payload(tau, radii, edges):
    n = len(tau)
    return {
        "sources": [{"id": f"s{i}", "uncertainty": radii[i]} for i in range(n)],
        "events": [{"id": f"e{i}", "source": f"s{i}", "time": tau[i]}
                   for i in range(n)],
        "constraints": [{"from": f"e{u}", "to": f"e{v}"} for u, v in edges],
    }


def witness_feasible(r, key):
    """校验见证是一版完整可行分配，并返回 (时刻映射, 见证)。"""
    w = r[key]
    times = {ev["id"]: ev["time"] for ev in w["events"]}
    # 每个事件时刻在自身闭区间内
    for ev in w["events"]:
        assert ev["lo"] - 1e-6 <= ev["time"] <= ev["hi"] + 1e-6
    return times, w


class ExactBounds(unittest.TestCase):
    """最小/最大值精确性：约束把朴素区间差收窄。"""

    def setUp(self):
        reset_all()
        # A[0,10] → B[4,6] → C[20,30]
        import_payload(payload([5, 5, 25], [5, 1, 5], [(0, 1), (1, 2)]))

    def test_bounds_are_exact_not_naive(self):
        r = audit_elapsed("e0", "e2", 10.0)
        self.assertTrue(r["feasible"])
        b = r["bounds"]
        # 朴素差 [10, 30]；联立 A→B→C 后：min = 20-6 = 14（B 压到最晚）
        self.assertEqual(b["minimum_seconds"], 14.0)
        self.assertEqual(b["maximum_seconds"], 30.0)
        self.assertEqual((b["naive_minimum_seconds"],
                          b["naive_maximum_seconds"]), (10.0, 30.0))
        self.assertTrue(b["start_reaches_end"])
        self.assertFalse(b["end_reaches_start"])
        self.assertFalse(b["same_component"])

    def test_minimum_and_maximum_witnesses(self):
        r = audit_elapsed("e0", "e2", 10.0)
        mn, mx = r["minimum_witness"], r["maximum_witness"]
        self.assertEqual(mn["kind"], "minimum")
        self.assertEqual(mn["elapsed"], 14.0)
        self.assertEqual(mn["end"]["time"], 20.0)      # 终点取最早
        self.assertEqual(mn["start"]["time"], 6.0)     # 起点被 B 压到晚于 4
        self.assertEqual(mx["kind"], "maximum")
        self.assertEqual(mx["elapsed"], 30.0)
        self.assertEqual(mx["start"]["time"], 0.0)
        self.assertEqual(mx["end"]["time"], 30.0)
        # 全量见证（全部 3 个事件，各自落在闭区间内）
        for w, expected in ((mn, {"e0": 6.0, "e1": 6.0, "e2": 20.0}),
                            # 最大见证：终点钉在 30；B 未被钉扎，取自身最晚 6
                            (mx, {"e0": 0.0, "e1": 6.0, "e2": 30.0})):
            self.assertEqual(len(w["events"]), 3)
            times = {ev["id"]: ev["time"] for ev in w["events"]}
            self.assertEqual(times, expected)

    def test_threshold_verdicts(self):
        # min=14 max=30
        self.assertEqual(audit_elapsed("e0", "e2", 10)["verdict"],
                         "must_exceed")          # 14 > 10
        self.assertEqual(audit_elapsed("e0", "e2", 14)["verdict"],
                         "possible_exceed")      # 14 ≤ 14 但 30 > 14（闭端点）
        self.assertEqual(audit_elapsed("e0", "e2", 20)["verdict"],
                         "possible_exceed")
        self.assertEqual(audit_elapsed("e0", "e2", 30)["verdict"],
                         "must_not_exceed")      # 30 ≤ 30（闭端点）
        self.assertEqual(audit_elapsed("e0", "e2", 0)["verdict"],
                         "must_exceed")

    def test_witness_is_full_feasible_assignment(self):
        r = audit_elapsed("e0", "e2", 10.0)
        for key in ("minimum_witness", "maximum_witness"):
            times, w = witness_feasible(r, key)
            self.assertGreaterEqual(times["e1"], times["e0"] - 1e-9)  # e0→e1
            self.assertGreaterEqual(times["e2"], times["e1"] - 1e-9)  # e1→e2
            self.assertAlmostEqual(times["e2"] - times["e0"],
                                   w["elapsed"], places=6)


class NegativeAndIndependent(unittest.TestCase):
    """无路径 / 反向约束：耗时可为负。"""

    def test_independent_point_events_negative(self):
        reset_all()
        import_payload(payload([100, 0], [0, 0], []))
        r = audit_elapsed("e0", "e1", 0)
        b = r["bounds"]
        self.assertEqual(b["minimum_seconds"], -100.0)
        self.assertEqual(b["maximum_seconds"], -100.0)
        self.assertFalse(b["start_reaches_end"])
        self.assertEqual(r["verdict"], "must_not_exceed")

    def test_independent_ranges_equal_naive(self):
        reset_all()
        # X[98,102], Y[-3,3]，无任何约束
        import_payload(payload([100, 0], [2, 3], []))
        r = audit_elapsed("e0", "e1", 0)
        b = r["bounds"]
        self.assertEqual(b["minimum_seconds"], -105.0)
        self.assertEqual(b["maximum_seconds"], -95.0)
        self.assertEqual((b["minimum_seconds"], b["maximum_seconds"]),
                         (b["naive_minimum_seconds"], b["naive_maximum_seconds"]))
        # 最大仍为负 → 必不超 0
        self.assertEqual(r["verdict"], "must_not_exceed")

    def test_reverse_edge_flips_bounds_nonpositive(self):
        reset_all()
        # p[9,11], q[10,12]，约束 q→p（t_p ≥ t_q），审计 p→q
        import_payload(payload([10, 11], [1, 1], [(1, 0)]))
        r = audit_elapsed("e0", "e1", 0)
        b = r["bounds"]
        self.assertEqual(b["maximum_seconds"], 0.0)   # 同刻可达
        self.assertEqual(b["minimum_seconds"], -1.0)  # p=11, q=10
        self.assertTrue(b["end_reaches_start"])
        self.assertFalse(b["start_reaches_end"])
        self.assertEqual(r["verdict"], "must_not_exceed")
        # 最小见证的差为负
        self.assertEqual(r["minimum_witness"]["elapsed"], -1.0)


class CyclesAllowSimultaneous(unittest.TestCase):
    """环（允许同刻）：环内差恒为 0。"""

    def test_two_cycle_zero(self):
        reset_all()
        import_payload(payload([10, 11], [1, 1], [(0, 1), (1, 0)]))
        r = audit_elapsed("e0", "e1", 0)
        b = r["bounds"]
        self.assertEqual(b["minimum_seconds"], 0.0)
        self.assertEqual(b["maximum_seconds"], 0.0)
        self.assertTrue(b["same_component"])
        self.assertEqual(r["verdict"], "must_not_exceed")  # 0 ≤ 0
        for w in (r["minimum_witness"], r["maximum_witness"]):
            self.assertTrue(w["start"]["in_cycle"])
            self.assertTrue(w["end"]["in_cycle"])
            self.assertEqual(w["start"]["time"], w["end"]["time"])

    def test_three_cycle_common_intersection(self):
        reset_all()
        # x[0,4] y[1,5] z[2,6]，三环；公共交集 [2,4]
        import_payload(payload([2, 3, 4], [2, 2, 2],
                               [(0, 1), (1, 2), (2, 0)]))
        r = audit_elapsed("e0", "e2", 0)
        self.assertEqual(r["bounds"]["minimum_seconds"], 0.0)
        self.assertEqual(r["bounds"]["maximum_seconds"], 0.0)
        self.assertTrue(r["bounds"]["same_component"])


class GlobalInfeasible(unittest.TestCase):
    """全局无解：展示矛盾见证，不输出耗时结论。"""

    def test_infeasible_returns_conflict_witness_no_verdict(self):
        reset_all()
        # a=10, b=0，半径 0，约束 a→b：lo(a)=10 > hi(b)=0
        import_payload(payload([10, 0], [0, 0], [(0, 1)]))
        r = audit_elapsed("e0", "e1", 5)
        self.assertFalse(r["feasible"])
        self.assertNotIn("verdict", r)
        self.assertNotIn("minimum_witness", r)
        self.assertNotIn("bounds", r)
        w = r["witness"]
        self.assertEqual(w["head"]["event"], "e0")
        self.assertEqual(w["tail"]["event"], "e1")
        self.assertGreater(w["overshoot"], 0)
        self.assertEqual(w["events"], ["e0", "e1"])

    def test_infeasible_sample_default(self):
        load_sample()
        # 默认假设下示例案件无解（a1→m1 单独即不可能）
        r = audit_elapsed("a1", "m1", 10)
        self.assertFalse(r["feasible"])
        self.assertNotIn("verdict", r)
        self.assertTrue(r["witness"]["steps"])


class Validation(unittest.TestCase):
    """未知 / 相同事件、非法阈值明确拒绝。"""

    def setUp(self):
        reset_all()
        import_payload(payload([10, 11], [1, 1], [(0, 1)]))

    def test_same_event_rejected(self):
        with self.assertRaises(ElapsedError):
            audit_elapsed("e0", "e0", 1)

    def test_unknown_event_rejected(self):
        with self.assertRaises(ElapsedError):
            audit_elapsed("nope", "e1", 1)
        with self.assertRaises(ElapsedError):
            audit_elapsed("e0", "nope", 1)

    def test_bad_threshold(self):
        for bad in (-1, -0.001, float("nan"), float("inf"),
                    float("-inf"), "10", None, True, [1], {}):
            with self.assertRaises(ElapsedError):
                audit_elapsed("e0", "e1", bad)

    def test_zero_threshold_allowed(self):
        r = audit_elapsed("e0", "e1", 0)
        self.assertEqual(r["threshold_seconds"], 0.0)

    def test_bad_event_id_type(self):
        for bad in (None, 123, "", "  "):
            with self.assertRaises(ElapsedError):
                audit_elapsed(bad, "e1", 1)


class OverridesAndReadOnly(unittest.TestCase):
    """携带未保存假设；只读、不受筛选影响。"""

    def test_overrides_make_feasible(self):
        reset_all()
        # a=10 r=1 [9,11], b=0 r=1 [-1,1]，a→b 默认无解
        import_payload(payload([10, 0], [1, 1], [(0, 1)]))
        self.assertFalse(audit_elapsed("e0", "e1", 0)["feasible"])
        # s0 offset +20：e0 真实时刻 -10 [-11,-9]，e0→e1 可行
        r = audit_elapsed("e0", "e1", 0,
                          overrides={"s0": {"clock_offset": 20}})
        self.assertTrue(r["feasible"])
        # b[-1,1] - a[-11,-9]：min = -1-(-9)=8, max = 1-(-11)=12
        self.assertEqual(r["bounds"]["minimum_seconds"], 8.0)
        self.assertEqual(r["bounds"]["maximum_seconds"], 12.0)

    def test_read_only(self):
        reset_all()
        import_payload(payload([10, 20], [1, 1], [(0, 1)]))
        with db() as conn:
            before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                      for t in ("events", "sources", "constraints")}
            off_before = conn.execute(
                "SELECT id, clock_offset FROM sources ORDER BY id").fetchall()
            off_before = [(r["id"], r["clock_offset"]) for r in off_before]
        audit_elapsed("e0", "e1", 5,
                      overrides={"s0": {"clock_offset": 3.5}})
        with db() as conn:
            after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                     for t in ("events", "sources", "constraints")}
            off_after = conn.execute(
                "SELECT id, clock_offset FROM sources ORDER BY id").fetchall()
            off_after = [(r["id"], r["clock_offset"]) for r in off_after]
        self.assertEqual(before, after)
        self.assertEqual(off_before, off_after)  # overrides 未落库


class Scale(unittest.TestCase):
    """5000 事件：深链性能与无递归。"""

    def test_5000_chain(self):
        reset_all()
        n = 5000
        import_payload({
            "sources": [{"id": "big", "uncertainty": 0.4}],
            "events": [{"id": f"e{i}", "source": "big", "time": 1_000_000 + i}
                       for i in range(n)],
        })
        t0 = time.monotonic()
        r = audit_elapsed("e0", f"e{n-1}", 0)
        elapsed = time.monotonic() - t0
        self.assertTrue(r["feasible"])
        b = r["bounds"]
        # 同来源 source_order 边间隔 1s > 0.8 总半径而不耦合；两端区间
        # ±0.4 直接决定总耗时：[4999-0.8, 4999+0.8] = [4998.2, 4999.8]
        self.assertAlmostEqual(b["minimum_seconds"], 4998.2, places=3)
        self.assertAlmostEqual(b["maximum_seconds"], 4999.8, places=3)
        self.assertEqual(len(r["minimum_witness"]["events"]), n)
        self.assertEqual(r["verdict"], "must_exceed")
        self.assertLess(elapsed, 20)

    def test_5000_tight_chain_constraints_bind(self):
        # 不确定度收窄到 0（点时刻）：source_order 链逐跳锁死，差恒定 n-1
        reset_all()
        n = 5000
        import_payload({
            "sources": [{"id": "big", "uncertainty": 0}],
            "events": [{"id": f"e{i}", "source": "big", "time": 1_000_000 + i}
                       for i in range(n)],
        })
        r = audit_elapsed("e0", f"e{n-1}", 0)
        b = r["bounds"]
        self.assertAlmostEqual(b["minimum_seconds"], n - 1, places=3)
        self.assertAlmostEqual(b["maximum_seconds"], n - 1, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
