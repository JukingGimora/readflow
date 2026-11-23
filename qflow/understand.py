"""本地小模型只做一道闭集选择题：这句话想查哪一类业务（或者是想办事 / 和这里无关 / 接着上一问）。

实测（2026-11-20，Qwen3-4B-Q4，见 docs/PLAN.md）：
- 判断业务类别：模型明显好于关键词。
- 要总数还是要明细、抽时间：模型做不好（偏向明细；会漏、会编时间），所以这两件事只用规则（qflow/ask.py、timewords.py）。
- 模型分不清「想办事」和「想查的东西这里没有」，所以这两种情况共用一句都成立的回答。

模型没配、连不上或答非所问时返回 None，调用方按纯规则走。选项由调用方从图谱里取，这里不认识任何系统。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass

from . import config

log = logging.getLogger(__name__)

WRITE = "write"
NONE = "none"
FOLLOWUP = "followup"
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_RETRY_AFTER = 30.0
_down_until = 0.0


@dataclass
class Group:
    key: str
    label: str
    hint: str


def _common_prefix(labels: list[str]) -> str:
    if not labels:
        return ""
    prefix = labels[0]
    for label in labels[1:]:
        while prefix and not label.startswith(prefix):
            prefix = prefix[:-1]
    return prefix or labels[0]


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


def enabled() -> bool:
    return bool(config.LLM_URL)


def ask_model(text: str, has_previous: bool = False, options: list[Group] | None = None) -> str | None:
    """返回业务类别的 key、FOLLOWUP、WRITE 或 NONE；模型不可用时返回 None。options 由 qflow/ask.py 从图谱里取。"""
    global _down_until
    if not enabled() or time.monotonic() < _down_until:
        return None
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
