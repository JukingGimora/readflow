import tempfile
import unittest
from pathlib import Path

import pandas as pd

from qflow import db, importer


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_alias_and_shipment_aggregation(self):
        frame = pd.DataFrame(
            [
                {"发货单号": "ASN2511010001", "收货日期": "2025-11-02", "实收数量": 1, "含税金额": 100},
                {"发货单号": "ASN2511010001", "收货日期": "2025-11-02", "实收数量": 2, "含税金额": 50},
                {"发货单号": "ASN2511010002", "收货日期": "2025-11-03", "实收数量": 3, "含税金额": 70},
            ]
        )
        stats = importer.import_frame(self.conn, frame, "shipments", "d", "f.xlsx")
        self.assertEqual(stats["rows_written"], 2)
        row = self.conn.execute(
            "SELECT amount, received_qty, line_count FROM shipments WHERE asn='ASN2511010001'"
        ).fetchone()
        self.assertEqual(row["amount"], 150.0)
        self.assertEqual(row["received_qty"], 3.0)
        self.assertEqual(row["line_count"], 2)

    def test_deduction_alias_and_dedupe(self):
        frame = pd.DataFrame(
            [
                {"收货单号": "ASN2511010001", "检测项目名称": "水分", "扣罚金额": 100, "制单日期": "2025-11-05"},
            ]
        )
        importer.import_frame(self.conn, frame, "deductions", "d", "f.xlsx")
        importer.import_frame(self.conn, frame, "deductions", "d", "f.xlsx")
        count = self.conn.execute("SELECT COUNT(*) FROM deductions").fetchone()[0]
        self.assertEqual(count, 1)
        row = self.conn.execute("SELECT biz_date, item, amount FROM deductions").fetchone()
        self.assertEqual(row["biz_date"], "2025-11-05")
        self.assertEqual(row["item"], "水分")
        self.assertEqual(row["amount"], 100.0)

    def test_total_row_is_skipped(self):
        frame = pd.DataFrame(
            [
                {"收货单号(必填)": "ASN2511010001", "扣罚金额(必填)": 10.0},
                {"收货单号(必填)": None, "扣罚金额(必填)": 10.0},
            ]
        )
        stats = importer.import_frame(self.conn, frame, "deductions", "d", "f.xlsx")
        self.assertEqual(stats["rows_written"], 1)
        self.assertEqual(stats["skipped"], 1)

    def test_asn_date_fallback(self):
        frame = pd.DataFrame(
            [{"收货单号(必填)": "ASN2508060031", "检测项目名称(必填)": "水分", "扣罚金额(必填)": 5.0}]
        )
        importer.import_frame(self.conn, frame, "deductions", "d", "f.xlsx")
        row = self.conn.execute("SELECT biz_date FROM deductions").fetchone()
        self.assertEqual(row["biz_date"], "2025-08-06")

    def test_import_path_records_sync_run(self):
        path = Path(self.tmp.name) / "orders.xlsx"
        pd.DataFrame(
            [{"发货单号": "ASN2511010001", "收货日期": "2025-11-02", "含税金额": 100}]
        ).to_excel(path, index=False)
        result = importer.import_path(self.conn, path)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["kind"], "shipments")
        row = self.conn.execute(
            "SELECT dataset, kind, status, rows_written FROM sync_runs"
        ).fetchone()
        self.assertEqual(row["dataset"], "orders")
        self.assertEqual(row["kind"], "shipments")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["rows_written"], 1)

    def test_merged_output_is_skipped(self):
        path = Path(self.tmp.name) / "1000new.xlsx"
        pd.DataFrame([{"发货单号": "ASN2511010001", "含税金额": 1}]).to_excel(path, index=False)
        result = importer.import_path(self.conn, path)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("合并产物", result["error"])


if __name__ == "__main__":
    unittest.main()
