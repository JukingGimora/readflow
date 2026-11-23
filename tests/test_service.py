"""HTTP 服务：口令门，以及页面 / 语音 / /query 走同一条通用路径。业务系统用假系统「小店订单」。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from qflow import agent as agents
from qflow import config, login, service, understand
from tests import fakesite
from tests.data import EXAMPLE_GRAPH


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.site = fakesite.Site()
        root = Path(self.tmp.name)
        for patcher in (
            mock.patch.object(config, "DATA_DIR", root / "data"),
            mock.patch.object(config, "SYSTEMS_DIR", root / "none"),
            mock.patch.object(understand, "ask_model", return_value=None),
            mock.patch.object(login, "unattended", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        target = root / "data" / "systems" / "demo_shop" / "graph.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(self.site.graph(), ensure_ascii=False), encoding="utf-8")
        self.token = "secret"
        self.server = service.make_server("127.0.0.1", 0, token=self.token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.site.close()
        self.tmp.cleanup()

    def _sign_in(self):
        self.site.sessions.add("s1")
        agents.get("demo_shop").save_session([{"name": "sid", "value": "s1", "domain": "127.0.0.1", "path": "/"}])

    def _request(self, path, method="GET", payload=None, token=None, raw=None, cookie=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-API-Key"] = token
        if cookie:
            headers["Cookie"] = cookie
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                self.last_headers = response.headers
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.last_headers = exc.headers
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _login(self, password):
        status, payload = self._request("/desk/api/login", method="POST", payload={"pass": password})
        cookie = (self.last_headers.get("Set-Cookie") or "").split(";")[0]
        return status, payload, cookie

    def test_desk_api_requires_auth(self):
        for method, path, payload in [
            ("GET", "/desk/api/status", None),
            ("GET", "/desk/api/systems", None),
            ("GET", "/desk/api/graph", None),
            ("POST", "/desk/api/graph/save", {"graph": {}}),
            ("POST", "/desk/api/query", {"text": "上个月订单合计"}),
            ("POST", "/desk/api/account", {"system": "demo_shop", "username": "a", "password": "b"}),
            ("POST", "/desk/api/login/step", {"system": "demo_shop", "action": "start"}),
            ("POST", "/desk/api/voice/session", {}),
            ("DELETE", "/desk/api/voice/session?sid=", None),
        ]:
            status, body = self._request(path, method=method, payload=payload)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "unauthorized")

    def test_desk_session_reports_login_state(self):
        status, payload = self._request("/desk/api/session")
        self.assertEqual((status, payload["required"], payload["authed"]), (200, True, False))

    def test_desk_login_sets_session_cookie(self):
        status, payload, cookie = self._login(self.token)
        self.assertEqual(status, 200)
        self.assertNotIn("_headers", payload)
        self.assertTrue(cookie.startswith("qflow_session="))
        self.assertIn("HttpOnly", self.last_headers.get("Set-Cookie"))
        self.assertNotIn(self.token, cookie)

        status, payload = self._request("/desk/api/session", cookie=cookie)
        self.assertTrue(payload["authed"])
        status, payload = self._request("/desk/api/graph", cookie=cookie)
        self.assertEqual(status, 200)

    def test_session_cookie_only_opens_desk_api(self):
        _, _, cookie = self._login(self.token)
        status, _ = self._request("/capabilities", cookie=cookie)
        self.assertEqual(status, 401)

    def test_forged_or_stale_cookie_rejected(self):
        status, _ = self._request("/desk/api/graph", cookie="qflow_session=20200101.deadbeef")
        self.assertEqual(status, 401)
        stale = self.server.RequestHandlerClass.api._session_value("20200101")
        status, _ = self._request("/desk/api/graph", cookie=f"qflow_session={stale}")
        self.assertEqual(status, 401)

    def test_desk_login_locks_after_too_many_failures(self):
        for expected_left in (4, 3, 2, 1, 0):
            status, payload, cookie = self._login("wrong")
            self.assertEqual((status, payload["left"]), (401, expected_left))
            self.assertEqual(cookie, "")
        status, payload, _ = self._login(self.token)
        self.assertEqual(status, 429)

    def test_blank_or_punctuation_only_pass_rejected(self):
        for said in ("", "   ", "，。！"):
            status, _, cookie = self._login(said)
            self.assertEqual((status, cookie), (401, ""))

    def test_desk_pass_replaces_token_for_login(self):
        self.server.RequestHandlerClass.api.desk_pass = "芝麻开门"
        status, _, _ = self._login(self.token)
        self.assertEqual(status, 401)
        status, _, cookie = self._login(" 芝麻，开门！ ")  # 多余的空格和标点不算错
        self.assertEqual(status, 200)
        status, _ = self._request("/desk/api/graph", cookie=cookie)
        self.assertEqual(status, 200)

    def test_health_is_public_and_lists_systems(self):
        status, payload = self._request("/health")
        self.assertEqual((status, payload["systems"]), (200, [{"id": "demo_shop", "status": "no_account"}]))

    def test_capabilities_come_from_the_graph(self):
        self.assertEqual(self._request("/capabilities")[0], 401)
        status, payload = self._request("/capabilities", token=self.token)
        self.assertEqual([c["id"] for c in payload["systems"][0]["capabilities"]], ["cap.order.summary", "cap.order.detail"])

    def test_account_is_saved_but_never_sent_back(self):
        status, payload = self._request("/desk/api/account", method="POST", token=self.token,
                                        payload={"system": "demo_shop", "username": "demo", "password": "demo123"})
        self.assertEqual((status, payload["account"]), (200, {"saved": True, "username": "demo"}))
        status, payload = self._request("/desk/api/systems", token=self.token)
        self.assertEqual(payload["systems"][0]["status"], "needs_login")
        self.assertNotIn("demo123", json.dumps(payload))
        status, _ = self._request("/desk/api/account", method="POST", token=self.token,
                                  payload={"system": "nope", "username": "a", "password": "b"})
        self.assertEqual(status, 404)
        status, _ = self._request("/desk/api/account", method="POST", token=self.token,
                                  payload={"system": "demo_shop", "username": "a", "password": ""})
        self.assertEqual(status, 400)

    def test_login_step_is_served_by_the_generic_flow(self):
        with mock.patch.object(login, "handle", return_value={"state": "challenge", "image": "data:image/png;base64,AA"}) as handle:
            status, payload = self._request("/desk/api/login/step", method="POST", token=self.token,
                                            payload={"system": "demo_shop", "action": "start"})
        self.assertEqual((status, payload["state"]), (200, "challenge"))
        self.assertEqual(handle.call_args.args[1], "start")

    def test_page_voice_and_query_share_one_path(self):
        self._sign_in()
        status, first = self._request("/desk/api/query", method="POST", token=self.token,
                                      payload={"text": "2025年11月订单合计", "state": {}})
        self.assertEqual(status, 200)
        self.assertIn("共 3 单，金额 1,580.50 元", first["reply"])
        self.assertEqual(first["data"]["groups"][0], {"label": "已签收", "value": 1500.5, "n": 2})
        self.assertEqual(first["plan"]["metrics"][0], {"key": "total", "label": "订单金额", "unit": "元"})
        self.assertIn("POST /api/orders/list", first["plan"]["steps"][0])
        status, second = self._request("/desk/api/query", method="POST", token=self.token,
                                       payload={"text": "明细呢", "state": first["state_patch"]})
        self.assertEqual([r["order_no"] for r in second["data"]["rows"]], ["SO20251101", "SO20251102", "SO20251103"])
        self.assertIn({"name": "buyer", "label": "买家"}, second["plan"]["columns"])

        status, voice = self._request("/voice/handle", method="POST", token=self.token,
                                      payload={"heard": "2025年11月订单合计", "context": {"state": {}}})
        self.assertEqual((status, voice["handled"], voice["reply"]), (200, True, first["reply"]))
        status, followup = self._request("/voice/handle", method="POST", token=self.token,
                                         payload={"heard": "只看签收了的", "context": {"state": voice["state_patch"]}})
        self.assertIn("1,500.50", followup["reply"])

        status, plain = self._request("/query", method="POST", token=self.token, payload={"text": "2025年11月订单合计"})
        self.assertEqual(plain["reply"], first["reply"])
        status, direct = self._request("/query", method="POST", token=self.token, payload={
            "system": "demo_shop", "capability": "cap.order.summary", "values": {"state": "待发货"}})
        self.assertEqual((status, direct["result"]["totals"]), (200, {"total": 80.0, "count": 1}))

    def test_needs_login_is_said_plainly(self):
        status, payload = self._request("/desk/api/query", method="POST", token=self.token, payload={"text": "上个月订单合计"})
        self.assertEqual((status, payload["needs_login"], payload["system"]), (200, True, "demo_shop"))
        self.assertIn("还没有填", payload["reply"])
        status, payload = self._request("/query", method="POST", token=self.token,
                                        payload={"system": "demo_shop", "capability": "cap.order.summary"})
        self.assertEqual((status, payload["needs_login"]), (409, True))

    def test_unknown_question_and_bad_requests(self):
        status, payload = self._request("/desk/api/query", method="POST", token=self.token, payload={"text": "你好啊"})
        self.assertEqual((status, payload["handled"]), (200, False))
        self.assertTrue(payload["followups"])
        self.assertEqual(self._request("/query", method="POST", token=self.token, raw=b"{oops")[1]["error"], "invalid_json")
        self.assertEqual(self._request("/query", method="POST", token=self.token, payload={})[1]["error"], "text_required")
        status, payload = self._request("/query", method="POST", token=self.token,
                                        payload={"system": "demo_shop", "capability": "cap.nope"})
        self.assertEqual(status, 400)
        self.assertEqual(self._request("/nope", token=self.token)[0], 404)

    def test_desk_page(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/desk", timeout=5) as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        for needle in ("readflow", "/desk/api/query", "/desk/api/login/step", "/desk/api/account"):
            self.assertIn(needle, html)
        # 页面不写死业务系统，也不依赖外网资源
        for word in ("圣农", "扣款", "googleapis"):
            self.assertNotIn(word, html)

    def test_graph_view_and_import(self):
        status, payload = self._request("/desk/api/graph", token=self.token)
        types = {node["type"] for node in payload["view"]["nodes"]}
        self.assertEqual(types, {"app", "module", "page", "api", "entity", "capability"})
        ids = {node["id"] for node in payload["view"]["nodes"]}
        self.assertTrue(all(edge["from"] in ids and edge["to"] in ids for edge in payload["view"]["edges"]))
        self.assertEqual(payload["docs"]["demo_shop"]["system"]["label"], "小店订单")

        doc = json.loads(EXAMPLE_GRAPH.read_text(encoding="utf-8"))
        status, payload = self._request("/desk/api/spec/validate", method="POST", payload=doc, token=self.token)
        self.assertTrue(payload["report"]["ok"])
        status, payload = self._request("/desk/api/graph/save", method="POST", payload={"graph": doc}, token=self.token)
        self.assertEqual((status, payload["system"]), (200, "example_erp"))
        status, payload = self._request("/desk/api/systems", token=self.token)
        self.assertEqual({s["id"] for s in payload["systems"]}, {"demo_shop", "example_erp"})

        doc["operations"][0].pop("evidence")
        status, payload = self._request("/desk/api/graph/save", method="POST", payload={"graph": doc}, token=self.token)
        self.assertEqual(status, 400)
        self.assertFalse(payload["report"]["ok"])
        doc = json.loads(EXAMPLE_GRAPH.read_text(encoding="utf-8"))
        doc["system"]["id"] = "../escape"
        status, payload = self._request("/desk/api/graph/save", method="POST", payload={"graph": doc}, token=self.token)
        self.assertEqual(status, 400)

    def test_desk_voice_session_delete(self):
        status, payload = self._request("/desk/api/voice/session?sid=", method="DELETE", token=self.token)
        self.assertEqual((status, payload["ok"]), (200, True))
