"""圣农 SRM 登录态（cookie）的保存和读取。

取数已经不在这里：接口地址、翻页、字段对应写在 systems/sunner_srm/graph.json，
由 qflow/runner.py 按图谱执行。这个文件只剩登录态，T5 之后也会被通用实现取代。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar

from . import config

logger = logging.getLogger("qflow.srmapi")

SESSION_FILE = config.DEFAULT_DATA_DIR / "live" / "session.json"


class AuthError(RuntimeError):
    """会话失效：需要重新登录（自动 OCR 或人工验证码）。"""


# ---------- session 持久化 ----------


def save_session(cookies: list[dict]) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cookies": [
            {k: c.get(k) for k in ("name", "value", "domain", "path", "secure", "expiry")}
            for c in cookies
        ],
    }
    SESSION_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_session() -> list[dict] | None:
    try:
        payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        cookies = payload.get("cookies") or []
        return cookies or None
    except Exception:
        return None


def session_mtime() -> float:
    try:
        return SESSION_FILE.stat().st_mtime
    except OSError:
        return 0.0

# 页面列名 -> API 字段（与 data/live 抓取的表头逐列核对过）
FIELD_MAP = {
    "asn": "refNo",            # 收货单号
    "item": "projectName",     # 检测项目名称
    "amount": "itemAmount",    # 扣罚金额
    "biz_date": "postDate",    # 制单日期
    "result_text": "projectResult",  # 检测结果
    "range_text": "dedRange",  # 扣罚范围
    "formula": "formula",      # 扣罚公式
    "qty": "qty",              # 数量
    "order_no": "poNo",        # 订单号
}


def _cookie_from_dict(c: dict) -> Cookie:
    domain = c.get("domain") or ""
    return Cookie(
        version=0,
        name=c["name"],
        value=c.get("value") or "",
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=c.get("path") or "/",
        path_specified=True,
        secure=bool(c.get("secure")),
        expires=c.get("expiry"),
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def opener_from_driver(driver) -> urllib.request.OpenerDirector:
    """把 Selenium cookie 灌进 urllib opener。"""
    cj = CookieJar()
    for c in driver.get_cookies():
        cj.set_cookie(_cookie_from_dict(c))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def opener_from_cookies(cookies: list[dict]) -> urllib.request.OpenerDirector:
    """把 session.json 里的 cookie 灌进 urllib opener（纯 HTTP，不需要浏览器）。"""
    cj = CookieJar()
    for c in cookies or []:
        try:
            cj.set_cookie(_cookie_from_dict(c))
        except Exception:
            continue
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
