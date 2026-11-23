"""通用执行器：按 v1 图谱（docs/graph-spec.md）取数、过滤、计算、回答。

这里没有任何某个系统专用的代码。换一个系统 = 换一份图谱文件。

    graph = runner.load("systems/sunner_srm/graph.json")      # 校验不过不加载
    result = runner.run(graph, "cap.deduction.summary",
                        {"period": {"start": "2025-11-01", "end": "2025-11-30"}},
                        send=runner.http_sender(opener))

分工：听懂问题（哪个能力、条件是什么）在 understand / nlu；这里只负责执行。
数字全部由这里算，不经过任何模型。只执行 access=read 的 http 操作。

还没做的（见 docs/PLAN.md T2）：接进 voice.handle、按图谱 auth 登录（T5）、
一个能力串多个接口（G5）、从页面读数据（G1）。
"""

from __future__ import annotations

import calendar
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import online, spec
from .dates import now_local, to_date_str

MAX_PAGES = 50
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class RunError(ValueError):
    """图谱或参数有问题，执行不了。"""


class AuthExpired(RuntimeError):
    """登录态失效，需要重新登录（由上层走登录接力）。"""


class SystemGraph:
    def __init__(self, doc: dict):
        self.doc = doc
        self.system = doc["system"]
        self.entities = {e["id"]: e for e in doc["entities"]}
        self.operations = {o["id"]: o for o in doc["operations"]}
        self.capabilities = {c["id"]: c for c in doc["capabilities"]}

    def field(self, entity_id: str, name: str) -> dict:
        for field in self.entities[entity_id]["fields"]:
            if field["name"] == name:
                return field
        return {"name": name, "type": "string"}


