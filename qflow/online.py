"""在线只读适配层（可选）：Playwright + 人工登录会话.

安全边界（很重要）：
- 只导航菜单、只读取表格，不点任何"保存/提交/审批/发货/删除"按钮；
- 浏览器层拦截写请求（POST/PUT/PATCH/DELETE 命中写路径直接 abort）；
- 登录只支持人工完成（验证码/短信/SSO），本模块不保存账号密码，只保存
  Playwright storage_state（cookie + localStorage）；
- 默认 headless，用于服务器定时刷新；探索模式建议在测试账号上跑。

注意：Playwright 未安装时本模块仍可导入（函数内部延迟导入），
所以 `python -m unittest` 不会因为缺浏览器依赖而失败。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("qflow.online")

WRITE_URL_RE = re.compile(
    r"(?:^|/)(?:save|submit|delete|remove|update|edit|add|create|approve|audit|"
    r"ship|deliver|confirm|commit|pay|settle|return|cancel|void|export|print|upload)"
    r"(?:[/?#]|$)",
    re.IGNORECASE,
)
ACTION_WORDS = (
    "保存", "提交", "删除", "移除", "发货", "审批", "审核", "确认", "确定",
    "新增", "编辑", "修改", "作废", "退回", "付款", "结算", "导出", "打印",
    "上传", "停用", "启用",
)
# 菜单/导航通常以这些词结尾，不能因为包含"发货/结算"就被当成写按钮
NAV_SUFFIXES = (
    "管理", "记录", "看板", "清单", "列表", "明细", "查询", "统计", "报表",
    "详情", "首页", "设置", "概览", "中心", "分析", "日志", "信息", "档案",
)


class ReadOnlyViolation(RuntimeError):
    """尝试执行写操作时抛出。"""


def is_write_request(method: str, url: str) -> bool:
    method = (method or "").upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    # DELETE / PUT / PATCH 一律视为写；POST 允许查询类接口（很多后台列表用 POST）
    if method in {"DELETE", "PUT", "PATCH"}:
        return True
    if method != "POST":
        return True
    path = urlparse(url).path or ""
    return bool(WRITE_URL_RE.search(path))


def is_dangerous_click(text: str | None) -> bool:
    """按钮/菜单文本是否可能触发写操作。

    导航项（如"发货管理""发货记录""结算管理"）放行；真正动作按钮
    （"保存""发货""确认发货""删除记录"）拦截。
    """
    if not text:
        return False
    normalized = re.sub(r"\s+", "", str(text))
    if normalized.startswith(("删除", "移除", "清空", "作废")):
        return True
    if normalized.endswith(NAV_SUFFIXES):
        return False
    return any(word in normalized for word in ACTION_WORDS)


def assert_readonly_request(method: str, url: str) -> None:
    if is_write_request(method, url):
        raise ReadOnlyViolation(f"已拦截写请求: {method} {url}")


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        raise RuntimeError(
            "在线只读功能需要 playwright：pip install playwright && playwright install chromium"
        ) from exc


class SrmSession:
    """带只读网闸的浏览器会话。"""

    def __init__(self, state_path: str | Path, headless: bool = True):
        self.state_path = Path(state_path)
        self.headless = headless
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self) -> "SrmSession":
        _require_playwright()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)
        kwargs: dict = {}
        if self.state_path.exists():
            kwargs["storage_state"] = str(self.state_path)
        self.context = self.browser.new_context(**kwargs)
        self.context.route("**/*", self._guard)
        self.page = self.context.new_page()
        return self

    def _guard(self, route, request):
        if is_write_request(request.method, request.url):
            logger.warning("blocked write request: %s %s", request.method, request.url)
            route.abort()
        else:
            route.continue_()

    def goto(self, url: str, wait_ms: int = 2000):
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(wait_ms)
        return self.page

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.context is not None:
                self.context.close()
            if self.browser is not None:
                self.browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()


def login_and_save(url: str, state_path: str | Path) -> Path:
    """打开有头浏览器，人工登录完成后回车，保存会话。"""
    _require_playwright()
    from playwright.sync_api import sync_playwright

    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"打开浏览器：{url}")
    print("请在浏览器里完成登录（验证码/短信/SSO），确认已进入系统后回到这里按回车。")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        input("登录完成后按回车保存会话…")
        context.storage_state(path=str(state_path))
        print(f"会话已保存：{state_path}")
        browser.close()
    return state_path


_TABLE_JS = """
() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  document.querySelectorAll('.el-table').forEach((table) => {
    const headers = [...table.querySelectorAll('.el-table__header th')].map((th) => clean(th.innerText));
    const rows = [...table.querySelectorAll('.el-table__body tr')].slice(0, 5)
      .map((tr) => [...tr.querySelectorAll('td')].map((td) => clean(td.innerText)));
    if (headers.length) out.push({ kind: 'element-table', headers, rows });
  });
  document.querySelectorAll('table').forEach((table) => {
    if (table.closest('.el-table')) return;
    const headers = [...table.querySelectorAll('thead th')].map((th) => clean(th.innerText));
    const rows = [...table.querySelectorAll('tbody tr')].slice(0, 5)
      .map((tr) => [...tr.querySelectorAll('td')].map((td) => clean(td.innerText)));
    if (headers.length || rows.length) out.push({ kind: 'table', headers, rows });
  });
  return out.slice(0, 10);
}
"""

_BUTTON_JS = """
() => [...document.querySelectorAll('button, .el-button, a')]
  .map((el) => (el.innerText || '').replace(/\\s+/g, ' ').trim())
  .filter((text) => text && text.length <= 20)
  .slice(0, 60)
