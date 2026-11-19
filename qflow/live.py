"""把 SRM 质量扣款明细页的可见表格导入本地库（只读）。

页面结构（Element UI）：
- 第一张表头 26 列，行首有一个选择框空列；
- 表头会因固定列出现第二份，只取前 26 列；
- 只使用只读字段：扣罚金额、检测项目、制单日期等。

不抓账号密码，不点保存/确认/发货。导入后走既有 SQLite 查询与回答层。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from . import importer
from .dates import now_local

# 目标列（匹配 importer.DED_ALIASES） <- 页面表头
FIELD_MAP = {
    "收货单号(必填)": "收货单号",
    "检测项目名称(必填)": "检测项目名称",
    "扣罚范围(必填)": "扣罚范围",
    "检测结果(必填)": "检测结果",
    "扣罚标准(必填)": "扣罚标准",
    "扣罚公式(必填)": "扣罚公式",
    "扣罚金额(必填)": "扣罚金额",
    "数量(必填)": "数量",
    "含税单价(必填)": "含税单价",
    "含税金额(必填)": "含税金额",
    "订单号(必填)": "订单号",
    "制单日期(必填)": "制单日期",
}


def table_to_frame(table: dict) -> pd.DataFrame:
    """把页面抓到的 headers/rows 转成 importer 可识别的 DataFrame。"""
    headers = [str(h).strip() for h in (table.get("headers") or [])]
    # 固定列会把表头复制一遍，取前一半
    unique_headers: list[str] = []
    for header in headers:
        if header not in unique_headers:
            unique_headers.append(header)
    index = {name: i for i, name in enumerate(unique_headers)}
    missing = [label for label in FIELD_MAP.values() if label not in index]
    if missing:
        raise ValueError(f"页面缺少字段: {', '.join(missing)}")

    rows = []
    seen = set()
    for raw in table.get("rows") or []:
        cells = list(raw)
        # 行首多一个选择框空列；若长度与表头一致则不丢
        if len(cells) == len(unique_headers) + 1:
            cells = cells[1:]
        if len(cells) < len(unique_headers):
            continue
        record = {target: cells[index[label]] for target, label in FIELD_MAP.items()}
        if not str(record.get("收货单号(必填)", "")).strip():
            continue
        # Element UI 的固定列会把同一行渲染两遍，按业务字段去重
        key = tuple(str(record[target]) for target in FIELD_MAP)
        if key in seen:
            continue
        seen.add(key)
        rows.append(record)
    return pd.DataFrame(rows)


def import_table(
    conn,
    table: dict,
    *,
    dataset: str = "live_srm",
    source: str = "live:quajud/vendorQuery",
) -> dict:
    """导入一张页面表格，并写 sync_runs 审计记录。"""
    frame = table_to_frame(table)
    started = now_local().isoformat(timespec="seconds")
    stats = importer.import_frame(conn, frame, "deductions", dataset, source)
    finished = now_local().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO sync_runs
           (dataset, kind, source_file, started_at, finished_at,
            rows_read, rows_written, skipped, status)
           VALUES (?, 'deductions', ?, ?, ?, ?, ?, ?, 'success')""",
        (
            dataset,
            source,
            started,
            finished,
            stats["rows_read"],
            stats["rows_written"],
            stats["skipped"],
        ),
    )
    conn.commit()
    return {**stats, "dataset": dataset, "source": source, "finished_at": finished}
