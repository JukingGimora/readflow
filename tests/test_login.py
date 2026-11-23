"""通用登录：照图谱 auth 一节走。假系统是真的 HTTP 服务；浏览器先用假的测流程，装了 selenium 再用真的测一遍。"""

import json
import shutil
import urllib.request
from unittest import mock

import pytest

from qflow import agent as agents
from qflow import browser as browsers
from qflow import config, login
from tests import fakesite


class FakeBrowser:
    """照着假系统登录页的行为写的最小浏览器：只认图谱里出现的那几个定位。"""

    def __init__(self, site):
        self.site, self.path, self.form, self.sid, self.closed, self.focus = site, None, {}, None, False, "#code"

    def _post_login(self):
        body = json.dumps({"user": self.form.get("#user"), "pass": self.form.get("#pass"), "code": self.form.get("#code")}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.site.base_url + "/api/login", data=body)) as response:
            if json.loads(response.read())["ok"]:
                self.sid = response.headers["Set-Cookie"].split(";")[0].split("=")[1]
                self.path = "/home"
                return
        urllib.request.urlopen(self.site.base_url + "/captcha.png").read()  # 页面会自己换一张

    def goto(self, url):
        self.path = "/home" if self.sid in self.site.sessions else "/login"
        if self.path == "/login":
            urllib.request.urlopen(self.site.base_url + "/captcha.png").read()

    def url(self):
        return self.site.base_url + self.path

    def exists(self, locator):
        return locator == "#home" if self.path == "/home" else locator in ("#user", "#pass", "#captcha", "#code", "#go")

    def fill(self, locator, text):
        self.form[locator] = text

    def click(self, locator):
        if locator == "#captcha":
            urllib.request.urlopen(self.site.base_url + "/captcha.png").read()
        elif locator == "#go":
            self._post_login()

    def element_png(self, locator):
        return fakesite.png(self.site.code)

    def screenshot(self):
        return b"jpeg"

    def size(self):
        return 1280, 800

    def mouse(self, kind, x, y):
        pass

    def text(self, text):
        self.form[self.focus] = text

    def key(self, name):
        if name == "Enter":
            self._post_login()

    def cookies(self):
        return [{"name": "sid", "value": self.sid, "domain": "127.0.0.1", "path": "/"}] if self.sid else []

    def close(self):
        self.closed = True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    site = fakesite.Site()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SYSTEMS_DIR", tmp_path / "none")
    target = tmp_path / "data" / "systems" / "demo_shop" / "graph.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(site.graph(), ensure_ascii=False), encoding="utf-8")
    opened = []
    monkeypatch.setattr(login, "open_browser", lambda agent: opened.append(FakeBrowser(site)) or opened[-1])
    monkeypatch.setattr(login, "WAIT_PAGE", 0.2)
    monkeypatch.setattr(login, "WAIT_RESULT", 0.2)
    monkeypatch.setattr(login, "PAUSE", 0.01)
    login._flows.clear()
    shop = agents.get("demo_shop")
    shop.save_account(fakesite.USER, fakesite.PASSWORD)
    yield site, shop, opened
    site.close()


def reads(site, right=True):
    return mock.patch.object(browsers, "read_captcha", side_effect=lambda png, length=None: site.code if right else None)


def test_fills_account_and_reads_captcha_without_a_human(env):
    site, shop, opened = env
    with reads(site):
        out = login.handle(shop, "start")
    assert out["state"] == "done" and opened[0].closed
    assert shop.status()["status"] == "online" and shop.run("cap.order.summary", {})["count"] == 5


def test_captcha_goes_to_the_human_when_it_cannot_be_read(env):
    site, shop, opened = env
    with reads(site, right=False):
        out = login.handle(shop, "start")
    assert out["state"] == "challenge" and out["image"].startswith("data:image/png;base64,")
    json.dumps(out)  # 能原样发给页面
    assert fakesite.PASSWORD not in json.dumps(out)
    wrong = login.handle(shop, "answer", {"text": "xxxx"})
    assert wrong["state"] == "challenge" and "验证码" in wrong["error"]
    assert login.handle(shop, "refresh")["state"] == "challenge"
    out = login.handle(shop, "answer", {"text": site.code})
    assert out["state"] == "done" and opened[0].closed and shop.check()


