"""查询引擎：所有 SQL 都在这里，服务层/CLI 不直接写 SQL.

约定：
- 时间范围都是闭区间（含首尾），按 biz_date 过滤；
- 金额单位元，保留 2 位；重量单位吨；
- 返回 dict 里带 start / end / range_label，方便回答时说明口径。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .dates import resolve_range

DED_GROUP = {
    "item": "COALESCE(item, '未注明')",
    "month": "substr(biz_date, 1, 7)",
    "asn": "asn",
    "dataset": "dataset",
}
SHIP_GROUP = {
    "month": "substr(biz_date, 1, 7)",
    "plate": "COALESCE(plate, '未登记')",
    "asn": "asn",
    "dataset": "dataset",
}


def _as_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _round(value, digits: int = 2):
    return None if value is None else round(float(value), digits)


def _ded_where(start, end, asn, item) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []
    if start:
        clauses.append("biz_date >= ?")
        args.append(start)
    if end:
        clauses.append("biz_date <= ?")
        args.append(end)
    if asn:
        clauses.append("asn = ?")
        args.append(asn.upper())
    if item:
        clauses.append("item LIKE ?")
        args.append(f"%{item}%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def _ship_where(start, end, asn, plate) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []
    if start:
        clauses.append("biz_date >= ?")
        args.append(start)
    if end:
        clauses.append("biz_date <= ?")
        args.append(end)
    if asn:
        clauses.append("asn = ?")
        args.append(asn.upper())
    if plate:
        clauses.append("plate LIKE ?")
        args.append(f"%{plate}%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def deduction_summary(
    conn,
    *,
    start=None,
    end=None,
    asn=None,
    item=None,
    group_by: str = "item",
    limit: int = 20,
) -> dict:
    group_by = group_by if group_by in DED_GROUP else "item"
    expr = DED_GROUP[group_by]
    where, args = _ded_where(start, end, asn, item)
    limit = max(1, min(int(limit), 200))

    totals = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total,
                   COUNT(DISTINCT asn) AS asn_count
            FROM deductions{where}""",
        args,
    ).fetchone()

    rows = conn.execute(
        f"""SELECT {expr} AS bucket, COUNT(*) AS n,
                   ROUND(SUM(amount), 2) AS total,
                   ROUND(AVG(amount), 2) AS avg,
                   ROUND(MAX(amount), 2) AS max_amount,
                   COUNT(DISTINCT asn) AS asn_count
            FROM deductions{where}
            GROUP BY bucket
            ORDER BY total DESC
            LIMIT ?""",
        [*args, limit],
    ).fetchall()

    return {
        "start": start,
        "end": end,
        "range_label": resolve_range({"start": start, "end": end})[2],
        "group_by": group_by,
        "filters": {"asn": asn, "item": item},
        "count": int(totals["n"] or 0),
        "total_amount": _round(totals["total"]),
        "asn_count": int(totals["asn_count"] or 0),
        "groups": [_as_dict(row) for row in rows],
    }


