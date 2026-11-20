"""vendorQuery 接口直连：复用已登录 driver 的 cookie，用 urllib 分页拉取扣款明细。

比翻页抓 DOM 快一个量级，也不触发页面渲染；请求形态与前端一致
（POST form page/rows），只是跳过了浏览器渲染层。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar

from . import config, online, srm

logger = logging.getLogger("qflow.srmapi")

API_PATH = "/srm/qm/quajud/vendorQuery"
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


def post(opener, data: dict) -> dict:
    online.assert_readonly_request("POST", srm.URL + API_PATH)
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        srm.URL + API_PATH,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": srm.DEDUCTION_URL,
            "Origin": srm.URL,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with opener.open(req, timeout=30) as resp:
            final_url = resp.geturl()
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError(f"HTTP {exc.code}") from exc
        raise
    if "/login" in final_url:
        raise AuthError("被重定向到登录页")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # 登录页/网关错误页返回 HTML，说明会话失效
        raise AuthError("vendorQuery 返回非 JSON（会话可能已过期）") from exc
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise AuthError(f"vendorQuery 失败: {str(payload)[:200]}")
    return payload


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_api_row(row: dict) -> dict | None:
    """API 行 -> 与 parse_table 相同的内存记录结构。"""
    asn = str(row.get("refNo") or "").strip()
    amount = _num(row.get("itemAmount"))
    if not asn or amount is None:
        return None
    biz_date = str(row.get("postDate") or row.get("createTime") or "").strip()[:10]
    return {
        "id": str(row.get("id") or "").strip() or None,
        "asn": asn,
        "item": str(row.get("projectName") or "").strip() or None,
        "amount": amount,
        "biz_date": biz_date,
        "result_text": str(row.get("projectResult") or "").strip() or None,
        "range_text": _num(row.get("dedRange")),
        "formula": str(row.get("formula") or "").strip() or None,
        "qty": _num(row.get("qty")),
        "order_no": str(row.get("poNo") or "").strip() or None,
    }


def fetch_all_opener(opener, page_size: int = 200, max_pages: int = 20) -> tuple[list[dict], int | None]:
    """用 opener 拉全量扣款明细。返回 (records, total)。会话失效抛 AuthError。"""
    records: list[dict] = []
    seen: set[str] = set()
    total: int | None = None
    for page in range(1, max_pages + 1):
        payload = post(opener, {"page": page, "rows": page_size})
        rows = payload.get("rows") or []
        if total is None:
            total = payload.get("total")
        for raw in rows:
            record = parse_api_row(raw)
            if record is None:
                continue
            # id 只到收货行粒度：同一行的不同检测项目共用 id，必须拼 projectName；
            # 缺字段时用整行原文做键
            key = (
                f"{record['id']}|{record['item']}"
                if record["id"]
                else json.dumps(raw, ensure_ascii=False, sort_keys=True)
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
        page_count = payload.get("pageCount")
        if page_count is not None and page >= int(page_count):
            break
        if not rows:
            break
        if total is not None and len(records) >= int(total):
            break
    return records, total


def fetch_all(driver, page_size: int = 200, max_pages: int = 20) -> tuple[list[dict], int | None]:
    """兼容旧调用：从 driver 取 cookie 后走 fetch_all_opener。"""
    return fetch_all_opener(opener_from_driver(driver), page_size, max_pages)
