import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from qflow.nlu import parse

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class NluTests(unittest.TestCase):
    def test_deduction_summary_with_item_and_month(self):
        parsed = parse("去年11月水分扣了多少", NOW)
        self.assertEqual(parsed.intent, "deduction.summary")
        self.assertEqual(parsed.params["start"], "2025-11-01")
        self.assertEqual(parsed.params["end"], "2025-11-30")
        self.assertEqual(parsed.params["item"], "水分")

    def test_deduction_detail_by_asn(self):
        parsed = parse("ASN2511210602 扣了哪些项目", NOW)
        self.assertEqual(parsed.intent, "deduction.detail")
        self.assertEqual(parsed.params["asn"], "ASN2511210602")

    def test_shipment_detail_by_plate(self):
        parsed = parse("查一下陕KM8696的发货记录", NOW)
        self.assertEqual(parsed.intent, "shipment.detail")
        self.assertEqual(parsed.params["plate"], "陕KM8696")

    def test_reconcile(self):
        parsed = parse("2025年11月对账", NOW)
        self.assertEqual(parsed.intent, "reconcile")
        self.assertEqual(parsed.params["start"], "2025-11-01")

    def test_last_month(self):
        parsed = parse("上个月扣款明细", NOW)
        self.assertEqual(parsed.intent, "deduction.detail")
        self.assertEqual(parsed.params["start"], "2026-10-01")
        self.assertEqual(parsed.params["end"], "2026-10-31")

    def test_this_year_shipment_summary(self):
        parsed = parse("今年发货多少吨", NOW)
        self.assertEqual(parsed.intent, "shipment.summary")
        self.assertEqual(parsed.params["start"], "2026-01-01")
        self.assertEqual(parsed.params["end"], "2026-12-31")

    def test_recent_days(self):
        parsed = parse("最近7天扣款", NOW)
        self.assertEqual(parsed.intent, "deduction.summary")
        self.assertEqual(parsed.params["start"], "2026-11-10")
        self.assertEqual(parsed.params["end"], "2026-11-16")

    def test_order_numbers_are_detail(self):
        parsed = parse("订单号有哪些", NOW)
        self.assertEqual(parsed.intent, "deduction.detail")

    def test_overview(self):
        parsed = parse("数据概览", NOW)
        self.assertEqual(parsed.intent, "overview")
        self.assertNotIn("未指定时间范围", " ".join(parsed.notes))

    def test_fallback_is_low_confidence(self):
        parsed = parse("查一下", NOW)
        self.assertEqual(parsed.intent, "overview")
        self.assertLess(parsed.confidence, 0.5)

    def test_empty_text(self):
        parsed = parse("", NOW)
        self.assertEqual(parsed.intent, "overview")
        self.assertLess(parsed.confidence, 0.3)


if __name__ == "__main__":
    unittest.main()
