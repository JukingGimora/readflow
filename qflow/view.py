"""把标准结果和图谱整理成页面、语音用的样子。只读图谱里的名字，不认识任何具体系统。"""

from __future__ import annotations

STATE_KEY = "flow"  # 多轮状态在调用方（页面、VoiceLab）那边存放的键


def answer(agent, out: dict) -> dict:
    """ask 的输出 → 一轮回答。有结果时带上图表要用的数字、表头和「怎么查的」。"""
    payload = {
        "reply": " ".join(str(out.get("reply") or "").splitlines()),
        "handled": bool(out.get("handled")),
        "followups": out.get("followups") or [],
        "state_patch": {STATE_KEY: out.get("state") or {}},
        "system": out.get("system"),
        "needs_login": bool(out.get("needs_login")),
    }
    result = out.get("result")
    if not result or agent is None:
        return payload
    graph = agent.graph
    capability = graph.capabilities[result["capability"]]
    operation = graph.operations[capability["operation"]]
    entity = graph.entities[operation["response"]["entity"]]
    labels = {f["name"]: f.get("label") or f["name"] for f in entity["fields"]}
    compute = capability.get("compute") or {}
    metrics = compute.get("metrics") or []
    main = next((m for m in metrics if m["fn"] == "sum"), metrics[0] if metrics else None)
    counter = next((m for m in metrics if m["fn"] == "count"), None)
    group_by = compute.get("group_by") or []
    trace = result.get("trace") or {}
    request = operation["request"]
    steps = [f"调用「{agent.label}」的接口 {request['method']} {request['path']}，{trace.get('requests', 0)} 次请求，取回 {trace.get('rows', 0)} 条"]
    if result.get("conditions"):
        steps.append("按条件筛选：" + "，".join(f"{k}={v}" for k, v in result["conditions"].items() if v))
    steps.append("本地计算：" + "、".join(m.get("label") or m["key"] for m in metrics) if metrics else f"列出 {result.get('count', 0)} 条记录")
    payload.update({
        "capability": result["capability"],
        "by": out.get("by"),
        "data": {
            **(result.get("totals") or {}),
            "count": result.get("count"),
            "groups": [
                {
                    "label": " / ".join(str(g.get(f) if g.get(f) is not None else "（空）") for f in group_by),
                    "value": g.get(main["key"]) if main else None,
                    "n": g.get(counter["key"]) if counter else None,
                }
                for g in result.get("groups") or []
            ],
            "rows": result.get("rows") or [],
        },
        "plan": {
            "label": capability.get("label") or capability["id"],
            "source": agent.label,
            "read_only": True,
            "complete": trace.get("complete", True),
            "metrics": [{"key": m["key"], "label": m.get("label") or m["key"], "unit": m.get("unit")} for m in metrics],
            "columns": [{"name": name, "label": label} for name, label in labels.items()],
            "steps": steps,
        },
    })
    return payload


def graph_view(agents: dict) -> dict:
    """几份 v1 图谱 → 管理页那张连线图的节点和边。"""
    nodes, edges = [], []
    for sid, agent in agents.items():
        doc = agent.graph.doc
        uid = lambda local: f"{sid}/{local}"  # noqa: E731
        nodes.append({"id": sid, "type": "app", "label": agent.label, "base_url": doc["system"].get("base_url"),
                      "generated": doc.get("generated")})
        modules: dict[str, str] = {}
        for nav in doc.get("navigation") or []:
            path = [str(p) for p in nav.get("path") or [nav["id"]]]
            if len(path) > 1 and path[0] not in modules:
                modules[path[0]] = uid("module:" + path[0])
                nodes.append({"id": modules[path[0]], "type": "module", "label": path[0]})
                edges.append({"from": sid, "to": modules[path[0]]})
            queryable = bool(nav.get("capabilities"))
            nodes.append({"id": uid(nav["id"]), "type": "page", "label": path[-1], "route": nav.get("route"),
                          "available": queryable,
                          **({} if queryable else {"not_queryable": (nav.get("not_queryable") or {}).get("reason")})})
            edges.append({"from": modules.get(path[0], sid), "to": uid(nav["id"])})
            edges += [{"from": uid(nav["id"]), "to": uid(c)} for c in nav.get("capabilities") or []]
        for op in doc.get("operations") or []:
            request = op.get("request") or {}
            nodes.append({"id": uid(op["id"]), "type": "api", "label": op.get("label") or op["id"],
                          "method": request.get("method"), "path": request.get("path"), "read_only": op.get("access") == "read",
                          "evidence": (op.get("evidence") or {}).get("captured_at")})
            edges.append({"from": uid(op["id"]), "to": uid(op["response"]["entity"])})
        for entity in doc.get("entities") or []:
            nodes.append({"id": uid(entity["id"]), "type": "entity", "label": entity.get("label") or entity["id"],
                          "fields": [{"name": f["name"], "label": f.get("label")} for f in entity.get("fields") or []]})
        for cap in doc.get("capabilities") or []:
            nodes.append({"id": uid(cap["id"]), "type": "capability", "label": cap.get("label") or cap["id"],
                          "read_only": cap.get("access", "read") == "read", "examples": cap.get("examples") or [],
                          "slots": [{"name": s["name"], "label": s.get("label")} for s in cap.get("slots") or []],
                          "keywords": cap.get("keywords") or []})
            edges.append({"from": uid(cap["id"]), "to": uid(cap["operation"])})
    return {"nodes": nodes, "edges": edges}