def deduction_detail(
    conn,
    *,
    start=None,
    end=None,
    asn=None,
    item=None,
    limit: int = 50,
) -> dict:
    where, args = _ded_where(start, end, asn, item)
    limit = max(1, min(int(limit), 500))
    totals = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total
            FROM deductions{where}""",
        args,
    ).fetchone()
    rows = conn.execute(
        f"""SELECT asn, biz_date, item, result_text, range_text, formula,
                   amount, qty, source_file, dataset
            FROM deductions{where}
            ORDER BY COALESCE(biz_date, '') DESC, amount DESC
            LIMIT ?""",
        [*args, limit],
    ).fetchall()
    return {
        "start": start,
        "end": end,
        "range_label": resolve_range({"start": start, "end": end})[2],
        "filters": {"asn": asn, "item": item},
        "count": int(totals["n"] or 0),
        "total_amount": _round(totals["total"]),
        "returned": len(rows),
        "rows": [_as_dict(row) for row in rows],
    }


def shipment_summary(
    conn,
    *,
    start=None,
    end=None,
    asn=None,
    plate=None,
    group_by: str = "month",
    limit: int = 20,
) -> dict:
    group_by = group_by if group_by in SHIP_GROUP else "month"
    expr = SHIP_GROUP[group_by]
    where, args = _ship_where(start, end, asn, plate)
    limit = max(1, min(int(limit), 200))
    totals = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(received_qty), 0) AS qty,
                   COALESCE(SUM(amount), 0) AS total
            FROM shipments{where}""",
        args,
    ).fetchone()
    rows = conn.execute(
        f"""SELECT {expr} AS bucket, COUNT(*) AS n,
                   ROUND(SUM(received_qty), 3) AS qty,
                   ROUND(SUM(amount), 2) AS total
            FROM shipments{where}
            GROUP BY bucket
            ORDER BY bucket DESC
            LIMIT ?""",
        [*args, limit],
    ).fetchall()
    return {
        "start": start,
        "end": end,
        "range_label": resolve_range({"start": start, "end": end})[2],
        "group_by": group_by,
        "filters": {"asn": asn, "plate": plate},
        "count": int(totals["n"] or 0),
        "total_qty": _round(totals["qty"], 3),
        "total_amount": _round(totals["total"]),
        "groups": [_as_dict(row) for row in rows],
    }


