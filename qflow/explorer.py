"""通用系统探索器：登录 → 爬菜单/页面/接口 → 生成数据流图谱 → 销毁会话.

- 每次任务临时浏览器 profile，结束时退出并删除，不保留登录态；
- 系统差异全部在 systems/<name>.json 里配置，代码不写死任何一家；
- 输出 data/explore/<name>/pages.json、apis.json、graph.json。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import PROJECT_ROOT

DENY_TEXT = ("保存", "提交", "删除", "移除", "审批", "审核", "发货", "确认", "确定", "新增", "编辑", "修改", "作废", "退回", "付款", "结算", "导出", "打印", "上传", "停用", "启用")
NAV_SUFFIX = ("管理", "记录", "看板", "清单", "列表", "明细", "查询", "统计", "报表", "详情", "首页", "设置", "概览", "中心", "分析", "日志", "信息", "档案")


def _driver_module():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    return webdriver, Options, Service


def is_dangerous(text: str) -> bool:
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return False
    if text.endswith(NAV_SUFFIX):
        return False
    return any(word in text for word in DENY_TEXT)


class EphemeralBrowser:
    """一次任务一个会话：退出即删 profile。"""

    def __init__(self, cfg: dict, driver_path: str | None = None):
        self.cfg = cfg
        self.tmp = Path(tempfile.mkdtemp(prefix="explorer-profile-"))
        webdriver, Options, Service = _driver_module()
        options = Options()
        for arg in ("--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                    "--window-size=1440,1000", "--ignore-certificate-errors", "--lang=zh-CN"):
            options.add_argument(arg)
        options.add_argument("--headless=new")
        options.add_argument(f"--user-data-dir={self.tmp}")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        options.set_capability("goog:chromeOptions", {"prefs": {"profile.default_content_setting_values.notifications": 2}})
        driver_path = driver_path or str(PROJECT_ROOT / "data" / "drivers" / "chromedriver-mac-arm64" / "chromedriver")
        self.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        self.driver.set_page_load_timeout(40)
        self._inject_session()

    def _inject_session(self) -> None:
        try:
            from . import srmapi

            cookies = srmapi.load_session()
            if not cookies:
                return
            self.driver.get(self.cfg["base_url"])
            time.sleep(1.5)
            for c in cookies:
                try:
                    self.driver.add_cookie({
                        "name": c["name"],
                        "value": c.get("value") or "",
                        "path": c.get("path") or "/",
                        "domain": c.get("domain") or "",
                        "secure": bool(c.get("secure")),
                        **({"expiry": int(c["expiry"])} if c.get("expiry") else {}),
                    })
                except Exception:
                    continue
            self.driver.refresh()
            time.sleep(2)
            print(f"  session injected: {len(cookies)} cookies", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  session inject failed: {exc}", flush=True)

    # ---------- 登录 ----------

    def _ocr_models(self):
        try:
            import ddddocr
        except ImportError:
            return {"default": None}
        models = {}
        for name, kwargs in (("default", {}), ("beta", {"beta": True}), ("old", {"old": True})):
            try:
                models[name] = ddddocr.DdddOcr(show_ad=False, **kwargs)
            except Exception:
                pass
        return models

    def _read_captcha(self, selector: str, models):
        from selenium.webdriver.common.by import By

        images = []
        for img in self.driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if img.is_displayed():
                    images.append(img)
            except Exception:
                continue
        if not images:
            return None, None
        strong, weak = [], []
        for img in images:
            texts = []
            for model in models.values():
                if model is None:
                    continue
                try:
                    text = re.sub(r"[^A-Za-z0-9]", "", model.classification(img.screenshot_as_png) or "")
                except Exception:
                    continue
                if len(text) == 4:
                    texts.append(text)
            if len(set(texts)) == 1 and texts:
                strong.append((img, texts[0]))
            weak.extend((img, text) for text in texts)
        return (strong[0][0], strong[0][1]) if strong else ((weak[0][0], weak[0][1]) if weak else (None, None))

    def login(self) -> bool:
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        auth = self.cfg.get("auth") or {}
        fields_cfg = auth.get("fields") or []
        captcha_cfg = auth.get("captcha") or {}
        submit_cfg = auth.get("submit") or {}
        marker = auth.get("logged_in_marker") or {}
        creds = auth.get("credentials_env") or {}
        import os

        username = os.environ.get(creds.get("username", "SRM_USER"), "") or creds.get("username_value", "")
        password = os.environ.get(creds.get("password", "SRM_PASS"), "") or creds.get("password_value", "")
        if not username:
            username = auth.get("username_value", "")

        self.driver.get(self.cfg["base_url"])
        WebDriverWait(self.driver, 25).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(1.5)

        def logged_in() -> bool:
            for rule in marker.get("any") or []:
                if rule.get("match") == "url":
                    hit = rule.get("contains", "") in (self.driver.current_url or "")
                    if rule.get("negate"):
                        hit = not hit
                    if hit:
                        return True
            for field in fields_cfg:
                if field.get("key") == "username":
                    try:
                        found = any(
                            field["contains"] in (el.get_attribute("placeholder") or "")
                            for el in self.driver.find_elements(By.CSS_SELECTOR, "input")
                            if el.is_displayed()
                        )
                    except Exception:
                        found = False
                    if found:
                        return False
            return True

        if logged_in():
            return True

        models = self._ocr_models()
        for attempt in range(1, 7):
            print(f"  login attempt {attempt}", flush=True)
            try:
                values = {}
                for field in fields_cfg:
                    key, contains = field["key"], field.get("contains", "")
                    values[key] = next(
                        (el for el in self.driver.find_elements(By.CSS_SELECTOR, "input")
                         if contains in (el.get_attribute("placeholder") or "") and el.is_displayed()),
                        None,
                    )
                if not values.get("username"):
                    raise RuntimeError("找不到账号输入框")
                for key, el in values.items():
                    if el is None:
                        continue
                    text = {"username": username, "password": password}.get(key, "")
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'})", el)
                    el.click()
                    el.clear()
                    el.send_keys(text)
                if values.get("captcha") is not None and captcha_cfg.get("css"):
                    code = None
                    image = None
                    for _ in range(6):
                        image, code = self._read_captcha(captcha_cfg["css"], models)
                        if code:
                            break
                        if image is not None:
                            self.driver.execute_script("arguments[0].click()", image)
                            time.sleep(0.6)
                    if not code:
                        raise RuntimeError("验证码识别失败")
                    values["captcha"].clear()
                    values["captcha"].send_keys(code)
                time.sleep(0.3)
                clicked = False
                for _ in range(4):
                    try:
                        buttons = self.driver.find_elements(By.CSS_SELECTOR, submit_cfg.get("css", "button"))
                        buttons[0].click()
                        clicked = True
                        break
                    except StaleElementReferenceException:
                        time.sleep(0.5)
                if not clicked:
                    raise RuntimeError("登录按钮不可用")
                time.sleep(2)
                if logged_in():
                    return True
            except Exception as exc:  # noqa: BLE001
                print(f"  login attempt {attempt}: {exc}", flush=True)
            try:
                self.driver.get(self.cfg["base_url"])
                time.sleep(2)
            except Exception:
                pass
        return False

    # ---------- 爬取 ----------

    def snapshot(self) -> dict:
        return self.driver.execute_script(
            """
            const clean = el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const label = el => {
              const p = el.closest('.el-form-item,.ant-form-item,.form-group');
              const l = p && p.querySelector('label,.el-form-item__label,.ant-form-item-label');
              return clean(l || el) || el.getAttribute('placeholder') || '';
            };
            return {
              url: location.href,
              title: document.title,
              nav: [...document.querySelectorAll('a,[role="menuitem"],.el-menu-item,.el-submenu__title,.ant-menu-item')]
                    .filter(vis).map(clean).filter(Boolean).slice(0, 120),
              buttons: [...document.querySelectorAll('button,.el-button,[role="button"]')]
                    .filter(vis).map(clean).filter(Boolean).slice(0, 80),
              fields: [...document.querySelectorAll('input,textarea,select')].filter(vis)
                    .map(el => ({label: label(el), placeholder: el.getAttribute('placeholder'), widget: el.closest('.el-select,.ant-select') ? 'select' : el.closest('.el-date-editor,.ant-picker') ? 'date' : 'input'}))
                    .filter(x => x.label || x.placeholder).slice(0, 60),
              headers: [...document.querySelectorAll('.el-table__header th,table thead th,.ant-table-thead th,[role="columnheader"]')]
                    .filter(vis).map(clean).filter(Boolean).slice(0, 80),
              sample: [...document.querySelectorAll('.el-table__body-wrapper tbody tr,table tbody tr,.ant-table-tbody tr')]
                    .filter(vis).slice(0, 3).map(tr => [...tr.querySelectorAll('td')].map(clean)),
            };
            """
        )

    def drain_apis(self) -> list[dict]:
        try:
            entries = self.driver.get_log("performance")
        except Exception:
            return []
        seen = getattr(self, "_seen_reqs", None)
        if seen is None:
            seen = self._seen_reqs = set()
        requests, responses, finished = {}, {}, []
        for item in entries:
            try:
                msg = json.loads(item["message"])["message"]
                method, params = msg.get("method"), msg.get("params", {})
                rid = params.get("requestId")
                if method == "Network.requestWillBeSent" and params.get("type") in {"XHR", "Fetch"}:
                    req = params.get("request", {})
                    parsed = urlsplit(req.get("url", ""))
                    body_keys = []
                    try:
                        data = json.loads(req.get("postData") or "")
                        body_keys = sorted(data.keys())[:20] if isinstance(data, dict) else []
                    except Exception:
                        pass
                    requests[rid] = {"method": req.get("method"), "path": parsed.path,
                                     "query_keys": sorted(parse_qs(parsed.query).keys()), "body_keys": body_keys}
                elif method == "Network.responseReceived" and rid in requests:
                    responses[rid] = {"status": params.get("response", {}).get("status")}
                elif method == "Network.loadingFinished" and rid in requests:
                    finished.append(rid)
            except Exception:
                continue
        apis = []
        for rid, req in requests.items():
            if rid in seen:
                continue
            seen.add(rid)
            out = {**req, **responses.get(rid, {})}
            if rid in finished and responses.get(rid, {}).get("status") == 200:
                try:
                    body = json.loads(self.driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid}).get("body", "{}"))
                    if isinstance(body, dict):
                        out["response_keys"] = sorted(body.keys())[:20]
                        rows = body.get("rows") or body.get("data") or body.get("records")
                        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                            out["row_keys"] = sorted(rows[0].keys())[:40]
                except Exception:
                    pass
            apis.append(out)
        return apis

    def crawl(self) -> dict:
        crawl_cfg = self.cfg.get("crawl") or {}
        menu_selectors = crawl_cfg.get("menu_selectors", ["a", "[role=menuitem]"])
        max_pages = int(crawl_cfg.get("max_pages", 30))
        wait = float(crawl_cfg.get("page_wait_s", 1.2))
        start = self.cfg["base_url"] + (crawl_cfg.get("start", ""))
        self.driver.get(start)
        time.sleep(2)

        pages, apis = [], []
        first = self.snapshot()
        first["menu"] = "(首页)"
        pages.append(first)
        apis.extend(self.drain_apis())

        expand_selectors = crawl_cfg.get("expand_selectors", [".el-submenu__title"])
        expand_sel = ", ".join(expand_selectors)
        item_sel = ", ".join(menu_selectors)

        def menu_texts(sel):
            return self.driver.execute_script(
                """
                const sel = arguments[0];
                const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return [...new Set([...document.querySelectorAll(sel)].filter(vis)
                    .map(n => (n.innerText || '').replace(/\\s+/g, ' ').trim())
                    .filter(t => t && t.length <= 12))];
                """,
                sel,
            )

        def click_text(sel, wanted):
            return self.driver.execute_script(
                """
                const wanted = arguments[0];
                const sel = arguments[1];
                const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const items = [...document.querySelectorAll(sel)].filter(vis)
                    .filter(el => (el.innerText || '').replace(/\\s+/g, ' ').trim() === wanted);
                if (items.length !== 1) return false;
                items[0].click();
                return true;
                """,
                wanted,
                sel,
            )

        seen_menus = set()
        done = 0
        # 一级菜单逐个打开，点完它的子项再换下一个
        for title in menu_texts(expand_sel):
            if title in seen_menus or is_dangerous(title):
                continue
            seen_menus.add(title)
            if not click_text(expand_sel, title):
                continue
            time.sleep(1.2)
            while done < max_pages:
                items = [t for t in menu_texts(item_sel) if t not in seen_menus and not is_dangerous(t)]
                if not items:
                    break
                target = items[0]
                seen_menus.add(target)
                if not click_text(item_sel, target):
                    break
                done += 1
                time.sleep(wait)
                snapshot = self.snapshot()
                snapshot["menu"] = target
                pages.append(snapshot)
                apis.extend(self.drain_apis())
                print(f"  [{done}/{max_pages}] {target} -> {snapshot['url'][:80]}", flush=True)
                # 跳转后子菜单可能收起，收起就重新点开当前一级菜单
                if not menu_texts(item_sel):
                    click_text(expand_sel, title)
                    time.sleep(1.0)


        # 菜单爬不到的页面：按配置的种子路由直接访问
        for seed in crawl_cfg.get("seeds", []):
            try:
                self.driver.get(self.cfg["base_url"] + seed)
                time.sleep(max(wait, 2.5))
                snapshot = self.snapshot()
                snapshot["menu"] = "(种子)" + seed
                pages.append(snapshot)
                apis.extend(self.drain_apis())
                print(f"  [seed] {seed} -> {snapshot['url'][:80]}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  [seed] {seed} failed: {exc}", flush=True)
        return {"pages": pages, "apis": apis}

    # ---------- 图谱 ----------

    def build_graph(self, crawled: dict) -> dict:
        name = self.cfg.get("name", "system")
        nodes, edges = [], []
        nodes.append({"id": f"app.{name}", "type": "app", "label": self.cfg.get("label", name),
                      "base_url": self.cfg["base_url"], "read_only": True})
        page_apis: dict[str, list] = {}
        # 每页收集期间调用的接口（粗略归属：前一次 drain 到这次之间新增的）
        api_by_page = []
        seen_api_ids = set()
        prev_len = 0
        for snapshot in crawled["pages"]:
            fresh = crawled["apis"][prev_len:]
            prev_len = len(crawled["apis"])
            api_by_page.append(fresh)
        for snapshot, fresh in zip(crawled["pages"], api_by_page):
            pid = "page." + hashlib.sha1(snapshot["url"].encode()).hexdigest()[:10]
            nodes.append({
                "id": pid, "type": "page", "label": snapshot.get("menu") or snapshot.get("title") or snapshot["url"],
                "route": snapshot["url"], "read_only": True,
                "table_headers": snapshot.get("headers") or [],
            })
            edges.append({"from": f"app.{name}", "to": pid, "type": "contains"})
            if snapshot.get("headers"):
                eid = "entity." + hashlib.sha1(("|".join(snapshot["headers"])).encode()).hexdigest()[:10]
                unique_headers = []
                for h in snapshot["headers"]:
                    if h not in unique_headers:
                        unique_headers.append(h)
                nodes.append({
                    "id": eid, "type": "entity",
                    "label": f"{snapshot.get('menu') or '页面'}-表格",
                    "fields": [{"name": h, "label": h} for h in unique_headers],
                })
                edges.append({"from": pid, "to": eid, "type": "renders"})
                for api in fresh:
                    aid = "api." + hashlib.sha1((api["method"] + api["path"]).encode()).hexdigest()[:10]
                    if aid in seen_api_ids:
                        continue
                    seen_api_ids.add(aid)
                    api["id"] = aid
                    api["type"] = "api"
                    api["read_only"] = True
                    nodes.append(api)
                    edges.append({"from": pid, "to": aid, "type": "calls"})
                    edges.append({"from": aid, "to": eid, "type": "returns"})
        return {"version": 1, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "read_only": True, "nodes": nodes, "edges": edges}

    def teardown(self):
        try:
            self.driver.quit()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def run(cfg_path: str, out_dir: str = "data/explore") -> dict:
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    name = cfg.get("name", Path(cfg_path).stem)
    out = Path(out_dir) / name
    out.mkdir(parents=True, exist_ok=True)
    browser = EphemeralBrowser(cfg)
    try:
        if not browser.login():
            raise RuntimeError("登录失败")
        print("LOGIN OK", flush=True)
        crawled = browser.crawl()
        graph = browser.build_graph(crawled)
        (out / "pages.json").write_text(json.dumps(crawled["pages"], ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "apis.json").write_text(json.dumps(crawled["apis"], ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "system": name,
            "pages": len(crawled["pages"]),
            "apis": len(crawled["apis"]),
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "graph": str(out / "graph.json"),
        }
        print("SUMMARY", json.dumps(summary, ensure_ascii=False), flush=True)
        return summary
    finally:
        browser.teardown()
        print("SESSION DESTROYED", flush=True)
