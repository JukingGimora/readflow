"""实测「听懂一句话」选对能力的比例：纯规则 vs 规则 + 本地模型。对着一份图谱跑，不发任何业务请求。

    QFLOW_LLM_URL=http://127.0.0.1:8050/v1/chat/completions \\
        python scripts/eval_understand.py systems/sunner_srm/graph.json tests/cases_sunner_srm.json

用例文件：[{"batch": 批次名, "q": 原话, "want": 应该落到的能力, "prev": 上一问的能力（测追问，可选）}]
want 写能力 id 去掉 "cap." 的部分；图谱里没有的能力、"refuse.write"、"clarify" 都算「应该答不了」。
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qflow import ask, config, runner, understand  # noqa: E402


def run(graph, cases):
    def send(method, url, params, body_type, headers):  # 只看选了哪个能力，不取数
        raise runner.RunError("eval")

    results, took = [], []
    for case in cases:
        previous = None
        if case.get("prev") and f"cap.{case['prev']}" in graph.capabilities:
            previous = {"capability": f"cap.{case['prev']}", "values": {"period": {"start": "2025-11-01", "end": "2025-11-30"}}}
        picked = []
        original = runner.run
        runner.run = lambda g, capability_id, *a, **k: picked.append(capability_id) or {"answer": "", "capability": capability_id}
        started = time.perf_counter()
        try:
            ask.ask(graph, case["q"], previous, send)
        finally:
            runner.run = original
        took.append(time.perf_counter() - started)
        results.append(picked[0] if picked else None)
    return results, took


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    graph = runner.load(sys.argv[1])
    cases = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    expected = [f"cap.{c['want']}" if f"cap.{c['want']}" in graph.capabilities else None for c in cases]
    llm_url, config.LLM_URL = config.LLM_URL, ""
    rules, _ = run(graph, cases)
    combined, took = None, []
    if llm_url:
        config.LLM_URL = llm_url
        combined, took = run(graph, cases)
        if understand._down_until:
            print("本地模型连不上，下面的「规则+模型」一列不可信")

    by_batch = defaultdict(lambda: [0, 0, 0])
    for i, case in enumerate(cases):
        row = by_batch[case["batch"]]
        row[0] += 1
        row[1] += rules[i] == expected[i]
        row[2] += bool(combined) and combined[i] == expected[i]
    print(f"{'批次':<28}{'句数':>4}{'纯规则':>8}{'规则+模型':>10}")
    for batch, (n, r, c) in by_batch.items():
        print(f"{batch:<28}{n:>4}{r:>8}{c if combined else '-':>10}")
    n = len(cases)
    print(f"{'合计':<28}{n:>4}{sum(r == e for r, e in zip(rules, expected)):>8}"
          f"{sum(r == e for r, e in zip(combined, expected)) if combined else '-':>10}")
    final = combined or rules
    if took:
        print(f"每句耗时：中位数 {sorted(took)[n // 2] * 1000:.0f} 毫秒，最长 {max(took) * 1000:.0f} 毫秒")
    for i, case in enumerate(cases):
        if final[i] != expected[i]:
            prev = f"（上一问 {case['prev']}）" if case.get("prev") else ""
            print(f"  错：{case['q']}{prev} → {final[i] or '答不了'}，应为 {expected[i] or '答不了'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
