"""验证 vendorQuery API 直连：登录拿 cookie → urllib POST 分页拉取。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.parse
from http.cookiejar import CookieJar
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qflow import srm  # noqa: E402

API = srm.URL + "/srm/qm/quajud/vendorQuery"


def opener_from_driver(driver):
    cj = CookieJar()
    for c in driver.get_cookies():
        # Selenium cookie dict -> http.cookiejar.Cookie
        from http.cookiejar import Cookie
        cj.set_cookie(Cookie(
            version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
            domain=c.get("domain", ""), domain_specified=bool(c.get("domain")),
            domain_initial_dot=c.get("domain", "").startswith("."),
            path=c.get("path", "/"), path_specified=True,
            secure=bool(c.get("secure")), expires=c.get("expiry"),
            discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def post(opener, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": srm.DEDUCTION_URL,
        "Origin": srm.URL,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    })
    with opener.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    driver = srm.new_driver(headless=True)
    try:
        if not srm.login(driver):
            print("LOGIN FAILED")
            return 1
        opener = opener_from_driver(driver)
        # 顺便验证：driver 里 fetch 同接口也能通（对照）
        for rows in (10, 100, 200):
            try:
                data = post(opener, {"page": 1, "rows": rows})
                print(f"rows={rows}: success={data.get('success')} total={data.get('total')} "
                      f"pageCount={data.get('pageCount')} rows_len={len(data.get('rows') or [])}")
                if rows == 10 and data.get("rows"):
                    print("SAMPLE:", json.dumps(data["rows"][0], ensure_ascii=False)[:1500])
            except Exception as exc:
                print(f"rows={rows}: ERROR {type(exc).__name__} {exc}")
        # 翻页测试
        try:
            data = post(opener, {"page": 2, "rows": 10})
            print("page2 first asn-ish:", json.dumps((data.get("rows") or [{}])[0], ensure_ascii=False)[:300])
        except Exception as exc:
            print("page2 ERROR", exc)
    finally:
        driver.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
