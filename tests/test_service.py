import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from qflow import service

from .helpers import make_seeded_db


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        make_seeded_db(self.db_path)
        self.token = "secret"
        self.server = service.make_server("127.0.0.1", 0, self.db_path, token=self.token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

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
            ("POST", "/desk/api/graph/save", {"graph": {"nodes": []}}),
            ("POST", "/desk/api/query", {"text": "上个月扣款合计"}),
            ("GET", "/desk/api/auth/challenge", None),
            ("POST", "/desk/api/auth/submit", {"code": "1234"}),
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

    def test_desk_pass_replaces_token_for_login(self):
        self.server.RequestHandlerClass.api.desk_pass = "芝麻开门"
        status, _, _ = self._login(self.token)
        self.assertEqual(status, 401)
        status, _, cookie = self._login("芝麻开门")
        self.assertEqual(status, 200)
        status, _ = self._request("/desk/api/graph", cookie=cookie)
        self.assertEqual(status, 200)

    def test_health_is_public(self):
        status, payload = self._request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"]["shipments"], 3)

    def test_capabilities_requires_token(self):
        status, _ = self._request("/capabilities")
        self.assertEqual(status, 401)

    def test_capabilities_with_token(self):
        status, payload = self._request("/capabilities", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["read_only"])
        self.assertIn("deduction.summary", payload["intents"])
        self.assertTrue(all(cap["read_only"] for cap in payload["capabilities"]))
        self.assertEqual(payload["graph"]["capabilities"], 6)

    def test_tools_schema(self):
        status, payload = self._request("/tools", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["tools"][0]["function"]["name"], "query_business_data")

    def test_structured_query(self):
        status, payload = self._request(
            "/query",
            method="POST",
            token=self.token,
            payload={"intent": "deduction.summary", "params": {"month": "2025-11"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["total_amount"], 180.0)
        self.assertIn("180.00", payload["answer"])
        self.assertEqual(payload["intent"], "deduction.summary")
        self.assertEqual(payload["plan"]["provider"]["kind"], "cache")
        self.assertEqual(payload["plan"]["entity"]["id"], "entity.deduction")

    def test_text_query(self):
        status, payload = self._request(
            "/query",
            method="POST",
            token=self.token,
            payload={"text": "2025年11月扣款合计"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["count"], 3)
        self.assertEqual(payload["data"]["total_amount"], 180.0)

    def test_query_requires_auth(self):
        status, _ = self._request(
            "/query", method="POST", payload={"text": "2025年11月扣款合计"}
        )
        self.assertEqual(status, 401)

    def test_voice_handle(self):
        status, payload = self._request(
            "/voice/handle",
            method="POST",
            token=self.token,
            payload={"heard": "2025年11月扣款合计", "context": {"history": []}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["handled"])
        self.assertIn("180.00", payload["reply"])
        self.assertEqual(payload["state_patch"]["snxt"]["intent"], "deduction.summary")

    def test_desk_page(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/desk", timeout=5
        ) as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        self.assertIn("readflow", html)
        self.assertIn("/desk/api/query", html)
        self.assertIn("/desk/api/login", html)
        # 页面不写死业务系统，也不依赖外网资源
        self.assertNotIn("圣农", html)
        self.assertNotIn("googleapis", html)

    def test_desk_graph_lists_executable_intents(self):
        status, payload = self._request("/desk/api/graph", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("deduction.summary", payload["executable_intents"])
        self.assertTrue(payload["graph"]["nodes"])

    def test_desk_graph_save_rejects_empty_graph(self):
        status, payload = self._request(
            "/desk/api/graph/save", method="POST", payload={"graph": {"nodes": []}}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_desk_voice_session_delete(self):
        status, payload = self._request("/desk/api/voice/session?sid=", method="DELETE", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_desk_followup(self):
        status, first = self._request(
            "/desk/api/query",
            method="POST",
            token=self.token,
            payload={"text": "2025年11月扣款合计", "state": {}},
        )
        self.assertEqual(status, 200)
        self.assertIn("180.00", first["reply"])
        status, second = self._request(
            "/desk/api/query",
            method="POST",
            token=self.token,
            payload={"text": "那水分呢", "state": first["state_patch"]},
        )
        self.assertEqual(status, 200)
        self.assertIn("130.00", second["reply"])

    def test_invalid_json(self):
        status, payload = self._request(
            "/query", method="POST", token=self.token, raw=b"{oops"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_json")

    def test_unknown_intent(self):
        status, payload = self._request(
            "/query", method="POST", token=self.token, payload={"intent": "delete.everything"}
        )
        self.assertEqual(status, 400)
        self.assertIn("未知 intent", payload["error"])

    def test_missing_text_and_intent(self):
        status, payload = self._request("/query", method="POST", token=self.token, payload={})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "text_or_intent_required")

    def test_not_found(self):
        status, payload = self._request("/nope", token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
