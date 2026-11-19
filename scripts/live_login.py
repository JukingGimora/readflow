"""只读登录探针：人工/视觉过验证码，登录后抓取页面结构。

用法（首轮）：
    python scripts/live_login.py --profile data/chrome-profile \
        --captcha /tmp/srm_captcha.png --code-file /tmp/srm_code.txt

脚本会：
1. 打开 SRM 登录页，填入账号/密码；
2. 截取验证码到 --captcha，等待 --code-file 出现（另一进程/人写入）；
3. 填入验证码并登录；
4. 把最终 URL、页面文本、菜单、cookies 写入 data/live/login_result.json。

只读：不点击保存/提交/审批/发货/删除，只做登录和页面读取。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://srm.sunnercn.com:10001")
    parser.add_argument("--profile", default="data/chrome-profile")
    parser.add_argument("--captcha", default="/tmp/srm_captcha.png")
    parser.add_argument("--code-file", default="/tmp/srm_code.txt")
    parser.add_argument("--ready-file", default="/tmp/srm_ready.txt")
    parser.add_argument("--result", default="data/live/login_result.json")
    parser.add_argument("--screenshot", default="data/live/after_login.png")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--creds", default="/tmp/srm_creds.json")
    args = parser.parse_args()

    creds = json.loads(Path(args.creds).read_text(encoding="utf-8"))
    profile = Path(args.profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    captcha_path = Path(args.captcha)
    code_path = Path(args.code_file)
    ready_path = Path(args.ready_file)
    result_path = Path(args.result)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    for path in (code_path, ready_path):
        if path.exists():
            path.unlink()

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1440,1000")
    opts.add_argument("--lang=zh-CN")
    opts.add_argument(f"--user-data-dir={profile}")
    service = Service(executable_path=str(Path("data/drivers/chromedriver-mac-arm64/chromedriver").resolve()))

    driver = webdriver.Chrome(options=opts, service=service)
    driver.set_page_load_timeout(60)
    try:
        driver.get(args.url)
        wait = WebDriverWait(driver, 30)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.el-input__inner")))
        time.sleep(2)

        def fill_inputs():
            for element in driver.find_elements(By.CSS_SELECTOR, "input.el-input__inner"):
                placeholder = element.get_attribute("placeholder") or ""
                if "账号" in placeholder or "邮箱" in placeholder:
                    element.clear()
                    element.send_keys(creds["username"])
                elif "密码" in placeholder:
                    element.clear()
                    element.send_keys(creds["password"])

        fill_inputs()
        captcha = driver.find_element(By.CSS_SELECTOR, "img.vcode")
        captcha.screenshot(str(captcha_path))
        ready_path.write_text("ready", encoding="utf-8")
        print(f"CAPTCHA_SAVED {captcha_path} size={captcha.size}", flush=True)
        try:
            import ddddocr

            raw = captcha_path.read_bytes()
            candidates = {}
            for name, kwargs in (("default", {}), ("beta", {"beta": True})):
                try:
                    candidates[name] = ddddocr.DdddOcr(show_ad=False, **kwargs).classification(raw)
                except Exception as exc:  # noqa: BLE001
                    candidates[name] = f"ERR:{exc}"
            print("OCR_CANDIDATES", candidates, flush=True)
        except Exception as exc:  # noqa: BLE001
            print("OCR_UNAVAILABLE", type(exc).__name__, str(exc)[:200], flush=True)

        deadline = time.time() + args.timeout
        code = ""
        while time.time() < deadline:
            if code_path.exists():
                code = code_path.read_text(encoding="utf-8").strip()
                if code:
                    break
            time.sleep(0.5)
        if not code:
            print("TIMEOUT waiting for code", flush=True)
            return 3
        print(f"CODE_RECEIVED len={len(code)}", flush=True)

        for element in driver.find_elements(By.CSS_SELECTOR, "input.el-input__inner"):
            placeholder = element.get_attribute("placeholder") or ""
            if "验证码" in placeholder:
                element.clear()
                element.send_keys(code)

        password_len = len(
            next(
                (
                    element.get_attribute("value") or ""
                    for element in driver.find_elements(By.CSS_SELECTOR, "input.el-input__inner")
                    if "密码" in (element.get_attribute("placeholder") or "")
                ),
                "",
            )
        )
        print(f"PASSWORD_FILLED_LEN={password_len}", flush=True)

        button = driver.find_element(By.CSS_SELECTOR, "button.login-btn")
        button.click()
        messages = []
        for _ in range(12):
            time.sleep(0.5)
            for selector in (".el-message", ".el-message--error", ".el-notification", ".el-form-item__error"):
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    text = (element.text or "").strip()
                    if text and text not in messages:
                        messages.append(text)
        driver.save_screenshot(str(Path(args.screenshot).resolve()))

        body = driver.find_element(By.TAG_NAME, "body").text
        menu_texts = driver.execute_script(
            """
            const root = document.querySelector('ul.el-menu, .el-menu, aside, .sidebar');
            if (!root) return [];
            return [...new Set([...root.querySelectorAll('li, a, [role=menuitem]')]
              .map(n => (n.innerText || '').replace(/\\s+/g, ' ').trim())
              .filter(Boolean))];
            """
        )
        payload = {
            "ok": "/#/login" not in driver.current_url,
            "url": driver.current_url,
            "title": driver.title,
            "body": body[:5000],
            "messages": messages,
            "menu": menu_texts,
            "cookies": driver.get_cookies(),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        driver.save_screenshot(str(Path(args.screenshot).resolve()))
        print("LOGIN_RESULT", json.dumps({k: payload[k] for k in ("ok", "url", "title")}, ensure_ascii=False), flush=True)
        print("MENU", menu_texts[:40], flush=True)
        print("MESSAGES", messages, flush=True)
        print("BODY", body[:1200].replace("\n", " | "), flush=True)
        return 0 if payload["ok"] else 4
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
