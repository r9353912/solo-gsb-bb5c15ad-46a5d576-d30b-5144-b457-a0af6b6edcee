"""双来源事件对账的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_reconciliation.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-recon-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app import reconciliation as rc  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)


def ev(eid, src, t, etype="t", actor="u", summary=""):
    return {"id": eid, "source": src, "time": f"2026-01-01T00:00:{t}Z",
            "type": etype, "actor": actor, "summary": summary or eid}


def make(events, sources=None, **extra):
    sources = sources or [
        {"id": "s1", "name": "S1", "uncertainty": 0, "reliability": 0.9},
        {"id": "s2", "name": "S2", "uncertainty": 0, "reliability": 0.9},
    ]
    payload = {"sources": sources, "events": events}
    payload.update(extra)
    return import_payload(payload)


def pair_ids(r):
    return sorted((p["a"]["id"], p["b"]["id"]) for p in r["pairs"])


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        reset_all()

    # --------------------------------------------------------- 基本配对

    def test_basic_pairing_same_actor_type_within_tolerance(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        r = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual(r["stats"]["matched_pairs"], 1)
        p = r["pairs"][0]
        self.assertEqual((p["a"]["id"], p["b"]["id"]), ("a1", "b1"))
        self.assertEqual(p["mid_delta_ms"], 0)
        self.assertEqual(p["closed_gap_ms"], 0)
        self.assertEqual(r["unmatched_a"], [])
        self.assertEqual(r["unmatched_b"], [])

    def test_different_actor_or_type_not_paired(self):
        make([
            ev("a1", "s1", "00", actor="alice"),
            ev("a2", "s1", "00", etype="login"),
            ev("b1", "s2", "00", actor="bob"),      # actor 不同
            ev("b2", "s2", "00", etype="logout"),   # type 不同
        ])
        r = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual(r["stats"]["matched_pairs"], 0)
        self.assertEqual({e["id"] for e in r["unmatched_a"]}, {"a1", "a2"})
        self.assertEqual({e["id"] for e in r["unmatched_b"]}, {"b1", "b2"})

    def test_empty_actor_never_paired(self):
        make([
            ev("a1", "s1", "00", actor=""),
            ev("a2", "s1", "00", actor="   "),     # 纯空白等同空
            ev("b1", "s2", "00", actor="u"),
        ])
        r = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual(r["stats"]["matched_pairs"], 0)
        # 即使对侧有 actor，空白 actor 也不参与；两侧都不匹配
        self.assertEqual({e["id"] for e in r["unmatched_a"]}, {"a1", "a2"})

    def test_actor_match_is_exact_after_strip(self):
        make([
            ev("a1", "s1", "00", actor="u "),       # 去尾空白后相等
            ev("b1", "s2", "00", actor=" u"),
            ev("b2", "s2", "00", actor="U"),        # 大小写不做折叠
        ])
        r = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual(pair_ids(r), [("a1", "b1")])

    def test_tolerance_boundary_closed_interval(self):
        # a1=0ms, b1=1000ms；闭区间点间距恰 1000ms，tol=1000 闭区间应可配
        make([ev("a1", "s1", "00"), ev("b1", "s2", "01")])
        self.assertEqual(rc.reconcile_sources("s1", "s2", 999)["stats"]["matched_pairs"], 0)
        r = rc.reconcile_sources("s1", "s2", 1000)
        self.assertEqual(r["stats"]["matched_pairs"], 1)
        self.assertEqual(r["pairs"][0]["closed_gap_ms"], 1000)

    def test_overlapping_intervals_have_zero_closed_gap(self):
        # 两侧不确定度各 1s，中点相距 1.5s，区间 [-1,1] 与 [0.5,2.5] 重叠
        make([ev("a1", "s1", "00"), ev("b1", "s2", "01.500")], sources=[
            {"id": "s1", "uncertainty": 1}, {"id": "s2", "uncertainty": 1}])
        r = rc.reconcile_sources("s1", "s2", 0)  # 容差 0：闭区间重叠即可
        self.assertEqual(r["stats"]["matched_pairs"], 1)
        self.assertEqual(r["pairs"][0]["closed_gap_ms"], 0)
        self.assertEqual(r["pairs"][0]["mid_delta_abs_ms"], 1500)

    # --------------------------------------------------------- 舍入

    def test_half_up_rounding_unit(self):
        f = rc.round_half_up_ms
        self.assertEqual(f(0.0000), 0)
        self.assertEqual(f(0.0005), 1)    # 0.5ms 进位（非银行家舍入）
        self.assertEqual(f(0.0015), 2)    # 1.5ms 进位
        self.assertEqual(f(0.0025), 3)
        self.assertEqual(f(-0.0005), -1)  # 负值向远离 0 方向进位

    def test_half_up_timestamp_boundary(self):
        # b1 在 1.5ms（half-up → 2ms）：tol=1 拒绝、tol=2 接受
        make([ev("a1", "s1", "00.000"),
              {"id": "b1", "source": "s2", "time": "2026-01-01T00:00:00.001500Z",
               "type": "t", "actor": "u"}])
        self.assertEqual(rc.reconcile_sources("s1", "s2", 1)["stats"]["matched_pairs"], 0)
        self.assertEqual(rc.reconcile_sources("s1", "s2", 2)["stats"]["matched_pairs"], 1)

    def test_tolerance_half_up_normalized(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00.000500")])  # b1 = 0.5ms → 1ms
        # 0.4ms 容差 half-up → 0ms：拒绝；0.5ms → 1ms：接受
        self.assertEqual(rc.reconcile_sources("s1", "s2", 0.4)["stats"]["matched_pairs"], 0)
        r = rc.reconcile_sources("s1", "s2", 0.5)
        self.assertEqual(r["tolerance_ms"], 1)
        self.assertEqual(r["stats"]["matched_pairs"], 1)

    # ------------------------------------------------- 全局最优（反贪心）

    def test_each_event_at_most_one_pair(self):
        make([
            ev("a1", "s1", "00"),
            ev("b1", "s2", "00"),
            ev("b2", "s2", "00"),  # 与 b1 同一时刻，只能配一个
        ])
        r = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual(r["stats"]["matched_pairs"], 1)
        used_a = [p["a"]["id"] for p in r["pairs"]]
        used_b = [p["b"]["id"] for p in r["pairs"]]
        self.assertEqual(len(used_a), len(set(used_a)))
        self.assertEqual(len(used_b), len(set(used_b)))
        # 恰好一个 B 事件未匹配
        self.assertEqual({e["id"] for e in r["unmatched_b"]},
                         {"b1", "b2"} - set(used_b))

    def test_global_optimum_beats_nearest_greedy_cardinality(self):
        # 经典"最近邻贪心"陷阱（按最小距离先配）：
        #   a1@0  仅能配 b1@10
        #   a2@10 可配 b1@10(0) / b2@20(10)，tol=10ms
        # 最近邻贪心先取 a2-b1(距离0)，a1 无对 => 仅 1 对；
        # 全局最优 a1-b1 + a2-b2 => 2 对。
        make([
            ev("a1", "s1", "00.000"), ev("a2", "s1", "00.010"),
            ev("b1", "s2", "00.010"), ev("b2", "s2", "00.020"),
        ])
        r = rc.reconcile_sources("s1", "s2", 10)
        self.assertEqual(r["stats"]["matched_pairs"], 2)
        self.assertEqual(pair_ids(r), [("a1", "b1"), ("a2", "b2")])

    def test_maximize_count_then_minimize_midpoint_sum(self):
        # 全部交叉可行（tol=10s）：两种完美匹配，交叉代价 20s、同序代价 0
        make([
            ev("a1", "s1", "00"), ev("a2", "s1", "10"),
            ev("b1", "s2", "00"), ev("b2", "s2", "10"),
        ])
        r = rc.reconcile_sources("s1", "s2", 10000)
        self.assertEqual(r["stats"]["matched_pairs"], 2)
        self.assertEqual(pair_ids(r), [("a1", "b1"), ("a2", "b2")])
        self.assertEqual(r["stats"]["mid_delta_abs_sum_ms"], 0)

    def test_fewer_pairs_preferred_over_forbidden(self):
        # a2 只能配 b2；a1 可配 b1,b2；最优必须把 b2 让给 a2
        make([
            ev("a1", "s1", "00"), ev("a2", "s1", "05"),
            ev("b1", "s2", "00"), ev("b2", "s2", "05"),
        ])
        r = rc.reconcile_sources("s1", "s2", 5000)
        self.assertEqual(pair_ids(r), [("a1", "b1"), ("a2", "b2")])

    def test_stable_output_sorted_and_deterministic(self):
        # 四个同刻事件：任何完美匹配代价相同；输出必须确定且按 A 侧 ID 排序
        make([
            ev("a1", "s1", "00"), ev("a2", "s1", "00"),
            ev("b1", "s2", "00"), ev("b2", "s2", "00"),
        ])
        r1 = rc.reconcile_sources("s1", "s2", 0)
        r2 = rc.reconcile_sources("s1", "s2", 0)
        ids1 = [(p["a"]["id"], p["b"]["id"]) for p in r1["pairs"]]
        ids2 = [(p["a"]["id"], p["b"]["id"]) for p in r2["pairs"]]
        self.assertEqual(ids1, ids2)  # 确定性
        a_ids = [p["a"]["id"] for p in r1["pairs"]]
        self.assertEqual(a_ids, sorted(a_ids))  # 按 A 侧事件 ID 排序
        # 未匹配列表同样按 ID 排序
        evs = [ev(f"a{i}", "s1", "00", actor=f"g{i}") for i in range(3)]
        reset_all()
        make(evs + [ev("b0", "s2", "00", actor="zzz")])
        r = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual([e["id"] for e in r["unmatched_a"]],
                         sorted(e["id"] for e in r["unmatched_a"]))

    # --------------------------------------------------------- 分组独立

    def test_side_swap_symmetric_and_rectangular(self):
        # B 侧事件更多（矩形 n<m）：换侧结果事件对一致，方向标签随之交换
        make([
            ev("a1", "s1", "00"),
            ev("b1", "s2", "00"), ev("b2", "s2", "00"),
        ])
        r1 = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual(r1["stats"]["matched_pairs"], 1)
        self.assertEqual(pair_ids(r1), [("a1", "b1")])
        self.assertEqual({e["id"] for e in r1["unmatched_b"]}, {"b2"})
        # 交换来源顺序：仍配同一对，A/B 标签对调
        r2 = rc.reconcile_sources("s2", "s1", 0)
        self.assertEqual(r2["pairs"][0]["a"]["id"], "b1")
        self.assertEqual(r2["pairs"][0]["b"]["id"], "a1")
        self.assertEqual({e["id"] for e in r2["unmatched_a"]}, {"b2"})
        self.assertEqual(r2["pairs"][0]["mid_delta_ms"],
                     -r1["pairs"][0]["mid_delta_ms"])

    def test_float_tolerance_is_half_up_rounded(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00.000500")])
        r = rc.reconcile_sources("s1", "s2", 0.5)
        self.assertIsInstance(r["tolerance_ms"], int)
        self.assertEqual(r["tolerance_ms"], 1)
        self.assertEqual(r["stats"]["matched_pairs"], 1)

    def test_same_assumption_is_read_only_no_side_effects(self):
        # 连续两次相同请求结果一致（只读、无状态污染）
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        r1 = rc.reconcile_sources("s1", "s2", 10, {"s1": {"clock_offset": 1}})
        r2 = rc.reconcile_sources("s1", "s2", 10, {"s1": {"clock_offset": 1}})
        self.assertEqual(pair_ids(r1), pair_ids(r2))
        self.assertEqual(
            r1["stats"]["mid_delta_abs_sum_ms"],
            r2["stats"]["mid_delta_abs_sum_ms"])

    def test_groups_independent(self):
        make([
            ev("a1", "s1", "00", actor="alice", etype="login"),
            ev("a2", "s1", "00", actor="bob", etype="login"),
            ev("b1", "s2", "00", actor="bob", etype="login"),
            ev("b2", "s2", "00", actor="alice", etype="logout"),  # type 不同
        ])
        r = rc.reconcile_sources("s1", "s2", 0)
        # 只有 bob/login 与 bob 配；alice/logout 因 type 不同不配
        self.assertEqual(pair_ids(r), [("a2", "b1")])

    # --------------------------------------------------------- 未保存假设

    def test_unsaved_overrides_shift_intervals(self):
        # s1 时钟快 1s（offset=1）：a1 本地 00:01 校正后恰为 00:00
        make([ev("a1", "s1", "01"), ev("b1", "s2", "00")])
        # 不用未保存假设：相距 1000ms，tol=0 不配
        self.assertEqual(rc.reconcile_sources("s1", "s2", 0)["stats"]["matched_pairs"], 0)
        # 携带未保存 offset=1：a1 区间平移到 00:00，配 b1
        r = rc.reconcile_sources("s1", "s2", 0, {"s1": {"clock_offset": 1}})
        self.assertEqual(r["stats"]["matched_pairs"], 1)
        self.assertEqual(r["pairs"][0]["mid_delta_abs_ms"], 0)

    # --------------------------------------------------------- 输入校验

    def test_same_source_rejected(self):
        make([ev("a1", "s1", "00")])
        with self.assertRaisesRegex(rc.ReconcileError, "不同来源"):
            rc.reconcile_sources("s1", "s1", 0)

    def test_missing_source_404(self):
        make([ev("a1", "s1", "00")])
        with self.assertRaisesRegex(rc.ReconcileError, "来源不存在"):
            rc.reconcile_sources("s1", "ghost", 0)

    def test_blank_source_rejected(self):
        with self.assertRaisesRegex(rc.ReconcileError, "必须选择"):
            rc.reconcile_sources("  ", "s2", 0)

    def test_negative_non_numeric_non_finite_tolerance(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        for bad in (-1, "-1", "abc", float("inf"), float("-inf"), float("nan")):
            with self.assertRaises(rc.ReconcileError):
                rc.reconcile_sources("s1", "s2", bad)

    # --------------------------------------------------------- 200 上限

    def test_limit_200_per_side_accepted_and_exceed_rejected(self):
        events = [ev(f"a{i:03d}", "s1", "00", actor=f"u{i}") for i in range(200)]
        events += [ev(f"b{i:03d}", "s2", "00", actor=f"u{i}") for i in range(200)]
        make(events)
        r = rc.reconcile_sources("s1", "s2", 0)
        self.assertEqual(r["stats"]["events_a"], 200)
        self.assertEqual(r["stats"]["events_b"], 200)
        self.assertEqual(r["stats"]["matched_pairs"], 200)

        reset_all()
        events = [ev(f"a{i:03d}", "s1", "00", actor=f"u{i}") for i in range(201)]
        events.append(ev("b0", "s2", "00"))
        make(events)
        with self.assertRaisesRegex(rc.ReconcileError, "200"):
            rc.reconcile_sources("s1", "s2", 0)

    # --------------------------------------------------------- 只读 / 结构

    def test_response_is_read_only_shape(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        r = rc.reconcile_sources("s1", "s2", 500)
        self.assertEqual(r["reconcile_type"], "dual-source-event-reconciliation")
        self.assertEqual(r["sides"]["a"]["source_id"], "s1")
        self.assertEqual(r["tolerance_ms"], 500)
        p = r["pairs"][0]
        for key in ("a", "b", "actor", "event_type", "mid_delta_ms",
                    "mid_delta_abs_ms", "closed_gap_ms", "within_tolerance"):
            self.assertIn(key, p)
        for evrec in (p["a"], p["b"]):
            for key in ("id", "source_id", "event_type", "actor", "summary",
                        "lo_ms", "hi_ms", "mid_ms", "lo_text", "hi_text",
                        "mid_text", "est_text"):
                self.assertIn(key, evrec)
        self.assertTrue(p["within_tolerance"])
        self.assertIsInstance(p["mid_delta_ms"], int)
        self.assertIsInstance(p["closed_gap_ms"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
