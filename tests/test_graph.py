import unittest

from qflow.graph import Graph, GraphError


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = Graph.load()

    def test_stats(self):
        stats = self.graph.stats()
        self.assertEqual(stats["capabilities"], 6)
        self.assertEqual(stats["answerable"], 6)
        self.assertEqual(stats["coverage"], 1.0)
        self.assertEqual(stats["online_pending"], 0)
        self.assertEqual(stats["by_type"]["entity"], 3)

    def test_plan_deduction_summary(self):
        plan = self.graph.plan("deduction.summary", {"month": "2025-11", "item": "水分"})
        self.assertEqual(plan["provider"]["id"], "provider.cache.deductions")
        self.assertEqual(plan["provider"]["kind"], "cache")
        self.assertEqual(plan["entity"]["id"], "entity.deduction")
        self.assertEqual(plan["bindings"]["month"], {"field": "biz_date", "value": "2025-11"})
        self.assertTrue(plan["read_only"])
        self.assertEqual(len(plan["fallbacks"]), 2)
        self.assertTrue(plan["fallbacks"][0]["available"])
        self.assertEqual(plan["fallbacks"][0]["kind"], "api")
        self.assertIn("明细呢", plan["followups"])
        self.assertEqual(plan["metrics"][0]["key"], "total_amount")

    def test_every_capability_declares_examples_and_followups(self):
        for node in self.graph.nodes_of("capability"):
            self.assertTrue(node.get("examples"), node["id"])
            self.assertTrue(node.get("followups"), node["id"])
        self.assertEqual(len(self.graph.examples()), 3)

    def test_plan_reconcile(self):
        plan = self.graph.plan("reconcile", {"start": "2025-11-01", "end": "2025-11-30"})
        self.assertEqual(plan["provider"]["id"], "provider.cache.reconciliation")
        self.assertIn("按 asn 对齐", plan["steps"])

    def test_plan_overview(self):
        plan = self.graph.plan("overview")
        self.assertEqual(plan["provider"]["kind"], "cache")
        self.assertIsNone(plan["entity"])

    def test_unknown_intent(self):
        with self.assertRaises(GraphError):
            self.graph.plan("delete.everything")

    def test_dangling_edge_rejected(self):
        with self.assertRaises(GraphError):
            Graph(
                {
                    "nodes": [{"id": "a", "type": "app"}],
                    "edges": [{"from": "a", "to": "missing", "type": "x"}],
                }
            )

    def test_duplicate_node_rejected(self):
        with self.assertRaises(GraphError):
            Graph({"nodes": [{"id": "a", "type": "app"}, {"id": "a", "type": "app"}]})


if __name__ == "__main__":
    unittest.main()
