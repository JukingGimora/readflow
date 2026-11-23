"""原子 agent：图谱 + 账号 + 登录态。用假系统「小店订单」，真的走 HTTP。"""

import json
import stat
from datetime import datetime
from unittest import mock

import pytest

from qflow import agent as agents
from qflow import config, login, runner, understand
from tests import fakesite

NOW = datetime(2025, 12, 20, 10, 0)


@pytest.fixture()
def site():
    server = fakesite.Site()
    yield server
    server.close()


@pytest.fixture()
def shop(site, tmp_path, monkeypatch):
    """假系统的图谱放进「页面导入」目录，注册表里就只有它。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SYSTEMS_DIR", tmp_path / "none")
    target = tmp_path / "data" / "systems" / "demo_shop" / "graph.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(site.graph(), ensure_ascii=False), encoding="utf-8")
    with mock.patch.object(understand, "ask_model", return_value=None), mock.patch.object(login, "unattended", return_value=False):
        yield agents.get("demo_shop")


def sign_in(site, agent):
    site.sessions.add("s1")
    agent.save_session([{"name": "sid", "value": "s1", "domain": "127.0.0.1", "path": "/"}])


def test_account_is_private_and_never_echoed(shop):
    assert shop.status()["status"] == "no_account"
    shop.save_account(" demo ", "demo123")
    assert shop.account() == {"username": "demo", "password": "demo123"}
    mode = stat.S_IMODE((shop.store / "account.json").stat().st_mode)
    assert mode == 0o600
    public = json.dumps(shop.status(), ensure_ascii=False)
    assert "demo123" not in public and shop.status()["status"] == "needs_login"
    with pytest.raises(ValueError):
        shop.save_account("demo", "")


def test_run_pages_through_real_http_with_the_saved_session(site, shop):
    sign_in(site, shop)
    out = shop.run("cap.order.summary", {"period": {"start": "2025-11-01", "end": "2025-11-30"}})
    assert out["answer"] == "2025-11-01 至 2025-11-30 共 3 单，金额 1,580.50 元。"
    assert [s["form"]["page"] for s in site.seen] == ["1", "2", "3"] and all(s["authed"] for s in site.seen)
    assert shop.status()["status"] == "online"
    shop.run("cap.order.detail", {})  # 同一个接口、同样的参数：两分钟内不再打扰业务系统
    assert len(site.seen) == 3


def test_ask_uses_this_systems_own_words(site, shop):
    sign_in(site, shop)
    first = agents.ask("上个月卖了多少钱", now=NOW)
    assert first["capability"] == "cap.order.summary" and first["system"] == "demo_shop"
    assert first["result"]["totals"] == {"total": 1580.5, "count": 3}
    narrowed = agents.ask("只看签收了的", first["state"], now=NOW)
    assert narrowed["state"]["values"]["state"] == "已签收" and narrowed["result"]["totals"]["total"] == 1500.5
    detail = agents.ask("明细呢", narrowed["state"], now=NOW)
    assert [r["order_no"] for r in detail["result"]["rows"]] == ["SO20251101", "SO20251102"]
    one = agents.ask("SO20251201 这单怎么样", now=NOW)
    assert one["capability"] == "cap.order.detail" and one["result"]["rows"][0]["buyer"] == "王五"


def test_expired_session_is_reported_not_guessed(site, shop):
    out = agents.ask("去年11月订单合计", now=NOW)
    assert out["needs_login"] and "还没有填" in out["reply"]
    shop.save_account("demo", "demo123")
    sign_in(site, shop)
    assert agents.ask("上个月订单合计", now=NOW)["result"]["count"] == 3
    site.sessions.clear()  # 业务系统那边把登录态作废了
    shop._cache.clear()
    out = agents.ask("这个月订单合计", now=NOW)
    assert out["needs_login"] and "登录过期" in out["reply"]
    assert shop.status()["status"] == "needs_login"
    with pytest.raises(runner.AuthExpired):
        shop.run("cap.order.summary", {})


def test_unattended_relogin_retries_once(site, shop):
    def relogin(agent):
        sign_in(site, agent)
        return True

    with mock.patch.object(login, "unattended", side_effect=relogin) as unattended:
        assert shop.run("cap.order.summary", {})["count"] == 5
    assert unattended.call_count == 1


def test_check_calls_the_declared_operation(site, shop):
    assert shop.check() is False
    sign_in(site, shop)
    assert shop.check() is True and site.seen[-1]["form"]["page"] == "1"


def test_registry_reports_bad_graphs_and_routes_between_systems(site, shop, tmp_path):
    bad = tmp_path / "data" / "systems" / "broken" / "graph.json"
    bad.parent.mkdir()
    bad.write_text("{}", encoding="utf-8")
    other = site.graph()
    other["system"] = {"id": "second_shop", "label": "第二家店", "base_url": site.base_url}
    for capability in other["capabilities"]:
        capability["keywords"] = ["工单"]
    second = tmp_path / "data" / "systems" / "second_shop" / "graph.json"
    second.parent.mkdir()
    second.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
    found = agents.all()
    assert set(found) == {"demo_shop", "second_shop"} and str(bad) in agents.problems
    for agent in found.values():
        sign_in(site, agent)
    assert agents.ask("去年11月工单合计", now=NOW)["system"] == "second_shop"
    first = agents.ask("去年11月订单合计", now=NOW)
    assert first["system"] == "demo_shop"
    assert agents.ask("明细呢", first["state"], now=NOW)["system"] == "demo_shop"
