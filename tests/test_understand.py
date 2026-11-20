import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from qflow import config, db, nlu, understand, voice
from tests.helpers import make_seeded_db

NOW = datetime(2026, 11, 20, 12, 0, 0)


def model_says(value):
    return mock.patch.object(understand, "ask_model", return_value=value)


class SpokenTimeTests(unittest.TestCase):
    def test_chinese_numerals_and_spoken_weeks(self):
        cases = {
            "去年十一月水分扣了多少": ("2025-11-01", "2025-11-30"),
            "十月份的罚款": ("2026-10-01", "2026-10-31"),
            "九月份扣款明细": ("2026-09-01", "2026-09-30"),
            "十二月份到了几车货": ("2026-12-01", "2026-12-31"),
            "上礼拜杂质罚了几笔": ("2026-11-09", "2026-11-15"),
            "今年三月到五月的扣款": ("2026-03-01", "2026-05-31"),
            "上半年拉了多少趟": ("2026-01-01", "2026-06-30"),
            "去年下半年": ("2025-07-01", "2025-12-31"),
            "去年11月": ("2025-11-01", "2025-11-30"),
        }
        for text, (start, end) in cases.items():
            self.assertEqual(nlu.extract_time(text, NOW), {"start": start, "end": end}, text)

    def test_words_that_are_not_dates_are_left_alone(self):
        for text in ("这个月", "上个月", "近三个月的日子", "一共多少"):
            self.assertEqual(nlu.normalize_time_words(text), text)

    def test_item_aliases(self):
        self.assertEqual(nlu.extract_entities("水份超标罚了多少")["item"], "水分")
        self.assertEqual(nlu.extract_entities("黄曲霉扣了几次")["item"], "黄曲霉毒素")

    def test_wants_detail(self):
        for text in ("把扣钱的单子一条条列出来", "杂质那几笔具体是哪天的", "前天那车货收了没有"):
            self.assertTrue(nlu.wants_detail(text), text)
        for text in ("上个月被罚了多少钱", "这周送了几趟", "今年一共被扣了多少"):
            self.assertFalse(nlu.wants_detail(text), text)


class PromptTests(unittest.TestCase):
    def test_groups_come_from_graph(self):
        found = {g.key: g for g in understand.groups()}
        self.assertEqual(set(found), {"deduction", "shipment", "reconcile", "overview"})
        self.assertEqual(found["deduction"].label, "扣款")
        self.assertEqual(found["deduction"].intents, ["deduction.summary", "deduction.detail"])

    def test_prompt_is_a_closed_choice(self):
        options = understand.groups()
        prompt, answers = understand.build_prompt("被罚了多少", options, has_previous=False)
        self.assertEqual(list(answers.values()), [g.key for g in options] + [understand.WRITE, understand.NONE])
        self.assertIn("「被罚了多少」", prompt)
        _, answers = understand.build_prompt("那发货呢", options, has_previous=True)
        self.assertIn(understand.FOLLOWUP, answers.values())


class AskModelTests(unittest.TestCase):
    def setUp(self):
        understand._down_until = 0.0
        self.addCleanup(setattr, understand, "_down_until", 0.0)

    def test_disabled_without_url(self):
        with mock.patch.object(config, "LLM_URL", ""):
            self.assertIsNone(understand.ask_model("被罚了多少"))

    def test_reads_one_letter(self):
        reply = json.dumps({"choices": [{"message": {"content": " b。"}}]}).encode()
        with mock.patch.object(config, "LLM_URL", "http://llm.invalid/v1/chat/completions"), mock.patch(
            "urllib.request.urlopen", return_value=io.BytesIO(reply)
        ) as urlopen:
            self.assertEqual(understand.ask_model("拉了几车"), "shipment")
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual((sent["max_tokens"], sent["temperature"]), (4, 0))

    def test_unexpected_letter_is_ignored(self):
        reply = json.dumps({"choices": [{"message": {"content": "Z"}}]}).encode()
        with mock.patch.object(config, "LLM_URL", "http://llm.invalid/x"), mock.patch(
            "urllib.request.urlopen", return_value=io.BytesIO(reply)
        ):
            self.assertIsNone(understand.ask_model("拉了几车"))

    def test_model_down_falls_back_and_backs_off(self):
        with mock.patch.object(config, "LLM_URL", "http://llm.invalid/x"), mock.patch(
            "urllib.request.urlopen", side_effect=OSError("refused")
        ) as urlopen:
            self.assertIsNone(understand.ask_model("拉了几车"))
            self.assertIsNone(understand.ask_model("拉了几车"))
        self.assertEqual(urlopen.call_count, 1)


