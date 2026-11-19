import tempfile
import unittest
from pathlib import Path

from qflow import db, engine

from .helpers import make_seeded_db


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        make_seeded_db(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_deduction_summary(self):
        result = engine.deduction_summary(
            self.conn, start="2025-11-01", end="2025-11-30", group_by="item"
        )
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["total_amount"], 180.0)
        self.assertEqual(result["asn_count"], 2)
        groups = {row["bucket"]: row for row in result["groups"]}
        self.assertEqual(groups["水分"]["n"], 2)
        self.assertEqual(groups["水分"]["total"], 130.0)
        self.assertEqual(groups["杂质"]["total"], 50.0)

    def test_deduction_detail(self):
        result = engine.deduction_detail(self.conn, asn="ASN2511010001")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total_amount"], 150.0)
        self.assertEqual(len(result["rows"]), 2)

    def test_deduction_detail_item_filter(self):
        result = engine.deduction_detail(
            self.conn, start="2025-11-01", end="2025-11-30", item="水分"
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total_amount"], 130.0)

    def test_shipment_detail_plate_filter(self):
        result = engine.shipment_detail(self.conn, plate="陕A12345")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["rows"][0]["asn"], "ASN2511010001")
        self.assertEqual(result["total_qty"], 49.5)

    def test_reconcile_november(self):
        result = engine.reconcile(self.conn, start="2025-11-01", end="2025-11-30")
        self.assertEqual(result["ship_count"], 2)
        self.assertEqual(result["ded_count"], 3)
        self.assertEqual(result["ded_asn_count"], 2)
        self.assertEqual(result["ship_total"], 208535.0)
        self.assertEqual(result["ded_total"], 180.0)
        self.assertEqual(result["net_total"], 208355.0)
        self.assertEqual(result["with_deduction"], 2)
        self.assertEqual(result["ship_only"], 1)
        self.assertEqual(result["ded_only"], 1)
        rows = {row["asn"]: row for row in result["rows"]}
        self.assertEqual(rows["ASN2511010001"]["ded_count"], 2)
        self.assertFalse(rows["ASN2511020004"]["has_shipment"])

    def test_reconcile_december(self):
        result = engine.reconcile(self.conn, start="2025-12-01", end="2025-12-31")
        self.assertEqual(result["ship_count"], 1)
        self.assertEqual(result["ded_count"], 1)
        self.assertEqual(result["net_total"], 69880.0)

    def test_overview(self):
        result = engine.overview(self.conn)
        self.assertEqual(result["shipments"]["count"], 3)
        self.assertEqual(result["deductions"]["count"], 4)
        self.assertEqual(result["deductions"]["total_amount"], 200.0)


if __name__ == "__main__":
    unittest.main()