def load(path) -> SystemGraph:
    """读一份 v1 图谱。校验有任何一条错误就不加载。"""
    result = spec.validate_file(path)
    if not result["ok"]:
        first = result["errors"][0]
        raise RunError(f"图谱没通过校验（共 {len(result['errors'])} 条错误），第一条：{first['where']} {first['msg']}")
    return SystemGraph(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------- 发请求 ----------


def http_sender(opener=None, timeout: float = 30.0):
    """真正发 HTTP 的 send。opener 里带着这个用户的登录 cookie。"""
    opener = opener or urllib.request.build_opener()

    def send(method: str, url: str, params: dict, body_type: str, headers: dict) -> dict:
        data = None
        if method == "GET":
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        elif body_type == "json":
            data = json.dumps(params, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json", **headers}
        else:
            data = urllib.parse.urlencode(params).encode("utf-8")
            headers = {"Content-Type": "application/x-www-form-urlencoded", **headers}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                return {
                    "status": response.status,
                    "url": response.geturl(),
                    "text": response.read().decode("utf-8", "replace"),
                }
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "url": url, "text": ""}

    return send


def opener_from_cookie_file(path):
    """读登录接力存下来的 cookie 文件（{"cookies": [{name, value, domain, path}]}）。"""
    from http.cookiejar import Cookie, CookieJar

    jar = CookieJar()
    for c in json.loads(Path(path).read_text(encoding="utf-8")).get("cookies") or []:
        domain = str(c.get("domain") or "")
        jar.set_cookie(Cookie(
            0, c["name"], c["value"], None, False, domain, bool(domain), domain.startswith("."),
            c.get("path") or "/", True, bool(c.get("secure")), c.get("expiry"), False, None, None, {},
        ))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _check_expired(graph: SystemGraph, reply: dict) -> None:
    rules = ((graph.doc.get("auth") or {}).get("session") or {}).get("expired_when") or []
    for rule in rules:
        if "status" in rule and reply.get("status") == rule["status"]:
            raise AuthExpired(f"HTTP {reply.get('status')}")
        if rule.get("redirect_contains") and rule["redirect_contains"] in str(reply.get("url") or ""):
            raise AuthExpired("被转到了登录页")
    if reply.get("status") in (401, 403):
        raise AuthExpired(f"HTTP {reply.get('status')}")


def _literal(text: str):
    text = text.strip()
    if text in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[text]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def holds(expr: str | None, payload) -> bool:
    """`$.success == true` 这类判断。只支持 == 和 !=。"""
    if not expr:
        return True
    for op in ("==", "!="):
        if op in expr:
            left, right = expr.split(op, 1)
            same = spec.jpath(payload, left.strip()) == _literal(right)
            return same if op == "==" else not same
    return bool(spec.jpath(payload, expr.strip()))


# ---------- 取数 ----------


def _coerce(value, kind: str):
    if value is None or value == "":
        return None
    if kind in ("number", "money"):
        try:
            return float(str(value).replace(",", "").strip())
        except ValueError:
            return None
    if kind == "date":
        return to_date_str(value) or str(value).strip()[:10]
    if kind == "bool":
        return bool(value)
    if kind in ("string", "enum", "datetime"):
        return str(value).strip() or None
    return value


def _map_row(graph: SystemGraph, entity_id: str, mapping: dict, raw: dict) -> dict:
    row = {}
    for name, paths in mapping.items():
        value = None
        for path in paths if isinstance(paths, list) else [paths]:  # 写成列表 = 依次取，第一个有值的算
            value = spec.jpath(raw, path)
            if value not in (None, ""):
                break
        row[name] = _coerce(value, graph.field(entity_id, name).get("type", "string"))
    return row


def fetch(graph: SystemGraph, op_id: str, params: dict, send, max_pages: int | None = None) -> tuple[list[dict], dict]:
    """调一个操作并翻完所有页。返回（按实体字段名整理好的记录，取数过程）。"""
    op = graph.operations.get(op_id)
    if op is None:
        raise RunError(f"图谱里没有操作 {op_id}")
    if op.get("kind") != "http" or op.get("access") != "read":
        raise RunError(f"{op_id} 不是只读的 http 操作，本地不执行")
    request, response = op["request"], op["response"]
    method = str(request["method"]).upper()
    url = graph.system["base_url"].rstrip("/") + request["path"]
    online.assert_readonly_request(method, url)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": _UA,
        "Origin": graph.system["base_url"].rstrip("/"),
        **(request.get("headers") or {}),
    }
    paging = op.get("pagination") or {"style": "none"}
    paged = paging.get("style") == "page"
    entity = graph.entities[response["entity"]]
    key_fields = entity.get("key") or []
    required = [f["name"] for f in entity["fields"] if f.get("required")]

    rows, seen, total, pages, received, complete = [], set(), None, 0, 0, False
    for page in range(1, (max_pages or MAX_PAGES) + 1):
        body = dict(params)
        if paged:
            body[paging["page_param"]] = page
            body[paging["size_param"]] = paging.get("max_size", 100)
        reply = send(method, url, body, request.get("body_type", "form"), headers)
        pages += 1
        _check_expired(graph, reply)
        if int(reply.get("status") or 0) >= 400:
            raise RunError(f"{op_id} 返回 HTTP {reply.get('status')}")
        try:
            payload = json.loads(reply.get("text") or "")
        except json.JSONDecodeError as exc:
            raise AuthExpired("返回的不是 JSON，登录态可能已失效") from exc
        if not holds(response.get("ok_when"), payload):
            raise RunError(f"{op_id} 返回失败：{str(payload)[:120]}")
        batch = spec.jpath(payload, response["rows_path"]) or []
        received += len(batch)
        for raw in batch:
            row = _map_row(graph, response["entity"], response["map"], raw)
            if any(row.get(name) is None for name in required):
                continue  # 必填字段取不到的记录不算数（例如没有金额的空行）
            key = tuple(row.get(k) for k in key_fields)
            if key_fields and all(v is not None for v in key):
                if key in seen:
                    continue
                seen.add(key)
            rows.append(row)
        if not paged or not batch:
            complete = True
            break
        if total is None and paging.get("total_path"):
            total = spec.jpath(payload, paging["total_path"])
        # 按实际收到的条数判断，不按请求的每页条数：有的系统会把每页条数压到自己的上限
        if total is not None and received >= int(total):
            complete = True
            break
    return rows, {"operation": op_id, "requests": pages, "rows": len(rows), "reported_total": total,
                  "complete": complete}


# ---------- 条件 ----------


def default_range(name: str, now: datetime | None = None) -> dict | None:
    today = (now or now_local()).date()
    if name == "this_month":
        return {"start": today.replace(day=1).isoformat(), "end": today.isoformat()}
    if name == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return {"start": last.replace(day=1).isoformat(), "end": last.isoformat()}
    if name == "this_year":
        return {"start": today.replace(month=1, day=1).isoformat(), "end": today.isoformat()}
    if name == "last_year":
        year = today.year - 1
        return {"start": f"{year}-01-01", "end": f"{year}-12-{calendar.monthrange(year, 12)[1]}"}
    return None


