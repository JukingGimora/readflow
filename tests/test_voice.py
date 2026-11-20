import unittest
from datetime import datetime
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from qflow import db, voice

from .helpers import make_seeded_db

NOW = datetime(2025, 12, 15, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


class VoiceMultiTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        make_seeded_db(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def first(self):
        return voice.handle(
            self.conn, "2025年11月扣款合计", {}, now=NOW
        )

    def test_first_turn_saves_state(self):
        result = self.first()
        self.assertTrue(result["handled"])
        self.assertIn("180.00", result["reply"])
        state = result["state_patch"]["snxt"]
        self.assertEqual(state["intent"], "deduction.summary")
        self.assertEqual(state["params"]["start"], "2025-11-01")
        self.assertEqual(state["params"]["end"], "2025-11-30")

    def test_followup_changes_month(self):
        state = self.first()["state_patch"]
        result = voice.handle(self.conn, "那12月呢", {"state": state}, now=NOW)
        self.assertIn("20.00", result["reply"])
        self.assertEqual(result["intent"], "deduction.summary")

    def test_followup_changes_item(self):
        state = self.first()["state_patch"]
        result = voice.handle(self.conn, "那水分呢", {"state": state}, now=NOW)
        self.assertIn("130.00", result["reply"])
        self.assertIn("水分", result["reply"])

    def test_followup_switches_detail(self):
        state = self.first()["state_patch"]
        result = voice.handle(self.conn, "明细呢", {"state": state}, now=NOW)
        self.assertEqual(result["intent"], "deduction.detail")
        self.assertIn("明细", result["reply"])

    def test_followup_switches_shipment_keeps_time(self):
        state = self.first()["state_patch"]
        result = voice.handle(self.conn, "换发货呢", {"state": state}, now=NOW)
        self.assertEqual(result["intent"], "shipment.summary")
        self.assertIn("2 单", result["reply"])

    def test_courtesy_does_not_repeat(self):
        state = self.first()["state_patch"]
        result = voice.handle(self.conn, "谢谢", {"state": state}, now=NOW)
        self.assertEqual(result["reply"], "好，有需要再问。")
        self.assertEqual(result["state_patch"], {})

    def test_history_fallback_without_state(self):
        context = {
            "state": {},
            "history": [{"heard": "2025年11月扣款合计", "reply": "..."}],
        }
        result = voice.handle(self.conn, "那水分呢", context, now=NOW)
        self.assertIn("130.00", result["reply"])

    def test_followup_without_context_asks_clarification(self):
        result = voice.handle(self.conn, "那6月呢", {}, now=NOW)
        self.assertTrue(result["handled"])
        self.assertIn("扣款", result["reply"])
        self.assertEqual(result["state_patch"]["snxt"], {})
        self.assertTrue(result["clarify"])
        self.assertTrue(result["followups"])

    def test_answer_carries_followups(self):
        result = self.first()
        self.assertIn("明细呢", result["followups"])

    def test_followup_with_new_time_replaces_old_range(self):
        state = self.first()["state_patch"]
        result = voice.handle(self.conn, "那12月呢", {"state": state}, now=NOW)
        params = result["state_patch"]["snxt"]["params"]
        self.assertEqual(result["intent"], "deduction.summary")
        self.assertNotIn("2025-11", " ".join(str(v) for v in params.values()))

    def test_unclear_turn_keeps_context(self):
        state = self.first()["state_patch"]
        unclear = voice.handle(self.conn, "帮我订个机票", {"state": state}, now=NOW)
        self.assertTrue(unclear["clarify"])
        self.assertEqual(unclear["state_patch"], {})
        result = voice.handle(self.conn, "明细呢", {"state": state}, now=NOW)
        self.assertEqual(result["intent"], "deduction.detail")


if __name__ == "__main__":
    unittest.main()
