import time
import unittest
from datetime import datetime
from unittest import mock

from qflow import capabilities, db, livequery
from qflow.livequery import LiveDeduction, parse_table

from .helpers import make_seeded_db
import tempfile
from pathlib import Path

HEADERS = [
    "工厂", "物料编码", "物料名称", "收货单号", "年度", "检验批", "数量", "含税单价",
    "含税金额", "检测项目名称", "验收标准", "扣罚范围", "检测结果", "扣罚标准",
    "扣罚公式", "扣罚金额", "备注", "制单日期", "清单状态", "订单号", "行号",
    "采购组", "批次", "库存地点", "确认状态", "操作",
]


def make_row(asn="5005850554", item="玉米赤霉烯酮", amount="903.8", date="2026-06-04"):
    values = {name: "" for name in HEADERS}
    values.update({"收货单号": asn, "检测项目名称": item, "扣罚金额": amount,
                   "制单日期": date, "数量": "45.19", "检测结果": "423.690",
                   "扣罚公式": "45.19000*|20.000|", "订单号": "4100227923"})
    return [""] + [values[name] for name in HEADERS]


class ParseTableTests(unittest.TestCase):
    def test_dedup_and_parse(self):
        table = {
            "headers": HEADERS + HEADERS,
            "rows": [
                make_row(),
                make_row(),  # 固定列重复
                make_row(asn="5005850555", item="水分", amount="", date="2026-06-05"),  # 无金额
            ],
        }
        rows = parse_table(table)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["asn"], "5005850554")
        self.assertEqual(row["amount"], 903.8)
        self.assertEqual(row["biz_date"], "2026-06-04")
        self.assertEqual(row["item"], "玉米赤霉烯酮")

    def test_missing_headers_raises(self):
        with self.assertRaises(RuntimeError):
            parse_table({"headers": ["收货单号"], "rows": []})


class LiveSummaryTests(unittest.TestCase):
    def setUp(self):
        self.client = LiveDeduction(ttl=9999)
        self.client.rows = [
            {"asn": "A1", "biz_date": "2026-05-01", "item": "水分", "amount": 100.0},
            {"asn": "A1", "biz_date": "2026-05-02", "item": "杂质", "amount": 50.0},
            {"asn": "A2", "biz_date": "2026-05-03", "item": "水分", "amount": 30.0},
            {"asn": "A3", "biz_date": "2026-06-01", "item": "水分", "amount": 20.0},
        ]
        self.client.fetched_at = time.time()
        self.client.page_total = 4

    def test_summary_filters_and_groups(self):
        result = self.client.summary(start="2026-05-01", end="2026-05-31", group_by="item")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total_amount"], 180.0)
        self.assertEqual(result["asn_count"], 2)
        groups = {row["bucket"]: row for row in result["groups"]}
        self.assertEqual(groups["水分"]["total"], 130.0)
        self.assertEqual(groups["杂质"]["total"], 50.0)
        self.assertIn("live_fetched_at", result)

    def test_summary_item_filter(self):
        result = self.client.summary(start="2026-05-01", end="2026-05-31", item="水分")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total_amount"], 130.0)

    def test_detail_order_and_limit(self):
        result = self.client.detail(limit=2)
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["rows"][0]["asn"], "A3")  # 日期最新
        self.assertEqual(result["rows"][0]["dataset"], "live")

    def test_overview(self):
        overview = self.client.overview()
        self.assertEqual(overview["count"], 4)
        self.assertEqual(overview["total_amount"], 200.0)
        self.assertEqual(overview["mode"], "live")


class CapabilitiesLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        make_seeded_db(self.db_path)
        self.conn = db.connect(self.db_path)
        self.fake = LiveDeduction(ttl=9999)
        self.fake.rows = [
            {"asn": "A1", "biz_date": "2026-05-01", "item": "水分", "amount": 100.0,
             "result_text": None, "formula": None, "qty": None, "order_no": None},
            {"asn": "A2", "biz_date": "2026-05-02", "item": "水分", "amount": 30.0,
             "result_text": None, "formula": None, "qty": None, "order_no": None},
        ]
        self.fake.fetched_at = time.time()
        self.fake.page_total = 2

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_live_summary_query(self):
        with mock.patch("qflow.livequery.get_client", return_value=self.fake):
            result = capabilities.query(
                self.conn, text="2026年5月扣款合计", live=True
            )
        self.assertEqual(result["data"]["total_amount"], 130.0)
        self.assertIn("实时读取于", result["answer"])
        self.assertEqual(result["plan"]["provider"]["id"], "live.srm")

    def test_live_unsupported_intent_raises(self):
        with mock.patch("qflow.livequery.get_client", return_value=self.fake):
            with self.assertRaises(ValueError):
                capabilities.dispatch(self.conn, "shipment.summary", {}, live=True)

    def test_live_overview(self):
        with mock.patch("qflow.livequery.get_client", return_value=self.fake):
            result = capabilities.query(self.conn, intent="overview", live=True)
        self.assertIn("实时读取", result["answer"])
        self.assertEqual(result["data"]["deductions"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
