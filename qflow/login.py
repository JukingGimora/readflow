"""通用登录：照图谱 auth 一节做，不认识任何具体系统。两级：

1. 自动：打开 login_url，按 steps 填页面上保存的账号密码；图片验证码先自动识别，认不出就把图发给页面让人填。
2. 窗口（兜底）：滑块、扫码、短信、第三方登录，或者自动那一级走不通时，把服务器上这个浏览器的画面
   显示在页面里，人直接点、输入、拖动。图谱里一步都没写也能用，只要有 login_url。

怎么算登录成功：auth.success（url_not_contains / absent），没写就用「不在登录页了」推断；
然后拿取到的 cookie 真调一次接口（agent.check），接口认了才算。成功后存登录态、关浏览器。

页面只调一个入口：handle(agent, action, payload)，action 是 start / answer / refresh / screen / input / cancel，
返回 {"state": idle | challenge | window | done | error, ...}。

TODO(T6)：短信验证码的「点发送」还没有单独的步骤，现在走窗口由人点。
TODO：auth.session.carrier 只支持 cookie；header / local_storage 里的令牌还取不走。
"""

from __future__ import annotations

import base64
import logging
import threading

from . import browser as browsers

log = logging.getLogger(__name__)

AUTO_TRIES = 3  # 自动识别验证码最多试几次
WAIT_PAGE, WAIT_RESULT, PAUSE = 15.0, 8.0, 0.8  # 等登录页出来、等登录结果、换验证码后停一下（秒）
_flows: dict[str, "Flow"] = {}
_flows_lock = threading.Lock()
open_browser = None  # 测试里换成假浏览器


def _open(agent):
    if open_browser is not None:
        return open_browser(agent)
    return browsers.Browser(agent.store / "browser-profile")


