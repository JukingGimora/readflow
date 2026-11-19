"""语音多轮：把 voicelab 的 context 变成可追问的查询状态.

voicelab 的 window 策略同时带 history 和 state；state_patch 会合并进会话。
所以这里把“上一问查什么”压成一个 snxt 槽位：
    {"snxt": {"intent": "...", "params": {...}}}
支持：
- 那6月呢 / 上个月呢        -> 只换时间，沿用上次 intent 和 item
- 那水分呢                 -> 只换检测项目，沿用上次时间
- 明细呢 / 汇总呢           -> 切换 detail / summary
- 换发货呢                 -> 换 intent，沿用上次时间
- 谢谢 / 好的               -> 不重复上一次答案
"""
from __future__ import annotations

from datetime import datetime

from . import capabilities, nlu

STATE_KEY = "snxt"
DETAIL_WORDS = ("明细", "列表", "哪几笔", "哪几单", "详情")
SUMMARY_WORDS = ("汇总", "合计", "总计", "统计")
COURTESY = ("谢谢", "好的", "好", "嗯", "知道了", "收到", "可以", "行")
PARAM_KEYS = {"start", "end", "month", "days", "asn", "plate", "item"}


def _params(values: dict | None) -> dict:
    return {
        key: value
        for key, value in (values or {}).items()
        if key in PARAM_KEYS and value not in (None, "")
    }


def _from_history(context: dict) -> dict:
    """state 没写进来时，从最近一轮原话恢复。"""
    for turn in reversed(context.get("history") or []):
        heard = (turn or {}).get("heard") or ""
        if not heard:
            continue
        parsed = nlu.parse(heard)
        if parsed.intent != "overview":
            return {"intent": parsed.intent, "params": _params(parsed.params)}
    return {}


def _previous(context: dict | None) -> dict:
    context = context or {}
    state = context.get("state") or {}
    if isinstance(state, dict):
        saved = state.get(STATE_KEY)
        if isinstance(saved, dict) and saved.get("intent"):
            return {"intent": saved["intent"], "params": _params(saved.get("params"))}
    return _from_history(context)


def _out(result: dict, intent: str, params: dict) -> dict:
    return {
        "reply": " ".join(result["answer"].splitlines()),
        "state_patch": {STATE_KEY: {"intent": intent, "params": _params(params)}},
        "handled": True,
        "intent": intent,
        "data": result.get("data"),
        "plan": result.get("plan"),
        "data_as_of": result.get("data_as_of"),
    }


def handle(conn, heard: str, context: dict | None = None, now: datetime | None = None, live: bool = False) -> dict:
    text = " ".join((heard or "").split())
    if not text:
        return {
            "reply": "没听清，请再说一遍。",
            "state_patch": {},
            "handled": False,
        }

    context = context or {}
    previous = _previous(context)
    parsed = nlu.parse(text, now)
    params = _params(parsed.params)
    overview = any(word in text for word in ("概览", "总览", "数据情况"))
    explicit = parsed.intent != "overview"
    followup = bool(previous) and not explicit and not overview and bool(
        params or any(word in text for word in DETAIL_WORDS + SUMMARY_WORDS)
    )

    if text in COURTESY and not params:
        return {
            "reply": "好，有需要再问。",
            "state_patch": {},
            "handled": True,
            "intent": previous.get("intent") or "chat",
        }

    if overview:
        result = capabilities.query(conn, text=text, now=now, live=live)
        payload = _out(result, "overview", {})
        payload["state_patch"] = {STATE_KEY: {}}
        return payload

    if followup:
        intent = previous["intent"]
        merged = {**_params(previous.get("params")), **params}
        if any(word in text for word in DETAIL_WORDS) and intent.startswith(
            ("deduction", "shipment")
        ):
            intent = intent.split(".")[0] + ".detail"
        elif any(word in text for word in SUMMARY_WORDS) and intent.startswith(
            ("deduction", "shipment")
        ):
            intent = intent.split(".")[0] + ".summary"
        result = capabilities.query(conn, intent=intent, params=merged, now=now, live=live)
        return _out(result, intent, merged)

    if explicit:
        merged = dict(params)
        # 换方向时保留上一次的时间范围（"那发货呢"）
        if previous:
            for key in ("start", "end", "month", "days"):
                if key not in merged and key in _params(previous.get("params")):
                    merged[key] = _params(previous.get("params"))[key]
            if parsed.intent.startswith("deduction"):
                merged.pop("plate", None)
            if parsed.intent.startswith("shipment"):
                merged.pop("item", None)
        result = capabilities.query(conn, text=text, params=merged, now=now, live=live)
        return _out(result, parsed.intent, merged)

    # 没有上下文、也没有明确意图：返回概览并提示可以问什么
    result = capabilities.query(conn, text=text, now=now, live=live)
    payload = _out(result, "overview", {})
    payload["reply"] = (
        "我还没听出要查扣款、发货还是对账。可以问：2026 年 5 月扣款合计是多少？"
    )
    payload["state_patch"] = {STATE_KEY: {}}
    return payload
