"""时间词、总数/明细的规则，以及本地模型那道选择题。"""

import io
import json
from datetime import datetime
from unittest import mock

import pytest

from qflow import ask, config, timewords, understand

NOW = datetime(2026, 11, 20, 10, 0)
OPTIONS = [understand.Group("nav.a", "扣款", "例如：上个月扣了多少"), understand.Group("nav.b", "发货", "例如：拉了几车")]


@pytest.mark.parametrize("text, start, end", [
    ("去年十一月水分扣了多少", "2025-11-01", "2025-11-30"),
    ("十月份的罚款", "2026-10-01", "2026-10-31"),
    ("九月份扣款明细", "2026-09-01", "2026-09-30"),
    ("十二月份到了几车货", "2025-12-01", "2025-12-31"),  # 今年 12 月还没到，取最近的那个
    ("上礼拜杂质罚了几笔", "2026-11-09", "2026-11-15"),
    ("今年三月到五月的扣款", "2026-03-01", "2026-05-31"),
    ("上半年拉了多少趟", "2026-01-01", "2026-06-30"),
    ("去年下半年", "2025-07-01", "2025-12-31"),
    ("去年11月", "2025-11-01", "2025-11-30"),
])
def test_spoken_time(text, start, end):
    assert timewords.extract_time(text, NOW) == {"start": start, "end": end}


def test_words_that_are_not_dates_are_left_alone():
    for text in ("这个月", "上个月", "近三个月的日子", "一共多少"):
        assert timewords.normalize_time_words(text) == text
    assert timewords.extract_time("一共多少", NOW) == {}


def test_wants_detail():
    for text in ("把扣钱的单子一条条列出来", "杂质那几笔具体是哪天的", "前天那车货收了没有"):
        assert ask.wants_detail(text), text
    for text in ("上个月被罚了多少钱", "这周送了几趟", "今年一共被扣了多少"):
        assert not ask.wants_detail(text), text


def test_prompt_is_a_closed_choice():
    prompt, answers = understand.build_prompt("被罚了多少", OPTIONS, has_previous=False)
    assert list(answers.values()) == ["nav.a", "nav.b", understand.WRITE, understand.NONE]
    assert "「被罚了多少」" in prompt
    _, answers = understand.build_prompt("那发货呢", OPTIONS, has_previous=True)
    assert understand.FOLLOWUP in answers.values()


@pytest.fixture()
def llm():
    understand._down_until = 0.0
    with mock.patch.object(config, "LLM_URL", "http://llm.invalid/v1/chat/completions"):
        yield
    understand._down_until = 0.0


def test_disabled_without_url():
    with mock.patch.object(config, "LLM_URL", ""):
        assert understand.ask_model("被罚了多少", options=OPTIONS) is None


def test_reads_one_letter(llm):
    reply = json.dumps({"choices": [{"message": {"content": " b。"}}]}).encode()
    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(reply)) as urlopen:
        assert understand.ask_model("拉了几车", options=OPTIONS) == "nav.b"
    sent = json.loads(urlopen.call_args.args[0].data)
    assert (sent["max_tokens"], sent["temperature"]) == (4, 0)


def test_unexpected_letter_is_ignored(llm):
    reply = json.dumps({"choices": [{"message": {"content": "Z"}}]}).encode()
    with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(reply)):
        assert understand.ask_model("拉了几车", options=OPTIONS) is None


def test_model_down_falls_back_and_backs_off(llm):
    with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")) as urlopen:
        assert understand.ask_model("拉了几车", options=OPTIONS) is None
        assert understand.ask_model("拉了几车", options=OPTIONS) is None
    assert urlopen.call_count == 1
