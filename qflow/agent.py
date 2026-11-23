"""原子 agent：一个系统 = 图谱 + 账号 + 登录态，对外三个动作。

    agent = agent.get("sunner_srm")
    agent.status()                      现在能不能查
    agent.run(capability_id, values)    执行一个能力，返回标准结果（qflow/runner.py）
    agent.ask("上个月扣了多少", state)   听懂一句话再执行（qflow/ask.py）
    登录见 qflow/login.py：登录成功后调 agent.save_session(cookies)

这里没有任何某个系统专用的代码。接入新系统 = 放一份通过校验的图谱 + 在页面上填账号。
以后的多系统串联、并联在这一层之上做：按顺序或同时调用几个 agent 的 run（docs/graph-spec.md G4，还没做）。

文件：
    systems/<id>/graph.json            随仓库发布的图谱
    data/systems/<id>/graph.json       页面导入的图谱（同 id 时优先）
    data/systems/<id>/account.json     账号密码，权限 600，不进仓库，不回传给页面
    data/systems/<id>/session.json     登录态（cookie），权限 600
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, build_opener

from . import ask as asking
from . import config, runner
from .dates import now_local

log = logging.getLogger(__name__)


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mask(name: str) -> str:
    return name if len(name) <= 4 else name[:2] + "*" * (len(name) - 4) + name[-2:]


class Agent:
    def __init__(self, graph_path: Path, store: Path):
        self.graph_path = Path(graph_path)
        self.graph = runner.load(self.graph_path)
        self.id = self.graph.system["id"]
        self.label = self.graph.system.get("label") or self.id
        self.store = Path(store) / self.id
        self.expired: str | None = None  # 最近一次被系统拒绝的原因；登录成功后清掉
        self.last_ok: datetime | None = None
        self._cache: dict = {}
        self._lock = threading.Lock()

    # ---- 账号：页面填一次，存在服务器上 ----
    def account(self) -> dict | None:
        saved = _read(self.store / "account.json")
        return saved if saved.get("username") and saved.get("password") else None

    def save_account(self, username: str, password: str) -> None:
        username, password = (username or "").strip(), password or ""
        if not username or not password:
            raise ValueError("账号和密码都要填")
        _write_private(self.store / "account.json", {"username": username, "password": password})
        self.clear_session()  # 换了账号，旧登录态不能再用

    def account_public(self) -> dict:
        saved = self.account()
        return {"saved": bool(saved), "username": _mask(saved["username"]) if saved else ""}

    # ---- 登录态 ----
    def cookies(self) -> list[dict]:
        return _read(self.store / "session.json").get("cookies") or []

    def save_session(self, cookies: list[dict]) -> None:
        _write_private(self.store / "session.json", {"saved_at": now_local().isoformat(timespec="seconds"), "cookies": cookies})
        self.expired = None
        self._cache.clear()

    def clear_session(self) -> None:
        (self.store / "session.json").unlink(missing_ok=True)
        self._cache.clear()

    def sender(self):
        """带着这个账号登录态的 send；同一个请求在 CACHE_TTL 秒内只真正发一次，避免连着追问时反复拉全量。"""
        jar = CookieJar()
        for c in self.cookies():
            domain = str(c.get("domain") or "")
            try:
                jar.set_cookie(Cookie(
                    0, c["name"], c.get("value") or "", None, False, domain, bool(domain), domain.startswith("."),
                    c.get("path") or "/", True, bool(c.get("secure")), c.get("expiry"), False, None, None, {},
                ))
            except (KeyError, TypeError):
                continue
        real = runner.http_sender(build_opener(HTTPCookieProcessor(jar)))

        def send(method, url, params, body_type, headers):
            key = (method, url, json.dumps(params, sort_keys=True, ensure_ascii=False), body_type)
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < config.CACHE_TTL:
                return hit[1]
            reply = real(method, url, params, body_type, headers)
            if reply.get("status") == 200:
                self._cache[key] = (time.monotonic(), reply)
            return reply

        return send

    # ---- 三个动作里的两个：执行、听懂后执行 ----
    def _with_login(self, call):
        """登录态失效时，能无人值守登录就登录后重试一次；不能就把失效抛给上层去请人。"""
        try:
            result = call()
        except runner.AuthExpired as exc:
            self.expired = str(exc)
            self._cache.clear()
            from . import login

            if not login.unattended(self):
                raise
            result = call()
        self.expired, self.last_ok = None, now_local()
        return result

    def run(self, capability_id: str, values: dict | None = None, now: datetime | None = None, limit: int = 50) -> dict:
        return self._with_login(lambda: runner.run(self.graph, capability_id, values, self.sender(), now=now, limit=limit))

    def ask(self, text: str, state: dict | None = None, now: datetime | None = None) -> dict:
        return self._with_login(lambda: asking.ask(self.graph, text, state, self.sender(), now=now))

    def check(self) -> bool:
        """登录态现在还管不管用：真调一次图谱里声明的检查接口（没声明就用第一个只读接口）。"""
        session = (self.graph.doc.get("auth") or {}).get("session") or {}
        op_id = (session.get("check") or {}).get("operation") or next(
            (o["id"] for o in self.graph.doc["operations"] if o.get("access") == "read"), None)
        if not op_id or not self.cookies():
            return False
        self._cache.clear()
        try:
            runner.fetch(self.graph, op_id, {}, self.sender(), max_pages=1)
        except runner.AuthExpired as exc:
            self.expired = str(exc)
            return False
        self.expired, self.last_ok = None, now_local()
        return True

    def status(self) -> dict:
        """no_account（还没填账号）/ needs_login（要登录）/ online。不发请求，只看已知的事实。"""
        if not self.account() and not self.cookies():
            state = "no_account"
        elif self.expired or not self.cookies():
            state = "needs_login"
        else:
            state = "online"
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.graph.system.get("base_url"),
            "status": state,
            "error": self.expired,
            "account": self.account_public(),
            "session_saved_at": _read(self.store / "session.json").get("saved_at"),
            "capabilities": [c.get("label") or c["id"] for c in self.graph.capabilities.values()],
        }


# ---------- 这台机器上接入了哪些系统 ----------

_agents: dict[str, Agent] = {}
_seen: dict[str, float] = {}
_registry_lock = threading.Lock()
problems: dict[str, str] = {}  # 图谱文件 → 为什么没加载


def store_dir() -> Path:
    return config.DATA_DIR / "systems"


def all() -> dict[str, Agent]:  # noqa: A001
    """扫一遍图谱目录；文件变了就重新加载。页面导入的排在后面，同 id 时覆盖随仓库发布的。"""
    paths = sorted(config.SYSTEMS_DIR.glob("*/graph.json")) + sorted(store_dir().glob("*/graph.json"))
    with _registry_lock:
        found: dict[str, Agent] = {}
        for path in paths:
            key, mtime = str(path), path.stat().st_mtime
            old = next((a for a in _agents.values() if str(a.graph_path) == key), None)
            if old is not None and _seen.get(key) == mtime:
                found[old.id] = old
                continue
            try:
                fresh = Agent(path, store_dir())
            except (runner.RunError, ValueError, KeyError) as exc:
                problems[key] = str(exc)
                log.warning("图谱没加载：%s：%s", path, exc)
                continue
            problems.pop(key, None)
            _seen[key] = mtime
            found[fresh.id] = fresh
        _agents.clear()
        _agents.update(found)
        return dict(_agents)


def get(system_id: str) -> Agent | None:
    return all().get(system_id)


def ask(text: str, state: dict | None = None, now: datetime | None = None) -> dict:
    """多个系统时先选系统再问。现在的选法很简单：谁的关键词对上就问谁，都对不上就接着上一问的系统。

    TODO(G4)：一句话要用到两个系统的数据时（先查 A 再查 B、两边汇总），在这里编排，agent 本身不用改。
    """
    agents = all()
    if not agents:
        return {"handled": False, "reply": "还没有接入任何系统。请先在管理页导入一份图谱。", "state": state or {}, "followups": []}
    state = state or {}
    chosen = next((a for a in agents.values() if asking.mentions(a.graph, text)), None) \
        or agents.get(str(state.get("system") or "")) or next(iter(agents.values()))
    try:
        out = chosen.ask(text, state if state.get("system") == chosen.id else None, now=now)
    except runner.AuthExpired:
        reply = (
            f"还没有填「{chosen.label}」的账号。请在页面上填一次，之后就能直接问了。"
            if chosen.status()["status"] == "no_account"
            else f"「{chosen.label}」的登录过期了，需要重新登录一次。请在页面上按提示登录后再问。"
        )
        return {"handled": True, "reply": reply, "state": state, "followups": [], "system": chosen.id, "needs_login": True}
    out["system"] = chosen.id
    out["state"] = {**(out.get("state") or {}), "system": chosen.id} if out.get("state") else state
    return out
