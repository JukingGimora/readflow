"""能力注册表 + 意图分发 + voiceAI 工具描述.

voiceAI 有两种接法：
1) 直接 POST /query {"text": "去年11月扣款合计"} —— 本模块兜底解析；
2) 用 /tools 的 function schema 让 voiceAI 先做意图/参数抽取，再 POST
   {"intent": "deduction.summary", "params": {"month": "2025-11"}}。
"""
from __future__ import annotations

import copy
import time

from . import answer, engine, nlu
from .dates import resolve_range
from .graph import Graph, GraphError

INTENTS = (
    "deduction.summary",
    "deduction.detail",
    "shipment.summary",
    "shipment.detail",
    "reconcile",
    "overview",
)

ALLOWED_PARAMS = {"start", "end", "month", "days", "asn", "plate", "item", "group_by", "limit"}

_COMMON_PROPS = {
    "start": {"type": "string", "description": "开始日期，YYYY-MM-DD，含当天"},
    "end": {"type": "string", "description": "结束日期，YYYY-MM-DD，含当天"},
    "month": {"type": "string", "description": "月份，YYYY-MM；与 start/end 二选一"},
    "days": {"type": "integer", "minimum": 1, "description": "最近 N 天"},
    "asn": {"type": "string", "description": "发货/收货单号，如 ASN2511210602"},
    "plate": {"type": "string", "description": "车牌号，如 宁D56908"},
    "item": {"type": "string", "description": "检测项目，如 水分、杂质、玉米赤霉烯酮"},
    "group_by": {
        "type": "string",
        "enum": ["item", "month", "asn", "plate", "dataset"],
        "description": "汇总维度：扣款用 item，发货用 month/plate",
    },
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "返回条数"},
}

CAPABILITIES = [
    {
        "intent": "deduction.summary",
        "title": "扣款汇总",
        "read_only": True,
        "description": "按检测项目/月份/单号汇总扣款笔数与金额，适合回答“扣了多少钱”“哪项扣得多”。",
        "params": {"type": "object", "properties": copy.deepcopy(_COMMON_PROPS), "required": []},
        "examples": ["去年11月扣款合计", "这个月水分扣了多少钱", "按项目统计扣款"],
    },
    {
        "intent": "deduction.detail",
        "title": "扣款明细",
        "read_only": True,
        "description": "列出每笔扣款的单号、日期、检测项目、检测值、扣罚金额。",
        "params": {"type": "object", "properties": copy.deepcopy(_COMMON_PROPS), "required": []},
        "examples": ["查一下上个月扣款明细", "ASN2511210602 扣了哪些项目", "11月杂质扣款列表"],
    },
    {
        "intent": "shipment.summary",
        "title": "发货汇总",
        "read_only": True,
        "description": "按月份/车牌汇总发货单数、实收吨数与含税金额。",
        "params": {"type": "object", "properties": copy.deepcopy(_COMMON_PROPS), "required": []},
        "examples": ["今年发货多少吨", "按月统计发货金额", "11月发了多少车"],
    },
    {
        "intent": "shipment.detail",
        "title": "发货明细",
        "read_only": True,
        "description": "列出每单的日期、单号、车牌、实收数量、含税金额。",
        "params": {"type": "object", "properties": copy.deepcopy(_COMMON_PROPS), "required": []},
        "examples": ["查宁D56908的发货记录", "11月发货明细", "ASN2511240665 这单信息"],
    },
    {
        "intent": "reconcile",
        "title": "发货-扣款对账",
        "read_only": True,
        "description": "按单号对齐发货金额与扣款金额，给出净额，并列出无发货/无扣款的异常单。",
        "params": {"type": "object", "properties": copy.deepcopy(_COMMON_PROPS), "required": []},
        "examples": ["2025年11月对账", "上个月扣完还剩多少", "这个月差额多少"],
    },
    {
        "intent": "overview",
        "title": "数据概览",
        "read_only": True,
        "description": "本地已导入的数据量、时间范围、数据集与数据截止时间。",
        "params": {"type": "object", "properties": {}, "required": []},
        "examples": ["数据概览", "现在有哪些数据"],
    },
]

CAPABILITY_BY_INTENT = {item["intent"]: item for item in CAPABILITIES}


