"""SQLite storage for the read-only query layer.

两张事实表 + 一张同步审计表：
- shipments：一行一个发货单（多行订单会聚合，line_count 记录行数）
- deductions：一行一笔扣款（同一 ASN 可多笔，检测项目不同）
- sync_runs：每次导入的来源、行数、状态，便于回答"数据截止到什么时候"
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    rows_read     INTEGER NOT NULL DEFAULT 0,
    rows_written  INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'running',
    error         TEXT
);

CREATE TABLE IF NOT EXISTS shipments (
    asn             TEXT PRIMARY KEY,
    biz_date        TEXT,
    ship_date       TEXT,
    receive_date    TEXT,
    shipped_qty     REAL,
    received_qty    REAL,
    unit_price      REAL,
    amount          REAL,
    tax_free_amount REAL,
    plate           TEXT,
    ref_no          TEXT,
    line_count      INTEGER NOT NULL DEFAULT 1,
    dataset         TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    imported_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deductions (
    dedup_key     TEXT PRIMARY KEY,
    asn           TEXT NOT NULL,
    biz_date      TEXT,
    item          TEXT,
    range_text    TEXT,
    result_text   TEXT,
    standard_text TEXT,
    formula       TEXT,
    amount        REAL,
    qty           REAL,
    unit_price    REAL,
    amount_incl   REAL,
    order_no      TEXT,
    dataset       TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    imported_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shipments_biz ON shipments(biz_date);
CREATE INDEX IF NOT EXISTS idx_shipments_plate ON shipments(plate);
CREATE INDEX IF NOT EXISTS idx_deductions_asn ON deductions(asn);
CREATE INDEX IF NOT EXISTS idx_deductions_biz ON deductions(biz_date);
CREATE INDEX IF NOT EXISTS idx_deductions_item ON deductions(item);
CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接并确保 schema 存在。调用方负责 close。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "shipments": conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
        "deductions": conn.execute("SELECT COUNT(*) FROM deductions").fetchone()[0],
        "sync_runs": conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0],
    }


def last_successful_sync(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(finished_at) FROM sync_runs WHERE status = 'success'"
    ).fetchone()
    return row[0] if row and row[0] else None
