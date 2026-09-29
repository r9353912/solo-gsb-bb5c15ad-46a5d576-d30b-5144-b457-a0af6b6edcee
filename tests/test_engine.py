"""审计推理引擎的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_engine.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app.db import init_db  # noqa: E402
from app.engine import build_export, compute  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402
from app.timeutil import fmt, to_unix_seconds  # noqa: E402

init_db(load_sample=False)


class TimeParsing(unittest.TestCase):
    def test_units(self):
        self.assertAlmostEqual(to_unix_seconds(1700000000000), 1700000000.0)
        self.assertAlmostEqual(to_unix_seconds(1700000000123456), 1700000000.123456, places=3)
        self.assertAlmostEqual(to_unix_seconds("1700000000"), 1700000000.0)

    def test_iso(self):
        self.assertEqual(to_unix_seconds("2026-01-01T00:00:00Z"), 1767225600.0)
        self.assertEqual(to_unix_seconds("2026-01-01 00:00:00"), 1767225600.0)

    def test_bad(self):
        self.assertIsNone(to_unix_seconds("garbage"))
        self.assertIsNone(to_unix_seconds(None))

    def test_fmt(self):
        self.assertTrue(fmt(1767225600).endswith("Z"))


class AuditWorkflow(unittest.TestCase):
    def setUp(self):
        reset_all()
        with open(os.path.join(os.path.dirname(__file__), "..", "examples",
                               "login-anomaly.json"), encoding="utf-8") as f:
            import json
            self.payload = json.load(f)
        import_payload(self.payload)

    def test_initial_conflict(self):
        r = compute()
        self.assertEqual(r["stats"]["event_count"], 8)
        kinds = [c["kind"] for c in r["conflicts"]]
        self.assertIn("impossible_order", kinds)

    def test_offset_adjustment_flips_conflict(self):
        r = compute({"app-server": {"clock_offset": 60},
                     "mobile-app": {"clock_offset": -30}})
        # 原始矛盾消失，暴露出 g3 -> a3 的“先登出后导出”造假
        self.assertEqual(r["conflicts"][0]["events"][:2], ["g3", "a3"])
        # 此时 m2 与 g2 区间重叠 -> 歧义
        self.assertGreaterEqual(r["stats"]["ambiguity_group_count"], 1)

    def test_shrinking_uncertainty_resolves_ambiguity(self):
        r = compute({"app-server": {"clock_offset": 60},
                     "mobile-app": {"clock_offset": -30, "uncertainty": 1}})
        self.assertEqual(r["stats"]["ambiguity_group_count"], 0)
        # 时间线顺序变为因果正确
        ids = [e["id"] for e in r["events"]]
        self.assertLess(ids.index("g1"), ids.index("a1"))
        self.assertLess(ids.index("a1"), ids.index("m1"))
        self.assertLess(ids.index("m2"), ids.index("g2"))
        self.assertLess(ids.index("g2"), ids.index("a2"))

    def test_confidence_bounds(self):
        r = compute()
        for e in r["events"]:
            self.assertGreaterEqual(e["confidence"], 0)
            self.assertLessEqual(e["confidence"], 1)
            self.assertLessEqual(e["lo"], e["est"] + 1e-9)
            self.assertGreaterEqual(e["hi"], e["est"] - 1e-9)

    def test_export_shape(self):
        ex = build_export({"app-server": {"clock_offset": 60}})
        self.assertEqual(ex["export_type"], "event-evidence-chain-audit")
        for key in ("events", "edges", "conflicts", "ambiguity_groups",
                    "sources", "applied_source_assumptions", "clock_model"):
            self.assertIn(key, ex)


class ConstraintsAndCycles(unittest.TestCase):
    def test_causal_cycle(self):
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 25},
                        {"id": "s2", "uncertainty": 25}],
            "events": [
                {"id": "x", "source": "s1", "time": "2026-01-01T00:00:30Z"},
                {"id": "y", "source": "s2", "time": "2026-01-01T00:00:20Z"},
                {"id": "z", "source": "s1", "time": "2026-01-01T00:00:10Z"},
            ],
            "constraints": [
                {"from": "x", "to": "y"},
                {"from": "y", "to": "z"},
                {"from": "z", "to": "x"},
            ],
        })
        r = compute()
        self.assertTrue(any(c["kind"] == "causal_cycle" for c in r["conflicts"]))

    def test_idempotent_import_and_validation(self):
        reset_all()
        s1 = import_payload({"events": [
            {"id": "e1", "source": "h", "time": "2026-01-01T00:00:00Z"}]})
        self.assertEqual(s1["events_added"], 1)
        self.assertEqual(s1["sources_added"], 1)  # 未知来源自动登记
        s2 = import_payload({"events": [
            {"id": "e1", "source": "h", "time": "2026-01-01T00:00:00Z"}]})
        self.assertEqual(s2["events_added"], 0)  # 幂等
        s3 = import_payload({"events": [{"id": "bad", "source": "h", "time": "nope"}]})
        self.assertEqual(len(s3["rejected"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
