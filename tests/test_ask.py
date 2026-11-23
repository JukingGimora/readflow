"""一句话 → 能力 + 条件 → 执行，按 v1 图谱。离线：接口用测试数据假装，模型用 mock。"""

import json
from datetime import datetime
from unittest import mock

import pytest

from qflow import ask, runner, understand
from tests.data import EXAMPLE_GRAPH, SRM_GRAPH, srm_sender

NOW = datetime(2025, 12, 20, 10, 0)


@pytest.fixture()
def env():
    with mock.patch.object(understand, "ask_model", return_value=None):  # 默认：模型不在线
        yield runner.load(SRM_GRAPH), srm_sender()


def say(env, text, previous=None):
    graph, send = env
    return ask.ask(graph, text, previous, send, now=NOW)


def test_summary_with_spoken_month_and_item(env):
    out = say(env, "十一月水分扣了多少钱")
    assert out["capability"] == "cap.deduction.summary" and out["by"] == "rules"
    assert out["state"]["values"] == {"period": {"start": "2025-11-01", "end": "2025-11-30"}, "item": "水分"}
    assert out["reply"] == "2025-11-01 至 2025-11-30 共 2 笔扣款，合计 130.00 元。"


def test_synonym_and_detail_words(env):
    out = say(env, "上个月水份扣款有哪几笔")
    assert out["capability"] == "cap.deduction.detail"
    assert out["state"]["values"]["item"] == "水分"
    assert [r["amount"] for r in out["result"]["rows"]] == [100.0, 30.0]


def test_identifier_means_detail(env):
    out = say(env, "ASN2511010001 扣了多少")
    assert out["capability"] == "cap.deduction.detail"
    assert out["state"]["values"] == {"asn": "ASN2511010001"}
    assert out["result"]["count"] == 2


def test_followups_keep_conditions_and_replace_time(env):
    first = say(env, "上个月扣款合计")
    detail = say(env, "明细呢", first["state"])
    assert detail["capability"] == "cap.deduction.detail"
    assert detail["state"]["values"] == first["state"]["values"] and detail["result"]["count"] == 3
    narrowed = say(env, "只看杂质", detail["state"])
    assert narrowed["capability"] == "cap.deduction.detail" and narrowed["result"]["count"] == 1
    moved = say(env, "那12月呢", narrowed["state"])
    assert moved["state"]["values"] == {"period": {"start": "2025-12-01", "end": "2025-12-31"}, "item": "杂质"}
    total = say(env, "合计多少", moved["state"])
    assert total["capability"] == "cap.deduction.summary" and total["result"]["totals"]["total"] == 20.0


def test_unknown_keeps_state_and_offers_examples(env):
    first = say(env, "上个月扣款合计")
    out = say(env, "你好啊", first["state"])
    assert out["handled"] is False and out["state"] == first["state"] and out["followups"]
    # 带了时间词但模型说无关：不硬接上一问
    with mock.patch.object(understand, "ask_model", return_value=understand.NONE):
        out = say(env, "今天天气怎么样", first["state"])
    assert out["handled"] is False and out["state"] == first["state"]


def test_model_picks_topic_when_rules_cannot(env):
    graph, _ = env
    key = ask.topics(graph)[0].key
    with mock.patch.object(understand, "ask_model", return_value=key) as model:
        out = say(env, "上个月被罚了多少")
    assert out["capability"] == "cap.deduction.summary" and out["by"] == "model"
    assert out["result"]["totals"]["total"] == 180.0
    options = model.call_args.kwargs["options"]
    assert [o.label for o in options] == ["扣款"]


def test_model_write_verdict_refuses_without_calling_the_system(env):
    graph, _ = env
    with mock.patch.object(understand, "ask_model", return_value=understand.WRITE):
        out = ask.ask(graph, "把这笔扣款删了", None, lambda *a: pytest.fail("不应该发请求"), now=NOW)
    assert "不能替你在系统里办事" in out["reply"] and "扣款" in out["reply"]


def test_same_code_answers_another_system():
    """示例图谱是另一个虚构系统：能力、关键词、枚举值都不一样，代码不用改。"""
    graph = runner.load(EXAMPLE_GRAPH)
    sample = graph.operations["op.deduction.list"]["evidence"]["sample"]["response"]
    sent = []

    def send(method, url, params, body_type, headers):
        sent.append(dict(params))
        return {"status": 200, "url": url, "text": json.dumps(sample, ensure_ascii=False)}

    with mock.patch.object(understand, "ask_model", return_value=None):
        out = ask.ask(graph, "去年10月含水扣款合计", None, send, now=datetime(2027, 1, 15))
    assert sent[0]["startDate"] == "2026-10-01" and sent[0]["endDate"] == "2026-10-31"
    assert out["state"]["values"]["item"] == "水分" and out["result"]["totals"]["total"] == 120.5


def test_bare_month_follows_the_previous_year_or_the_latest_past_one(env):
    graph, send = env
    jan = datetime(2026, 1, 15)
    alone = ask.ask(graph, "12月扣款合计", None, send, now=jan)
    assert alone["state"]["values"]["period"]["start"] == "2025-12-01"  # 不是还没到的 2026 年 12 月
    first = ask.ask(graph, "2024年11月扣款合计", None, send, now=jan)
    moved = ask.ask(graph, "那12月呢", first["state"], send, now=jan)
    assert moved["state"]["values"]["period"] == {"start": "2024-12-01", "end": "2024-12-31"}
