"""实测「听懂一句话」的准确率：纯规则 vs 规则 + 本地模型。

    QFLOW_LLM_URL=http://127.0.0.1:8050/v1/chat/completions python scripts/eval_understand.py

走的是线上同一条路（voice.handle），用一份临时的测试数据库，不碰真实数据。

也可以直接测一个已部署的服务（只发问题读回答，不写任何东西；口令从环境变量 QFLOW_TOKEN 读）：

    QFLOW_TOKEN=... python scripts/eval_understand.py --service http://127.0.0.1:8790

用例在 tests/understand_cases.json：q 是原话，want 是应该落到的能力
（refuse.write = 想办事被拒绝，clarify = 没听出来、反问），prev 是上一问的能力（测追问）。
"""

from __future__ import annotations

import json
import sys
import os
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qflow import config, db, understand, voice  # noqa: E402
from tests.helpers import make_seeded_db  # noqa: E402


def run(cases, conn):
    results, took = [], []
    for case in cases:
        state = {}
        if case.get("prev"):
            state = {voice.STATE_KEY: {"intent": case["prev"], "params": {"start": "2025-11-01", "end": "2025-11-30"}}}
        started = time.perf_counter()
        out = voice.handle(conn, case["q"], {"state": state})
        took.append(time.perf_counter() - started)
        results.append("clarify" if out.get("clarify") else out.get("intent"))
    return results, took


def run_service(cases, base: str):
    """把同一批句子发给已部署服务的 /voice/handle（VoiceLab 调用的就是这个接口）。"""
    headers = {"Content-Type": "application/json"}
    if os.environ.get("QFLOW_TOKEN"):
        headers["X-API-Key"] = os.environ["QFLOW_TOKEN"]
    results, took = [], []
    for case in cases:
        state = {}
        if case.get("prev"):
            state = {voice.STATE_KEY: {"intent": case["prev"], "params": {"start": "2025-11-01", "end": "2025-11-30"}}}
        body = json.dumps({"heard": case["q"], "context": {"state": state}}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(base.rstrip("/") + "/voice/handle", data=body, headers=headers)
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=20) as response:
            out = json.loads(response.read().decode("utf-8"))
        took.append(time.perf_counter() - started)
        results.append("clarify" if out.get("clarify") else out.get("intent"))
    return results, took


def report_service(cases, got, took) -> int:
    by_batch = defaultdict(lambda: [0, 0])
    for case, result in zip(cases, got):
        by_batch[case["batch"]][0] += 1
        by_batch[case["batch"]][1] += result == case["want"]
    print(f"{'批次':<28}{'句数':>4}{'答对':>8}")
    for batch, (n, right) in by_batch.items():
        print(f"{batch:<28}{n:>4}{right:>8}")
    n = len(cases)
    print(f"{'合计':<28}{n:>4}{sum(g == c['want'] for g, c in zip(got, cases)):>8}")
    print(f"每句耗时：中位数 {sorted(took)[n // 2] * 1000:.0f} 毫秒，最长 {max(took) * 1000:.0f} 毫秒")
    for case, result in zip(cases, got):
        if result != case["want"]:
            prev = f"（上一问 {case['prev']}）" if case.get("prev") else ""
            print(f"  错：{case['q']}{prev} → {result}，应为 {case['want']}")
    return 0


def main() -> int:
    cases = json.loads((ROOT / "tests" / "understand_cases.json").read_text(encoding="utf-8"))
    if "--service" in sys.argv:
        got, took = run_service(cases, sys.argv[sys.argv.index("--service") + 1])
        return report_service(cases, got, took)
    llm_url = config.LLM_URL
    with tempfile.TemporaryDirectory() as tmp:
        make_seeded_db(Path(tmp) / "eval.db")
        conn = db.connect(Path(tmp) / "eval.db")
        config.LLM_URL = ""
        rules, _ = run(cases, conn)
        combined, took = (None, [])
        if llm_url:
            config.LLM_URL = llm_url
            combined, took = run(cases, conn)
            if understand._down_until:
                print("本地模型连不上，下面的「规则+模型」一列不可信")
        conn.close()

    by_batch = defaultdict(lambda: [0, 0, 0])
    for i, case in enumerate(cases):
        row = by_batch[case["batch"]]
        row[0] += 1
        row[1] += rules[i] == case["want"]
        row[2] += bool(combined) and combined[i] == case["want"]
    print(f"{'批次':<28}{'句数':>4}{'纯规则':>8}{'规则+模型':>10}")
    for batch, (n, r, c) in by_batch.items():
        print(f"{batch:<28}{n:>4}{r:>8}{c if combined else '-':>10}")
    n = len(cases)
    print(f"{'合计':<28}{n:>4}{sum(r == c['want'] for r, c in zip(rules, cases)):>8}"
          f"{sum(r == c['want'] for r, c in zip(combined, cases)) if combined else '-':>10}")
    if combined:
        print(f"每句耗时：中位数 {sorted(took)[n // 2] * 1000:.0f} 毫秒，最长 {max(took) * 1000:.0f} 毫秒")
        for i, case in enumerate(cases):
            if combined[i] != case["want"]:
                prev = f"（上一问 {case['prev']}）" if case.get("prev") else ""
                print(f"  错：{case['q']}{prev} → {combined[i]}，应为 {case['want']}；纯规则 {rules[i]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
