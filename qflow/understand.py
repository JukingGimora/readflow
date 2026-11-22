"""听懂一句话：规则 + 本地小模型的分工。

实测（2026-11-20，Qwen3-4B-Q4，64 句口语问法，见 docs/PLAN.md）：
- 「想查哪一类业务 / 是想办事 / 和这里无关」：模型对 60 句，关键词规则对 32 句。
- 「要总数还是要明细」、抽时间词：模型做不好（偏向明细；会漏、会编时间），规则更准。
- 模型分不清「想办事」和「想查的东西这里没有」（报价单在哪看 → 常被判成想办事）。
  换了五种提示词写法都在 97–99/104 之间，属于噪声，所以不再调提示词，
  而是让这两种情况共用一句对两边都成立的回答（见 voice.handle）。

所以模型只回答一个闭集选择题（一个字母），其余都由 nlu 里的规则做：
- 规则认出了业务关键词 → 用规则（免费、确定）。模型只保留一票否决：它说「想办事」就拒绝。
- 规则没认出来 → 用模型选的那一类；总数/明细仍按规则的提示词判断。
- 模型没配、连不上或答非所问 → 退回纯规则，和没有模型时完全一样。

选项来自图谱的 capability 节点（label / hint / examples），换一个系统不用改这里。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass

from . import config
from .graph import Graph, GraphError

log = logging.getLogger(__name__)

WRITE = "write"
NONE = "none"
FOLLOWUP = "followup"
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_RETRY_AFTER = 30.0
_down_until = 0.0


@dataclass
class Group:
    key: str  # intent 的前缀，例如 deduction
    label: str
    hint: str
    intents: list[str]


def _common_prefix(labels: list[str]) -> str:
    if not labels:
        return ""
    prefix = labels[0]
    for label in labels[1:]:
        while prefix and not label.startswith(prefix):
            prefix = prefix[:-1]
    return prefix or labels[0]


def groups(graph: Graph | None = None) -> list[Group]:
    """同一类业务的能力（汇总、明细）合成一个选项：模型分不清汇总和明细，但分得清业务。"""
    try:
        graph = graph or Graph.load()
    except GraphError:
        return []
    found: dict[str, dict] = {}
    for node in graph.nodes_of("capability"):
        intent = str(node.get("intent") or "")
        if not intent:
            continue
        slot = found.setdefault(intent.split(".")[0], {"labels": [], "hints": [], "examples": [], "intents": []})
        slot["labels"].append(str(node.get("label") or intent))
        slot["intents"].append(intent)
        if node.get("hint"):
            slot["hints"].append(str(node["hint"]))
        slot["examples"] += [str(x) for x in (node.get("examples") or [])[:2]]
    return [
        Group(
            key=key,
            label=_common_prefix(slot["labels"]),
            hint="；".join(slot["hints"]) or "例如：" + "；".join(slot["examples"][:3]),
            intents=slot["intents"],
        )
        for key, slot in found.items()
    ]


def build_prompt(text: str, options: list[Group], has_previous: bool) -> tuple[str, dict[str, str]]:
    lines, answers = [], {}

    def add(value: str, line: str):
        letter = _LETTERS[len(answers)]
        answers[letter] = value
        lines.append(f"{letter}. {line}")

    for group in options:
        add(group.key, f"{group.label}：{group.hint}")
    if has_previous:
        add(FOLLOWUP, "接着刚才那个问题问：只是换个时间、换个条件，或者要明细、要合计")
    add(WRITE, "不是查询，是想让系统办事（新增、提交、修改、删除、发货、下单）")
    add(NONE, "以上都不是（闲聊、和这个系统无关，或者上面没有列出的业务）")
    prompt = (
        "/no_think 这是一个业务系统的查询助手，只能查，不能办事。判断用户这句话想查哪一类：\n"
        + "\n".join(lines)
        + f"\n\n用户说：「{text}」\n只回答一个字母。"
    )
    return prompt, answers


def labels(graph: Graph | None = None) -> list[str]:
    return [group.label for group in groups(graph)]


def enabled() -> bool:
    return bool(config.LLM_URL)


def ask_model(text: str, has_previous: bool = False, graph: Graph | None = None,
              options: list[Group] | None = None) -> str | None:
    """返回业务类别的 key、FOLLOWUP、WRITE 或 NONE；模型不可用时返回 None。

    options 不给就从旧格式图谱里取；v1 图谱由 qflow/ask.py 传进来。
    """
    global _down_until
    if not enabled() or time.monotonic() < _down_until:
        return None
    options = options or groups(graph)
    if not options:
        return None
    prompt, answers = build_prompt(text, options, has_previous)
    body = json.dumps(
        {
            "model": config.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4,
            "temperature": 0,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(config.LLM_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=config.LLM_TIMEOUT) as response:
            payload = json.loads(response.read())
        letter = str(payload["choices"][0]["message"]["content"]).strip()[:1].upper()
    except Exception as exc:  # 模型挂了不能拖垮查询
        _down_until = time.monotonic() + _RETRY_AFTER
        log.warning("本地模型不可用，%d 秒内只用规则：%s", _RETRY_AFTER, exc)
        return None
    return answers.get(letter)


def route(parsed, text: str, has_previous: bool = False, graph: Graph | None = None) -> dict:
    """决定这句话怎么走。

    返回 {"kind": ..., "intent": ..., "by": "rules" | "model"}，kind 是：
    intent（查这个能力）、followup（接着上一问）、write（想办事，拒绝）、unknown（没听出来）。
    """
    rule_intent = parsed.intent if parsed.confidence > 0.3 else None
    verdict = ask_model(text, has_previous, graph)
    if verdict == WRITE:
        return {"kind": "write", "intent": None, "by": "model"}
    if rule_intent == "overview" and verdict == "overview":
        return {"kind": "intent", "intent": "overview", "by": "model"}
    if rule_intent:
        return {"kind": "intent", "intent": rule_intent, "by": "rules"}
    if verdict is None:
        return {"kind": "unknown", "intent": None, "by": "rules"}
    if verdict == FOLLOWUP:
        return {"kind": "followup", "intent": None, "by": "model"}
    if verdict == NONE:
        return {"kind": "unknown", "intent": None, "by": "model"}
    group = next((g for g in groups(graph) if g.key == verdict), None)
    if group is None:
        return {"kind": "unknown", "intent": None, "by": "model"}
    return {"kind": "intent", "intent": pick_in_group(group, parsed), "by": "model"}


def pick_in_group(group: Group, parsed) -> str:
    """一类业务里选汇总还是明细：只看规则认出的提示词，默认汇总（答错成本低，可以追问「明细呢」）。"""
    if len(group.intents) == 1:
        return group.intents[0]
    from . import nlu

    want = "detail" if nlu.wants_detail(parsed.text, parsed.params) else "summary"
    return next((i for i in group.intents if i.endswith("." + want)), group.intents[0])
