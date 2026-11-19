"""用仓库里已验证的验证码策略登录 SRM，抓取质量扣款明细（离线演示/采集用）。

登录/验证码/翻页的实现已移到 qflow.srm；本脚本保留 CLI，并顺手把抓到的
表格导入本地库（只用于演示/测试；生产查询走 qflow.livequery 实时读取）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

READFLOW = Path(__file__).resolve().parents[1]
PROJECT = READFLOW.parent
sys.path.insert(0, str(READFLOW))

from qflow import config, db, live  # noqa: E402
from qflow.srm import (  # noqa: E402
    URL,
    captcha_images,
    creds,
    extract_table,
    goto_deduction,
    login,
    messages,
    new_driver,
    next_page,
    recognize,
    DEDUCTION_URL,
    pagination_total,
    reset_filters,
)

OUT = READFLOW / "data" / "live"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=30)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    driver = new_driver(headless=True)
    try:
        if not login(driver):
            print("LOGIN FAILED", flush=True)
            return 4

        goto_deduction(driver)
        table = extract_table(driver)
        headers = table.get("headers") or []
        rows = list(table.get("rows") or [])
        pages = 1
        if rows:
            signature = rows[0]
            while pages < args.max_pages:
                if not next_page(driver):
                    break
                import time

                time.sleep(1.0)
                page = extract_table(driver)
                more = page.get("rows") or []
                if not more or more[0] == signature:
                    break
                signature = more[0]
                rows.extend(more)
                pages += 1
        print(f"TABLE rows={len(rows)} pages={pages} total={pagination_total(driver)}", flush=True)

        table = {"headers": headers, "rows": rows}
        if rows:
            conn = db.connect(config.DEFAULT_DB)
            try:
                stats = live.import_table(conn, table, dataset="live_srm", source="live:quajud/page")
            finally:
                conn.close()
            print("LIVE IMPORT", stats, flush=True)

        payload = {
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "read_only": True,
            "url": DEDUCTION_URL,
            "table": table,
            "total": pagination_total(driver),
        }
        (OUT / "deduction_live.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved", OUT / "deduction_live.json", flush=True)
        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
