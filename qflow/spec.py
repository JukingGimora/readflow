"""系统图谱标准 v1 的校验器（docs/graph-spec.md 第 8 节第 1–3、6 条）。

只做静态检查，不联网、不登录。第 4 条（示例问法能否对回能力）和第 5 条（真实试跑）
需要本地模型和登录态，还没做，见 docs/PLAN.md。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import online

SPEC_VERSION = "1.0"
FIELD_TYPES = {"string", "number", "money", "date", "datetime", "enum", "bool"}
SLOT_TYPES = FIELD_TYPES | {"date_range"}
CHALLENGE_TYPES = {"image_captcha", "sms_code", "slider", "qr", "sso"}
SUCCESS_KEYS = {"url_contains", "url_not_contains", "present", "absent"}
METRIC_FNS = {"sum", "count", "avg", "min", "max"}
ACCESS = {"read", "write", "irreversible"}
SECRET_KEYS = re.compile(r"cookie|authorization|passw(or)?d|token|secret|session_?id", re.I)
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PATH_STEP = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def jpath(obj, path: str):
    """极简 JSONPath：只支持 `$`、`.name`、`[n]`。取不到返回 None。"""
    if not isinstance(path, str) or not path.startswith("$"):
        return None
    rest, pos = path[1:], 0
    while pos < len(rest):
        m = _PATH_STEP.match(rest, pos)
        if not m:
            return None
        if m.group(1) is not None:
            if not isinstance(obj, dict) or m.group(1) not in obj:
                return None
            obj = obj[m.group(1)]
        else:
            idx = int(m.group(2))
            if not isinstance(obj, list) or idx >= len(obj):
                return None
            obj = obj[idx]
        pos = m.end()
    return obj


class _Report:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def err(self, where: str, msg: str):
        self.errors.append({"where": where, "msg": msg})

    def warn(self, where: str, msg: str):
        self.warnings.append({"where": where, "msg": msg})


def _is_list_of_dicts(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, dict) for x in value)


def _find_secret_keys(obj, prefix: str = ""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{prefix}.{key}" if prefix else str(key)
            if SECRET_KEYS.search(str(key)):
                yield here
            yield from _find_secret_keys(value, here)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _find_secret_keys(value, f"{prefix}[{i}]")


def validate(doc) -> dict:
    r = _Report()
    if not isinstance(doc, dict):
        r.err("$", "图谱必须是一个 JSON 对象")
        return _result(r, {})

    # ---- 顶层结构 ----
    if doc.get("spec_version") != SPEC_VERSION:
        r.err("spec_version", f"必须是 \"{SPEC_VERSION}\"")
    system = doc.get("system") if isinstance(doc.get("system"), dict) else {}
    for key in ("id", "label", "base_url"):
        if not system.get(key):
            r.err(f"system.{key}", "必填")
    generated = doc.get("generated") if isinstance(doc.get("generated"), dict) else {}
    for key in ("at", "by", "account_role"):
        if not generated.get(key):
            r.err(f"generated.{key}", "必填")
    if not isinstance(doc.get("auth"), dict):
        r.err("auth", "必填，说明怎么登录")
    sections = {}
    for name in ("navigation", "entities", "operations", "capabilities"):
        value = doc.get(name)
        if not _is_list_of_dicts(value):
            r.err(name, "必须是对象数组")
            value = []
        sections[name] = value

    # ---- id 唯一 ----
    seen: dict[str, str] = {}
    index: dict[str, dict[str, dict]] = {}
    for name, items in sections.items():
        index[name] = {}
        for i, item in enumerate(items):
            item_id = item.get("id")
            where = f"{name}[{i}]"
            if not item_id or not isinstance(item_id, str):
                r.err(where, "缺少 id")
                continue
            if item_id in seen:
                r.err(where, f"id 重复：{item_id}（已在 {seen[item_id]} 出现）")
                continue
            seen[item_id] = where
            index[name][item_id] = item
    navs, entities, ops, caps = (index[n] for n in ("navigation", "entities", "operations", "capabilities"))

    # ---- entities ----
    fields_of: dict[str, set] = {}
    for eid, entity in entities.items():
        names = set()
        fields = entity.get("fields")
        if not _is_list_of_dicts(fields) or not fields:
            r.err(eid, "fields 必须是非空数组")
            fields = []
        for field in fields:
            fname = field.get("name")
            if not fname:
                r.err(eid, "有字段缺少 name")
                continue
            if fname in names:
                r.err(eid, f"字段名重复：{fname}")
            names.add(fname)
            if field.get("type") not in FIELD_TYPES:
                r.err(f"{eid}.{fname}", f"type 必须是 {sorted(FIELD_TYPES)} 之一")
            if field.get("type") == "enum" and not field.get("values"):
                r.err(f"{eid}.{fname}", "enum 字段必须列出 values")
            if not field.get("label"):
                r.warn(f"{eid}.{fname}", "没有 label（中文名），用户的说法对不上")
        for key in entity.get("key") or []:
            if key not in names:
                r.err(eid, f"key 引用了不存在的字段：{key}")
        fields_of[eid] = names

    # ---- operations ----
    base_url = str(system.get("base_url") or "").rstrip("/")
    params_of: dict[str, set] = {}
    for oid, op in ops.items():
        access = op.get("access")
        if access not in ACCESS:
            r.err(oid, f"access 必须是 {sorted(ACCESS)} 之一")
        if op.get("kind") != "http":
            r.err(oid, "v1 只支持 kind: \"http\"（其它情况记进 navigation[].not_queryable，见第 10 节）")
        request = op.get("request") if isinstance(op.get("request"), dict) else {}
        method, path = str(request.get("method") or "").upper(), str(request.get("path") or "")
        if not method or not path.startswith("/"):
            r.err(oid, "request.method 和以 / 开头的 request.path 必填")
        elif access == "read":
            try:
                online.assert_readonly_request(method, base_url + path)
            except online.ReadOnlyViolation as exc:
                r.err(oid, f"标成了 read，但没通过本地只读检查：{exc}")
        params = request.get("params") if _is_list_of_dicts(request.get("params")) else []
        params_of[oid] = {p.get("name") for p in params if p.get("name")}
        pagination = op.get("pagination")
        if isinstance(pagination, dict):
            for key in ("page_param", "size_param", "total_path"):
                if not pagination.get(key):
                    r.err(oid, f"pagination.{key} 必填")
        elif pagination is not None:
            r.err(oid, "pagination 必须是对象；不翻页就省略")

        response = op.get("response") if isinstance(op.get("response"), dict) else {}
        entity_id = response.get("entity")
        mapping = response.get("map") if isinstance(response.get("map"), dict) else {}
        if not response.get("rows_path"):
            r.err(oid, "response.rows_path 必填")
        if entity_id not in entities:
            r.err(oid, f"response.entity 引用了不存在的实体：{entity_id}")
        if not mapping:
            r.err(oid, "response.map 必填")
        for fname in mapping:
            if entity_id in fields_of and fname not in fields_of[entity_id]:
                r.err(oid, f"map 里的 {fname} 不是 {entity_id} 的字段")
        for fname in sorted(fields_of.get(entity_id, set()) - set(mapping)):
            r.warn(oid, f"{entity_id} 的字段 {fname} 没有出现在 map 里，这个接口取不到它")

        # ---- 证据：样例响应按 rows_path 和 map 能取出声明的字段 ----
        evidence = op.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("verified_at"):
            r.err(oid, "缺少 evidence（真实调通的时间和脱敏样例）；没有证据的操作不加载")
            continue
        sample = evidence.get("sample") if isinstance(evidence.get("sample"), dict) else {}
        if not isinstance(sample.get("request"), dict) or not isinstance(sample.get("response"), (dict, list)):
            r.err(oid, "evidence.sample 必须同时有 request 和 response")
            continue
        for leaked in _find_secret_keys(sample):
            r.err(oid, f"evidence 里出现了疑似凭据的键：{leaked}")
        rows = jpath(sample["response"], str(response.get("rows_path") or ""))
        if not isinstance(rows, list) or not rows:
            r.err(oid, f"按 rows_path {response.get('rows_path')!r} 在样例响应里取不到非空列表")
            continue
        for fname, fpath in mapping.items():
            # 写成列表 = 依次取，第一个有值的算（同一个字段在不同记录里放在不同的键上）
            paths = fpath if isinstance(fpath, list) else [fpath]
            if all(jpath(row, path) is None for row in rows for path in paths):
                r.err(oid, f"样例响应的每一行按 {fpath!r} 都取不到 {fname}")
        if pagination and jpath(sample["response"], str(pagination.get("total_path") or "")) is None:
            r.err(oid, f"样例响应里按 total_path {pagination.get('total_path')!r} 取不到总数")
        ok_when = str(response.get("ok_when") or "")
        m = re.fullmatch(r"\s*(\$[^\s=]*)\s*==\s*(.+?)\s*", ok_when)
        if not ok_when:
            r.warn(oid, "没有 response.ok_when，本地无法判断请求是否成功")
        elif not m:
            r.err(oid, f"ok_when 只支持「路径 == 值」的写法：{ok_when!r}")
        else:
            try:
                expected = json.loads(m.group(2))
            except json.JSONDecodeError:
                expected = m.group(2).strip("'\"")
            if jpath(sample["response"], m.group(1)) != expected:
                r.err(oid, f"样例响应不满足 ok_when：{ok_when!r}")

    # ---- auth：登录地址必填；步骤写了就得写全，没写的系统只能由人在浏览器窗口里登录 ----
    auth = doc.get("auth") if isinstance(doc.get("auth"), dict) else {}
    if auth:
        if not isinstance(auth.get("login_url"), str) or not auth.get("login_url"):
            r.err("auth.login_url", "必填：登录页的地址")
        steps = auth.get("steps") or []
        if not _is_list_of_dicts(steps):
            r.err("auth.steps", "必须是对象数组")
            steps = []
        if not steps:
            r.warn("auth.steps", "没有登录步骤：本地不会自动登录，每次都要人在浏览器窗口里登录")
        for i, step in enumerate(steps):
            where, kind = f"auth.steps[{i}]", step.get("kind")
            if kind not in ("input", "challenge", "submit"):
                r.err(where, "kind 必须是 input / challenge / submit 之一")
            elif kind == "input":
                if step.get("field") not in ("username", "password"):
                    r.err(where, "field 必须是 username 或 password（值来自用户在页面上保存的账号）")
                if not step.get("locator"):
                    r.err(where, "缺少 locator（CSS 选择器）")
            elif kind == "submit" and not step.get("locator"):
                r.err(where, "缺少 locator（CSS 选择器）")
            elif kind == "challenge":
                if step.get("type") not in CHALLENGE_TYPES:
                    r.err(where, f"type 必须是 {sorted(CHALLENGE_TYPES)} 之一")
                elif step["type"] == "image_captcha" and not (step.get("locator") and step.get("answer_locator")):
                    r.err(where, "图片验证码要写 locator（图片）和 answer_locator（输入框）")
        success = auth.get("success")
        if success is not None and (not isinstance(success, dict) or not success or set(success) - SUCCESS_KEYS):
            r.err("auth.success", f"只能用这些键：{sorted(SUCCESS_KEYS)}")
        session = auth.get("session") if isinstance(auth.get("session"), dict) else {}
        check_op = (session.get("check") or {}).get("operation") if isinstance(session.get("check"), dict) else None
        if check_op and (check_op not in index["operations"] or index["operations"][check_op].get("access") != "read"):
            r.err("auth.session.check", f"operation 必须是一个已声明的只读操作：{check_op}")
        if session.get("carrier", "cookie") != "cookie":
            r.warn("auth.session.carrier", "本地目前只会取 cookie 里的登录态")

    # ---- capabilities ----
    for cid, cap in caps.items():
        if cap.get("access") != "read":
            r.err(cid, "v1 只加载 access 为 read 的能力")
        op = ops.get(cap.get("operation"))
        if op is None:
            r.err(cid, f"operation 引用了不存在的操作：{cap.get('operation')}")
        elif op.get("access") != "read":
            r.err(cid, f"指向的操作 {cap.get('operation')} 不是 read")
        if cap.get("nav") not in navs:
            r.err(cid, f"nav 引用了不存在的菜单：{cap.get('nav')}")
        entity_id = ((op or {}).get("response") or {}).get("entity") if isinstance((op or {}).get("response"), dict) else None
        fields, params = fields_of.get(entity_id, set()), params_of.get(cap.get("operation"), set())
        names = set()
        for slot in cap.get("slots") if _is_list_of_dicts(cap.get("slots")) else []:
            sname = slot.get("name")
            if not sname:
                r.err(cid, "有 slot 缺少 name")
                continue
            names.add(sname)
            if slot.get("pattern"):
                try:
                    re.compile(str(slot["pattern"]))
                except re.error as exc:
                    r.err(f"{cid}.{sname}", f"pattern 不是合法的正则：{exc}")
            if slot.get("type") not in SLOT_TYPES:
                r.err(f"{cid}.{sname}", f"slot type 必须是 {sorted(SLOT_TYPES)} 之一")
            to = slot.get("to")
            targets = list(to.values()) if isinstance(to, dict) else [to] if isinstance(to, str) else []
            for target in targets:
                if target not in params:
                    r.err(f"{cid}.{sname}", f"to 指向了操作里不存在的参数：{target}")
            if slot.get("filter") == "local" and slot.get("field") not in fields:
                r.err(f"{cid}.{sname}", f"本地过滤的 field 不是 {entity_id} 的字段：{slot.get('field')}")
            if not targets and slot.get("filter") != "local":
                r.err(f"{cid}.{sname}", "既没有 to（传给接口）也没有 filter: local（本地过滤），这个条件不会生效")
        compute = cap.get("compute")
        if isinstance(compute, dict):
            for group in compute.get("group_by") or []:
                if group not in fields:
                    r.err(cid, f"group_by 的 {group} 不是 {entity_id} 的字段")
            for metric in compute.get("metrics") if _is_list_of_dicts(compute.get("metrics")) else []:
                key = metric.get("key")
                if not key:
                    r.err(cid, "有 metric 缺少 key")
                    continue
                names.add(key)
                if metric.get("fn") not in METRIC_FNS:
                    r.err(f"{cid}.{key}", f"fn 必须是 {sorted(METRIC_FNS)} 之一")
                if metric.get("fn") != "count" and metric.get("field") not in fields:
                    r.err(f"{cid}.{key}", f"field 不是 {entity_id} 的字段：{metric.get('field')}")
        for placeholder in _PLACEHOLDER.findall(str(cap.get("answer") or "")):
            if placeholder not in names and placeholder != "count":  # count = 查到的条数，总是可用
                r.err(cid, f"answer 里的 {{{placeholder}}} 不是 slots 或 metrics 里的名字")
        examples, followups = cap.get("examples") or [], cap.get("followups") or []
        if len(examples) < 3:
            r.err(cid, "examples 至少 3 条")
        if len(followups) < 1:
            r.err(cid, "followups 至少 1 条")

    # ---- navigation 与覆盖率 ----
    coverage = {"leaves": 0, "with_capability": 0, "not_queryable": 0, "neither": []}
    cap_navs = {cap.get("nav") for cap in caps.values()}
    for nid, nav in navs.items():
        if not isinstance(nav.get("path"), list) or not nav.get("path"):
            r.err(nid, "path 必须是非空数组（一级 > 二级 > 三级）")
        listed = nav.get("capabilities") or []
        for cid in listed:
            if cid not in caps:
                r.err(nid, f"capabilities 引用了不存在的能力：{cid}")
            elif caps[cid].get("nav") != nid:
                r.err(nid, f"能力 {cid} 的 nav 没有指回这个菜单")
        coverage["leaves"] += 1
        if listed:
            coverage["with_capability"] += 1
            if nav.get("not_queryable"):
                r.err(nid, "既挂了能力又标了 not_queryable")
        elif nav.get("not_queryable"):
            coverage["not_queryable"] += 1
            if not (isinstance(nav["not_queryable"], dict) and nav["not_queryable"].get("reason")):
                r.err(nid, "not_queryable 必须写 reason")
        else:
            coverage["neither"].append(nid)
            r.warn(nid, "既没有能力也没有写 not_queryable 的原因，探索不完整")
        if nid in cap_navs and not listed:
            r.err(nid, "有能力指向这个菜单，但它的 capabilities 里没列")
    return _result(r, coverage)


def _result(report: _Report, coverage: dict) -> dict:
    return {"ok": not report.errors, "errors": report.errors, "warnings": report.warnings, "coverage": coverage}


def validate_file(path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "errors": [{"where": "$", "msg": f"读不了文件：{exc}"}], "warnings": [], "coverage": {}}
    except json.JSONDecodeError as exc:
        return {"ok": False, "errors": [{"where": "$", "msg": f"不是合法 JSON：{exc}"}], "warnings": [], "coverage": {}}
    return validate(doc)


def format_report(result: dict) -> str:
    lines = ["通过" if result["ok"] else f"不通过：{len(result['errors'])} 个错误"]
    lines += [f"  错误 {e['where']}: {e['msg']}" for e in result["errors"]]
    lines += [f"  提醒 {w['where']}: {w['msg']}" for w in result["warnings"]]
    cov = result.get("coverage") or {}
    if cov.get("leaves"):
        lines.append(
            f"覆盖率：{cov['leaves']} 个叶子页面，有能力 {cov['with_capability']}，"
            f"标了不可查 {cov['not_queryable']}，两者都没有 {len(cov['neither'])}"
        )
    return "\n".join(lines)
