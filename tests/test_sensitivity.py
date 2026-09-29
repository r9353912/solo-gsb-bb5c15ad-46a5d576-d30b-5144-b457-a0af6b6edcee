"""偏移敏感性扫描的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_sensitivity.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-sens-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app import sensitivity  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")


class ScanTest(unittest.TestCase):
    def setUp(self):
        reset_all()
        with open(EXAMPLE, encoding="utf-8") as f:
            import_payload(json.load(f))

    def test_critical_points_are_analytic(self):
        r = sensitivity.scan_offset("app-server", -200, 200)
        pts = sorted(p["offset"] for p in r["critical_points"])
        # g3->a3: 48.7/51.3；g1->a1: 68.7/71.3；a1->m1: 73.8/86.2；g2->a2: 88.7/91.3
        self.assertEqual(len(pts), 8)
        for got, want in zip(pts, [48.7, 51.3, 68.7, 71.3, 73.8, 86.2, 88.7, 91.3]):
            self.assertAlmostEqual(got, want, places=3)
        self.assertEqual(len(r["intervals"]), 9)  # 8 个临界点 → 9 段
        self.assertEqual(r["involved_edge_count"], 4)
        self.assertIsNone(r["notice"])
        self.assertEqual(r["current_offset"], 0.0)

    def test_offset_cancellation_same_source_edges(self):
        r = sensitivity.scan_offset("app-server", -200, 200)
        cancelled = {(e["from"], e["to"]) for e in r["cancelled_edges"]}
        # 同来源时序边两端都在 app-server：offset 整体平移抵消
        self.assertIn(("a1", "a2"), cancelled)
        self.assertIn(("a2", "a3"), cancelled)
        # 抵消边不制造任何临界点
        for p in r["critical_points"]:
            self.assertNotEqual((p["from"], p["to"]), ("a1", "a2"))
            self.assertNotEqual((p["from"], p["to"]), ("a2", "a3"))
        # mobile-app 的声明约束 m1->m2 同样是同来源边
        r2 = sensitivity.scan_offset("mobile-app", -200, 200)
        self.assertIn(("m1", "m2"),
                      {(e["from"], e["to"]) for e in r2["cancelled_edges"]})

    def test_intervals_cover_range_and_current_is_stable(self):
        r = sensitivity.scan_offset("app-server", -200, 200)
        iv = r["intervals"]
        self.assertAlmostEqual(iv[0]["lo"], -200.0)
        self.assertAlmostEqual(iv[-1]["hi"], 200.0)
        for a, b in zip(iv, iv[1:]):  # 区间首尾相接、完整覆盖扫描范围
            self.assertAlmostEqual(a["hi"], b["lo"])
        cur = [s for s in iv if s["contains_current"]]
        self.assertEqual(len(cur), 1)
        # 当前所在区间以当前 offset 为代表值，与当前结果零差异
        self.assertAlmostEqual(cur[0]["representative"], 0.0)
        self.assertEqual(cur[0]["conflicts"]["added"], [])
        self.assertEqual(cur[0]["conflicts"]["removed"], [])
        self.assertEqual(cur[0]["impossible_edges"]["added"], [])
        self.assertEqual(cur[0]["impossible_edges"]["removed"], [])
        self.assertEqual(len(cur[0]["conflicts"]["persisted"]), 1)

    def test_interval_diff_signatures(self):
        r = sensitivity.scan_offset("app-server", -200, 200)
        iv = r["intervals"]
        # 区间 (51.3, 68.7)：g3->a3 新暴露（合规造假），a1->m1 持续
        seg = next(s for s in iv if abs(s["lo"] - 51.3) < 0.01)
        self.assertIn("impossible:g3->a3",
                      [x["signature"] for x in seg["impossible_edges"]["added"]])
        self.assertIn("impossible:g3->a3",
                      [x["signature"] for x in seg["conflicts"]["added"]])
        self.assertIn("impossible:a1->m1",
                      [x["signature"] for x in seg["impossible_edges"]["persisted"]])
        # 最后一段 (91.3, 200]：a1->m1 修复消失，三条 gateway->app 边全部不可能
        last = iv[-1]
        self.assertIn("impossible:a1->m1",
                      [x["signature"] for x in last["impossible_edges"]["removed"]])
        self.assertIn("impossible:a1->m1",
                      [x["signature"] for x in last["conflicts"]["removed"]])
        self.assertEqual(
            {x["signature"] for x in last["impossible_edges"]["added"]},
            {"impossible:g1->a1", "impossible:g2->a2", "impossible:g3->a3"})
        # 中点落在区间内部（前端点击段落时写入该值）
        for s in iv:
            self.assertLessEqual(s["lo"], s["midpoint"])
            self.assertLessEqual(s["midpoint"], s["hi"])

    def test_scan_respects_unsaved_overrides_context(self):
        # 未保存调整（mobile-app offset=-30）会平移 a1->m1 的临界点
        r = sensitivity.scan_offset("app-server", -200, 200,
                                    {"mobile-app": {"clock_offset": -30}})
        pts = {(p["from"], p["to"], p["bound"]): p["offset"]
               for p in r["critical_points"]}
        self.assertAlmostEqual(pts[("a1", "m1", "gap_hi")], 43.8, places=3)
        self.assertAlmostEqual(pts[("a1", "m1", "gap_lo")], 56.2, places=3)

    def test_no_critical_points_notice(self):
        r = sensitivity.scan_offset("app-server", -500, -300)
        self.assertEqual(r["critical_points"], [])
        self.assertEqual(len(r["intervals"]), 1)  # 整个范围为单一稳定区间
        self.assertAlmostEqual(r["intervals"][0]["lo"], -500.0)
        self.assertAlmostEqual(r["intervals"][0]["hi"], -300.0)
        self.assertIn("没有临界点", r["notice"])
        # 该区间与当前结果一致（a1->m1 在两侧都不可能）
        self.assertEqual(r["intervals"][0]["conflicts"]["added"], [])
        self.assertEqual(r["intervals"][0]["conflicts"]["removed"], [])

    def test_source_without_cross_source_edges(self):
        import_payload({"events": [
            {"id": "solo1", "source": "solo", "time": "2026-09-18T09:00:00Z"},
            {"id": "solo2", "source": "solo", "time": "2026-09-18T09:01:00Z"},
        ]})
        r = sensitivity.scan_offset("solo", -100, 100)
        self.assertEqual(r["involved_edge_count"], 0)
        self.assertIn("不参与任何跨来源约束边", r["notice"])
        self.assertEqual(len(r["intervals"]), 1)
        # solo1->solo2 为同来源时序边：识别为 offset 抵消
        self.assertEqual({(e["from"], e["to"]) for e in r["cancelled_edges"]},
                         {("solo1", "solo2")})

    def test_invalid_inputs(self):
        with self.assertRaises(sensitivity.SensitivityError):
            sensitivity.scan_offset("app-server", 100, -100)   # 下界 > 上界
        with self.assertRaises(sensitivity.SensitivityError):
            sensitivity.scan_offset("app-server", 0, 0)        # 下界 = 上界
        with self.assertRaises(sensitivity.SensitivityError):
            sensitivity.scan_offset("app-server", "abc", 100)  # 非数值
        with self.assertRaises(sensitivity.SensitivityError):
            sensitivity.scan_offset("app-server", None, 100)   # 缺省
        with self.assertRaises(sensitivity.SensitivityError):
            sensitivity.scan_offset("app-server", 0, float("inf"))
        with self.assertRaises(sensitivity.SensitivityError):
            sensitivity.scan_offset("app-server", float("nan"), 100)
        with self.assertRaises(sensitivity.SensitivityError) as ctx:
            sensitivity.scan_offset("ghost-source", -100, 100)
        self.assertIn("不存在", str(ctx.exception))

    def test_too_many_critical_points_rejected(self):
        reset_all()
        # 30 条跨来源边、临界点两两不同 → 60 个临界点，超出段数上限
        events = ([{"id": f"s{i}", "source": "scan-me",
                    "time": f"2026-01-01T00:00:{i:02d}Z"} for i in range(30)]
                  + [{"id": f"g{i}", "source": "ref",
                      "time": f"2026-01-01T01:{(10 * i) // 60:02d}:{(10 * i) % 60:02d}Z"}
                     for i in range(30)])
        import_payload({
            "sources": [{"id": "scan-me", "uncertainty": 1},
                        {"id": "ref", "uncertainty": 1}],
            "events": events,
            "constraints": [{"from": f"s{i}", "to": f"g{i}"} for i in range(30)],
        })
        with self.assertRaises(sensitivity.SensitivityError) as ctx:
            sensitivity.scan_offset("scan-me", -100000, 100000)
        self.assertIn("临界点过多", str(ctx.exception))
        # 缩小范围后可用
        r = sensitivity.scan_offset("scan-me", -3700, -3600)
        self.assertLessEqual(len(r["intervals"]), sensitivity.MAX_INTERVALS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
