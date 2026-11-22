"""圣农扣款接口的字段对应：现在写在图谱里，由通用执行器执行（离线，不依赖 SRM）。"""
from __future__ import annotations

import json

from qflow import livequery

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


def fetch(rows):
    from qflow import runner

    def send(method, url, params, body_type, headers):
        text = json.dumps({"success": True, "total": len(rows), "rows": rows}, ensure_ascii=False)
        return {"status": 200, "url": url, "text": text}

    return runner.fetch(livequery._graph(), livequery.DEDUCTION_OP, {}, send)[0]


def test_graph_maps_api_row_to_the_same_record():
    rec = fetch([SAMPLE])[0]
    assert rec["asn"] == "5005850554"
    assert rec["item"] == "玉米赤霉烯酮"
    assert rec["amount"] == 903.8
    assert rec["biz_date"] == "2026-06-04"  # 制单日期 = postDate
    assert rec["result_text"] == "423.690"
    assert rec["range_text"] == 400.0
    assert rec["formula"] == "45.19000*|20.000|"
    assert rec["qty"] == 45.19
    assert rec["order_no"] == "4100227923"
    assert rec["row_id"] == SAMPLE["id"]


def test_incomplete_rows_are_dropped():
    assert fetch([{"refNo": "", "itemAmount": 1}, {"refNo": "X", "itemAmount": None}]) == []


def test_amount_string_and_date_fallback():
    rec = fetch([{**SAMPLE, "itemAmount": "1,234.50", "postDate": None}])[0]
    assert rec["amount"] == 1234.5
    assert rec["biz_date"] == "2026-06-08"  # postDate 空时退到 createTime


def test_same_row_different_items_kept_exact_duplicates_dropped():
    rows = [SAMPLE, {**SAMPLE, "projectName": "水分"}, SAMPLE]
    assert [r["item"] for r in fetch(rows)] == ["玉米赤霉烯酮", "水分"]
