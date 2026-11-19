"""测试夹具：临时库 + 小样本数据（不依赖仓库里的真实 Excel）。"""
from __future__ import annotations

import pandas as pd

from qflow import db, importer

SHIPMENTS = [
    {
        "发货单号": "ASN2511010001",
        "收货日期": "2025-11-02",
        "实收数量": 49.5,
        "含税金额": 115335.0,
        "车牌号-物流信息": "陕A12345",
    },
    {
        "发货单号": "ASN2511010002",
        "收货日期": "2025-11-03",
        "实收数量": 40.0,
        "含税金额": 93200.0,
        "车牌号-物流信息": "陕B54321",
    },
    {
        "发货单号": "ASN2512010003",
        "收货日期": "2025-12-01",
        "实收数量": 30.0,
        "含税金额": 69900.0,
        "车牌号-物流信息": "陕C00001",
    },
]

DEDUCTIONS = [
    {
        "收货单号(必填)": "ASN2511010001",
        "检测项目名称(必填)": "水分",
        "检测结果(必填)": "15.5",
        "扣罚金额(必填)": 100.0,
        "制单日期(必填)": "2025-11-05",
    },
    {
        "收货单号(必填)": "ASN2511010001",
        "检测项目名称(必填)": "杂质",
        "扣罚金额(必填)": 50.0,
        "制单日期(必填)": "2025-11-05",
    },
    {
        "收货单号(必填)": "ASN2511020004",
        "检测项目名称(必填)": "水分",
        "扣罚金额(必填)": 30.0,
        "制单日期(必填)": "2025-11-06",
    },
    {
        "收货单号(必填)": "ASN2511010002",
        "检测项目名称(必填)": "杂质",
        "扣罚金额(必填)": 20.0,
        "制单日期(必填)": "2025-12-02",
    },
]


def seed(conn) -> None:
    importer.import_frame(
        conn, pd.DataFrame(SHIPMENTS), "shipments", "test_ship", "ship.xlsx"
    )
    importer.import_frame(
        conn, pd.DataFrame(DEDUCTIONS), "deductions", "test_ded", "ded.xlsx"
    )


def make_seeded_db(path) -> None:
    conn = db.connect(path)
    try:
        seed(conn)
    finally:
        conn.close()
