"""只读 HTTP 服务：页面、VoiceLab 和其它工具都从这里进。

    GET  /desk                 查询页面
    GET  /health               服务是否在线，接入了哪些系统
    GET  /capabilities         每个系统能查什么（来自图谱）
    POST /query                {"text": "...", "state": {...}}  或  {"system": id, "capability": id, "values": {...}}
    POST /voice/handle         VoiceLab 适配：{"heard": "...", "context": {"state": {...}}}
    /desk/api/*                页面用：query、systems、account、login、graph、graph/save、spec/validate、voice/*

这里没有任何某个系统专用的东西：系统来自图谱（qflow/agent.py），取数走通用执行器（qflow/runner.py）。
只用标准库，不引入 Web 框架。
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from . import agent as agents
from . import config, login, online, runner, spec, view

logger = logging.getLogger("qflow.service")


def _voicelab_request(path: str, *, method: str = "POST", body: bytes | None = None, content_type: str | None = None) -> dict:
    url = f"{config.VOICELAB_URL}{path}"
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"VoiceLab 返回 HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"连接 VoiceLab 失败 ({url}): {exc}") from exc
MAX_BODY = 1_048_576  # 1 MB
SESSION_COOKIE = "qflow_session"
MAX_LOGIN_FAILS = 5  # 同一来源一天最多猜错几次
MAX_LOGIN_FAILS_TOTAL = 30  # 所有来源合计
_PASS_NOISE = re.compile(r"[\s,.，。！!？?、·~～-]+")


def _normalize_pass(text: str) -> str:
    """口令是一句话，人会多打空格和标点、混用全半角；比对前抹平。"""
    return _PASS_NOISE.sub("", unicodedata.normalize("NFKC", text)).lower()


DESK_HTML = Path(__file__).resolve().parent / "web" / "index.html"


class Api:
    def __init__(self, token: str = ""):
        self.token = (token or "").strip()
        # 页面口令：给人输入用，可以比 token 短；安全性靠失败次数上限
        self.desk_pass = config.DESK_PASS
        self._fails: dict = {}
        self._fail_lock = threading.Lock()

    # ---- 取数：页面、语音、/query 走同一条路 ----
    def _ask(self, text: str, state: dict | None) -> dict:
        saved = (state or {}).get(view.STATE_KEY)
        try:
            out = agents.ask(text, saved if isinstance(saved, dict) else None)
        except (runner.RunError, online.ReadOnlyViolation) as exc:
            logger.warning("查询失败：%s", exc)
            return {"reply": "这次没查成：" + str(exc), "handled": True, "followups": [], "state_patch": {}, "error": str(exc)}
        return view.answer(agents.get(out.get("system") or ""), out)

    def _systems(self) -> list[dict]:
        return [a.status() for a in agents.all().values()]

    @staticmethod
    def _json(body: bytes):
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    # ---- 页面登录门：口令换一个当天有效的签名会话，不落库 ----
    def _session_value(self, day: str | None = None) -> str:
        day = day or time.strftime("%Y%m%d")
        msg = f"desk:{day}:{self.desk_pass}".encode("utf-8")
        sig = hmac.new(self.token.encode("utf-8"), msg, "sha256").hexdigest()
        return f"{day}.{sig}"

    def _session_ok(self, headers: dict) -> bool:
        normalized = {str(k).lower(): v for k, v in headers.items()}
        for part in str(normalized.get("cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return hmac.compare_digest(value.encode("utf-8"), self._session_value().encode("utf-8"))
        return False

    def _login(self, headers: dict, payload: dict):
        normalized = {str(k).lower(): v for k, v in headers.items()}
        who = str(normalized.get("cf-connecting-ip") or normalized.get("x-forwarded-for") or "direct").split(",")[0].strip()
        day = time.strftime("%Y%m%d")
        key = (who, day)
        with self._fail_lock:
            # 来源地址可以伪造，所以再加一个不分来源的总上限
            total = sum(v for k, v in self._fails.items() if k[1] == day)
            if self._fails.get(key, 0) >= MAX_LOGIN_FAILS or total >= MAX_LOGIN_FAILS_TOTAL:
                return 429, {"ok": False, "error": "今天试错次数太多，明天再来", "left": 0}
        said = _normalize_pass(str(payload.get("pass") or ""))
        expected = _normalize_pass(self.desk_pass or self.token)
        if said and hmac.compare_digest(said.encode("utf-8"), expected.encode("utf-8")):
            cookie = f"{SESSION_COOKIE}={self._session_value()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400"
            if str(normalized.get("x-forwarded-proto") or "").lower() == "https":
                cookie += "; Secure"
            return 200, {"ok": True, "_headers": [("Set-Cookie", cookie)]}
        with self._fail_lock:
            self._fails = {k: v for k, v in self._fails.items() if k[1] == key[1]}
            self._fails[key] = self._fails.get(key, 0) + 1
            left = max(0, MAX_LOGIN_FAILS - self._fails[key])
        return 401, {"ok": False, "error": "口令不对", "left": left}

    def _authorized(self, headers: dict, allow_session: bool = False) -> bool:
        if not self.token:
            return True
        if allow_session and self._session_ok(headers):
            return True
        normalized = {str(k).lower(): v for k, v in headers.items()}
        key = normalized.get("x-api-key") or ""
        auth = normalized.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        return hmac.compare_digest(str(key).encode("utf-8"), self.token.encode("utf-8"))

    def handle(self, method: str, raw_path: str, headers: dict, body: bytes):
        path = urlparse(raw_path).path.rstrip("/") or "/"
        if path.startswith("/api/"):
            path = path[4:]
        method = method.upper()

        if method == "GET" and path == "/health":
            return 200, {"ok": True, "systems": [{"id": s["id"], "status": s["status"]} for s in self._systems()]}

        desk = path.startswith("/desk/api/")
        if method == "GET" and path == "/desk/api/session":
            return 200, {"ok": True, "required": bool(self.token), "authed": self._authorized(headers, True)}
        if method == "POST" and path == "/desk/api/login":
            if not self.token:
                return 200, {"ok": True}
            return self._login(headers, self._json(body) or {})

        # 页面接口和其它接口一样要鉴权；页面多一种方式：登录后的会话 cookie
        if not self._authorized(headers, allow_session=desk):
            return 401, {"ok": False, "error": "unauthorized"}

        payload = None
        if method == "POST" and not path.startswith("/desk/api/voice/"):
            payload = self._json(body)
            if payload is None:
                return 400, {"ok": False, "error": "invalid_json"}

        if method == "GET" and path == "/capabilities":
            return 200, {"ok": True, "systems": [
                {"id": a.id, "label": a.label, "capabilities": [
                    {"id": c["id"], "label": c.get("label"), "slots": c.get("slots") or [], "examples": c.get("examples") or []}
                    for c in a.graph.capabilities.values()]}
                for a in agents.all().values()]}

        if method == "GET" and path in {"/desk/api/status", "/desk/api/systems"}:
            return 200, {"ok": True, "systems": self._systems(), "problems": agents.problems}

        if method == "GET" and path == "/desk/api/graph":
            found = agents.all()
            return 200, {"ok": True, "view": view.graph_view(found), "docs": {sid: a.graph.doc for sid, a in found.items()}}

        if method == "POST" and path == "/desk/api/spec/validate":
            return 200, {"ok": True, "report": spec.validate(payload.get("graph", payload))}

        if method == "POST" and path == "/desk/api/graph/save":
            doc = payload.get("graph")
            report = spec.validate(doc)
            if not report["ok"]:
                return 400, {"ok": False, "error": "图谱没通过校验", "report": report}
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", str(doc["system"]["id"])):
                return 400, {"ok": False, "error": "system.id 只能用字母、数字、下划线和短横线"}
            target = agents.store_dir() / doc["system"]["id"] / "graph.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 200, {"ok": True, "system": doc["system"]["id"], "report": report}

        if method == "POST" and path in {"/desk/api/account", "/desk/api/login/step"}:
            target = agents.get(str(payload.get("system") or ""))
            if target is None:
                return 404, {"ok": False, "error": "没有这个系统"}
            try:
                if path == "/desk/api/account":
                    target.save_account(str(payload.get("username") or ""), str(payload.get("password") or ""))
                    return 200, {"ok": True, "account": target.account_public()}
                return 200, {"ok": True, **login.handle(target, str(payload.get("action") or ""), payload)}
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}

        if method == "POST" and path == "/desk/api/voice/session":
            try:
                session = _voicelab_request(
                    "/v1/sessions",
                    body=json.dumps({"agent_id": config.VOICELAB_AGENT_ID}).encode("utf-8"),
                    content_type="application/json",
                )
                sid = session.get("sid") or session.get("session_id") or session.get("id")
                return 200, {"ok": True, "session_id": sid}
            except Exception as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "POST" and path == "/desk/api/voice/turn":
            sid = (parse_qs(urlparse(raw_path).query).get("sid") or [""])[0].strip()
            if not sid:
                return 400, {"ok": False, "error": "missing_sid"}
            content_type = headers.get("content-type") or headers.get("Content-Type") or ""
            try:
                result = _voicelab_request(
                    f"/v1/sessions/{quote(sid, safe='')}/turn",
                    body=body,
                    content_type=content_type,
                )
                return 200, {
                    "ok": True,
                    "heard": result.get("heard", ""),
                    "reply": result.get("reply", ""),
                    "errors": result.get("errors", []),
                }
            except Exception as exc:
                return 502, {"ok": False, "error": str(exc)}

        if method == "DELETE" and path == "/desk/api/voice/session":
            sid = (parse_qs(urlparse(raw_path).query).get("sid") or [""])[0].strip()
            if sid:
                try:
                    _voicelab_request(f"/v1/sessions/{quote(sid, safe='')}", method="DELETE")
                except Exception:
                    pass
            return 200, {"ok": True}

        if method == "POST" and path in {"/voice/handle", "/voice"}:
            heard = payload.get("heard") or payload.get("text")
            if not heard:
                return 400, {"ok": False, "error": "heard_required"}
            context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
            return 200, self._ask(str(heard), context.get("state") if isinstance(context.get("state"), dict) else None)

        if method == "POST" and path in {"/desk/api/query", "/query"}:
            state = payload.get("state")
            if state is not None and not isinstance(state, dict):
                return 400, {"ok": False, "error": "state_must_be_object"}
            text = payload.get("text") or payload.get("query")
            if text:
                return 200, {"ok": True, **self._ask(str(text), state)}
            # 不经过听懂：直接点名一个系统的一个能力（给上层编排用）
            target = agents.get(str(payload.get("system") or ""))
            if target is None or not payload.get("capability"):
                return 400, {"ok": False, "error": "text_required"}
            try:
                values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
                return 200, {"ok": True, "result": target.run(str(payload["capability"]), values)}
            except runner.AuthExpired as exc:
                return 409, {"ok": False, "error": f"登录态失效：{exc}", "needs_login": True}
            except (runner.RunError, online.ReadOnlyViolation) as exc:
                return 400, {"ok": False, "error": str(exc)}

        return 404, {"ok": False, "error": "not_found", "path": path}


class _Handler(BaseHTTPRequestHandler):
    api: Api = None  # type: ignore[assignment]
    server_version = "qflow/0.1"
    protocol_version = "HTTP/1.1"

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, None)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in {"/", "/desk"}:
            if not DESK_HTML.exists():
                self._send(404, {"ok": False, "error": "desk_page_missing"})
                return
            self._send_bytes(200, DESK_HTML.read_bytes())
            return
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, {"ok": False, "error": "body_too_large"})
            return
        body = self.rfile.read(length) if length else b""
        try:
            status, payload = self.api.handle(method, self.path, dict(self.headers), body)
        except Exception:  # noqa: BLE001
            logger.exception("unhandled error: %s %s", method, self.path)
            status, payload = 500, {"ok": False, "error": "internal_error"}
        extra = payload.pop("_headers", None) if isinstance(payload, dict) else None
        self._send(status, payload, extra)

    def _send_bytes(self, status: int, data: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, status: int, payload, extra_headers=None):
        data = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        for name, value in extra_headers or ():
            self.send_header(name, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def log_message(self, fmt, *args):  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)


def make_server(host: str, port: int, token: str = "") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"api": Api(token)})
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    return server


def serve(host: str, port: int, token: str = "") -> None:
    server = make_server(host, port, token)
    actual_host, actual_port = server.server_address[:2]
    found = agents.all()
    print(f"readflow: http://{actual_host}:{actual_port}/desk")
    print("  已接入：" + ("、".join(f"{a.label}（{a.status()['status']}）" for a in found.values()) or "还没有系统"))
    for path, why in agents.problems.items():
        print(f"  没加载 {path}：{why}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
