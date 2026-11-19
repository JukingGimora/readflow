"""把查询结果渲染成可以直接朗读的中文回答，同时保留结构化 data.

回答风格：先给结论，再给 1~5 条明细；金额带千分位；明确时间口径和数据截止时间。
"""
from __future__ import annotations


def money(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f}"


def qty(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.3f}".rstrip("0").rstrip(".")


def _empty(result: dict, as_of: str | None) -> str:
    text = f"没有查到符合条件的数据（{result.get('range_label', '全部时间')}"
    filters = result.get("filters") or {}
    labels = {"asn": "单号", "plate": "车牌", "item": "检测项目"}
    extra = [f"{labels[key]} {value}" for key, value in filters.items() if key in labels and value]
    if extra:
        text += "；" + "，".join(extra)
    text += "）"
    if as_of:
        text += f"，本地库同步于 {as_of}"
    text += "。可以换一个时间范围或筛选条件，或先导入新的导出文件。"
    return text


def _deduction_summary(result: dict, as_of: str | None) -> str:
    if not result["count"]:
        return _empty(result, as_of)
    lines = [
        f"{result['range_label']} 共 {result['count']} 笔扣款，"
        f"合计 {money(result['total_amount'])} 元，涉及 {result['asn_count']} 个收货单。"
    ]
    groups = result.get("groups") or []
    if groups:
        parts = [
            f"{row['bucket']} {row['n']} 笔 / {money(row['total'])} 元"
            for row in groups[:5]
        ]
        labels = {"item": "检测项目", "month": "月份", "asn": "单号", "dataset": "数据集"}
        lines.append("按" + labels.get(result.get("group_by"), "维度") + "：" + "；".join(parts) + "。")
    if as_of:
        lines.append(f"本地库同步于 {as_of}。")
    return "\n".join(lines)


def _deduction_detail(result: dict, as_of: str | None) -> str:
    if not result["count"]:
        return _empty(result, as_of)
    lines = [
        f"{result['range_label']} 共 {result['count']} 笔扣款，"
        f"合计 {money(result['total_amount'])} 元。"
    ]
    if result["returned"]:
        lines.append("明细如下：")
    for index, row in enumerate(result["rows"], start=1):
        line = f"{index}. {row.get('biz_date') or '日期未知'} {row['asn']}"
        if row.get("order_no"):
            line += f"（订单 {row['order_no']}）"
        line += f" {row.get('item') or '未注明'}"
        if row.get("result_text"):
            line += f"（检测值 {row['result_text']}）"
        line += f" 扣 {money(row.get('amount'))} 元"
        lines.append(line)
    if result["count"] > result["returned"]:
        lines.append(f"（仅列出前 {result['returned']} 笔，完整数据见 data.rows）")
    return "\n".join(lines)


def _shipment_summary(result: dict, as_of: str | None) -> str:
    if not result["count"]:
        return _empty(result, as_of)
    lines = [
        f"{result['range_label']} 共 {result['count']} 单，"
        f"实收 {qty(result['total_qty'])} 吨，含税金额 {money(result['total_amount'])} 元。"
    ]
    groups = result.get("groups") or []
    if groups:
        parts = [
            f"{row['bucket']}：{row['n']} 单 / {qty(row['qty'])} 吨 / {money(row['total'])} 元"
            for row in groups[:5]
        ]
        labels = {"month": "月份", "plate": "车牌", "asn": "单号", "dataset": "数据集"}
        lines.append("按" + labels.get(result.get("group_by"), "维度") + "：" + "；".join(parts) + "。")
    if as_of:
        lines.append(f"本地库同步于 {as_of}。")
    return "\n".join(lines)


def _shipment_detail(result: dict, as_of: str | None) -> str:
    if not result["count"]:
        return _empty(result, as_of)
    lines = [
        f"{result['range_label']} 共 {result['count']} 单，"
        f"实收 {qty(result['total_qty'])} 吨，含税金额 {money(result['total_amount'])} 元。"
    ]
    for index, row in enumerate(result["rows"], start=1):
        line = (
            f"{index}. {row.get('biz_date') or '日期未知'} {row['asn']} "
            f"{row.get('plate') or '车牌未登记'} 实收 {qty(row.get('received_qty'))} 吨"
            f" 含税 {money(row.get('amount'))} 元"
        )
        lines.append(line)
    if result["count"] > result["returned"]:
        lines.append(f"（仅列出前 {result['returned']} 单，完整数据见 data.rows）")
    return "\n".join(lines)


def _reconcile(result: dict, as_of: str | None) -> str:
    if not result["row_count"]:
        return _empty(result, as_of)
    lines = [
        f"{result['range_label']} 按单号对齐：发货 {result['ship_count']} 单，"
        f"含税 {money(result['ship_total'])} 元；扣款 {result['ded_count']} 笔"
        f"（{result['ded_asn_count']} 个单），合计 {money(result['ded_total'])} 元；"
        f"净额 {money(result['net_total'])} 元。"
    ]
    lines.append(
        f"其中有扣款的单 {result['with_deduction']} 个，只发货未扣款 {result['ship_only']} 个，"
        f"有扣款但无发货记录 {result['ded_only']} 个。"
    )
    top = [row for row in result["rows"] if row.get("has_deduction") and row.get("ded_amount")]
    if top:
        parts = [
            f"{row['asn']} 扣 {money(row['ded_amount'])} 元（{row['ded_count']} 笔，净额 {money(row['net'])} 元）"
            for row in top[:3]
        ]
        lines.append("扣款最多：" + "；".join(parts) + "。")
    if result["ded_only"]:
        lines.append("提示：有扣款但无发货记录的单，可能不在当前导入的订单批次里。")
    if as_of:
        lines.append(f"本地库同步于 {as_of}。")
    return "\n".join(lines)


def _overview(result: dict, as_of: str | None) -> str:
    ship = result["shipments"]
    ded = result["deductions"]
    if not ship["count"] and not ded["count"]:
        return "本地还没有数据。先运行 `python -m qflow sync --dir ..` 导入导出文件。"
    lines = [
        f"本地已导入 {ship['count']} 个发货单、{ded['count']} 笔扣款。"
    ]
    if ship["count"]:
        lines.append(
            f"发货：{ship['min_date'] or '?'} 至 {ship['max_date'] or '?'}，"
            f"实收 {qty(ship['total_qty'])} 吨，含税 {money(ship['total_amount'])} 元。"
        )
    if ded["count"]:
        lines.append(
            f"扣款：{ded['min_date'] or '?'} 至 {ded['max_date'] or '?'}，"
            f"合计 {money(ded['total_amount'])} 元。"
        )
    datasets = result.get("datasets") or []
    if datasets:
        names = "、".join(f"{row['dataset']}（{row['rows_written']} 行）" for row in datasets[:8])
        lines.append(f"数据集：{names}。")
    if as_of:
        lines.append(f"本地库同步于 {as_of}。")
    return "\n".join(lines)


def render(intent: str, result: dict, parsed=None, as_of: str | None = None) -> str:
    if intent == "deduction.summary":
        return _deduction_summary(result, as_of)
    if intent == "deduction.detail":
        return _deduction_detail(result, as_of)
    if intent == "shipment.summary":
        return _shipment_summary(result, as_of)
    if intent == "shipment.detail":
        return _shipment_detail(result, as_of)
    if intent == "reconcile":
        return _reconcile(result, as_of)
    if intent == "overview":
        return _overview(result, as_of)
    return _overview(result, as_of)