def _norm_params(params: dict | None) -> dict:
    clean: dict = {}
    for key, value in (params or {}).items():
        if key not in ALLOWED_PARAMS or value in (None, ""):
            continue
        clean[key] = value
    if "asn" in clean:
        clean["asn"] = str(clean["asn"]).strip().upper()
    if "plate" in clean:
        clean["plate"] = str(clean["plate"]).strip().upper()
    if "item" in clean:
        clean["item"] = str(clean["item"]).strip()
    if "limit" in clean:
        clean["limit"] = max(1, min(int(clean["limit"]), 500))
    if "days" in clean:
        clean["days"] = max(1, min(int(clean["days"]), 3660))
    return clean


def _dispatch_live(intent: str, clean: dict, now=None) -> dict:
    """实时模式：登录 SRM 现场读取，内存汇总，不落库。"""
    from . import livequery

    client = livequery.get_client()
    try:
        return _dispatch_live_with_client(intent, clean, now, client)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"实时读取失败：{exc}") from exc  # noqa: B904


def _dispatch_live_with_client(intent: str, clean: dict, now, client) -> dict:
    if intent == "overview":
        overview = client.overview()
        return {
            "mode": "live",
            "shipments": {
                "count": 0, "total_qty": None, "total_amount": None,
                "min_date": None, "max_date": None,
            },
            "deductions": {
                "count": overview["count"],
                "total_amount": overview["total_amount"],
                "min_date": overview["min_date"],
                "max_date": overview["max_date"],
            },
            "datasets": [
                {"dataset": "live:srm", "rows_written": overview["count"]}
            ],
            "last_sync": overview["live_fetched_at"],
            "live_fetched_at": overview["live_fetched_at"],
            "page_total": overview["page_total"],
        }
    start, end, _ = resolve_range(clean, now)
    common = {"start": start, "end": end}
    if intent == "deduction.summary":
        group_by = clean.get("group_by", "item")
        if group_by not in ("item", "month", "asn"):
            raise ValueError("deduction.summary 的 group_by 仅支持: item / month / asn")
        return client.summary(
            **common,
            asn=clean.get("asn"),
            item=clean.get("item"),
            group_by=group_by,
            limit=clean.get("limit", 20),
        )
    if intent == "deduction.detail":
        return client.detail(
            **common,
            asn=clean.get("asn"),
            item=clean.get("item"),
            limit=clean.get("limit", 50),
        )
    raise ValueError(
        "实时查询目前支持：扣款汇总 / 扣款明细 / 数据概览；发货与对账还未接实时。"
    )


def dispatch(conn, intent: str, params: dict | None = None, now=None, live: bool = False) -> dict:
    if intent not in CAPABILITY_BY_INTENT:
        raise ValueError(f"未知 intent: {intent}; 可选: {', '.join(INTENTS)}")
    clean = _norm_params(params)
    if live:
        return _dispatch_live(intent, clean, now)
    if intent == "overview":
        return engine.overview(conn)

    start, end, _ = resolve_range(clean, now)
    common = {"start": start, "end": end}

    if intent == "deduction.summary":
        group_by = clean.get("group_by", "item")
        if group_by not in engine.DED_GROUP:
            raise ValueError(f"deduction.summary 的 group_by 仅支持: {', '.join(engine.DED_GROUP)}")
        return engine.deduction_summary(
            conn,
            **common,
            asn=clean.get("asn"),
            item=clean.get("item"),
            group_by=group_by,
            limit=clean.get("limit", 20),
        )
    if intent == "deduction.detail":
        return engine.deduction_detail(
            conn,
            **common,
            asn=clean.get("asn"),
            item=clean.get("item"),
            limit=clean.get("limit", 50),
        )
    if intent == "shipment.summary":
        group_by = clean.get("group_by", "month")
        if group_by not in engine.SHIP_GROUP:
            raise ValueError(f"shipment.summary 的 group_by 仅支持: {', '.join(engine.SHIP_GROUP)}")
        return engine.shipment_summary(
            conn,
            **common,
            asn=clean.get("asn"),
            plate=clean.get("plate"),
            group_by=group_by,
            limit=clean.get("limit", 20),
        )
    if intent == "shipment.detail":
        return engine.shipment_detail(
            conn,
            **common,
            asn=clean.get("asn"),
            plate=clean.get("plate"),
            limit=clean.get("limit", 50),
        )
    return engine.reconcile(conn, **common, limit=clean.get("limit", 50))


