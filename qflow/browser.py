"""登录用的浏览器。只有登录时才开，登录成功就关；查询是纯 HTTP，不经过这里。

qflow/login.py 只用下面这几个方法，测试里用假浏览器代替。selenium 延迟导入，没装也能启动服务。
反爬严的系统：设 QFLOW_BROWSER_HEADLESS=0 并提供 DISPLAY（服务器上用 Xvfb），就是一个真窗口；
每个系统有自己的浏览器资料目录，设备信任之类的状态会留下来。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from . import config

KEYS = {"Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, ""), "Tab": ("Tab", 9, ""), "Escape": ("Escape", 27, "")}


class Browser:
    def __init__(self, profile: Path, width: int = 1280, height: int = 800):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("登录需要浏览器驱动：pip install -r requirements-live.txt") from exc
        options = Options()
        if config.CHROME_BINARY:
            options.binary_location = config.CHROME_BINARY
        for arg in ("--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--lang=zh-CN",
                    "--disable-blink-features=AutomationControlled", f"--window-size={width},{height}"):
            options.add_argument(arg)
        if config.BROWSER_HEADLESS:
            options.add_argument("--headless=new")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        profile = Path(profile)
        profile.mkdir(parents=True, exist_ok=True)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (profile / name).unlink(missing_ok=True)
        options.add_argument(f"--user-data-dir={profile}")
        self.driver = webdriver.Chrome(service=Service(config.CHROME_DRIVER) if config.CHROME_DRIVER else Service(), options=options)
        self.driver.set_page_load_timeout(40)

    def _find(self, locator: str):
        from selenium.webdriver.common.by import By

        return [e for e in self.driver.find_elements(By.CSS_SELECTOR, locator) if e.is_displayed()]

    def goto(self, url: str) -> None:
        self.driver.get(url)

    def url(self) -> str:
        return self.driver.current_url or ""

    def exists(self, locator: str) -> bool:
        try:
            return bool(self._find(locator))
        except Exception:  # 页面正在跳转时元素会失效
            return False

    def fill(self, locator: str, text: str) -> None:
        element = self._find(locator)[0]
        element.clear()
        element.send_keys(text)

    def click(self, locator: str) -> None:
        self.driver.execute_script("arguments[0].click()", self._find(locator)[0])

    def element_png(self, locator: str) -> bytes:
        return self._find(locator)[-1].screenshot_as_png

    def screenshot(self) -> bytes:
        shot = self.driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
        return base64.b64decode(shot["data"])

    def size(self) -> tuple[int, int]:
        width, height = self.driver.execute_script("return [window.innerWidth, window.innerHeight]")
        return int(width), int(height)

    def mouse(self, kind: str, x: float, y: float) -> None:
        """kind: down / move / up，坐标是页面可见区域里的像素。"""
        event = {"down": "mousePressed", "move": "mouseMoved", "up": "mouseReleased"}[kind]
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": event, "x": x, "y": y, "button": "left", "buttons": 0 if kind == "up" else 1,
            "clickCount": 0 if kind == "move" else 1,
        })

    def text(self, text: str) -> None:
        self.driver.execute_cdp_cmd("Input.insertText", {"text": text})

    def key(self, name: str) -> None:
        key, code, char = KEYS[name]
        base = {"key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyDown", **base, **({"text": char} if char else {})})
        self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    def cookies(self) -> list[dict]:
        return self.driver.get_cookies()

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass


def read_captcha(png: bytes, length: int | None = None) -> str | None:
    """自动识别图片验证码；没装识别库或认不出来返回 None，由人来填。"""
    try:
        import ddddocr
    except ImportError:
        return None
    text = "".join(ch for ch in (ddddocr.DdddOcr(show_ad=False).classification(png) or "") if ch.isalnum())
    return text if text and (length is None or len(text) == length) else None


def wait(condition, seconds: float, every: float = 0.4) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(every)
    return bool(condition())