class RouteTests(unittest.TestCase):
    def route(self, text, verdict, previous=False):
        with model_says(verdict):
            return understand.route(nlu.parse(text, NOW), text, has_previous=previous)

    def test_rules_win_when_they_recognise_the_business(self):
        self.assertEqual(
            self.route("上个月扣款合计", "shipment"),
            {"kind": "intent", "intent": "deduction.summary", "by": "rules"},
        )

    def test_model_fills_in_when_rules_have_nothing(self):
        self.assertEqual(self.route("上个月被罚了多少钱", "deduction")["intent"], "deduction.summary")
        self.assertEqual(self.route("我送过去的货都有哪些", "shipment")["intent"], "shipment.detail")
        self.assertEqual(self.route("我这个月能结多少钱", "reconcile")["intent"], "reconcile")

    def test_overview_needs_model_agreement_to_skip_followup_logic(self):
        self.assertEqual(self.route("系统里现在有多少数据", "overview")["by"], "model")
        self.assertEqual(self.route("系统里现在有多少数据", None)["by"], "rules")

    def test_write_veto_beats_rules(self):
        self.assertEqual(self.route("帮我提交一下发货单", understand.WRITE)["kind"], "write")

    def test_none_followup_and_no_model(self):
        self.assertEqual(self.route("讲个笑话", understand.NONE)["kind"], "unknown")
        self.assertEqual(self.route("那一笔一笔的呢", understand.FOLLOWUP, previous=True)["kind"], "followup")
        self.assertEqual(self.route("上个月被罚了多少钱", None), {"kind": "unknown", "intent": None, "by": "rules"})


class VoiceWithModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_seeded_db(Path(self.tmp.name) / "test.db")
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def handle(self, text, verdict, state=None):
        with model_says(verdict):
            return voice.handle(self.conn, text, {"state": state or {}}, now=NOW)

    def test_colloquial_question_is_answered_with_model(self):
        out = self.handle("去年十一月被罚了多少钱", "deduction")
        self.assertEqual(out["intent"], "deduction.summary")
        self.assertEqual(
            out["state_patch"]["snxt"]["params"], {"start": "2025-11-01", "end": "2025-11-30"}
        )
        self.assertNotIn("clarify", out)

    def test_same_question_without_model_asks_back(self):
        self.assertTrue(self.handle("去年十一月被罚了多少钱", None).get("clarify"))

    def test_write_request_is_refused_and_context_kept(self):
        out = self.handle("把这单删了", understand.WRITE)
        self.assertEqual(out["intent"], "refuse.write")
        self.assertEqual(out["state_patch"], {})
        self.assertIn("只能帮你查扣款、发货", out["reply"])

    def test_model_switches_business_and_keeps_time(self):
        state = {"snxt": {"intent": "deduction.summary", "params": {"start": "2025-11-01", "end": "2025-11-30"}}}
        out = self.handle("那拉了多少吨", "shipment", state)
        self.assertEqual(out["intent"], "shipment.summary")
        self.assertEqual(out["state_patch"]["snxt"]["params"]["start"], "2025-11-01")


if __name__ == "__main__":
    unittest.main()
