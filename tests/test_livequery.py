import time
import unittest
from datetime import datetime
from unittest import mock

from qflow import capabilities, db, livequery
from qflow.livequery import LiveDeduction

from .helpers import make_seeded_db
import tempfile
from pathlib import Path

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


class LiveFetchThroughGraphTests(unittest.TestCase):
    """实时取数走图谱 + 通用执行器，不再有圣农专用的请求代码。"""

    ROWS = [
        {"id": "r1", "refNo": "A1", "postDate": "2026-05-01", "projectName": "水分", "itemAmount": 100.0},
        {"id": "r2", "refNo": "A2", "postDate": "2026-05-03", "projectName": "杂质", "itemAmount": 30.0},
    ]

    def _client(self, send):
        from qflow import runner, srmapi

        client = LiveDeduction(ttl=9999)
        patches = [
            mock.patch.object(srmapi, "load_session", return_value=[{"name": "sid", "value": "x", "domain": "h", "path": "/"}]),
            mock.patch.object(srmapi, "session_mtime", return_value=0.0),
            mock.patch.object(runner, "http_sender", return_value=send),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return client

    def test_fetch_uses_request_declared_in_graph(self):
        import json

        calls = []

        def send(method, url, params, body_type, headers):
            calls.append((method, url, dict(params), body_type))
            return {"status": 200, "url": url, "text": json.dumps({"success": True, "total": 2, "rows": self.ROWS})}

        client = self._client(send)
        result = client.summary(start="2026-05-01", end="2026-05-31")
        self.assertEqual(result["total_amount"], 130.0)
        self.assertEqual(calls, [("POST", "https://srm.sunnercn.com:10001/srm/qm/quajud/vendorQuery", {"page": 1, "rows": 200}, "form")])
        self.assertEqual(client.page_total, 2)

    def test_expired_session_falls_through_to_login(self):
        from qflow import srmapi, srmlogin

        client = self._client(lambda *a: {"status": 200, "url": "https://srm.sunnercn.com:10001/#/login", "text": "<html>"})
        session = mock.Mock()
        session.auto_login.return_value = False
        with mock.patch.object(srmlogin, "get_session", return_value=session):
            with self.assertRaises(srmapi.AuthError):
                client._fetch_api()
        session.auto_login.assert_called_once()