def query(
    conn,
    *,
    text: str | None = None,
    intent: str | None = None,
    params: dict | None = None,
    now=None,
    live: bool = False,
) -> dict:
    """统一入口：自然语言或结构化意图 -> 查询 -> 中文回答。"""
    started = time.perf_counter()

    if intent:
        if intent not in CAPABILITY_BY_INTENT:
            raise ValueError(f"未知 intent: {intent}")
        if text:
            parsed = nlu.parse(text, now)
            parsed.intent = intent
            parsed.confidence = 1.0
            parsed.notes = [note for note in parsed.notes if "未识别" not in note]
        else:
            parsed = nlu.ParsedQuery(
                text="",
                intent=intent,
                params={},
                confidence=1.0,
                notes=[],
            )
        merged = {**parsed.params, **_norm_params(params)}
        parsed.params = merged
        if intent == "overview":
            parsed.notes = [note for note in parsed.notes if "时间范围" not in note]
    else:
        parsed = nlu.parse(text or "", now)
        parsed.params.update(_norm_params(params))

    # v0.2：先过图谱 planner，选出 provider 并把路径写进响应，执行由离线 engine 承担
    try:
        plan = Graph.load().plan(parsed.intent, parsed.params)
    except GraphError as exc:
        raise ValueError(str(exc)) from exc

    result = dispatch(conn, parsed.intent, parsed.params, now, live=live)
    if live:
        as_of = result.get("live_fetched_at")
        plan["provider"] = {
            "id": "live.srm",
            "kind": "live",
            "label": "实时读取 SRM",
            "available": True,
        }
        plan["steps"] = ["复用登录会话 cookie", "vendorQuery 接口直连", "分页拉取", "内存汇总"]
        plan["fallbacks"] = []
        if parsed.intent == "overview":
            ded = result["deductions"]
            text_answer = (
                f"实时读取 SRM 质量扣款明细：共 {ded['count']} 条"
                f"（页面显示 {result.get('page_total') or '—'} 条），"
                f"金额合计 {ded['total_amount']:,.2f} 元；"
                f"实时读取于 {as_of or '刚刚'}。"
            )
        else:
            text_answer = answer.render(parsed.intent, result, parsed, as_of)
            text_answer = text_answer.replace("本地库同步于", "实时读取于")
            text_answer = text_answer.replace(
                "，或先导入新的导出文件", "。实时数据以 SRM 当前记录为准"
            )
    else:
        as_of = engine.data_as_of(conn)
        text_answer = answer.render(parsed.intent, result, parsed, as_of)

    return {
        "ok": True,
        "intent": parsed.intent,
        "answer": text_answer,
        "data": result,
        "plan": plan,
        "params": parsed.params,
        "confidence": parsed.confidence,
        "notes": parsed.notes,
        "data_as_of": as_of,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def capabilities_payload(conn=None) -> dict:
    payload = {
        "capabilities": CAPABILITIES,
        "intents": list(INTENTS),
        "read_only": True,
    }
    if conn is not None:
        payload["data_as_of"] = engine.data_as_of(conn)
    try:
        payload["graph"] = Graph.load().stats()
    except GraphError:
        pass
    return payload


def tools_payload(conn=None) -> dict:
    tool = {
        "type": "function",
        "function": {
            "name": "query_business_data",
            "description": (
                "只读查询圣农SRM的发货、扣款、对账数据。禁止用于保存/提交/审批/发货等写操作。"
                "回答时引用返回的 answer 与 data_as_of。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": list(INTENTS),
                        "description": "查询类型",
                    },
                    "params": {
                        "type": "object",
                        "properties": copy.deepcopy(_COMMON_PROPS),
                        "additionalProperties": False,
                    },
                },
                "required": ["intent"],
                "additionalProperties": False,
            },
        },
    }
    payload = {"tools": [tool], "read_only": True}
    if conn is not None:
        payload["data_as_of"] = engine.data_as_of(conn)
    try:
        payload["graph"] = Graph.load().stats()
    except GraphError:
        pass
    return payload