def _apply_slots(capability: dict, values: dict, now) -> tuple[dict, list, dict]:
    """把条件分成三份：传给接口的参数、本地过滤条件、给回答用的文字。"""
    params, filters, shown = {}, [], {}
    for slot in capability.get("slots") or []:
        name, kind = slot["name"], slot.get("type", "string")
        value = values.get(name)
        if value in (None, "", {}):
            value = default_range(str(slot.get("default") or ""), now) if kind == "date_range" else slot.get("default")
        if value in (None, "", {}):
            if slot.get("required"):
                raise RunError(f"缺少条件：{slot.get('label') or name}")
            continue
        if kind == "date_range":
            start, end = value.get("start"), value.get("end")
            shown[name] = f"{start} 至 {end}" if start and end else (start or end or "")
            if slot.get("to"):
                if start:
                    params[slot["to"]["start"]] = start
                if end:
                    params[slot["to"]["end"]] = end
            else:  # 接口不支持按时间查：全部取回来在本地过滤
                filters.append(("range", slot["field"], start, end))
        else:
            shown[name] = str(value)
            if slot.get("to"):
                params[slot["to"]] = value
            else:
                filters.append(("contains" if slot.get("match") == "contains" else "equals", slot["field"], value, None))
    return params, filters, shown


def _keep(row: dict, filters: list) -> bool:
    for kind, field, a, b in filters:
        value = row.get(field)
        if kind == "range":
            if value is None or (a and str(value) < a) or (b and str(value) > b):
                return False
        elif kind == "contains":
            if value is None or str(a).upper() not in str(value).upper():
                return False
        elif value is None or str(value).upper() != str(a).upper():
            return False
    return True


# ---------- 计算和回答 ----------


def _metric(rows: list[dict], metric: dict):
    if metric["fn"] == "count":
        return len(rows)
    values = [row[metric["field"]] for row in rows if isinstance(row.get(metric["field"]), (int, float))]
    if not values:
        return 0 if metric["fn"] == "sum" else None
    result = {"sum": sum, "min": min, "max": max, "avg": lambda v: sum(v) / len(v)}[metric["fn"]](values)
    return round(result, 2)


def compute(rows: list[dict], spec_: dict) -> dict:
    metrics = spec_.get("metrics") or []
    totals = {m["key"]: _metric(rows, m) for m in metrics}
    groups = []
    group_by = spec_.get("group_by") or []
    if group_by:
        buckets: dict[tuple, list] = {}
        for row in rows:
            buckets.setdefault(tuple(row.get(f) for f in group_by), []).append(row)
        for key, members in buckets.items():
            groups.append({**dict(zip(group_by, key)), **{m["key"]: _metric(members, m) for m in metrics}})
        order = next((m["key"] for m in metrics if m["fn"] == "sum"), metrics[0]["key"] if metrics else None)
        if order:
            groups.sort(key=lambda g: (-(g[order] or 0), str([g.get(f) for f in group_by])))
    return {"totals": totals, "groups": groups}


def _show(value, unit: str | None) -> str:
    if isinstance(value, float) or unit == "元":
        return f"{float(value or 0):,.2f}"
    return "—" if value is None else str(value)


def run(graph: SystemGraph, capability_id: str, values: dict | None, send, now: datetime | None = None,
        limit: int = 50) -> dict:
    """执行一个能力。values 是已经听懂的条件，例如 {"period": {"start": ..., "end": ...}, "item": "水分"}。"""
    capability = graph.capabilities.get(capability_id)
    if capability is None:
        raise RunError(f"图谱里没有能力 {capability_id}")
    if capability.get("access", "read") != "read":
        raise RunError(f"{capability_id} 不是只读能力，本地不执行")
    params, filters, shown = _apply_slots(capability, values or {}, now)
    fetched, trace = fetch(graph, capability["operation"], params, send)
    rows = [row for row in fetched if _keep(row, filters)]

    result = {
        "ok": True,
        "system": graph.system["id"],
        "capability": capability_id,
        "label": capability.get("label"),
        "conditions": shown,
        "count": len(rows),
        "trace": {**trace, "sent_params": params, "local_filters": [f[1] for f in filters]},
    }
    words = dict(shown)
    for slot in capability.get("slots") or []:
        words.setdefault(slot["name"], "全部")
    if capability.get("compute"):
        computed = compute(rows, capability["compute"])
        result.update(computed)
        result["groups"] = computed["groups"][:limit]
        for metric in capability["compute"].get("metrics") or []:
            words[metric["key"]] = _show(computed["totals"][metric["key"]], metric.get("unit"))
    else:
        result["rows"] = rows[:limit]
    words.setdefault("count", str(len(rows)))
    if not rows:
        scope = "，".join(v for v in shown.values() if v)
        result["answer"] = f"没有查到符合条件的数据{f'（{scope}）' if scope else ''}。"
    else:
        try:
            result["answer"] = capability["answer"].format(**words)
        except (KeyError, IndexError) as exc:
            raise RunError(f"{capability_id} 的回答模板引用了没有的名字：{exc}") from exc
    if not trace["complete"]:  # 没取完就不能当成完整结果报出去
        result["answer"] += f"注意：数据太多，只取了前 {trace['rows']} 条，这个结果不完整。"
    return result
