import copy
import json
import unittest
from pathlib import Path

from qflow import spec

EXAMPLE = Path(__file__).resolve().parent.parent / "docs" / "examples" / "graph-example.json"


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def _errors(self, doc=None):
        return [f"{e['where']}: {e['msg']}" for e in spec.validate(self.doc if doc is None else doc)["errors"]]

    def assertHasError(self, needle, doc=None):
        errors = self._errors(doc)
        self.assertTrue(any(needle in e for e in errors), f"{needle!r} not in {errors}")

    def test_example_passes_with_coverage(self):
        result = spec.validate(self.doc)
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["coverage"], {"leaves": 2, "with_capability": 1, "not_queryable": 1, "neither": []}
        )

    def test_jpath(self):
        data = {"a": {"b": [{"c": 1}, {"c": 2}]}, "z": None}
        self.assertEqual(spec.jpath(data, "$.a.b[1].c"), 2)
        self.assertIs(spec.jpath(data, "$"), data)
        for bad in ("$.a.x", "$.a.b[5]", "a.b", "$.a..b", "$.a.b[0].c.d"):
            self.assertIsNone(spec.jpath(data, bad), bad)

    def test_not_an_object(self):
        self.assertFalse(spec.validate([])["ok"])

    def test_duplicate_and_dangling_ids(self):
        self.doc["entities"].append(copy.deepcopy(self.doc["entities"][0]))
        self.assertHasError("id 重复")
        self.doc["entities"].pop()
        self.doc["capabilities"][0]["operation"] = "op.missing"
        self.assertHasError("不存在的操作")

    def test_write_operation_rejected(self):
        op = self.doc["operations"][0]
        op["access"] = "write"
        self.assertHasError("不是 read")
        op["access"] = "read"
        op["request"]["method"] = "DELETE"
        self.assertHasError("只读检查")
        op["request"].update(method="POST", path="/api/quality/deduction/delete")
        self.assertHasError("只读检查")

    def test_only_http_kind(self):
        self.doc["operations"][0]["kind"] = "page"
        self.assertHasError("只支持 kind")

    def test_evidence_required_and_must_match_map(self):
        op = self.doc["operations"][0]
        saved = op.pop("evidence")
        self.assertHasError("缺少 evidence")
        op["evidence"] = saved
        op["response"]["map"]["amount"] = "$.noSuchField"
        self.assertHasError("都取不到 amount")
        op["response"]["map"]["amount"] = "$.deductAmt"
        op["response"]["rows_path"] = "$.data.list"
        self.assertHasError("取不到非空列表")

    def test_evidence_ok_when_and_total(self):
        op = self.doc["operations"][0]
        op["evidence"]["sample"]["response"]["success"] = False
        self.assertHasError("不满足 ok_when")
        op["evidence"]["sample"]["response"]["success"] = True
        op["pagination"]["total_path"] = "$.count"
        self.assertHasError("取不到总数")

    def test_evidence_must_not_contain_credentials(self):
        self.doc["operations"][0]["evidence"]["sample"]["request"]["Cookie"] = "x"
        self.assertHasError("疑似凭据")

    def test_slot_and_metric_references(self):
        cap = self.doc["capabilities"][0]
        cap["slots"][0]["to"]["start"] = "beginDate"
        self.assertHasError("不存在的参数：beginDate")
        cap["slots"][0]["to"]["start"] = "startDate"
        cap["slots"][1]["field"] = "vendor"
        self.assertHasError("本地过滤的 field")
        cap["slots"][1]["field"] = "item"
        cap["compute"]["metrics"][0]["fn"] = "median"
        self.assertHasError("fn 必须是")
        cap["compute"]["metrics"][0]["fn"] = "sum"
        cap["answer"] = "合计 {sum_total} 元"
        self.assertHasError("{sum_total}")

    def test_examples_and_followups_minimum(self):
        cap = self.doc["capabilities"][1]
        cap["examples"] = cap["examples"][:2]
        cap["followups"] = []
        self.assertHasError("examples 至少 3 条")
        self.assertHasError("followups 至少 1 条")

    def test_navigation_leaf_must_be_accounted_for(self):
        nav = self.doc["navigation"][1]
        nav["not_queryable"] = None
        result = spec.validate(self.doc)
        self.assertTrue(result["ok"])  # 只是提醒，不拦
        self.assertEqual(result["coverage"]["neither"], ["nav.quote.online"])
        nav["not_queryable"] = {"gap": "G1"}
        self.assertHasError("必须写 reason")

    def test_validate_file_handles_bad_input(self):
        self.assertFalse(spec.validate_file("/no/such/file.json")["ok"])
        self.assertIn("通过", spec.format_report(spec.validate_file(EXAMPLE)))


if __name__ == "__main__":
    unittest.main()
