"""抓 vendorQuery 的真实请求：登录 → 进扣款页 → 翻页/查询 → 从 performance log 还原 POST body。

用法：
    .venv/bin/python scripts/capture_vendorquery.py [--filter-asn XXX] [--pages 2]

输出 data/live/vendorquery_capture.json，包含每个 vendorQuery 请求的
url / method / headers / postData / response 关键字段。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
READFLOW = HERE.parent
sys.path.insert(0, str(READFLOW))

from qflow import srm  # noqa: E402

OUT = READFLOW / "data" / "live" / "vendorquery_capture.json"
TARGET = "/srm/qm/quajud/vendorQuery"


def drain(driver) -> list[dict]:
    try:
        entries = driver.get_log("performance")
    except Exception:
        return []
    events = []
    for entry in entries:
        try:
            events.append(json.loads(entry["message"])["message"])
        except Exception:
            continue
    return events


def find_vendor_requests(events: list[dict]) -> list[dict]:
    found = []
    for event in events:
        if event.get("method") != "Network.requestWillBeSent":
            continue
        request = (event.get("params") or {}).get("request") or {}
        url = request.get("url") or ""
        if TARGET in url:
            found.append({
                "url": url,
                "method": request.get("method"),
                "headers": request.get("headers") or {},
                "postData": request.get("postData"),
                "hasPostData": request.get("hasPostData"),
            })
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=3, help="最多抓几次请求（翻页次数）")
    parser.add_argument("--asn", default="", help="可选：先在页面填收货单号再查询")
    args = parser.parse_args()

    def _perf(options):
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = srm.new_driver(headless=True, configure=_perf)

    captured: list[dict] = []
    try:
        ok = srm.login(driver)
        print("LOGIN", ok, flush=True)
        if not ok:
            return 1

        drain(driver)  # 丢掉登录过程的噪声

        srm.goto_deduction(driver)
        time.sleep(2)
        srm.reset_filters(driver)
        time.sleep(1)

        if args.asn:
            from selenium.webdriver.common.by import By
            inputs = [
                e for e in driver.find_elements(By.CSS_SELECTOR, "input")
                if e.is_displayed()
            ]
            # 收货单号输入框：找 placeholder 含 单号/ASN/收货 的
            target = None
            for e in inputs:
                ph = e.get_attribute("placeholder") or ""
                if any(k in ph for k in ("单号", "ASN", "asn", "收货")):
                    target = e
                    break
            if target is None and inputs:
                target = inputs[0]
            if target is not None:
                target.clear()
                target.send_keys(args.asn)
                print("FILTER asn =", args.asn, flush=True)
                time.sleep(0.5)

        srm.click_query(driver)
        time.sleep(3)
        captured.extend(find_vendor_requests(drain(driver)))

        for _ in range(max(0, args.pages - 1)):
            if not srm.next_page(driver):
                break
            time.sleep(2)
            captured.extend(find_vendor_requests(drain(driver)))

        # 再点一次查询（不带筛选的默认查询也抓一份）
        srm.reset_filters(driver)
        time.sleep(1)
        srm.click_query(driver)
        time.sleep(3)
        captured.extend(find_vendor_requests(drain(driver)))

        # 去重（按 postData）
        seen = set()
        uniq = []
        for item in captured:
            key = item["postData"] or item["url"]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(item)

        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"CAPTURED {len(uniq)} unique requests -> {OUT}", flush=True)
        for item in uniq:
            print("----")
            print("URL:", item["url"])
            print("postData:", item["postData"])
            headers = {k: v for k, v in item["headers"].items() if k.lower() in (
                "content-type", "authorization", "x-token", "token", "cookie", "tenant", "company")}
            print("headers:", json.dumps(headers, ensure_ascii=False))
        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
