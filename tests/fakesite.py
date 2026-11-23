"""一个假的业务系统「小店订单」，和圣农毫无关系：登录页（账号 + 密码 + 图片验证码）加一个要登录才能调的分页接口。

用来证明「换一个系统只换图谱」：tests 里用它测登录、取数、问答；也可以单独跑起来在页面上手动试：

    python tests/fakesite.py --port 8901      # 账号 demo / 密码 demo123，图谱打印到 tests 用的 graph()
"""

from __future__ import annotations

import json
import random
import struct
import threading
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "demo", "demo123"
ORDERS = [
    {"orderNo": "SO20251101", "day": "2025-11-03", "state": "已签收", "money": "1,200.00", "buyer": "张三"},
    {"orderNo": "SO20251102", "day": "2025-11-08", "state": "已签收", "money": "300.50", "buyer": "李四"},
    {"orderNo": "SO20251103", "day": "2025-11-20", "state": "待发货", "money": "80.00", "buyer": "张三"},
    {"orderNo": "SO20251201", "day": "2025-12-02", "state": "已发货", "money": "450.00", "buyer": "王五"},
    {"orderNo": "SO20251202", "day": "2025-12-09", "state": "已签收", "money": "999.00", "buyer": "李四"},
]
_FONT = {  # 3x5 点阵
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111", "4": "101101111001001",
    "5": "111100111001111", "6": "111100111101111", "7": "111001001001001", "8": "111101111101111", "9": "111101111001111",
}

LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>小店订单 - 登录</title>
<style>body{font:16px sans-serif;margin:60px auto;width:320px}input,button{display:block;width:100%;margin:10px 0;padding:8px;box-sizing:border-box}
img{height:40px;cursor:pointer}#msg{color:#c00;min-height:20px}</style>
<h2>小店订单</h2>
<input id="user" placeholder="账号"><input id="pass" type="password" placeholder="密码">
<img id="captcha" src="/captcha.png" alt="验证码" onclick="this.src='/captcha.png?'+Date.now()">
<input id="code" placeholder="验证码"><button id="go">登录</button><div id="msg"></div>
<script>
document.getElementById('go').onclick = async () => {
  const body = JSON.stringify({user: user.value, pass: pass.value, code: code.value});
  const res = await (await fetch('/api/login', {method: 'POST', body})).json();
  if (res.ok) location.href = '/home';
  else { msg.textContent = res.error; captcha.src = '/captcha.png?' + Date.now(); }
};
</script>"""


def png(text: str, scale: int = 8) -> bytes:
    width, height = (len(text) * 4 + 1) * scale, 7 * scale
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            cx, cy = x // scale - 1, y // scale - 1
            glyph = _FONT[text[cx // 4]] if 0 <= cx < len(text) * 4 and cx % 4 < 3 and 0 <= cy < 5 else None
            row.append(0 if glyph and glyph[cy * 3 + cx % 4] == "1" else 255)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


class Site:
    def __init__(self, port: int = 0, page_cap: int = 2):
        site = self
        self.code = "0000"
        self.sessions: set[str] = set()
        self.seen: list[dict] = []
        self.page_cap = page_cap  # 不管请求每页多少条，最多给这么多：逼着调用方翻页

        class Handler(BaseHTTPRequestHandler):
            def _authed(self) -> bool:
                cookies = dict(p.strip().split("=", 1) for p in (self.headers.get("Cookie") or "").split(";") if "=" in p)
                return cookies.get("sid") in site.sessions

            def _reply(self, status, body: bytes, kind="application/json; charset=utf-8", headers=()):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/captcha.png":
                    site.code = "".join(random.choice("0123456789") for _ in range(4))
                    return self._reply(200, png(site.code), "image/png", [("Cache-Control", "no-store")])
                if path == "/login":
                    return self._reply(200, LOGIN_PAGE.encode(), "text/html; charset=utf-8")
                if not self._authed():
                    return self._reply(302, b"", headers=[("Location", "/login")])
                self._reply(200, "<!doctype html><meta charset=utf-8><h2 id=home>小店订单</h2>已登录".encode(), "text/html; charset=utf-8")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if self.path == "/api/login":
                    said = json.loads(raw or "{}")
                    if said.get("code") != site.code:
                        return self._reply(200, json.dumps({"ok": False, "error": "验证码不对"}, ensure_ascii=False).encode())
                    if (said.get("user"), said.get("pass")) != (USER, PASSWORD):
                        return self._reply(200, json.dumps({"ok": False, "error": "账号或密码不对"}, ensure_ascii=False).encode())
                    sid = "%032x" % random.getrandbits(128)
                    site.sessions.add(sid)
                    return self._reply(200, b'{"ok": true}', headers=[("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")])
                if self.path == "/api/orders/list":
                    form = dict(urllib.parse.parse_qsl(raw))
                    site.seen.append({"form": form, "authed": self._authed(), "type": self.headers.get("Content-Type")})
                    if not self._authed():
                        return self._reply(401, b"{}")
                    size = min(int(form.get("size") or 10), site.page_cap)
                    page = int(form.get("page") or 1)
                    data = {"total": len(ORDERS), "list": ORDERS[(page - 1) * size: page * size]}
                    return self._reply(200, json.dumps({"code": 0, "data": data}, ensure_ascii=False).encode())
                self._reply(404, b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def graph(self) -> dict:
        slots = [
            {"name": "period", "label": "时间", "type": "date_range", "field": "day", "filter": "local"},
            {"name": "state", "label": "状态", "type": "enum", "field": "state", "filter": "local"},
            {"name": "order_no", "label": "订单号", "type": "string", "field": "order_no", "filter": "local",
             "pattern": "(SO\\d{6,})"},
        ]
        return {
            "spec_version": "1.0",
            "system": {"id": "demo_shop", "label": "小店订单", "base_url": self.base_url},
            "generated": {"at": "2026-11-23T10:00:00+08:00", "by": "手写（测试用假系统）", "account_role": "店主"},
            "auth": {
                "login_url": "/login",
                "steps": [
                    {"kind": "input", "field": "username", "locator": "#user"},
                    {"kind": "input", "field": "password", "locator": "#pass"},
                    {"kind": "challenge", "type": "image_captcha", "locator": "#captcha", "answer_locator": "#code",
                     "length": 4, "solver": ["ocr", "human"]},
                    {"kind": "submit", "locator": "#go"},
                ],
                "success": {"url_not_contains": "/login", "present": "#home"},
                "session": {"carrier": "cookie", "check": {"operation": "op.order.list"},
                            "expired_when": [{"status": 401}, {"redirect_contains": "/login"}]},
            },
            "navigation": [
                {"id": "nav.orders", "path": ["销售", "订单列表"], "route": "/orders",
                 "capabilities": ["cap.order.summary", "cap.order.detail"], "not_queryable": None},
                {"id": "nav.refunds", "path": ["销售", "退款"], "route": "/refunds", "capabilities": [],
                 "not_queryable": {"reason": "假系统里没有做这个页面"}},
            ],
            "entities": [{
                "id": "entity.order", "label": "订单", "key": ["order_no"],
                "fields": [
                    {"name": "order_no", "label": "订单号", "type": "string", "required": True},
                    {"name": "day", "label": "下单日期", "type": "date"},
                    {"name": "state", "label": "状态", "type": "enum", "values": ["待发货", "已发货", "已签收"],
                     "synonyms": {"已签收": ["签收了", "收到了"], "待发货": ["没发货", "还没发"]}},
                    {"name": "amount", "label": "金额", "type": "money", "unit": "元"},
                    {"name": "buyer", "label": "买家", "type": "string"},
                ],
            }],
            "operations": [{
                "id": "op.order.list", "kind": "http", "access": "read",
                "request": {"method": "POST", "path": "/api/orders/list", "body_type": "form", "params": []},
                "pagination": {"style": "page", "page_param": "page", "size_param": "size", "max_size": 50,
                               "total_path": "$.data.total"},
                "response": {"ok_when": "$.code == 0", "rows_path": "$.data.list", "entity": "entity.order",
                             "map": {"order_no": "$.orderNo", "day": "$.day", "state": "$.state", "amount": "$.money",
                                     "buyer": "$.buyer"}},
                "evidence": {"verified_at": "2026-11-23T10:00:00+08:00", "sample_rows": 2, "sample": {
                    "request": {"page": 1, "size": 2},
                    "response": {"code": 0, "data": {"total": 5, "list": ORDERS[:2]}}}},
            }],
            "capabilities": [
                {"id": "cap.order.summary", "label": "订单汇总", "access": "read", "nav": "nav.orders",
                 "operation": "op.order.list", "slots": slots, "keywords": ["订单", "下单", "卖了"],
                 "compute": {"group_by": ["state"], "metrics": [
                     {"key": "total", "fn": "sum", "field": "amount", "label": "订单金额", "unit": "元"},
                     {"key": "count", "fn": "count", "label": "单数", "unit": "单"}]},
                 "answer": "{period} 共 {count} 单，金额 {total} 元。",
                 "examples": ["上个月卖了多少钱", "去年11月订单合计", "这个月有多少订单"], "followups": ["明细呢", "只看已签收"]},
                {"id": "cap.order.detail", "label": "订单明细", "access": "read", "nav": "nav.orders",
                 "operation": "op.order.list", "slots": slots, "keywords": ["订单", "下单", "卖了"],
                 "answer": "{period} 的订单有 {count} 单。",
                 "examples": ["列一下上个月的订单", "去年11月订单明细", "没发货的订单有哪些"], "followups": ["合计多少"]},
            ],
        }


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--graph", default=None, help="把这个假系统的图谱写到这个文件")
    args = parser.parse_args()
    site = Site(args.port)
    if args.graph:
        with open(args.graph, "w", encoding="utf-8") as handle:
            json.dump(site.graph(), handle, ensure_ascii=False, indent=2)
    print(f"假系统：{site.base_url}/login  账号 {USER} 密码 {PASSWORD}")
    while True:
        time.sleep(3600)
