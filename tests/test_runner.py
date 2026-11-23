"""通用执行器：按 v1 图谱取数、过滤、计算。"""

import json
from pathlib import Path

import pytest

from qflow import runner
from tests.data import EXAMPLE_GRAPH, SRM_GRAPH, srm_sender

NOV = {"period": {"start": "2025-11-01", "end": "2025-11-30"}}


def test_srm_graph_loads_and_is_valid():
    graph = runner.load(SRM_GRAPH)
    assert set(graph.capabilities) == {"cap.deduction.summary", "cap.deduction.detail"}


def test_summary_filters_groups_and_answers():
    graph = runner.load(SRM_GRAPH)
    got = runner.run(graph, "cap.deduction.summary", NOV, srm_sender())
    assert got["count"] == 3 and got["totals"]["total"] == 180.0
    assert {g["item"]: (g["count"], g["total"]) for g in got["groups"]} == {"水分": (2, 130.0), "杂质": (1, 50.0)}
    assert got["answer"] == "2025-11-01 至 2025-11-30 共 3 笔扣款，合计 180.00 元。"


def test_item_filter_and_detail():
    graph = runner.load(SRM_GRAPH)
    got = runner.run(graph, "cap.deduction.detail", {**NOV, "item": "水分"}, srm_sender())
    assert sorted((r["asn"], r["amount"]) for r in got["rows"]) == [("ASN2511010001", 100.0), ("ASN2511020004", 30.0)]
    assert got["trace"]["local_filters"] == ["biz_date", "item"]


def test_pagination_walks_every_page_and_sends_declared_request():
    graph = runner.load(SRM_GRAPH)
    calls = []
    got = runner.run(graph, "cap.deduction.summary", {}, srm_sender(page_size_seen=3, calls=calls))
    assert got["count"] == 4 and len(calls) == 2  # 4 条，每页 3 条
    assert calls[0]["method"] == "POST" and calls[0]["url"].endswith("/srm/qm/quajud/vendorQuery")
    assert calls[0]["params"] == {"page": 1, "rows": 200} and calls[0]["body_type"] == "form"


def test_no_rows_gives_plain_answer():
    graph = runner.load(SRM_GRAPH)
    got = runner.run(graph, "cap.deduction.summary", {"period": {"start": "2024-01-01", "end": "2024-01-31"}}, srm_sender())
    assert got["count"] == 0 and "没有查到" in got["answer"]


def test_expired_session_raises_auth_expired():
    graph = runner.load(SRM_GRAPH)
    with pytest.raises(runner.AuthExpired):
        runner.run(graph, "cap.deduction.summary", NOV, lambda *a: {"status": 401, "url": "", "text": ""})
    with pytest.raises(runner.AuthExpired):
        runner.run(graph, "cap.deduction.summary", NOV, lambda *a: {"status": 200, "url": "https://x/#/login", "text": "<html>"})


def test_another_system_runs_without_code_changes():
    """示例图谱是另一个（虚构的）系统：接口支持按时间查，条件要作为参数发出去。"""
    graph = runner.load(EXAMPLE_GRAPH)
    sample = graph.operations["op.deduction.list"]["evidence"]["sample"]["response"]
    calls = []

    def send(method, url, params, body_type, headers):
        calls.append(dict(params))
        return {"status": 200, "url": url, "text": json.dumps(sample, ensure_ascii=False)}

    got = runner.run(graph, "cap.deduction.summary", {"period": {"start": "2026-10-01", "end": "2026-10-31"}, "item": "水分"}, send)
    assert calls[0]["startDate"] == "2026-10-01" and calls[0]["endDate"] == "2026-10-31"
    assert got["count"] == 1 and got["totals"]["total"] == 120.5


def test_refuses_write_operations_and_invalid_graphs(tmp_path):
    doc = json.loads(Path(SRM_GRAPH).read_text(encoding="utf-8"))
    doc["operations"][0]["access"] = "write"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(runner.RunError):
        runner.load(bad)
    graph = runner.SystemGraph(doc)  # 绕过校验也不执行
    with pytest.raises(runner.RunError):
        runner.run(graph, "cap.deduction.summary", {}, lambda *a: pytest.fail("不应该发请求"))


def test_readonly_guard_blocks_write_looking_paths():
    doc = json.loads(Path(SRM_GRAPH).read_text(encoding="utf-8"))
    doc["operations"][0]["request"]["path"] = "/srm/qm/quajud/save"
    with pytest.raises(Exception) as info:
        runner.run(runner.SystemGraph(doc), "cap.deduction.summary", {}, lambda *a: pytest.fail("不应该发请求"))
    assert "写请求" in str(info.value)


def test_real_http_end_to_end_with_cookie_and_cli(tmp_path, capsys):
    """起一个真的本地 HTTP 服务当 SRM：验证真实发出去的请求、cookie、翻页和命令行入口。"""
    import threading
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from qflow import cli

    fake = srm_sender(page_size_seen=3)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            seen.append({"path": self.path, "form": form, "cookie": self.headers.get("Cookie"),
                         "type": self.headers.get("Content-Type")})
            if "sid=abc" not in (self.headers.get("Cookie") or ""):
                self.send_response(401)
                self.end_headers()
                return
            body = fake("POST", self.path, form, "form", {})["text"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        doc = json.loads(Path(SRM_GRAPH).read_text(encoding="utf-8"))
        doc["system"]["base_url"] = f"http://127.0.0.1:{server.server_port}"
        graph_file = tmp_path / "graph.json"
        graph_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        cookies = tmp_path / "session.json"
        cookies.write_text(json.dumps({"cookies": [{"name": "sid", "value": "abc", "domain": "127.0.0.1", "path": "/"}]}))
        params = json.dumps(NOV)

        assert cli.main(["run", str(graph_file), "cap.deduction.summary", "--params", params, "--cookies", str(cookies)]) == 0
        assert "共 3 笔扣款，合计 180.00 元" in capsys.readouterr().out
        assert [s["form"]["page"] for s in seen] == ["1", "2"]
        assert seen[0]["path"] == "/srm/qm/quajud/vendorQuery"
        assert seen[0]["type"] == "application/x-www-form-urlencoded"

        assert cli.main(["run", str(graph_file), "cap.deduction.summary", "--params", params]) == 3  # 没带登录态
        assert "登录态失效" in capsys.readouterr().err
    finally:
        server.shutdown()


def test_hitting_page_limit_is_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(runner, "MAX_PAGES", 1)
    graph = runner.load(SRM_GRAPH)
    got = runner.run(graph, "cap.deduction.summary", {}, srm_sender(page_size_seen=3))
    assert got["trace"]["complete"] is False and "不完整" in got["answer"]
    full = runner.run(graph, "cap.deduction.summary", {}, srm_sender())
    assert full["trace"]["complete"] is True and "不完整" not in full["answer"]