def shipment_detail(
    conn,
    *,
    start=None,
    end=None,
    asn=None,
    plate=None,
    limit: int = 50,
) -> dict:
    where, args = _ship_where(start, end, asn, plate)
    limit = max(1, min(int(limit), 500))
    totals = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(received_qty), 0) AS qty,
                   COALESCE(SUM(amount), 0) AS total
            FROM shipments{where}""",
        args,
    ).fetchone()
    rows = conn.execute(
        f"""SELECT asn, biz_date, ship_date, receive_date, received_qty,
                   amount, plate, ref_no, line_count, source_file, dataset
            FROM shipments{where}
            ORDER BY COALESCE(biz_date, '') DESC, asn DESC
            LIMIT ?""",
        [*args, limit],
    ).fetchall()
    return {
        "start": start,
        "end": end,
        "range_label": resolve_range({"start": start, "end": end})[2],
        "filters": {"asn": asn, "plate": plate},
        "count": int(totals["n"] or 0),
        "total_qty": _round(totals["qty"], 3),
        "total_amount": _round(totals["total"]),
        "returned": len(rows),
        "rows": [_as_dict(row) for row in rows],
    }


def reconcile(conn, *, start=None, end=None, limit: int = 50) -> dict:
    """发货金额 - 扣款金额，按 ASN 对齐（只读版对账）。"""
    where, args = _ship_where(start, end, None, None)
    ship_rows = conn.execute(
        f"""SELECT asn, MAX(biz_date) AS biz_date, MAX(plate) AS plate,
                   ROUND(SUM(amount), 2) AS amount,
                   ROUND(SUM(received_qty), 3) AS qty
            FROM shipments{where}
            GROUP BY asn""",
        args,
    ).fetchall()

    ded_where, ded_args = _ded_where(start, end, None, None)
    ded_rows = conn.execute(
        f"""SELECT asn, MAX(biz_date) AS biz_date,
                   ROUND(SUM(amount), 2) AS amount, COUNT(*) AS n
            FROM deductions{ded_where}
            GROUP BY asn""",
        ded_args,
    ).fetchall()

    merged: dict[str, dict] = {}
    for row in ship_rows:
        merged[row["asn"]] = {
            "asn": row["asn"],
            "biz_date": row["biz_date"],
            "plate": row["plate"],
            "ship_amount": _round(row["amount"]),
            "ship_qty": _round(row["qty"], 3),
            "ded_amount": 0.0,
            "ded_count": 0,
            "has_shipment": True,
            "has_deduction": False,
        }
    for row in ded_rows:
        item = merged.setdefault(
            row["asn"],
            {
                "asn": row["asn"],
                "biz_date": row["biz_date"],
                "plate": None,
                "ship_amount": 0.0,
                "ship_qty": None,
                "ded_amount": 0.0,
                "ded_count": 0,
                "has_shipment": False,
                "has_deduction": False,
            },
        )
        item["ded_amount"] = _round(row["amount"])
        item["ded_count"] = int(row["n"] or 0)
        item["has_deduction"] = True
        if not item.get("biz_date"):
            item["biz_date"] = row["biz_date"]

    rows: list[dict] = []
    for item in merged.values():
        ship_amount = item["ship_amount"] or 0.0
        ded_amount = item["ded_amount"] or 0.0
        rows.append(
            {
                **item,
                "net": round(ship_amount - ded_amount, 2),
            }
        )

    rows.sort(
        key=lambda row: (
            -(row["ded_amount"] or 0.0),
            -abs(row["net"] or 0.0),
            row["asn"],
        )
    )

    ship_total = round(sum(row["ship_amount"] or 0.0 for row in rows), 2)
    ded_total = round(sum(row["ded_amount"] or 0.0 for row in rows), 2)
    return {
        "start": start,
        "end": end,
        "range_label": resolve_range({"start": start, "end": end})[2],
        "ship_count": sum(1 for row in rows if row["has_shipment"]),
        "ded_count": sum(int(row["ded_count"] or 0) for row in rows),
        "ded_asn_count": sum(1 for row in rows if row["has_deduction"]),
        "row_count": len(rows),
        "ship_total": ship_total,
        "ded_total": ded_total,
        "net_total": round(ship_total - ded_total, 2),
        "with_deduction": sum(1 for row in rows if row["has_deduction"]),
        "ship_only": sum(1 for row in rows if row["has_shipment"] and not row["has_deduction"]),
        "ded_only": sum(1 for row in rows if row["has_deduction"] and not row["has_shipment"]),
        "rows": rows[: max(1, min(int(limit), 500))],
    }


def overview(conn) -> dict:
    ship = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(received_qty), 0) AS qty,
                  COALESCE(SUM(amount), 0) AS total,
                  MIN(biz_date) AS min_date, MAX(biz_date) AS max_date
           FROM shipments"""
    ).fetchone()
    ded = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total,
                  MIN(biz_date) AS min_date, MAX(biz_date) AS max_date
           FROM deductions"""
    ).fetchone()
    # 每个 dataset/kind 只取最近一次导入，避免重复 sync 时行数累加
    datasets = conn.execute(
        """SELECT s.dataset, s.kind, s.finished_at, s.rows_written
           FROM sync_runs s
           JOIN (
               SELECT dataset, kind, MAX(id) AS max_id
               FROM sync_runs
               WHERE status = 'success'
               GROUP BY dataset, kind
           ) latest ON latest.max_id = s.id
           ORDER BY s.finished_at DESC"""
    ).fetchall()
    last_sync = conn.execute(
        "SELECT MAX(finished_at) FROM sync_runs WHERE status = 'success'"
    ).fetchone()[0]
    return {
        "shipments": {
            "count": int(ship["n"] or 0),
            "total_qty": _round(ship["qty"], 3),
            "total_amount": _round(ship["total"]),
            "min_date": ship["min_date"],
            "max_date": ship["max_date"],
        },
        "deductions": {
            "count": int(ded["n"] or 0),
            "total_amount": _round(ded["total"]),
            "min_date": ded["min_date"],
            "max_date": ded["max_date"],
        },
        "datasets": [_as_dict(row) for row in datasets],
        "last_sync": last_sync,
    }


def datasets(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, dataset, kind, source_file, started_at, finished_at,
                  rows_read, rows_written, skipped, status, error
           FROM sync_runs
           ORDER BY id DESC
           LIMIT 100"""
    ).fetchall()
    return [_as_dict(row) for row in rows]


def data_as_of(conn) -> str | None:
    row = conn.execute(
        "SELECT MAX(finished_at) FROM sync_runs WHERE status = 'success'"
    ).fetchone()
    return row[0] if row and row[0] else None


def health(conn) -> dict[str, Any]:
    counts = {
        "shipments": conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
        "deductions": conn.execute("SELECT COUNT(*) FROM deductions").fetchone()[0],
    }
    return {
        "ok": True,
        "counts": counts,
        "data_as_of": data_as_of(conn),
    }
