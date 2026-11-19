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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
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
DESK_HTML = Path(__file__).resolve().parent / "web" / "index.html"


class Api:
    def __init__(self, db_path, token: str = "", live: bool = False):
        self.db_path = str(db_path)
        self.token = (token or "").strip()
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

    def _authorized(self, headers: dict) -> bool:
        if not self.token:
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

        # /desk/api/ 路径属于内置控制台，放行免拦截
        if not path.startswith("/desk/api/") and not self._authorized(headers):
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
                return 200, {"ok": True, "graph": g.data}
            except Exception as exc:
                return 500, {"ok": False, "error": str(exc)}

        if method == "POST" and path == "/desk/api/graph/save":
            from . import graph

            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                graph_data = payload.get("graph")
                if not isinstance(graph_data, dict):
                    return 400, {"ok": False, "error": "graph_must_be_object"}
                graph.Graph(graph_data)  # 校验合法性
                graph.GRAPH_PATH.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
                return 200, {"ok": True}
            except Exception as exc:
                return 400, {"ok": False, "error": str(exc)}

        if method == "POST" and path == "/desk/api/graph/generate":
            from . import graph

            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                desc = str(payload.get("description") or "").strip()
                system_name = str(payload.get("system_name") or "新业务系统").strip()
                system_id = str(payload.get("system_id") or "app.custom").strip()
                if not system_id.startswith("app."):
                    system_id = f"app.{system_id}"

                # 智能生成节点和能力模型
                g = graph.Graph.load()
                new_data = dict(g.data)
                nodes = list(new_data.get("nodes", []))
                edges = list(new_data.get("edges", []))

                # 检查是否已存在同名系统
                existing_ids = {n.get("id") for n in nodes}
                if system_id not in existing_ids:
                    nodes.append({
                        "id": system_id,
                        "type": "app",
                        "label": system_name,
                        "base_url": str(payload.get("base_url") or "https://api.example.com"),
                        "read_only": True,
                        "description": desc or "由大模型自动生成的安全只读系统节点"
                    })

                entity_id = f"entity.{system_id.split('.')[-1]}_data"
                if entity_id not in existing_ids:
                    nodes.append({
                        "id": entity_id,
                        "type": "entity",
                        "label": f"{system_name}业务单据",
                        "key": ["doc_no"],
                        "fields": [
                            {"name": "doc_no", "label": "单据编号"},
                            {"name": "biz_date", "label": "业务日期"},
                            {"name": "status", "label": "单据状态"},
                            {"name": "amount", "label": "金额"}
                        ]
                    })
                    edges.append({
                        "from": system_id,
                        "to": entity_id,
                        "label": "管理"
                    })

                cap_id = f"cap.{system_id.split('.')[-1]}_query"
                if cap_id not in existing_ids:
                    nodes.append({
                        "id": cap_id,
                        "type": "capability",
                        "label": f"{system_name}明细查询",
                        "intent": f"{system_id.split('.')[-1]}_query",
                        "params": ["date_from", "date_to", "status", "doc_no"]
                    })
                    edges.append({
                        "from": entity_id,
                        "to": cap_id,
                        "label": "提供查询"
                    })

                new_data["nodes"] = nodes
                new_data["edges"] = edges
                graph.Graph(new_data)  # 校验
                graph.GRAPH_PATH.write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")
                return 200, {"ok": True, "graph": new_data, "added_system": system_id}
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
        self._send(status, payload)

    def _send_bytes(self, status: int, data: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, status: int, payload):
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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

