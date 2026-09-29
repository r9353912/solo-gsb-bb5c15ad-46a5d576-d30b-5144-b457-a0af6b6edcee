"""锚点校准（双来源对账结果上的 offset 反推）纯标准库测试。

用法:  python tests/test_anchor_calibration.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-anchor-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app import anchor_calibration as ac  # noqa: E402
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


class AnchorCalibrationTest(unittest.TestCase):
    def setUp(self):
        reset_all()

    # --------------------------------------------------------- 基本范围

    def test_point_intervals_single_anchor_exact_range(self):
        # 不确定度 0：s1@00、s2@01。校准 s1（A）时平移 -δ 后须等于 s2：
        # δ ∈ [lo_a - hi_b, hi_a - lo_b] = [-1000, -1000] ms
        make([ev("a1", "s1", "00"), ev("b1", "s2", "01")])
        r = ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "b1"}])
        self.assertTrue(r["feasible"])
        ix = r["intersection"]
        self.assertEqual((ix["delta_range_ms"]["lo"], ix["delta_range_ms"]["hi"]),
                         (-1000, -1000))
        self.assertEqual(ix["delta_midpoint_ms"], -1000)
        self.assertEqual(r["current_offset_seconds"], 0)
        self.assertEqual(ix["absolute_offset_midpoint_seconds"], -1.0)

    def test_calibrate_b_side_flips_sign(self):
        # 校准 s2（B）：s2 本地快 1s，offset 应 +1000ms
        make([ev("a1", "s1", "00"), ev("b1", "s2", "01")])
        r = ac.calibrate_anchors("s1", "s2", "b", [{"a": "a1", "b": "b1"}])
        self.assertTrue(r["feasible"])
        self.assertEqual(r["calibrated_source_id"], "s2")
        self.assertEqual(r["reference_source_id"], "s1")
        self.assertEqual(
            (r["intersection"]["delta_range_ms"]["lo"],
             r["intersection"]["delta_range_ms"]["hi"]),
            (1000, 1000))

    def test_uncertainty_widens_range_closed_intervals(self):
        # 半径各 1s：a 中点 0（区间 [-1,1]），b 中点 2（区间 [1,3]）。
        # 校准 A：δ ∈ [lo_a - hi_b, hi_a - lo_r] = [-1000 - 3000, 1000 - 1000]
        #       = [-4000, 0] ms；δ=-4000 时 a.hi 恰接 b.lo，δ=0 时 a.hi 恰接。
        make([ev("a1", "s1", "00"), ev("b1", "s2", "02")], sources=[
            {"id": "s1", "uncertainty": 1}, {"id": "s2", "uncertainty": 1}])
        r = ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "b1"}])
        self.assertTrue(r["feasible"])
        ix = r["intersection"]
        self.assertEqual((ix["delta_range_ms"]["lo"], ix["delta_range_ms"]["hi"]),
                         (-4000, 0))
        # 中点和为偶：(-4000+0)/2 = -2000ms
        self.assertEqual(ix["delta_midpoint_ms"], -2000)

    def test_half_ms_midpoint_granularity(self):
        # 校准方事件在 0.5ms（时间戳）、自带 0.5ms 抖动（error_ms=0.5）、
        # 不确定度 0：est=0.5ms、半径 0.5ms → 真实区间恰 [0, 1] ms（宽度 1）。
        # 参照方 b1 在整秒点 [0,0]：δ ∈ [0, 1] ms；端点和为奇数，
        # 中点按整数毫秒向下取整 → 0ms（0.5ms 粒度）。
        make([
            {"id": "a1", "source": "s1", "time": "2026-01-01T00:00:00.000500Z",
             "type": "t", "actor": "u", "error_ms": 0.5},
            ev("b1", "s2", "00"),
        ], sources=[
            {"id": "s1", "uncertainty": 0}, {"id": "s2", "uncertainty": 0}])
        r = ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "b1"}])
        self.assertTrue(r["feasible"])
        ix = r["intersection"]
        self.assertEqual((ix["delta_range_ms"]["lo"], ix["delta_range_ms"]["hi"]),
                         (0, 1))
        self.assertEqual(ix["delta_midpoint_ms"], 0)
        self.assertEqual(ix["delta_midpoint_seconds"], 0.0)
        self.assertEqual(ix["absolute_offset_midpoint_ms"], 0)
        self.assertLessEqual(ix["delta_range_ms"]["lo"], ix["delta_midpoint_ms"])
        self.assertLessEqual(ix["delta_midpoint_ms"], ix["delta_range_ms"]["hi"])

    # --------------------------------------------------------- 多锚点交集

    def test_multiple_anchors_intersection(self):
        # s1: a1@00, a2@10；s2: b1@01（a1 要 δ=-1000）、b2@08（a2 要 δ=+2000）
        make([ev("a1", "s1", "00"), ev("a2", "s1", "10"),
              ev("b1", "s2", "01"), ev("b2", "s2", "08")])
        r = ac.calibrate_anchors("s1", "s2", "a",
                                 [{"a": "a1", "b": "b1"}, {"a": "a2", "b": "b2"}])
        # 锚点 1：δ=-1000；锚点 2：δ=+2000 → 交集为空
        self.assertFalse(r["feasible"])
        c = r["conflict"]
        self.assertEqual(c["lower_anchor"]["a"]["id"], "a2")  # max lo = +2000
        self.assertEqual(c["upper_anchor"]["a"]["id"], "a1")  # min hi = -1000

    def test_consistent_anchors_nonempty_intersection(self):
        # 两对点区间一致差 1s：δ=-1000 都满足
        make([ev("a1", "s1", "00"), ev("a2", "s1", "10"),
              ev("b1", "s2", "01"), ev("b2", "s2", "11")])
        r = ac.calibrate_anchors("s1", "s2", "a",
                                 [{"a": "a1", "b": "b1"}, {"a": "a2", "b": "b2"}])
        self.assertTrue(r["feasible"])
        ix = r["intersection"]
        self.assertEqual((ix["delta_range_ms"]["lo"], ix["delta_range_ms"]["hi"]),
                         (-1000, -1000))

    def test_intersection_uses_max_lo_min_hi_with_widths(self):
        # a1 半径 1s @00 ↔ b1 @02 点：δ∈[-3000,-1000]
        # a2 点 @10 ↔ b2 半径 1s @11（[10000,12000]）：δ∈[-2000,0]
        make([ev("a1", "s1", "00"), ev("a2", "s1", "10"),
              ev("b1", "s2", "02"), ev("b2", "s2", "11")], sources=[
            {"id": "s1", "name": "S1", "uncertainty": 0},
            {"id": "s2", "name": "S2", "uncertainty": 0}])
        # 给 a1 单独的 sigma 不好按 source 区分；直接用来源半径会同时放宽。
        # 改为两来源半径 0，两对点间距分别 2s 与 1s 且带容差区间宽度为 0：
        # 退化为精确点；这里验证 max/min 用宽度场景放到 override 不确定度上。
        r = ac.calibrate_anchors("s1", "s2", "a",
                                 [{"a": "a1", "b": "b1"}, {"a": "a2", "b": "b2"}],
                                 {"s1": {"uncertainty": 1}})
        # s1 半径 1s：锚1 δ∈[-1000-2000, 1000-2000]=[-3000,-1000]
        #             锚2 δ∈[-1000-11000, 1000-10000]=[-2000,0]
        ix = r["intersection"]
        self.assertTrue(r["feasible"])
        self.assertEqual((ix["delta_range_ms"]["lo"], ix["delta_range_ms"]["hi"]),
                         (-2000, -1000))
        self.assertEqual(ix["delta_midpoint_ms"], -1500)
        # 标记决定上下界的锚点
        flags = {it["a"]["id"]: it for it in r["anchors"]}
        self.assertTrue(flags["a2"]["delta_range_ms"]["lo"] == -2000)
        self.assertTrue(flags["a1"]["delta_range_ms"]["hi"] == -1000)

    # --------------------------------------------------------- 冲突信息

    def test_conflict_lists_two_deciding_anchors_with_ranges(self):
        make([ev("a1", "s1", "00"), ev("a2", "s1", "10"),
              ev("b1", "s2", "01"), ev("b2", "s2", "08")])
        r = ac.calibrate_anchors("s1", "s2", "a",
                                 [{"a": "a1", "b": "b1"}, {"a": "a2", "b": "b2"}])
        self.assertFalse(r["feasible"])
        self.assertIsNone(r["intersection"])
        c = r["conflict"]
        self.assertEqual(c["lower_anchor"]["delta_range_ms"],
                         {"lo": 2000, "hi": 2000})
        self.assertEqual(c["upper_anchor"]["delta_range_ms"],
                         {"lo": -1000, "hi": -1000})
        self.assertIn("交集", r["summary"])
        self.assertIn("a2", r["summary"])
        self.assertIn("a1", r["summary"])
        self.assertIsNone(r["notice"])

    # --------------------------------------------------------- 未保存假设

    def test_unsaved_offset_in_current_and_absolute_range(self):
        # s1 本地快 1s；未保存 offset=1 已把 a1 校正到与 b1 同刻
        make([ev("a1", "s1", "01"), ev("b1", "s2", "00")])
        r = ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "b1"}],
                                 {"s1": {"clock_offset": 1}})
        self.assertTrue(r["feasible"])
        self.assertEqual(r["current_offset_seconds"], 1.0)
        ix = r["intersection"]
        self.assertEqual((ix["delta_range_ms"]["lo"], ix["delta_range_ms"]["hi"]),
                         (0, 0))
        self.assertEqual(ix["absolute_offset_range_seconds"],
                         {"lo": 1.0, "hi": 1.0})
        self.assertEqual(ix["absolute_offset_midpoint_seconds"], 1.0)

    def test_drift_is_fixed_and_translation_uniform(self):
        # 漂移固定为当前（含未保存）值，δ 只是对当前区间的统一平移，
        # 不重新拟合漂移：drift_ppm=100、a2 在 t_ref 后 10s 处被多移 1ms，
        # 两对点（a1↔b1 同刻、a2↔b2 纸面同刻）对 δ 的需求因此不一致：
        # a1 要 δ=0、a2 要 δ=-1ms（点区间），没有任何统一增量同时满足——
        # 交集为空，冲突锚点分别是 a1（下界）与 a2（上界）。
        make([ev("a1", "s1", "00"), ev("a2", "s1", "10"),
              ev("b1", "s2", "00.000"), ev("b2", "s2", "10.000")])
        r = ac.calibrate_anchors("s1", "s2", "a",
                                 [{"a": "a1", "b": "b1"}, {"a": "a2", "b": "b2"}],
                                 {"s1": {"drift_ppm": 100}})
        lo1 = r["anchors"][0]["delta_range_ms"]["lo"]
        lo2 = r["anchors"][1]["delta_range_ms"]["lo"]
        self.assertEqual(lo1, 0)
        self.assertEqual(lo2, -1)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["conflict"]["lower_anchor"]["a"]["id"], "a1")
        self.assertEqual(r["conflict"]["upper_anchor"]["a"]["id"], "a2")

    # --------------------------------------------------------- 输入校验

    def test_empty_anchors_rejected(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        with self.assertRaisesRegex(ac.ReconcileError, "至少勾选 1 对"):
            ac.calibrate_anchors("s1", "s2", "a", [])

    def test_too_many_anchors_rejected(self):
        events = []
        for i in range(21):
            events.append(ev(f"a{i:02d}", "s1", "00", actor=f"u{i}"))
            events.append(ev(f"b{i:02d}", "s2", "00", actor=f"u{i}"))
        make(events)
        anchors = [{"a": f"a{i:02d}", "b": f"b{i:02d}"} for i in range(21)]
        with self.assertRaisesRegex(ac.ReconcileError, "最多 20 对"):
            ac.calibrate_anchors("s1", "s2", "a", anchors)

    def test_twenty_anchors_accepted(self):
        events = []
        for i in range(20):
            events.append(ev(f"a{i:02d}", "s1", "00", actor=f"u{i}"))
            events.append(ev(f"b{i:02d}", "s2", "00", actor=f"u{i}"))
        make(events)
        anchors = [{"a": f"a{i:02d}", "b": f"b{i:02d}"} for i in range(20)]
        r = ac.calibrate_anchors("s1", "s2", "a", anchors)
        self.assertEqual(r["anchor_count"], 20)
        self.assertTrue(r["feasible"])

    def test_duplicate_anchor_pair_rejected(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        with self.assertRaisesRegex(ac.ReconcileError, "锚点重复"):
            ac.calibrate_anchors("s1", "s2", "a",
                                 [{"a": "a1", "b": "b1"}, {"a": "a1", "b": "b1"}])

    def test_same_event_reused_on_same_side_rejected(self):
        make([ev("a1", "s1", "00"),
              ev("b1", "s2", "00", actor="x"), ev("b2", "s2", "00", actor="y")])
        with self.assertRaisesRegex(ac.ReconcileError, "锚点重复"):
            ac.calibrate_anchors("s1", "s2", "a",
                                 [{"a": "a1", "b": "b1"}, {"a": "a1", "b": "b2"}])

    def test_deleted_event_rejected(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        with self.assertRaisesRegex(ac.ReconcileError, "事件不存在"):
            ac.calibrate_anchors("s1", "s2", "a", [{"a": "ghost", "b": "b1"}])
        with self.assertRaisesRegex(ac.ReconcileError, "事件不存在"):
            ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "ghost"}])

    def test_source_mismatch_rejected(self):
        # a1 在 s1，却把它放到 B 侧位置
        make([ev("a1", "s1", "00"), ev("a2", "s1", "02"), ev("b1", "s2", "00")])
        with self.assertRaisesRegex(ac.ReconcileError, "来源不一致"):
            ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "a2"}])

    def test_same_source_and_missing_source_rejected(self):
        make([ev("a1", "s1", "00")])
        with self.assertRaisesRegex(ac.ReconcileError, "不同来源"):
            ac.calibrate_anchors("s1", "s1", "a", [{"a": "a1", "b": "a1"}])
        with self.assertRaisesRegex(ac.ReconcileError, "来源不存在"):
            ac.calibrate_anchors("s1", "ghost", "a", [{"a": "a1", "b": "x"}])

    def test_bad_calibrated_side_rejected(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "00")])
        for bad in ("", "c", "A / B 之外", None):
            with self.assertRaises(ac.ReconcileError):
                ac.calibrate_anchors("s1", "s2", bad, [{"a": "a1", "b": "b1"}])

    def test_same_event_id_both_sides_rejected(self):
        make([ev("a1", "s1", "00")])
        with self.assertRaisesRegex(ac.ReconcileError, "跨两个来源"):
            ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "a1"}])

    # --------------------------------------------------------- 只读 / 结构

    def test_response_shape_and_read_only(self):
        make([ev("a1", "s1", "00"), ev("b1", "s2", "01")])
        r1 = ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "b1"}])
        r2 = ac.calibrate_anchors("s1", "s2", "a", [{"a": "a1", "b": "b1"}])
        self.assertEqual(r1, r2)  # 只读、结果确定
        self.assertEqual(r1["calibration_type"], "anchor-offset-calibration")
        self.assertEqual(r1["calibrated_side"], "a")
        self.assertEqual(r1["reference_side"], "b")
        item = r1["anchors"][0]
        for key in ("index", "a", "b", "calibrated_event", "reference_event",
                    "delta_range_ms", "delta_range_seconds",
                    "absolute_offset_range_seconds", "intersects_currently"):
            self.assertIn(key, item)
        self.assertEqual(item["calibrated_event"]["id"], "a1")
        self.assertEqual(item["reference_event"]["id"], "b1")
        for key in ("lo", "hi"):
            self.assertIsInstance(item["delta_range_ms"][key], int)
        ix = r1["intersection"]
        for key in ("delta_range_ms", "delta_range_seconds", "delta_midpoint_ms",
                    "delta_midpoint_seconds", "absolute_offset_range_ms",
                    "absolute_offset_range_seconds", "absolute_offset_midpoint_ms",
                    "absolute_offset_midpoint_seconds"):
            self.assertIn(key, ix)


if __name__ == "__main__":
    unittest.main(verbosity=2)
