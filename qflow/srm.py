"""圣农 SRM 只读浏览器助手（登录、验证码、质量扣款明细页抓取）。

供 livequery（实时查询）和脚本共用；不落业务数据。
Selenium / ddddocr 延迟导入，缓存模式（不实时）下没有它们也能启动服务。
凭据优先取环境变量 SRM_USER / SRM_PASS，取不到再读仓库 main.py（开发机用）。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

READFLOW = Path(__file__).resolve().parents[1]

URL = os.environ.get("QFLOW_SRM_URL", "https://srm.sunnercn.com:10001")
DEDUCTION_ROUTE = "/#/quajud/qua-jud-vendor-list"
DEDUCTION_URL = URL + DEDUCTION_ROUTE
# 不设就交给 Selenium 自己按当前平台找驱动
DRIVER = os.environ.get("QFLOW_CHROME_DRIVER", "")
PROFILE = os.environ.get("QFLOW_CHROME_PROFILE", str(READFLOW / "data" / "live" / "chrome-profile"))
CHROME_BINARY = os.environ.get("QFLOW_CHROME_BINARY", "")


def creds() -> dict:
    """登录凭据只从环境变量读；不同用户各用各的账号，不写进代码和图谱。"""
    username = os.environ.get("SRM_USER", "").strip()
    password = os.environ.get("SRM_PASS", "")
    if not (username and password):
        raise RuntimeError("没有 SRM 凭据：请设置 SRM_USER / SRM_PASS")
    return {"username": username, "password": password}


def _driver_module():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "实时查询需要 selenium：pip install selenium（并准备 ChromeDriver）"
        ) from exc
    return webdriver, Options, Service


def new_driver(headless: bool = True, configure=None, profile: str | None = None):
    webdriver, Options, Service = _driver_module()
    options = Options()
    if configure is not None:
        configure(options)
    if CHROME_BINARY:
        options.binary_location = CHROME_BINARY
    for arg in (
        "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--window-size=1440,1000", "--ignore-certificate-errors", "--lang=zh-CN",
    ):
        options.add_argument(arg)
    if headless:
        options.add_argument("--headless=new")
    profile = Path(profile or PROFILE)
    profile.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / name).unlink()
        except FileNotFoundError:
            pass
    options.add_argument(f"--user-data-dir={profile}")
    service = Service(DRIVER) if DRIVER else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(40)
    return driver


def captcha_images(driver):
    from selenium.webdriver.common.by import By

    result = []
    for image in driver.find_elements(By.TAG_NAME, "img"):
        try:
            src = image.get_attribute("src") or ""
            size = image.size
            if (
                image.is_displayed()
                and 85 <= size["width"] <= 180
                and 25 <= size["height"] <= 70
                and not src.startswith("data:image")
            ):
                result.append(image)
        except Exception:
            continue
    return result


def recognize(driver, ocr, rounds: int = 8):
    try:
        import ddddocr
    except ImportError:  # pragma: no cover
        models = {"default": ocr}
    else:
        models = {}
        for name, kwargs in (("default", {}), ("beta", {"beta": True}), ("old", {"old": True})):
            try:
                models[name] = ddddocr.DdddOcr(show_ad=False, **kwargs)
            except Exception:
                models[name] = ocr
    for round_no in range(1, rounds + 1):
        candidates = captcha_images(driver)
        if not candidates:
            raise RuntimeError("没有找到验证码图片")
        strong = []
        weak = []
        for image in candidates:
            texts = []
            for name, model in models.items():
                try:
                    text = re.sub(r"[^A-Za-z0-9]", "", model.classification(image.screenshot_as_png) or "")
                except Exception:
                    continue
                if len(text) == 4:
                    texts.append(text)
            if not texts:
                continue
            print(f"  OCR {round_no}: {texts}", flush=True)
            if len(set(texts)) == 1:
                strong.append((image, texts[0]))
            for text in texts:
                weak.append((image, text))
        if strong:
            return strong[0]
        if weak:
            return weak[0]
        driver.execute_script("arguments[0].click()", candidates[-1])
        time.sleep(0.6)
    raise RuntimeError(f"刷新 {rounds} 轮后仍没有得到 4 位验证码")


def messages(driver):
    from selenium.webdriver.common.by import By

    found = []
    for _ in range(6):
        time.sleep(0.4)
        for selector in (".el-message", ".el-message--error", ".el-notification", ".el-form-item__error"):
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = (element.text or "").strip()
                if text and text not in found:
                    found.append(text)
    return found


def is_login_page(driver):
    from selenium.webdriver.common.by import By

    try:
        if "/login" in (driver.current_url or ""):
            return True
        return any(
            element.is_displayed()
            and any(
                s in (element.get_attribute("placeholder") or "")
                for s in ("账号/邮箱", "验证码")
            )
            for element in driver.find_elements(By.CSS_SELECTOR, "input")
        )
    except Exception:
        return False


def login(driver) -> bool:
    """登录；复用 profile 时可能直接已登录。"""
    from selenium.common.exceptions import StaleElementReferenceException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        import ddddocr
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("实时查询需要 ddddocr：pip install ddddocr") from exc

    account = creds()
    ocr = ddddocr.DdddOcr(show_ad=False)
    driver.get(URL)
    driver.set_page_load_timeout(30)
    # 等到登录表单或主应用外壳出现，避免把"还没加载完"当成"已登录"
    WebDriverWait(driver, 25).until(
        lambda d: d.execute_script(
            "return document.readyState === 'complete' && ("
            "document.querySelectorAll('input[placeholder*=\"账号\"]').length > 0 || "
            "document.querySelectorAll('.el-menu, .el-menu-item, .sidebar').length > 0)"
        )
    )
    time.sleep(1.5)
    if not is_login_page(driver):
        return True

    for attempt in range(1, 13):
        print(f"LOGIN attempt {attempt}", flush=True)
        try:
            fields = {
                key: next(
                    (
                        item
                        for item in driver.find_elements(By.CSS_SELECTOR, "input")
                        if key in (item.get_attribute("placeholder") or "")
                    ),
                    None,
                )
                for key in ("账号/邮箱", "密码", "验证码")
            }
            if not all(fields.values()):
                raise RuntimeError(f"登录表单字段不完整: {list(fields)}")
            for key in ("账号/邮箱", "密码"):
                element = fields[key]
                driver.execute_script("arguments[0].scrollIntoView({block:'center'})", element)
                element.click()
                element.clear()
                element.send_keys(account["username"] if key == "账号/邮箱" else account["password"])

            _, code = recognize(driver, ocr)
            captcha = fields["验证码"]
            driver.execute_script("arguments[0].scrollIntoView({block:'center'})", captcha)
            captcha.click()
            captcha.clear()
            captcha.send_keys(code)
            time.sleep(0.3)

            clicked = False
            for _ in range(4):
                try:
                    buttons = driver.find_elements(By.CSS_SELECTOR, "button.login-btn") or driver.find_elements(By.TAG_NAME, "button")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'})", buttons[0])
                    buttons[0].click()
                    clicked = True
                    break
                except (StaleElementReferenceException, Exception):
                    time.sleep(0.5)
            if not clicked:
                raise RuntimeError("登录按钮不可用")
            messages(driver)

            if not is_login_page(driver):
                print("LOGIN OK", flush=True)
                return True
        except Exception as exc:  # noqa: BLE001 - 输入框被遮挡/刷新等
            print(f"LOGIN attempt {attempt} error: {exc}", flush=True)
        # 失败：重载页面再试
        try:
            driver.get(URL)
            time.sleep(2)
        except Exception:
            time.sleep(1)
    return False


def goto_deduction(driver, wait: float = 4.0):
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(DEDUCTION_URL)
    time.sleep(wait)
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script(
                "return document.querySelectorAll('.el-table__header th').length > 0"
            )
        )
    except Exception:
        pass


def click_query(driver) -> bool:
    from selenium.webdriver.common.by import By

    for element in driver.find_elements(By.CSS_SELECTOR, "button,.el-button"):
        if (element.text or "").strip() == "查询":
            driver.execute_script("arguments[0].click()", element)
            time.sleep(2.5)
            return True
    return False


def reset_filters(driver):
    from selenium.webdriver.common.by import By

    for element in driver.find_elements(By.CSS_SELECTOR, "button,.el-button"):
        if (element.text or "").strip() == "重置":
            driver.execute_script("arguments[0].click()", element)
            time.sleep(1.5)
            return True
    return False


def extract_table(driver):
    return driver.execute_script(
        """
        const clean = el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        const table = document.querySelector('.el-table');
        if (!table) return {headers: [], rows: []};
        const headers = [...table.querySelectorAll('.el-table__header-wrapper thead th, thead th')]
          .map(clean).filter(Boolean);
        const rows = [...table.querySelectorAll('.el-table__body-wrapper tbody tr, tbody tr')]
          .map(tr => [...tr.querySelectorAll('td')].map(clean))
          .filter(row => row.length);
        return {headers, rows};
        """
    )


def next_page(driver) -> bool:
    return bool(
        driver.execute_script(
            """
            const btn = document.querySelector('.el-pagination .btn-next');
            if (!btn || btn.disabled) return false;
            btn.click();
            return true;
            """
        )
    )


def pagination_total(driver) -> int | None:
    try:
        text = driver.execute_script(
            "return (document.querySelector('.el-pagination__total')||{}).innerText || ''"
        ) or ""
        match = re.search(r"共\s*(\d+)", text)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def deduction_pages(driver, max_pages: int = 30):
    """翻完所有页，返回原始 headers/rows（未去重）。"""
    table = {"headers": [], "rows": []}
    for _ in range(3):
        goto_deduction(driver)
        reset_filters(driver)
        table = extract_table(driver)
        if not table.get("headers"):
            # 有的会话第一次进来不自动查，点一次只读「查询」
            click_query(driver)
            table = extract_table(driver)
        if table.get("headers"):
            break
        time.sleep(2)
    headers = table.get("headers") or []
    if not headers:
        raise RuntimeError("质量扣款明细表格未加载（可能是会话过期）")
    rows = list(table.get("rows") or [])
    pages = 1
    while pages < max_pages:
        # 翻页按钮在表格加载时可能短暂 disabled，重试
        advanced = False
        for _ in range(3):
            if next_page(driver):
                advanced = True
                break
            time.sleep(1.0)
        if not advanced:
            break
        time.sleep(1.0)
        page = extract_table(driver)
        more = page.get("rows") or []
        if not more:
            break
        rows.extend(more)
        pages += 1
    return {"headers": headers, "rows": rows, "pages": pages, "total": pagination_total(driver)}