class Flow:
    def __init__(self, agent):
        self.agent = agent
        self.auth = agent.graph.doc.get("auth") or {}
        self.steps = self.auth.get("steps") or []
        self.browser = None
        self.state = "idle"
        self.error: str | None = None
        self.challenge: dict | None = None
        self.lock = threading.RLock()

    # ---- 图谱里声明的东西 ----
    def _url(self) -> str:
        login_url = str(self.auth.get("login_url") or "/")
        return login_url if login_url.startswith("http") else self.agent.graph.system["base_url"].rstrip("/") + login_url

    def _inputs(self) -> list[dict]:
        return [s for s in self.steps if s.get("kind") == "input"]

    def _submit(self) -> dict | None:
        return next((s for s in self.steps if s.get("kind") == "submit"), None)

    def _logged_in(self) -> bool:
        rule = self.auth.get("success") or {}
        if not rule:  # 没写就推断：地址里没有登录页的标志，登录输入框也不在了
            marks = [r["redirect_contains"] for r in (self.auth.get("session") or {}).get("expired_when") or [] if r.get("redirect_contains")]
            rule = {"url_not_contains": marks[0] if marks else None,
                    "absent": self._inputs()[0]["locator"] if self._inputs() else None}
            if not any(rule.values()):
                return False  # 什么都推断不了，只能靠接口检查（见 _try_finish）
        if rule.get("url_not_contains") and rule["url_not_contains"] in self.browser.url():
            return False
        if rule.get("url_contains") and rule["url_contains"] not in self.browser.url():
            return False
        if rule.get("absent") and self.browser.exists(rule["absent"]):
            return False
        if rule.get("present") and not self.browser.exists(rule["present"]):
            return False
        return True

    def _settled_in(self, seconds: float) -> bool:
        # 连续两次都成立才算，避免页面跳转途中的一瞬间
        return browsers.wait(lambda: self._logged_in() and self._logged_in(), seconds)

    # ---- 状态 ----
    def view(self) -> dict:
        out = {"state": self.state, "system": self.agent.id, "label": self.agent.label, "error": self.error}
        if self.state == "challenge" and self.challenge:
            out.update(self.challenge)
        return out

    def _fail(self, message: str) -> dict:
        self._close()
        self.state, self.error = "error", message
        return self.view()

    def _close(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def _try_finish(self) -> bool:
        """浏览器里看着像登录了：取走 cookie，真调一次接口确认。"""
        self.agent.save_session(self.browser.cookies())
        if not self.agent.check():
            return False
        self._close()
        self.state, self.error, self.challenge = "done", None, None
        log.info("%s 登录成功", self.agent.id)
        return True

    def _to_window(self, why: str | None = None) -> dict:
        self.state, self.error, self.challenge = "window", why, None
        return self.screen()

    # ---- 自动那一级 ----
    def _fill_account(self) -> bool:
        account = self.agent.account() or {}
        for step in self._inputs():
            value = account.get(step.get("field"))
            if not value or not self.browser.exists(step["locator"]):
                return False
            self.browser.fill(step["locator"], value)
        return True

    def _press_submit(self) -> bool:
        submit = self._submit()
        if submit:
            self.browser.click(submit["locator"])
        return self._settled_in(WAIT_RESULT) and self._try_finish()

    def _captcha(self, step: dict) -> dict:
        png = self.browser.element_png(step["locator"])
        return {"kind": "image_captcha", "image": "data:image/png;base64," + base64.b64encode(png).decode(),
                "hint": step.get("hint") or "请输入图片里的字符"}

    def start(self, manual: bool = False) -> dict:
        with self.lock:
            self._close()
            self.error = self.challenge = None
            if not self.auth.get("login_url"):
                return self._fail("图谱里没有写登录地址（auth.login_url）")
            try:
                self.browser = _open(self.agent)
                self.browser.goto(self._url())
                first = self._inputs()[0]["locator"] if self._inputs() else None
                browsers.wait(lambda: (first and self.browser.exists(first)) or self._logged_in(), WAIT_PAGE)
                if not (first and self.browser.exists(first)) and self._settled_in(PAUSE * 2) and self._try_finish():
                    return self.view()  # 浏览器里的登录还有效
                if manual or not self._inputs():
                    return self._to_window()
                if not self.agent.account():
                    return self._fail("还没有填这个系统的账号密码")
                if not self._fill_account():
                    return self._to_window("没找到登录输入框，请在下面的窗口里手动登录")
                challenges = [s for s in self.steps if s.get("kind") == "challenge"]
                if not challenges:
                    return self.view() if self._press_submit() else self._to_window("自动登录没成功，请在下面的窗口里看一下")
                step = challenges[0]
                if len(challenges) > 1 or step.get("type") != "image_captcha":
                    return self._to_window()  # 滑块、扫码、短信等：人来
                solvers = step.get("solver") or ["human"]
                if "ocr" in solvers:
                    for _ in range(AUTO_TRIES):
                        guess = browsers.read_captcha(self.browser.element_png(step["locator"]), step.get("length"))
                        if guess is None:
                            break
                        self.browser.fill(step["answer_locator"], guess)
                        if self._press_submit():
                            return self.view()
                        if not self.browser.exists(step["locator"]) or not self._fill_account():
                            return self._to_window("自动登录没成功，请在下面的窗口里看一下")
                        self.browser.click(step["locator"])  # 换一张
                        browsers.wait(lambda: False, PAUSE)
                if "human" not in solvers:
                    return self._to_window()
                self.state, self.challenge = "challenge", {**self._captcha(step), "step": step}
                return self.view()
            except Exception as exc:  # noqa: BLE001 浏览器的各种异常都报给页面
                log.exception("登录出错")
                return self._fail(f"登录出错：{exc}")

    def answer(self, text: str) -> dict:
        with self.lock:
            if self.state != "challenge" or self.browser is None:
                return self.view()
            step = self.challenge["step"]
            try:
                if not self._fill_account():
                    return self._to_window("登录页变了，请在下面的窗口里手动登录")
                self.browser.fill(step["answer_locator"], str(text or "").strip())
                if self._press_submit():
                    return self.view()
                if not self.browser.exists(step["locator"]):
                    return self._to_window("没登录上，请在下面的窗口里看一下")
                self.challenge = {**self._captcha(step), "step": step}
                self.error = "没登录上，可能是验证码不对，请再试一次"
                return self.view()
            except Exception as exc:  # noqa: BLE001
                log.exception("提交验证码出错")
                return self._fail(f"登录出错：{exc}")

    def refresh(self) -> dict:
        with self.lock:
            if self.state == "challenge" and self.browser is not None:
                step = self.challenge["step"]
                self.browser.click(step["locator"])
                browsers.wait(lambda: False, PAUSE)
                self.challenge, self.error = {**self._captcha(step), "step": step}, None
            return self.view()

    # ---- 窗口那一级 ----
    def screen(self) -> dict:
        with self.lock:
            if self.state != "window" or self.browser is None:
                return self.view()
            try:
                if self._logged_in() and self._logged_in() and self._try_finish():
                    return self.view()
                width, height = self.browser.size()
                image = "data:image/jpeg;base64," + base64.b64encode(self.browser.screenshot()).decode()
                return {**self.view(), "image": image, "width": width, "height": height}
            except Exception as exc:  # noqa: BLE001
                log.exception("取画面出错")
                return self._fail(f"浏览器窗口出错：{exc}")

    def input(self, events: list) -> dict:
        """页面上的操作原样转给服务器上的浏览器。坐标是画面里的像素。"""
        with self.lock:
            if self.state != "window" or self.browser is None:
                return self.view()
            try:
                for event in (events or [])[:200]:
                    kind = event.get("type")
                    if kind in ("down", "move", "up"):
                        self.browser.mouse(kind, float(event["x"]), float(event["y"]))
                    elif kind == "text":
                        self.browser.text(str(event.get("text") or "")[:500])
                    elif kind == "key" and event.get("key") in browsers.KEYS:
                        self.browser.key(event["key"])
                    elif kind == "account":  # 让服务器把保存的账号密码填进去，密码不经过页面
                        self._fill_account()
            except Exception as exc:  # noqa: BLE001
                log.exception("转发操作出错")
                return self._fail(f"浏览器窗口出错：{exc}")
            browsers.wait(lambda: False, PAUSE / 2)
            return self.screen()

    def cancel(self) -> dict:
        with self.lock:
            self._close()
            self.state, self.error, self.challenge = "idle", None, None
            return self.view()


def flow(agent) -> Flow:
    with _flows_lock:
        current = _flows.get(agent.id)
        if current is None or current.agent is not agent:
            if current is not None:
                current.cancel()
            current = _flows[agent.id] = Flow(agent)
        return current


def handle(agent, action: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    current = flow(agent)
    if action == "start":
        out = current.start(manual=bool(payload.get("manual")))
    elif action == "answer":
        out = current.answer(str(payload.get("text") or ""))
    elif action == "refresh":
        out = current.refresh()
    elif action == "screen":
        out = current.screen()
    elif action == "input":
        out = current.input(payload.get("events") if isinstance(payload.get("events"), list) else [])
    elif action == "cancel":
        out = current.cancel()
    else:
        raise ValueError(f"不认识的登录动作：{action}")
    return {k: v for k, v in out.items() if k != "step"}


def unattended(agent) -> bool:
    """查询途中发现登录态失效时调用：不需要人就能登录的系统直接登录，需要人的返回 False。"""
    auth = agent.graph.doc.get("auth") or {}
    challenges = [s for s in auth.get("steps") or [] if s.get("kind") == "challenge"]
    needs_human = any(s.get("type") != "image_captcha" or "ocr" not in (s.get("solver") or []) for s in challenges)
    if needs_human or not agent.account() or not auth.get("steps"):
        return False
    current = flow(agent)
    if current.state in ("challenge", "window"):
        return False  # 人正在登录，别打断
    state = current.start()["state"]
    if state != "done":
        current.cancel()  # 没人在看，浏览器不留着
    return state == "done"
