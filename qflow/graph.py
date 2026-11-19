"""系统查询图谱：实体/字段/关系 + 能力 + provider + planner.

这是 readflow 的 v0.2 运行时骨架：
- 图里存的是"语义"（实体、字段、关系、能力、提供者），不是页面坐标；
- 页面/接口只是 provider，可替换、可降级；
- planner 把 intent+params 解析成一条可审计的执行路径；
- 执行仍由 engine（离线 SQL）完成，在线 provider 待 discover 后接入。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .config import PROJECT_ROOT

GRAPH_PATH = PROJECT_ROOT / "graph.json"


class GraphError(ValueError):
    """图谱缺失、节点非法或没有可用 provider。"""


def node_kind(node: dict) -> str | None:
    """provider 的 kind；api/page 节点直接用自己的 type。"""
    return node.get("kind") or node.get("type")


class Graph:
    def __init__(self, data: dict, path: Path | None = None):
        self.data = data
        self.path = path
        self.version = data.get("version", 1)
        self.nodes: dict[str, dict] = {}
        for node in data.get("nodes", []):
            node_id = node.get("id")
            if not node_id:
                raise GraphError("节点缺少 id")
            if node_id in self.nodes:
                raise GraphError(f"节点 id 重复: {node_id}")
            self.nodes[node_id] = node
        self.edges: list[dict] = list(data.get("edges", []))
        for edge in self.edges:
            for key in ("from", "to"):
                if edge.get(key) not in self.nodes:
                    raise GraphError(f"边引用了不存在的节点: {edge.get(key)!r}")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Graph":
        path = Path(path) if path else GRAPH_PATH
        if not path.exists():
            raise GraphError(f"图谱不存在: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GraphError(f"图谱 JSON 解析失败: {exc}") from exc
        return cls(data, path)

    # ---------- 查询 ----------

    def nodes_of(self, node_type: str) -> list[dict]:
        return [node for node in self.nodes.values() if node.get("type") == node_type]

    def edges_of(self, edge_type: str) -> list[dict]:
        return [edge for edge in self.edges if edge.get("type") == edge_type]

    def capability(self, intent: str) -> dict:
        for node in self.nodes_of("capability"):
            if node.get("intent") == intent:
                return node
        raise GraphError(f"图谱中没有 intent={intent} 的能力")

    def providers(self, capability: dict) -> list[dict]:
        return [
            self.nodes[node_id]
            for node_id in capability.get("providers", [])
            if node_id in self.nodes
        ]

    def provider(self, node_id: str) -> dict:
        node = self.nodes.get(node_id)
        if not node or node.get("type") != "provider":
            raise GraphError(f"provider 不存在: {node_id}")
        return node

    def available_providers(self, capability: dict) -> list[dict]:
        return [item for item in self.providers(capability) if item.get("available")]

    def choose_provider(self, capability: dict) -> dict | None:
        providers = self.providers(capability)
        for item in providers:
            if item.get("available") and item.get("read_only", True):
                return item
        return None

    # ---------- planner ----------

    def plan(self, intent: str, params: dict | None = None) -> dict:
        capability = self.capability(intent)
        provider = self.choose_provider(capability)
        if provider is None:
            raise GraphError(
                f"{intent} 没有可用 provider；在线节点是否还没 discover/配置？"
            )
        entity = self.nodes.get(capability.get("entity") or "")
        bindings: dict[str, dict] = {}
        for param, field in (capability.get("bind") or {}).items():
            value = (params or {}).get(param)
            if value in (None, ""):
                continue
            bindings[param] = {"field": field, "value": value}

        fallbacks = [
            {
                "id": item["id"],
                "label": item.get("label"),
                "kind": node_kind(item),
                "available": bool(item.get("available")),
                "reason": item.get("reason"),
            }
            for item in self.providers(capability)
            if item["id"] != provider["id"]
        ]

        return {
            "intent": intent,
            "label": capability.get("label"),
            "entity": (
                {"id": entity["id"], "label": entity.get("label")}
                if entity
                else None
            ),
            "provider": {
                "id": provider["id"],
                "kind": node_kind(provider),
                "label": provider.get("label"),
                "available": bool(provider.get("available")),
            },
            "bindings": bindings,
            "steps": self._steps(intent, provider, entity, bindings),
            "fallbacks": fallbacks,
            "read_only": bool(capability.get("read_only", True)),
        }

    def _steps(self, intent, provider, entity, bindings) -> list[str]:
        entity_label = entity.get("label") if entity else "数据"
        kind = node_kind(provider)
        if kind == "cache":
            if intent == "reconcile":
                return ["读取本地发货单", "读取本地扣款明细", "按 asn 对齐", "计算净额"]
            if intent == "overview":
                return ["读取本地 sync_runs 与事实表", "汇总数据量与时间范围"]
            fields = "、".join(f"{key}→{value['field']}" for key, value in bindings.items())
            steps = [f"读取本地{entity_label}"]
            if fields:
                steps.append(f"按字段过滤：{fields}")
            return steps + ["返回结构化记录"]
        if kind == "api":
            method = provider.get("method", "GET")
            path = provider.get("path") or "<待 discover>"
            return [f"{method} {path}", "校验返回字段", "写入缓存或直接回答"]
        if kind == "page":
            route = provider.get("route") or "<待确认>"
            return [f"打开页面 {route}", "提取表格", "校验字段"]
        return [f"使用 provider {provider['id']}"]

    # ---------- 展示/统计 ----------

    def stats(self) -> dict:
        by_type = Counter(node.get("type") for node in self.nodes.values())
        capabilities = self.nodes_of("capability")
        answerable = sum(1 for cap in capabilities if self.choose_provider(cap))
        online = [
            item for item in self.nodes.values() if node_kind(item) in {"api", "page"}
        ]
        pending = [item for item in online if not item.get("available")]
        return {
            "version": self.version,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "by_type": dict(sorted(by_type.items())),
            "capabilities": len(capabilities),
            "answerable": answerable,
            "coverage": round(answerable / len(capabilities), 2) if capabilities else 0.0,
            "online_pending": len(pending),
        }

    def summary(self) -> str:
        stats = self.stats()
        lines = [
            f"图谱 v{stats['version']}：{stats['nodes']} 个节点 / {stats['edges']} 条边",
            f"能力 {stats['capabilities']} 个，可答 {stats['answerable']} 个"
            f"（覆盖率 {stats['coverage']:.0%}），在线待接 {stats['online_pending']} 个",
            "节点类型：" + "、".join(f"{key} {value}" for key, value in stats["by_type"].items()),
            "",
            "实体：",
        ]
        for node in self.nodes_of("entity"):
            fields = "、".join(field.get("label", field.get("name")) for field in node.get("fields", []))
            lines.append(f"  - {node['id']}  {node.get('label')}  字段：{fields}")
        lines.append("能力与 provider：")
        for cap in self.nodes_of("capability"):
            chosen = self.choose_provider(cap)
            target = chosen["id"] if chosen else "无可用"
            lines.append(f"  - {cap['intent']:20} → {target}")
        pending = [
            item
            for item in self.nodes.values()
            if node_kind(item) in {"api", "page"} and not item.get("available")
        ]
        if pending:
            lines.append("在线待接：")
            for item in pending:
                lines.append(f"  - {item['id']}（{item.get('reason', '未配置')}）")
        return "\n".join(lines)


def human_plan(plan: dict) -> str:
    lines = [
        f"意图：{plan['intent']}（{plan.get('label')}）",
        f"实体：{plan['entity']['label'] if plan.get('entity') else '—'}",
    ]
    if plan["bindings"]:
        pairs = "，".join(
            f"{key} → {value['field']} = {value['value']}"
            for key, value in plan["bindings"].items()
        )
        lines.append(f"过滤：{pairs}")
    lines.append(
        f"路径：{plan['provider']['id']}（{plan['provider'].get('label')}，{plan['provider'].get('kind')}）"
    )
    for index, step in enumerate(plan["steps"], start=1):
        lines.append(f"  {index}. {step}")
    if plan["fallbacks"]:
        lines.append("兜底：")
        for item in plan["fallbacks"]:
            state = "可用" if item["available"] else f"未就绪（{item.get('reason') or '—'}）"
            lines.append(f"  - {item['id']}：{state}")
    return "\n".join(lines)
