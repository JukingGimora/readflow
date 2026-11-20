"""只读 HTTP 服务：给 voiceAI / 内部工具调用.

端点：
    GET  /health        健康检查 + 数据量
    GET  /capabilities  能力清单（含参数 JSON Schema）
    GET  /tools         OpenAI function-calling 工具描述
    GET  /datasets      导入审计（哪些文件、多少行、什么时候）
    GET  /desk          可直接使用的查询页面（浏览器）
    POST /query         {"text": "..."} 或 {"intent": "...", "params": {...}}
    POST /desk/api/query 页面用：{text, state} -> 多轮回答

设计：只用标准库，不引入 Web 框架；每个请求独立打开 SQLite 连接，天然线程安全。
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

from . import capabilities, config, db, engine, voice

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
    def __init__(self, db_path, token: str = "", live: bool = False):
        self.db_path = str(db_path)
        self.token = (token or "").strip()
        # 页面口令：给人输入用，可以比 token 短；安全性靠失败次数上限
        self.desk_pass = config.DESK_PASS
        self._fails: dict = {}
        self._fail_lock = threading.Lock()
        self.live = live
        db.connect(self.db_path).close()  # 启动即建表，尽早暴露路径错误

    def _status_payload(self) -> dict:
        payload = {"ok": True, "mode": "live" if self.live else "cache"}
        if self.live:
            from . import livequery

            client = livequery.get_client()
            payload["live"] = {
                "rows": len(client.rows),
                "page_total": client.page_total,
                "fetched_at": client.fetched_iso(),
                "age_seconds": round(client.age, 1) if client.age is not None else None,
                "error": client.last_error,
            }
            payload["data_as_of"] = client.fetched_iso()
        else:
            conn = db.connect(self.db_path)
            try:
                payload.update(engine.health(conn))
            finally:
                conn.close()
        return payload

    def _systems_payload(self) -> dict:
        from . import authbridge, systems

        live_client = None
        if self.live:
            try:
                from . import livequery

                live_client = livequery.get_client()
            except Exception:
                live_client = None
        cards = []
        for spec in systems.list_systems():
            sid = spec["id"]
            bridge = authbridge.get_bridge(sid)
            if bridge is not None:
                st = bridge.status(live_client if sid == "sunner_srm" else None)
            else:
                st = {"status": "pending", "rows": None, "error": None}
            cards.append({**spec, **st})
        return {"ok": True, "mode": "live" if self.live else "cache", "systems": cards}

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
            return 200, self._status_payload()

        desk = path.startswith("/desk/api/")
        if method == "GET" and path == "/desk/api/session":
            return 200, {"ok": True, "required": bool(self.token), "authed": self._authorized(headers, True)}
        if method == "POST" and path == "/desk/api/login":
            if not self.token:
                return 200, {"ok": True}
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            return self._login(headers, payload if isinstance(payload, dict) else {})

        # 页面接口和其它接口一样要鉴权；页面多一种方式：登录后的会话 cookie
        if not self._authorized(headers, allow_session=desk):
            return 401, {"ok": False, "error": "unauthorized"}

        if method == "GET" and path == "/capabilities":
            conn = db.connect(self.db_path)
            try:
                return 200, capabilities.capabilities_payload(conn)
            finally:
                conn.close()

        if method == "GET" and path == "/tools":
            conn = db.connect(self.db_path)
            try:
                return 200, capabilities.tools_payload(conn)
            finally:
                conn.close()

        if method == "GET" and path == "/datasets":
            conn = db.connect(self.db_path)
            try:
                return 200, {
                    "ok": True,
                    "datasets": engine.datasets(conn),
                    "data_as_of": engine.data_as_of(conn),
                }
            finally:
                conn.close()

        if method == "GET" and path == "/desk/api/status":
            return 200, self._status_payload()

        if method == "GET" and path == "/desk/api/systems":
            return 200, self._systems_payload()

        if method == "GET" and path == "/desk/api/graph":
            from . import graph

            try:
                g = graph.Graph.load()
                return 200, {
                    "ok": True,
                    "graph": g.data,
                    # 当前运行层真正能执行的意图；图谱里其余能力只能展示
                    "executable_intents": list(capabilities.INTENTS),
                }
            except Exception as exc:
                return 500, {"ok": False, "error": str(exc)}

        if method == "POST" and path == "/desk/api/graph/save":
            from . import graph

            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                graph_data = payload.get("graph")
                if not isinstance(graph_data, dict):
                    return 400, {"ok": False, "error": "graph_must_be_object"}
                if not graph_data.get("nodes"):
                    return 400, {"ok": False, "error": "图谱里没有节点"}
                graph.Graph(graph_data)  # 校验合法性
                graph.GRAPH_PATH.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
                return 200, {"ok": True}
            except Exception as exc:
                return 400, {"ok": False, "error": str(exc)}

        if method == "GET" and path == "/desk/api/auth/challenge":
            from . import authbridge

            system = (parse_qs(urlparse(raw_path).query).get("system") or ["sunner_srm"])[0]
            bridge = authbridge.get_bridge(system)
            if bridge is None:
                return 404, {"ok": False, "error": "system_not_supported", "system": system}
            return 200, bridge.challenge()

        if method == "POST" and path == "/desk/api/auth/refresh":
            from . import authbridge

            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            system = str(payload.get("system") or "sunner_srm")
            bridge = authbridge.get_bridge(system)
            if bridge is None:
                return 404, {"ok": False, "error": "system_not_supported", "system": system}
            return 200, bridge.refresh()

        if method == "POST" and path == "/desk/api/auth/submit":
            from . import authbridge

            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"ok": False, "error": "invalid_json"}
            system = str(payload.get("system") or "sunner_srm")
            bridge = authbridge.get_bridge(system)
            if bridge is None:
                return 404, {"ok": False, "error": "system_not_supported", "system": system}
            return 200, bridge.submit(payload if isinstance(payload, dict) else {})

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
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"ok": False, "error": "invalid_json"}
            heard = payload.get("heard") or payload.get("text")
            if not heard:
                return 400, {"ok": False, "error": "heard_required"}
            conn = db.connect(self.db_path)
            try:
                result = voice.handle(
                    conn,
                    str(heard),
                    payload.get("context") if isinstance(payload.get("context"), dict) else {},
                    live=self.live,
                )
                return 200, result
            except ValueError as exc:
                return 200, {
                    "reply": "这个我没查到，换个说法再问一次。",
                    "state_patch": {},
                    "handled": False,
                    "error": str(exc),
                }
            finally:
                conn.close()

        if method == "POST" and path == "/desk/api/query":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"ok": False, "error": "invalid_json"}
            if not isinstance(payload, dict):
                return 400, {"ok": False, "error": "body_must_be_object"}
            text = payload.get("text")
            if not text:
                return 400, {"ok": False, "error": "text_required"}
            state = payload.get("state")
            if state is not None and not isinstance(state, dict):
                return 400, {"ok": False, "error": "state_must_be_object"}
            conn = db.connect(self.db_path)
            try:
                result = voice.handle(
                    conn, str(text), {"state": state or {}}, live=self.live
                )
                return 200, {"ok": True, **result}
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}
            finally:
                conn.close()

        if method == "POST" and path == "/query":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"ok": False, "error": "invalid_json"}
            if not isinstance(payload, dict):
                return 400, {"ok": False, "error": "body_must_be_object"}

            text = payload.get("text") or payload.get("query")
            intent = payload.get("intent")
            params = payload.get("params") or {}
            if not text and not intent:
                return 400, {"ok": False, "error": "text_or_intent_required"}
            if not isinstance(params, dict):
                return 400, {"ok": False, "error": "params_must_be_object"}

            conn = db.connect(self.db_path)
            try:
                result = capabilities.query(
                    conn,
                    text=str(text) if text else None,
                    intent=str(intent) if intent else None,
                    params=params,
                    live=self.live,
                )
                return 200, result
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}
            finally:
                conn.close()

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


def make_server(host: str, port: int, db_path, token: str = "", live: bool = False) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"api": Api(db_path, token, live)})
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    return server


def serve(host: str, port: int, db_path, token: str = "", live: bool | None = None) -> None:
    if live is None:
        live = config.LIVE_MODE
    if live:
        from . import livequery

        livequery.get_client()  # 后台预热：登录 + 第一次抓取
    server = make_server(host, port, db_path, token, live)
    actual_host, actual_port = server.server_address[:2]
    logger.info("readflow query service on http://%s:%s (db=%s)", actual_host, actual_port, db_path)
    print(f"readflow query service: http://{actual_host}:{actual_port}")
    print("  GET  /desk    /health  /capabilities  /tools  /datasets")
    print('  POST /query   {"text": "去年11月扣款合计"}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
        if live:
            try:
                from . import livequery

                livequery.get_client().close()
            except Exception:
                pass


if __name__ == "__main__":
    serve(config.DEFAULT_HOST, config.DEFAULT_PORT, config.DEFAULT_DB, config.DEFAULT_TOKEN)