def test_window_lets_a_human_drive_the_server_browser(env):
    site, shop, opened = env
    out = login.handle(shop, "start", {"manual": True})
    assert out["state"] == "window" and out["image"].startswith("data:image/jpeg;base64,") and out["width"] == 1280
    assert login.handle(shop, "screen")["state"] == "window"
    out = login.handle(shop, "input", {"events": [
        {"type": "account"}, {"type": "down", "x": 10, "y": 10}, {"type": "up", "x": 10, "y": 10},
        {"type": "text", "text": site.code}, {"type": "key", "key": "Enter"}]})
    assert out["state"] == "done" and opened[0].closed and shop.check()


def test_graph_without_steps_still_gets_a_window(env):
    site, shop, opened = env
    shop.graph.doc["auth"].pop("steps")
    login._flows.clear()
    assert login.handle(shop, "start")["state"] == "window"
    assert login.unattended(shop) is False
    assert login.handle(shop, "cancel")["state"] == "idle" and opened[0].closed


def test_missing_account_and_wrong_password_are_explained(env):
    site, shop, opened = env
    (shop.store / "account.json").unlink()
    out = login.handle(shop, "start")
    assert out["state"] == "error" and "账号" in out["error"] and opened[0].closed
    shop.save_account(fakesite.USER, "wrong")
    with reads(site):
        out = login.handle(shop, "start")
    assert out["state"] == "challenge"  # 自动试了几次都没进去，交给人看
    assert login.handle(shop, "answer", {"text": site.code})["state"] == "challenge"
    assert shop.status()["status"] == "needs_login"


def test_unattended_login_only_when_no_human_is_needed(env):
    site, shop, opened = env
    with reads(site, right=False):
        assert login.unattended(shop) is False
    assert opened[-1].closed and login.flow(shop).state == "idle"
    with reads(site):
        assert login.unattended(shop) is True
    site.sessions.clear()
    shop._cache.clear()
    with reads(site):
        assert shop.run("cap.order.summary", {})["count"] == 5  # 查询途中发现失效：自己登录后重试
    with pytest.raises(ValueError):
        login.handle(shop, "bogus")


@pytest.mark.skipif(not shutil.which("google-chrome") and not config.CHROME_BINARY, reason="没有 Chrome")
def test_real_browser_end_to_end(env, monkeypatch):
    """真的开 Chrome 登录假系统：自动填表、验证码交给人、窗口里用鼠标键盘操作。"""
    pytest.importorskip("selenium")
    site, shop, _ = env
    monkeypatch.setattr(login, "open_browser", None)
    monkeypatch.setattr(login, "WAIT_PAGE", 15)
    monkeypatch.setattr(login, "WAIT_RESULT", 6)
    monkeypatch.setattr(login, "PAUSE", 0.5)
    with reads(site, right=False):
        out = login.handle(shop, "start")
    assert out["state"] == "challenge", out
    out = login.handle(shop, "answer", {"text": site.code})
    assert out["state"] == "done", out
    assert shop.run("cap.order.summary", {})["count"] == 5

    shop.clear_session()
    site.sessions.clear()
    shutil.rmtree(shop.store / "browser-profile")
    out = login.handle(shop, "start", {"manual": True})
    assert out["state"] == "window" and len(out["image"]) > 2000, out
    flow = login.flow(shop)
    box = flow.browser.driver.execute_script("const r = document.querySelector('#code').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]")
    out = login.handle(shop, "input", {"events": [
        {"type": "account"}, {"type": "down", "x": box[0], "y": box[1]}, {"type": "up", "x": box[0], "y": box[1]},
        {"type": "text", "text": site.code}]})
    assert out["state"] == "window"
    go = flow.browser.driver.execute_script("const r = document.querySelector('#go').getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]")
    out = login.handle(shop, "input", {"events": [{"type": "down", "x": go[0], "y": go[1]}, {"type": "up", "x": go[0], "y": go[1]}]})
    for _ in range(10):
        if out["state"] != "window":
            break
        out = login.handle(shop, "screen")
        browsers.wait(lambda: False, 0.5)
    assert out["state"] == "done", {k: v for k, v in out.items() if k != "image"}
    assert shop.check()
