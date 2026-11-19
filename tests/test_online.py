import unittest

from qflow.online import (
    ReadOnlyViolation,
    assert_readonly_request,
    is_dangerous_click,
    is_write_request,
)


class ReadOnlyGuardTests(unittest.TestCase):
    def test_safe_methods(self):
        self.assertFalse(is_write_request("GET", "https://x/api/deduction/list?page=1"))
        self.assertFalse(is_write_request("HEAD", "https://x/api/deduction/list"))
        self.assertFalse(is_write_request("OPTIONS", "https://x/api/deduction/list"))

    def test_query_post_is_allowed(self):
        self.assertFalse(is_write_request("POST", "https://x/api/deduction/list"))
        self.assertFalse(is_write_request("POST", "https://x/api/report/query"))

    def test_write_paths_are_blocked(self):
        for url in (
            "https://x/api/deduction/save",
            "https://x/api/delivery/submit",
            "https://x/api/order/delete/1",
            "https://x/api/ship/confirm",
            "https://x/api/export",
        ):
            self.assertTrue(is_write_request("POST", url), url)

    def test_put_patch_delete_always_blocked(self):
        self.assertTrue(is_write_request("PUT", "https://x/api/deduction/list"))
        self.assertTrue(is_write_request("PATCH", "https://x/api/deduction/list"))
        self.assertTrue(is_write_request("DELETE", "https://x/api/deduction/list"))

    def test_dangerous_click_text(self):
        for text in ("保存", "提交", "发货", "审批", "确认", "删除", "导出"):
            self.assertTrue(is_dangerous_click(text), text)
        for text in ("查询", "重置", "采购协同", "发货管理", "详情"):
            self.assertFalse(is_dangerous_click(text), text)

    def test_assert_raises(self):
        with self.assertRaises(ReadOnlyViolation):
            assert_readonly_request("POST", "https://x/api/delivery/save")


if __name__ == "__main__":
    unittest.main()
