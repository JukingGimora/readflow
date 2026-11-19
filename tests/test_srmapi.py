"""vendorQuery API 行解析测试（离线，不依赖 SRM）。"""
from __future__ import annotations

from qflow import srmapi

SAMPLE = {
    "id": "c3277c0f-8c32-4d07-80ff-7d4468ca9f9c",
    "createTime": "2026-06-08",
    "postDate": "2026-06-04",
    "refNo": "5005850554",
    "projectName": "玉米赤霉烯酮",
    "itemAmount": 903.8,
    "projectResult": "423.690",
    "dedRange": 400.0,
    "formula": "45.19000*|20.000|",
    "qty": 45.19,
    "poNo": "4100227923",
}


def test_parse_api_row_maps_page_columns():
    rec = srmapi.parse_api_row(SAMPLE)
    assert rec["asn"] == "5005850554"
    assert rec["item"] == "玉米赤霉烯酮"
    assert rec["amount"] == 903.8
    assert rec["biz_date"] == "2026-06-04"  # 制单日期 = postDate
    assert rec["result_text"] == "423.690"
    assert rec["range_text"] == 400.0
    assert rec["formula"] == "45.19000*|20.000|"
    assert rec["qty"] == 45.19
    assert rec["order_no"] == "4100227923"
    assert rec["id"] == SAMPLE["id"]


def test_parse_api_row_rejects_incomplete():
    assert srmapi.parse_api_row({"refNo": "", "itemAmount": 1}) is None
    assert srmapi.parse_api_row({"refNo": "X", "itemAmount": None}) is None


def test_parse_api_row_amount_string():
    rec = srmapi.parse_api_row({**SAMPLE, "itemAmount": "1,234.50", "postDate": None})
    assert rec["amount"] == 1234.5
    assert rec["biz_date"] == "2026-06-08"  # postDate 空时退到 createTime