"""

_MENU_JS = """
() => {
  const root = document.querySelector('ul.el-menu, .el-menu, aside, .sidebar, .menu');
  if (!root) return [];
  const nodes = [...root.querySelectorAll('li, a, [role=menuitem]')];
  const texts = nodes.map((n) => (n.innerText || '').replace(/\\s+/g, ' ').trim());
  return [...new Set(texts)].filter((t) => t && t.length <= 30).slice(0, 100);
}
"""


def snapshot_page(page) -> dict:
    return {
        "url": page.url,
        "title": page.title(),
        "tables": page.evaluate(_TABLE_JS),
        "buttons": page.evaluate(_BUTTON_JS),
    }


def _api_summary(response) -> dict | None:
    try:
        content_type = (response.headers or {}).get("content-type", "")
        if "json" not in content_type.lower():
            return None
        parsed = urlparse(response.url)
        keys: list[str] = []
        try:
            body = response.json()
            if isinstance(body, dict):
                keys = list(body.keys())[:15]
        except Exception:
            body = None
        return {
            "method": response.request.method,
            "path": parsed.path,
            "query_keys": sorted(parse_qs(parsed.query).keys()),
            "status": response.status,
            "top_keys": keys,
        }
    except Exception:
        return None


def fetch_tables(
    url: str,
    state_path: str | Path,
    out_path: str | Path,
    *,
    wait_ms: int = 3000,
) -> dict:
    """只读打开单个页面，抓取表格快照（用于确认扣款明细页/接口）。

    不做筛选、不点查询按钮——页面默认加载什么就抓什么；需要条件查询时，
    后续把该页的查询接口编译成 recipe。
    """
    _require_playwright()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with SrmSession(state_path, headless=True) as session:
        page = session.goto(url, wait_ms=wait_ms)
        snapshot = snapshot_page(page)
    payload = {
        "url": url,
        "final_url": snapshot["url"],
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "read_only": True,
        **snapshot,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "output": str(out_path),
        "final_url": snapshot["url"],
        "tables": len(snapshot["tables"]),
        "note": "只读快照；若表格为空，可能需要先在该页设置筛选条件（当前不自动点查询）。",
    }


def discover(
    base_url: str,
    state_path: str | Path,
    out_path: str | Path,
    *,
    max_pages: int = 30,
) -> dict:
    """只读探索：遍历左侧菜单，记录路由、表格样例、XHR 接口。"""
    _require_playwright()
    state_path = Path(state_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pages: list[dict] = []
    apis: dict[str, dict] = {}
    errors: list[dict] = []

    with SrmSession(state_path, headless=True) as session:
        page = session.goto(base_url, wait_ms=3000)

        def on_response(response):
            summary = _api_summary(response)
            if summary:
                key = f"{summary['method']} {summary['path']}"
                apis.setdefault(key, summary)

        page.on("response", on_response)
        menu_texts = page.evaluate(_MENU_JS)

        for text in menu_texts[: max(1, max_pages)]:
            if is_dangerous_click(text):
                continue
            try:
                root = page.locator("ul.el-menu, .el-menu, aside").first
                root.get_by_text(text, exact=True).first.click(timeout=3000)
            except Exception as exc:  # noqa: BLE001 - 菜单项可能不可点
                errors.append({"menu": text, "error": str(exc)[:200]})
                continue
            page.wait_for_timeout(1200)
            snapshot = snapshot_page(page)
            snapshot["menu"] = text
            pages.append(snapshot)

    payload = {
        "base_url": base_url,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "read_only": True,
        "pages": pages,
        "apis": list(apis.values()),
        "errors": errors,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "output": str(out_path),
        "pages": len(pages),
        "apis": len(apis),
        "errors": len(errors),
        "note": "只读探索完成；接口列表用于后续把页面查询编译成 API 直连。",
    }
