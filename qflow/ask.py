"""一句话 → 能力 + 条件 → 执行，全部按 v1 图谱来。这里没有任何某个系统专用的词。

    out = ask.ask(graph, "去年十一月水分被罚了多少", previous_state, send)
    out["reply"]   一句话回答
    out["state"]   下一轮原样传回来，用来接追问

分工和 voice.handle 一样：
- 查哪一类：先看图谱里写的关键词（capabilities[].keywords 和能力名）；认不出来问本地模型。
- 汇总还是明细、时间、枚举值、单号：规则。模型不碰。
- 数字：runner 算。

还没做的（docs/PLAN.md T2）：voice.handle 和页面还没改用这里；枚举值的拼音匹配（T3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from . import nlu, runner, understand
from .dates import resolve_range

DETAIL_WORDS = ("明细", "列表", "哪几笔", "哪几单", "详情")
SUMMARY_WORDS = ("汇总", "合计", "总计", "统计", "一共", "总共")


@dataclass
class Topic:
    """同一个菜单页上的能力算一类（通常是汇总 + 明细）。"""

    key: str
    label: str
    summary: str | None
    detail: str | None
    keywords: list[str]  # 明说了这类业务
    values: list[str]  # 这类数据特有的枚举值（例如某个检测项目）：没明说业务时的旁证
    examples: list[str]


def topics(graph: runner.SystemGraph) -> list[Topic]:
    found: dict[str, list[dict]] = {}
    for capability in graph.capabilities.values():
        if capability.get("access", "read") == "read":
            found.setdefault(capability["nav"], []).append(capability)
    result = []
    for key, members in found.items():
        label = understand._common_prefix([str(c.get("label") or c["id"]) for c in members])
        words = {label} | {str(w) for c in members for w in c.get("keywords") or []}
        values: set[str] = set()
        for capability in members:
            entity_id = graph.operations[capability["operation"]]["response"]["entity"]
            for slot in capability.get("slots") or []:
                if slot.get("type") == "enum":
                    values |= set(_enum_names(graph.field(entity_id, slot.get("field") or slot["name"])))
        result.append(Topic(
            key=key,
            label=label,
            summary=next((c["id"] for c in members if c.get("compute")), None),
            detail=next((c["id"] for c in members if not c.get("compute")), None),
            keywords=sorted((w for w in words if w), key=len, reverse=True),
            values=sorted(values, key=len, reverse=True),
            examples=[str(e) for c in members for e in (c.get("examples") or [])[:2]],
        ))
    return result


def _enum_names(field: dict) -> dict[str, str]:
    """枚举字段的各种叫法 → 标准值。"""
    names = {str(v): str(v) for v in field.get("values") or []}
    for value, aliases in (field.get("synonyms") or {}).items():
        names.update({str(alias): str(value) for alias in aliases})
    return names


def fill_slots(graph: runner.SystemGraph, capability: dict, text: str, now: datetime | None = None) -> dict:
    """从一句话里取这个能力要的条件。取不到的不出现在结果里。"""
    entity_id = graph.operations[capability["operation"]]["response"]["entity"]
    values: dict = {}
    for slot in capability.get("slots") or []:
        kind = slot.get("type", "string")
        if kind == "date_range":
            found = nlu.extract_time(text, now)
            if found:
                start, end, _ = resolve_range(found, now)
                if start or end:
                    values[slot["name"]] = {"start": start, "end": end}
        elif kind == "enum":
            names = _enum_names(graph.field(entity_id, slot.get("field") or slot["name"]))
            hit = next((names[n] for n in sorted(names, key=len, reverse=True) if n in text), None)
            if hit:
                values[slot["name"]] = hit
        elif slot.get("pattern"):
            match = re.search(slot["pattern"], text, re.IGNORECASE)
            if match:
                values[slot["name"]] = (match.group(1) if match.groups() else match.group(0)).upper()
    return values


def _refuse(graph: runner.SystemGraph, state: dict, why: str) -> dict:
    can = "、".join(t.label for t in topics(graph)) or "已接入的数据"
    examples = [e for t in topics(graph) for e in t.examples][:3]
    reply = (
        f"这个我帮不上。我现在只能帮你查{can}，不能替你在系统里办事。"
        if why == "write"
        else f"我还没听出要查什么。可以问：{examples[0]}" if examples else f"我还没听出要查什么。现在能查：{can}。"
    )
    return {"handled": why == "write", "reply": reply, "state": state, "followups": examples, "why": why}


def ask(graph: runner.SystemGraph, text: str, previous: dict | None, send, now: datetime | None = None) -> dict:
    text = " ".join((text or "").split())
    previous = previous if (previous or {}).get("capability") in graph.capabilities else {}
    all_topics = topics(graph)
    wants_detail = nlu.wants_detail(text) or any(w in text for w in DETAIL_WORDS)
    wants_summary = any(w in text for w in SUMMARY_WORDS)

    topic = next((t for t in all_topics if any(k in text for k in t.keywords)), None)
    if topic is None:
        hinted = next((t for t in all_topics if any(v in text for v in t.values)), None)
        # 上一问就是这一类时，只提枚举值是在加条件（「只看杂质」），留给下面按追问处理
        if hinted and not (previous and graph.capabilities[previous["capability"]]["nav"] == hinted.key):
            topic = hinted
    by = "rules"
    options = [understand.Group(t.key, t.label, "例如：" + "；".join(t.examples[:3]), []) for t in all_topics]
    verdict = understand.ask_model(text, has_previous=bool(previous), options=options)
    if verdict == understand.WRITE:
        return _refuse(graph, previous, "write")
    if topic is None and verdict not in (None, understand.NONE, understand.FOLLOWUP):
        topic, by = next((t for t in all_topics if t.key == verdict), None), "model"

    inherited: dict = {}
    if topic is None and previous and verdict != understand.NONE:  # 模型说无关就不硬接
        # 没提业务：只要说了条件、要明细或要合计，或者模型认为是接着问，就接上一问
        last = graph.capabilities[previous["capability"]]
        said = fill_slots(graph, last, text, now)
        if said or wants_detail or wants_summary or verdict == understand.FOLLOWUP:
            topic = next(t for t in all_topics if t.key == last["nav"])
            inherited = dict(previous.get("values") or {})
            if not (wants_detail or wants_summary):
                wants_detail = not last.get("compute")
    elif topic is not None and previous:
        # 换了业务：只把时间带过去（「那发货呢」）
        inherited = {k: v for k, v in (previous.get("values") or {}).items() if isinstance(v, dict)}
    if topic is None:
        return _refuse(graph, previous, "unknown")

    identifiers = [s["name"] for c in (topic.summary, topic.detail) if c
                   for s in graph.capabilities[c].get("slots") or [] if s.get("pattern")]
    probe = graph.capabilities[topic.detail or topic.summary]
    if any(name in fill_slots(graph, probe, text, now) for name in identifiers):
        wants_detail = True  # 报了具体单号，就是要看那一单
    capability_id = (topic.detail if wants_detail and not wants_summary else topic.summary) or topic.detail or topic.summary
    capability = graph.capabilities[capability_id]
    allowed = {s["name"] for s in capability.get("slots") or []}
    values = {k: v for k, v in {**inherited, **fill_slots(graph, capability, text, now)}.items() if k in allowed}

    result = runner.run(graph, capability_id, values, send, now=now)
    return {
        "handled": True,
        "reply": result["answer"],
        "capability": capability_id,
        "by": by,
        "result": result,
        "followups": list(capability.get("followups") or []),
        "state": {"capability": capability_id, "values": values},
    }
