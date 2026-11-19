"""只读路径执行器：按图谱/配置在系统里现场查一次，用完即销毁会话。

给 voiceLab 用的契约：
    POST /exec  {"system": "sunner_srm", "text": "2026年5月扣款合计"}
    -> {"ok": true, "reply": "...", "data": {...}, "path": {"steps": [...]},
        "timings": {...}, "read_only": true}

每次调用 = 临时浏览器 -> 登录 -> 走路径（填筛选/查询/翻页）-> 内存汇总 -> 退出并删除
profile。不保存业务数据、不保持登录态。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import answer, nlu
from .config import PROJECT_ROOT
from .livequery import LiveDeduction, parse_table


def _cfg(name: str) -> dict:
    path = Path(PROJECT_ROOT) / "systems" / f"{name}.json"
    if not path.exists():
        raise ValueError(f"没有系统配置: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run_once(
    system: str,
    *,
    text: str | None = None,
    intent: str | None = None,
    params: dict | None = None,
) -> dict:
    from .explorer import EphemeralBrowser  # 延迟导入，避免缓存模式也要 selenium

    cfg = _cfg(system)
    started = time.perf_counter()
    parsed = nlu.parse(text or "")
    intent = intent or parsed.intent
    merged = dict(parsed.params)
    merged.update({k: v for k, v in (params or {}).items() if v not in (None, "")})
    if intent not in {"deduction.summary", "deduction.detail", "overview"}:
        raise ValueError(
            f"当前只实现了扣款路径（deduction.summary / deduction.detail / overview），收到 {intent}"
        )

    steps: list[str] = []
    browser = EphemeralBrowser(cfg)
    try:
        t0 = time.perf_counter()
        if not browser.login():
            raise RuntimeError("登录失败")
        steps.append("登录（验证码）")
        timings_login = time.perf_counter() - t0

        from . import srm

        t0 = time.perf_counter()
        query_page = cfg.get("query_page") or cfg.get("crawl", {}).get("seeds", [""])[0]
        srm.goto_deduction(browser.driver, wait=4.0)
        browser.driver.get(cfg["base_url"] + query_page)
        time.sleep(4)
        srm.reset_filters(browser.driver)

        # 通用填筛选：按字段标签匹配输入框
        fill = {"asn": merged.get("asn")}
        from selenium.webdriver.common.by import By

        for key, value in fill.items():
            if not value:
                continue
            label = {"asn": "订单号"}.get(key, key)
            for element in browser.driver.find_elements(By.CSS_SELECTOR, "input, textarea"):
                placeholder = element.get_attribute("placeholder") or ""
                if label in placeholder:
                    element.clear()
                    element.send_keys(str(value))
                    steps.append(f"填筛选 {label}={value}")
                    break
        if fill.get("asn"):
            srm.click_query(browser.driver)
        table = srm.deduction_pages(browser.driver)
        steps.append("翻页读取表格")
        timings_read = time.perf_counter() - t0

        rows = parse_table(table)
        live = LiveDeduction(ttl=9999)
        live.rows = rows
        live.page_total = table.get("total")
        live.fetched_at = time.time()
        timings_agg = time.perf_counter() - t0

        from .dates import resolve_range

        start, end, _ = resolve_range(merged)
        if intent == "overview":
            overview = live.overview()
            result = {
                "mode": "live",
                "shipments": {"count": 0, "total_qty": None, "total_amount": None,
                              "min_date": None, "max_date": None},
                "deductions": {
                    "count": overview["count"],
                    "total_amount": overview["total_amount"],
                    "min_date": overview["min_date"],
                    "max_date": overview["max_date"],
                },
                "datasets": [{"dataset": "live:srm", "rows_written": overview["count"]}],
                "last_sync": overview["live_fetched_at"],
                "page_total": overview["page_total"],
            }
            reply = (
                f"实时读取 SRM 质量扣款明细：共 {overview['count']} 条"
                f"（页面显示 {overview['page_total'] or '—'} 条），"
                f"金额合计 {overview['total_amount']:,.2f} 元。"
            )
        elif intent == "deduction.summary":
            result = live.summary(start=start, end=end, item=merged.get("item"),
                                  asn=merged.get("asn"), group_by=merged.get("group_by", "item"))
            reply = answer.render("deduction.summary", result, parsed, live.fetched_iso())
            reply = reply.replace("本地库同步于", "实时读取于")
        else:
            result = live.detail(start=start, end=end, item=merged.get("item"),
                                 asn=merged.get("asn"), limit=merged.get("limit", 50))
            reply = answer.render("deduction.detail", result, parsed, live.fetched_iso())
            reply = reply.replace("本地库同步于", "实时读取于")

        steps.append("内存汇总")
        return {
            "ok": True,
            "intent": intent,
            "reply": reply,
            "data": result,
            "path": {"steps": steps, "page": query_page, "system": system},
            "timings": {
                "login_s": round(timings_login, 3),
                "read_s": round(timings_read, 3),
                "total_s": round(time.perf_counter() - started, 3),
            },
            "read_only": True,
        }
    finally:
        browser.teardown()
