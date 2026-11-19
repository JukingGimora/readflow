import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from qflow.dates import (
    asn_date,
    month_bounds,
    resolve_range,
    shift_month,
    to_date_str,
    to_datetime_str,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class DateTests(unittest.TestCase):
    def test_asn_date(self):
        self.assertEqual(asn_date("ASN2508060031"), "2025-08-06")
        self.assertEqual(asn_date("asn2511210602"), "2025-11-21")
        self.assertIsNone(asn_date("ASN2513210031"))  # 13 月
        self.assertIsNone(asn_date("NOPE"))

    def test_month_bounds(self):
        self.assertEqual(month_bounds("2025-02"), ("2025-02-01", "2025-02-28"))
        self.assertEqual(month_bounds("2024-02"), ("2024-02-01", "2024-02-29"))
        with self.assertRaises(ValueError):
            month_bounds("2025-13")

    def test_shift_month(self):
        self.assertEqual(shift_month("2026-01", -1), "2025-12")
        self.assertEqual(shift_month("2025-12", 1), "2026-01")

    def test_to_date_str(self):
        self.assertEqual(to_date_str("2025/11/5"), "2025-11-05")
        self.assertEqual(to_date_str("2025年11月5日"), "2025-11-05")
        self.assertEqual(to_date_str(datetime(2025, 11, 5, 8, 30)), "2025-11-05")
        self.assertIsNone(to_date_str("not-a-date"))
        self.assertIsNone(to_date_str(None))

    def test_to_datetime_str(self):
        self.assertEqual(to_datetime_str("2025-11-24 17:32:49"), "2025-11-24 17:32:49")
        self.assertEqual(to_datetime_str("2025-11-24"), "2025-11-24")

    def test_resolve_range(self):
        self.assertEqual(resolve_range({"month": "2025-11"}, NOW), ("2025-11-01", "2025-11-30", "2025-11-01 至 2025-11-30"))
        self.assertEqual(resolve_range({"days": 7}, NOW), ("2026-11-10", "2026-11-16", "2026-11-10 至 2026-11-16"))
        self.assertEqual(resolve_range({}, NOW), (None, None, "全部数据"))
        self.assertEqual(resolve_range({"start": "2025-11-05"}, NOW)[:2], ("2025-11-05", "2025-11-05"))
        self.assertEqual(
            resolve_range({"start": "2025-11-30", "end": "2025-11-01"}, NOW)[:2],
            ("2025-11-01", "2025-11-30"),
        )


if __name__ == "__main__":
    unittest.main()
