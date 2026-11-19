import tempfile
import unittest
from pathlib import Path

from qflow import db, live

HEADERS = [
    "工厂", "物料编码", "物料名称", "收货单号", "年度", "检验批", "数量", "含税单价",
    "含税金额", "检测项目名称", "验收标准", "扣罚范围", "检测结果", "扣罚标准",
    "扣罚公式", "扣罚金额", "备注", "制单日期", "清单状态", "订单号", "行号",
    "采购组", "批次", "库存地点", "确认状态", "操作",
]


def make_row(asn="5005850554", item="玉米赤霉烯酮", amount="903.8", date="2026-06-04"):
    values = {name: "" for name in HEADERS}
    values.update(
        {
            "工厂": "甘肃圣越农牧发展饲料一厂",
            "收货单号": asn,
            "年度": "2026",
            "数量": "45.19",
            "含税单价": "2390",
            "含税金额": "1195000",
            "检测项目名称": item,
            "扣罚范围": "400",
            "检测结果": "423.690",
            "扣罚公式": "45.19000*|20.000|",
            "扣罚金额": amount,
            "制单日期": date,
            "订单号": "4100227923",
        }
    )
    return [""] + [values[name] for name in HEADERS]


class LiveTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_dedupe_fixed_columns_and_leading_select_cell(self):
        table = {
            "headers": HEADERS + HEADERS,  # Element UI 固定列重复表头
            "rows": [make_row(), make_row(), make_row(asn="5005850555", amount="100")],
        }
        frame = live.table_to_frame(table)
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.iloc[0]["收货单号(必填)"], "5005850554")
        self.assertEqual(frame.iloc[0]["扣罚金额(必填)"], "903.8")
        self.assertEqual(frame.iloc[0]["制单日期(必填)"], "2026-06-04")

    def test_missing_header_raises(self):
        table = {"headers": ["收货单号"], "rows": []}
        with self.assertRaises(ValueError):
            live.table_to_frame(table)

    def test_import_table_writes_deductions_and_sync_run(self):
        table = {"headers": HEADERS + HEADERS, "rows": [make_row(), make_row()]}
        stats = live.import_table(self.conn, table, dataset="live_test", source="live:test")
        self.assertEqual(stats["rows_written"], 1)
        count = self.conn.execute("SELECT COUNT(*) FROM deductions").fetchone()[0]
        self.assertEqual(count, 1)
        run = self.conn.execute(
            "SELECT dataset, kind, status, rows_read, rows_written FROM sync_runs"
        ).fetchone()
        self.assertEqual(run["dataset"], "live_test")
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["rows_read"], 1)


if __name__ == "__main__":
    unittest.main()
