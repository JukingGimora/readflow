"""SRM 登录会话：浏览器只在登录时存在，验证码可下发到页面给人人工填写。

流程：
    GET  captcha()      -> 取登录页验证码图片（base64）
    POST submit(code)   -> 填账号密码+验证码提交
    成功 -> cookie 存 session.json，浏览器退出；查询路径之后纯 HTTP

自动 OCR 登录作为无人值守的第一选择，失败才轮到人工。
"""
from __future__ import annotations

import base64
import logging
import threading
import time

from . import config, srm
from .srmapi import save_session

logger = logging.getLogger("qflow.srmlogin")

LOGIN_PROFILE = config.DEFAULT_DATA_DIR / "live" / "login-profile"


class LoginSession:
    def __init__(self):
        self._lock = threading.RLock()
        self._driver = None
        self.last_error: str | None = None

    # ---------- 浏览器生命周期 ----------

    def _ensure(self):
        if self._driver is None:
            self._driver = srm.new_driver(headless=True, profile=LOGIN_PROFILE)
        return self._driver

    def close(self):
        with self._lock:
            driver, self._driver = self._driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def _open_login_page(self):
        driver = self._ensure()
        driver.get(srm.URL)
        driver.set_page_load_timeout(30)
        time.sleep(2.5)
        # 已登录（比如 cookie 还活着）直接收割
        if not srm.is_login_page(driver):
            self._harvest(driver)
            return "already"
        # 等登录表单出现
        for _ in range(20):
            try:
                ok = driver.execute_script(
                    "return document.querySelectorAll('input[placeholder*=\"账号\"]').length > 0"
                )
            except Exception:
                ok = False
            if ok:
                return "login"
            time.sleep(0.5)
        raise RuntimeError("登录页未加载出表单")

    # ---------- 人工验证码接力 ----------

    def captcha(self) -> dict:
        """返回登录页验证码图片 base64；已登录则直接收割 cookie。"""
        with self._lock:
            try:
                state = self._open_login_page()
                if state == "already":
                    self.close()
                    return {"ok": True, "already_logged_in": True}
                images = srm.captcha_images(self._driver)
                if not images:
                    raise RuntimeError("没有找到验证码图片")
                png = images[-1].screenshot_as_png
                png = png() if callable(png) else png
                return {
                    "ok": True,
                    "image": "data:image/png;base64," + base64.b64encode(png).decode(),
                }
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.exception("captcha failed")
                return {"ok": False, "error": str(exc)}

    def refresh_captcha(self) -> dict:
        with self._lock:
            try:
                driver = self._ensure()
                images = srm.captcha_images(driver)
                if images:
                    driver.execute_script("arguments[0].click()", images[-1])
                    time.sleep(1.0)
                return self.captcha()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                return {"ok": False, "error": str(exc)}

    def submit(self, code: str) -> dict:
        """用服务端凭据 + 人工验证码提交登录。"""
        code = "".join(ch for ch in (code or "") if ch.isalnum())
        if len(code) != 4:
            return {"ok": False, "error": "验证码需要 4 位"}
        with self._lock:
            try:
                from selenium.webdriver.common.by import By

                driver = self._ensure()
                account = srm.creds()

                def _fields():
                    found = {}
                    for key in ("账号/邮箱", "密码", "验证码"):
                        found[key] = next(
                            (
                                e for e in driver.find_elements(By.CSS_SELECTOR, "input")
                                if key in (e.get_attribute("placeholder") or "")
                            ),
                            None,
                        )
                    return found

                fields = _fields()
                if not all(fields.values()):
                    self._open_login_page()
                    driver = self._ensure()
                    fields = _fields()
                if not all(fields.values()):
                    raise RuntimeError("登录表单字段不完整")

                fields["账号/邮箱"].clear()
                fields["账号/邮箱"].send_keys(account["username"])
                fields["密码"].clear()
                fields["密码"].send_keys(account["password"])
                fields["验证码"].clear()
                fields["验证码"].send_keys(code)
                time.sleep(0.4)
                buttons = driver.find_elements(By.CSS_SELECTOR, "button.login-btn") or driver.find_elements(By.TAG_NAME, "button")
                driver.execute_script("arguments[0].click()", buttons[0])
                # 等登录结果
                for _ in range(16):
                    time.sleep(0.5)
                    if not srm.is_login_page(driver):
                        self._harvest(driver)
                        self.close()
                        return {"ok": True}
                msgs = srm.messages(driver)
                return {"ok": False, "error": "；".join(msgs) or "验证码错误或登录失败", "retry": True}
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.exception("submit failed")
                return {"ok": False, "error": str(exc)}

    # ---------- 自动 OCR 登录（无人值守兜底） ----------

    def auto_login(self) -> bool:
        """用 ddddocr 自动过验证码；成功返回 True 并收割 cookie。"""
        with self._lock:
            try:
                driver = self._ensure()
                if not srm.login(driver):
                    return False
                self._harvest(driver)
                self.close()
                return True
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.exception("auto_login failed")
                return False

    def _harvest(self, driver):
        cookies = driver.get_cookies()
        save_session(cookies)
        logger.info("session harvested: %d cookies", len(cookies))


_session: LoginSession | None = None
_session_lock = threading.Lock()


def get_session() -> LoginSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = LoginSession()
        return _session
