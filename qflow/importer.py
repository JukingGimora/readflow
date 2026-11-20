"""Excel 导出 -> SQLite 导入器（只读数据的离线入口）.

支持的来源：
- shipments：SRM 订单导出（1000.xls / 3000.xls ...），一行一个发货单
- deductions：SRM 扣款导出（deduct.xls / (1).xls / (2).xls ...），一行一笔扣款
- 自动跳过 *new.xlsx：那是 merge_excel.py 的合并产物，再导会重复计数

原则：
- 不做任何业务判断，只做字段归一化、日期推断、去重；
- 同一 ASN 的订单多行 -> 金额/数量求和，line_count 记录行数，不丢数；
- 每笔导入写 sync_runs，回答里可以注明数据截止时间。
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

from . import db
from .dates import asn_date, now_local, to_date_str, to_datetime_str

SHIP_ALIASES = {
    "asn": ["发货单号", "收货单号", "ASN"],
    "ship_date": ["发货日期"],
    "receive_date": ["收货日期"],
    "shipped_qty": ["发货数量"],
    "received_qty": ["实收数量", "数量"],
    "unit_price": ["含税单价"],
    "amount": ["含税金额"],
    "tax_free_amount": ["未税金额"],
    "plate": ["车牌号-物流信息", "车牌号"],
    "ref_no": ["参考凭证号-行号", "参考凭证号"],
}

DED_ALIASES = {
    "asn": ["收货单号(必填)", "收货单号", "ASN"],
    "item": ["检测项目名称(必填)", "检测项目名称"],
    "range_text": ["扣罚范围(必填)", "扣罚范围"],
    "result_text": ["检测结果(必填)", "检测结果"],
    "standard_text": ["扣罚标准(必填)", "扣罚标准"],
    "formula": ["扣罚公式(必填)", "扣罚公式"],
    "amount": ["扣罚金额(必填)", "扣罚金额"],
    "qty": ["数量(必填)", "数量"],
    "unit_price": ["含税单价(必填)"],
    "amount_incl": ["含税金额(必填)"],
    "order_no": ["订单号(必填)", "订单号"],
    "created_date": ["制单日期(必填)", "制单日期"],
}


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if text.lower() in {"", "nan", "nat", "none", "null", "na", "<na>"}:
        return None
    return text


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    text = str(value).replace(",", "").replace("，", "").strip()
    if text.lower() in {"", "nan", "none", "na", "<na>", "-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick(df, aliases: list[str]) -> str | None:
    for alias in aliases:
        if alias in df.columns:
            return alias
    for alias in aliases:
        for column in df.columns:
            if alias in str(column):
                return column
    return None


def detect_kind(df) -> str | None:
    columns = {str(c).strip() for c in df.columns}
    has_ship = any("发货单号" in c for c in columns)
    has_ded = any("收货单号" in c for c in columns)
    if has_ship:
        return "shipments"
    if has_ded:
        return "deductions"
    return None


def _dedup_key(asn: str, item: str | None, amount: float, qty: float | None, formula: str | None) -> str:
    raw = "|".join(
        [asn, item or "", f"{amount:.4f}", f"{qty or 0.0:.4f}", formula or ""]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _aggregate_shipments(records: list[dict]) -> list[dict]:
    """同一 ASN 多行订单 -> 一行，金额与数量求和。"""
    merged: dict[str, dict] = {}
    for rec in records:
        asn = rec["asn"]
        if asn not in merged:
            merged[asn] = rec
            continue
        base = merged[asn]
        for field in ("shipped_qty", "received_qty", "amount", "tax_free_amount"):
            if rec[field] is not None:
                base[field] = round((base[field] or 0.0) + rec[field], 4)
        base["line_count"] += 1
        for field in ("plate", "ref_no", "ship_date", "receive_date", "unit_price", "biz_date"):
            if base.get(field) is None and rec.get(field) is not None:
                base[field] = rec[field]
    return list(merged.values())


def import_frame(conn, df, kind: str, dataset: str, source_file: str) -> dict:
    """把已经读入的 DataFrame 写入库。返回统计信息。"""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    aliases = SHIP_ALIASES if kind == "shipments" else DED_ALIASES
    cols = {name: _pick(df, names) for name, names in aliases.items()}
    if cols.get("asn") is None:
        raise ValueError(f"{source_file}: 找不到单号列（发货单号/收货单号）")

    imported_at = now_local().isoformat(timespec="seconds")
    records: list[dict] = []
    skipped = 0

    for _, row in df.iterrows():
        asn = _text(row.get(cols["asn"]))
        if not asn:
            skipped += 1  # 合计行 / 空行
            continue
        asn = asn.upper()

        if kind == "shipments":
            ship_date = to_datetime_str(row.get(cols["ship_date"])) if cols["ship_date"] else None
            receive_date = to_date_str(row.get(cols["receive_date"])) if cols["receive_date"] else None
            record = {
                "asn": asn,
                "ship_date": ship_date,
                "receive_date": receive_date,
                "shipped_qty": _num(row.get(cols["shipped_qty"])) if cols["shipped_qty"] else None,
                "received_qty": _num(row.get(cols["received_qty"])) if cols["received_qty"] else None,
                "unit_price": _num(row.get(cols["unit_price"])) if cols["unit_price"] else None,
                "amount": _num(row.get(cols["amount"])) if cols["amount"] else None,
                "tax_free_amount": _num(row.get(cols["tax_free_amount"])) if cols["tax_free_amount"] else None,
                "plate": _text(row.get(cols["plate"])) if cols["plate"] else None,
                "ref_no": _text(row.get(cols["ref_no"])) if cols["ref_no"] else None,
                "line_count": 1,
                "dataset": dataset,
                "source_file": source_file,
                "imported_at": imported_at,
            }
            record["biz_date"] = (
                receive_date
                or (ship_date[:10] if ship_date else None)
                or asn_date(asn)
            )
            records.append(record)
            continue

        amount = _num(row.get(cols["amount"])) if cols["amount"] else None
        if amount is None:
            skipped += 1
            continue
        item = _text(row.get(cols["item"])) if cols["item"] else None
        qty = _num(row.get(cols["qty"])) if cols["qty"] else None
        formula = _text(row.get(cols["formula"])) if cols["formula"] else None
        created = to_date_str(row.get(cols["created_date"])) if cols["created_date"] else None
        records.append(
            {
                "dedup_key": _dedup_key(asn, item, amount, qty, formula),
                "asn": asn,
                "biz_date": created or asn_date(asn),
                "item": item,
                "range_text": _text(row.get(cols["range_text"])) if cols["range_text"] else None,
                "result_text": _text(row.get(cols["result_text"])) if cols["result_text"] else None,
                "standard_text": _text(row.get(cols["standard_text"])) if cols["standard_text"] else None,
                "formula": formula,
                "amount": amount,
                "qty": qty,
                "unit_price": _num(row.get(cols["unit_price"])) if cols["unit_price"] else None,
                "amount_incl": _num(row.get(cols["amount_incl"])) if cols["amount_incl"] else None,
                "order_no": _text(row.get(cols["order_no"])) if cols["order_no"] else None,
                "dataset": dataset,
                "source_file": source_file,
                "imported_at": imported_at,
            }
        )

    if kind == "shipments":
        records = _aggregate_shipments(records)
        sql = """
        INSERT INTO shipments (
            asn, biz_date, ship_date, receive_date, shipped_qty, received_qty,
            unit_price, amount, tax_free_amount, plate, ref_no, line_count,
            dataset, source_file, imported_at
        ) VALUES (
            :asn, :biz_date, :ship_date, :receive_date, :shipped_qty, :received_qty,
            :unit_price, :amount, :tax_free_amount, :plate, :ref_no, :line_count,
            :dataset, :source_file, :imported_at
        )
        ON CONFLICT(asn) DO UPDATE SET
            biz_date = excluded.biz_date,
            ship_date = excluded.ship_date,
            receive_date = excluded.receive_date,
            shipped_qty = excluded.shipped_qty,
            received_qty = excluded.received_qty,
            unit_price = excluded.unit_price,
            amount = excluded.amount,
            tax_free_amount = excluded.tax_free_amount,
            plate = excluded.plate,
            ref_no = excluded.ref_no,
            line_count = excluded.line_count,
            dataset = excluded.dataset,
            source_file = excluded.source_file,
            imported_at = excluded.imported_at
        """
    else:
        sql = """
        INSERT INTO deductions (
            dedup_key, asn, biz_date, item, range_text, result_text, standard_text,
            formula, amount, qty, unit_price, amount_incl, order_no,
            dataset, source_file, imported_at
        ) VALUES (
            :dedup_key, :asn, :biz_date, :item, :range_text, :result_text, :standard_text,
            :formula, :amount, :qty, :unit_price, :amount_incl, :order_no,
            :dataset, :source_file, :imported_at
        )
        ON CONFLICT(dedup_key) DO UPDATE SET
            biz_date = excluded.biz_date,
            item = excluded.item,
            range_text = excluded.range_text,
            result_text = excluded.result_text,
            standard_text = excluded.standard_text,
            formula = excluded.formula,
            amount = excluded.amount,
            qty = excluded.qty,
            unit_price = excluded.unit_price,
            amount_incl = excluded.amount_incl,
            order_no = excluded.order_no,
            dataset = excluded.dataset,
            source_file = excluded.source_file,
            imported_at = excluded.imported_at
        """

    if records:
        conn.executemany(sql, records)
    conn.commit()
    return {
        "kind": kind,
        "rows_read": int(len(df)),
        "rows_written": len(records),
        "skipped": skipped,
        "status": "success",
    }


def import_path(conn, path: str | Path, dataset: str | None = None) -> dict:
    """导入单个 Excel 文件，并把过程写入 sync_runs。"""
    import pandas as pd  # 延迟导入，服务进程启动更快

    path = Path(path)
    dataset = dataset or path.stem
    started = now_local().isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO sync_runs (dataset, kind, source_file, started_at, status)
           VALUES (?, ?, ?, ?, 'running')""",
        (dataset, "unknown", str(path), started),
    )
    run_id = cur.lastrowid
    conn.commit()

    result = {
        "dataset": dataset,
        "source_file": str(path),
        "kind": "unknown",
        "rows_read": 0,
        "rows_written": 0,
        "skipped": 0,
        "status": "error",
        "error": None,
    }
    try:
        if path.suffix.lower() not in {".xls", ".xlsx"}:
            raise ValueError(f"不支持的扩展名: {path.suffix}")
        if path.name.startswith("~$"):
            raise ValueError("Excel 临时文件，跳过")
        if path.name.lower().endswith("new.xlsx"):
            raise ValueError("合并产物（*new.xlsx），跳过避免重复计数")

        df = pd.read_excel(path, sheet_name=0, dtype=object)
        kind = detect_kind(df)
        if kind is None:
            raise ValueError("无法识别：既没有发货单号也没有收货单号列")

        stats = import_frame(conn, df, kind, dataset, path.name)
        result.update(stats)
        result["status"] = "success"
        conn.execute(
            """UPDATE sync_runs
               SET kind=?, finished_at=?, rows_read=?, rows_written=?, skipped=?, status='success'
               WHERE id=?""",
            (
                kind,
                now_local().isoformat(timespec="seconds"),
                stats["rows_read"],
                stats["rows_written"],
                stats["skipped"],
                run_id,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 需要把错误写进审计表
        result["status"] = "skipped" if isinstance(exc, ValueError) else "error"
        result["error"] = str(exc)
        conn.execute(
            """UPDATE sync_runs
               SET finished_at=?, status=?, error=?
               WHERE id=?""",
            (now_local().isoformat(timespec="seconds"), result["status"], str(exc), run_id),
        )
        conn.commit()
    return result


def sync_dir(conn, directory: str | Path) -> list[dict]:
    """导入目录下所有 Excel（跳过合并产物和临时文件）。"""
    directory = Path(directory)
    paths = sorted(
        [
            p
            for p in directory.iterdir()
            if p.is_file()
            and p.suffix.lower() in {".xls", ".xlsx"}
            and not p.name.startswith("~$")
            and not p.name.lower().endswith("new.xlsx")
        ]
    )
    return [import_path(conn, p) for p in paths]


def sync_paths(conn, paths: list[str | Path]) -> list[dict]:
    return [import_path(conn, p) for p in paths]


def ensure_db(db_path) -> None:
    conn = db.connect(db_path)
    conn.close()
