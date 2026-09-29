"""命名假设方案与双方案对比的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_scenarios.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-scenario-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app import scenarios  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")

CORRECTED = {
    "app-server": {"clock_offset": 60},
    "mobile-app": {"clock_offset": -30},
}
PRECISE = {
    "app-server": {"clock_offset": 60},
    "mobile-app": {"clock_offset": -30, "uncertainty": 1},
}


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        reset_all()
        with open(EXAMPLE, encoding="utf-8") as f:
            import_payload(json.load(f))

    def test_snapshot_is_full_and_self_contained(self):
        sc = scenarios.create_scenario("校正", CORRECTED)
        # 快照包含全部来源 × 四个参数，未显式给出的取当前库存值
        for sid in ("gateway", "app-server", "mobile-app"):
            self.assertEqual(set(sc["assumptions"][sid]), set(scenarios.SCENARIO_PARAMS))
        self.assertEqual(sc["assumptions"]["app-server"]["clock_offset"], 60.0)
        self.assertEqual(sc["assumptions"]["mobile-app"]["uncertainty"], 5.0)

    def test_duplicate_and_validation(self):
        scenarios.create_scenario("S", {})
        with self.assertRaises(scenarios.ScenarioConflict):
            scenarios.create_scenario("S", {})
        with self.assertRaises(scenarios.ScenarioError):
            scenarios.create_scenario("", {})
        with self.assertRaises(scenarios.ScenarioError):
            scenarios.create_scenario("bad", {"app-server": {"reliability": 2}})
        with self.assertRaises(scenarios.ScenarioError):
            scenarios.create_scenario("bad", {"ghost": {"clock_offset": 1}})

    def test_update_and_delete(self):
        sc = scenarios.create_scenario("S", CORRECTED)
        sc2 = scenarios.update_scenario(sc["id"], assumptions=PRECISE)
        self.assertEqual(sc2["assumptions"]["mobile-app"]["uncertainty"], 1.0)
        # 更新以旧快照为基线：未提及的 offset=60 保留
        self.assertEqual(sc2["assumptions"]["app-server"]["clock_offset"], 60.0)
        self.assertEqual(len(scenarios.list_scenarios()), 1)
        scenarios.delete_scenario(sc["id"])
        self.assertEqual(scenarios.list_scenarios(), [])
        with self.assertRaises(scenarios.ScenarioError):
            scenarios.delete_scenario(999)

    def test_save_from_current_merges_overrides(self):
        sc = scenarios.create_scenario("当前", {"app-server": {"clock_offset": 12}},
                                       from_current=True)
        self.assertEqual(sc["assumptions"]["app-server"]["clock_offset"], 12.0)
        self.assertEqual(sc["assumptions"]["gateway"]["clock_offset"], 0.0)
        # 未知来源（严格模式下会报错）在 from_current 下被忽略
        sc2 = scenarios.create_scenario("当前2", {"ghost": {"clock_offset": 1}},
                                        from_current=True)
        self.assertNotIn("ghost", sc2["assumptions"])


class CompareTest(unittest.TestCase):
    def setUp(self):
        reset_all()
        with open(EXAMPLE, encoding="utf-8") as f:
            import_payload(json.load(f))
        self.s0 = scenarios.create_scenario("纸面", {})
        self.s1 = scenarios.create_scenario("校正", CORRECTED)
        self.s2 = scenarios.create_scenario("精确回执", PRECISE)

    def _cmp(self, a, b):
        return scenarios.compare_scenarios(a, b)

    def test_conflicts_added_removed_persisted(self):
        d = self._cmp({"kind": "scenario", "scenario_id": self.s0["id"]},
                      {"kind": "scenario", "scenario_id": self.s1["id"]})
        s = d["summary"]
        # 纸面 a1->m1 不可能；校正后消失，但 g3->a3 新暴露
        self.assertEqual(s["conflicts_removed"], 1)
        self.assertEqual(s["conflicts_added"], 1)
        self.assertEqual(s["conflicts_persisted"], 0)
        self.assertIn("impossible:a1->m1",
                      [r["signature"] for r in d["conflicts"]["removed"]])
        self.assertIn("impossible:g3->a3",
                      [r["signature"] for r in d["conflicts"]["added"]])

    def test_ambiguity_lifecycle(self):
        # 校正后 m2/g2 歧义新增；拿到精确回执后歧义消失
        d1 = self._cmp({"kind": "scenario", "scenario_id": self.s0["id"]},
                       {"kind": "scenario", "scenario_id": self.s1["id"]})
        self.assertEqual(d1["summary"]["ambiguities_added"], 1)
        d2 = self._cmp({"kind": "scenario", "scenario_id": self.s1["id"]},
                       {"kind": "scenario", "scenario_id": self.s2["id"]})
        self.assertEqual(d2["summary"]["ambiguities_removed"], 1)
        self.assertEqual(d2["summary"]["ambiguity_groups_a"], 1)
        self.assertEqual(d2["summary"]["ambiguity_groups_b"], 0)

    def test_edge_status_changes(self):
        d = self._cmp({"kind": "scenario", "scenario_id": self.s0["id"]},
                      {"kind": "scenario", "scenario_id": self.s1["id"]})
        changes = {(c["from"], c["to"]): (c["status_a"], c["status_b"])
                   for c in d["edge_changes"]}
        self.assertEqual(changes[("a1", "m1")], ("impossible", "ok"))
        self.assertEqual(changes[("g3", "a3")], ("ok", "impossible"))

    def test_timeline_inversions(self):
        d = self._cmp({"kind": "scenario", "scenario_id": self.s0["id"]},
                      {"kind": "scenario", "scenario_id": self.s1["id"]})
        t = d["timeline_changes"]
        self.assertGreater(t["swapped_pair_count"], 0)
        # 逆序对枚举与全排列核对（小数据直接验证一致性）
        ra = {eid: i for i, eid in enumerate(t["order_a"])}
        rb = {eid: i for i, eid in enumerate(t["order_b"])}
        brute = sum(1 for i, x in enumerate(t["order_a"]) for y in t["order_a"][i + 1:]
                    if (ra[x] - ra[y]) * (rb[x] - rb[y]) < 0)
        self.assertEqual(t["swapped_pair_count"], brute)
        self.assertEqual(len(t["order_a"]), 8)

    def test_assumption_diff_and_current_side(self):
        d = self._cmp({"kind": "scenario", "scenario_id": self.s0["id"]},
                      {"kind": "scenario", "scenario_id": self.s2["id"]})
        params = {x["source_id"]: {p["param"]: (p["a"], p["b"])
                                   for p in x["params"]}
                  for x in d["assumption_diff"]}
        self.assertEqual(params["app-server"]["clock_offset"], (0.0, 60.0))
        self.assertEqual(params["mobile-app"]["uncertainty"], (5.0, 1.0))
        # "当前"侧（库存=纸面）与纸面方案应零差异
        d2 = self._cmp({"kind": "current"},
                       {"kind": "scenario", "scenario_id": self.s0["id"]})
        self.assertEqual(d2["summary"]["conflicts_added"], 0)
        self.assertEqual(d2["summary"]["sources_with_assumption_diff"], 0)
        # 当前侧携带 overrides 等同于精确方案
        d3 = self._cmp({"kind": "current"}, {"kind": "current", "overrides": PRECISE})
        self.assertEqual(d3["summary"]["conflicts_b"], 1)

    def test_same_scenario_rejected_at_api_layer(self):
        # 同一套方案作为两侧：API 层直接拒绝
        with self.assertRaises(scenarios.ScenarioError):
            self._cmp({"kind": "scenario", "scenario_id": self.s1["id"]},
                      {"kind": "scenario", "scenario_id": self.s1["id"]})
        # current vs current（含相同 overrides）合法但差异全零
        d = self._cmp({"kind": "current", "overrides": CORRECTED},
                      {"kind": "current", "overrides": dict(CORRECTED)})
        self.assertEqual(d["summary"]["edges_changed"], 0)
        self.assertEqual(d["summary"]["timeline_swapped_pairs"], 0)

    def test_unknown_side_errors(self):
        with self.assertRaises(scenarios.ScenarioError):
            self._cmp({"kind": "scenario", "scenario_id": 4242},
                      {"kind": "current"})
        with self.assertRaises(scenarios.ScenarioError):
            self._cmp({"kind": "weird"}, {"kind": "current"})

    def test_export_shape(self):
        d = self._cmp({"kind": "scenario", "scenario_id": self.s0["id"]},
                      {"kind": "scenario", "scenario_id": self.s1["id"]})
        for key in ("comparison_type", "sides", "summary", "conflicts",
                    "ambiguity_groups", "edge_changes", "timeline_changes",
                    "assumption_diff", "stats_a", "stats_b"):
            self.assertIn(key, d)
        for side in ("a", "b"):
            self.assertIn("assumptions", d["sides"][side])


if __name__ == "__main__":
    unittest.main(verbosity=2)
