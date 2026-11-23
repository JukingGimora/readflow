"""测试用的固定数据：圣农扣款接口返回的样子（键名照图谱里的 map，数值是编的）。"""

import json

from qflow import config

SRM_GRAPH = config.PROJECT_ROOT / "systems" / "sunner_srm" / "graph.json"
EXAMPLE_GRAPH = config.PROJECT_ROOT / "docs" / "examples" / "graph-example.json"
SRM_ROWS = [
    {"id": "1", "refNo": "ASN2511010001", "postDate": "2025-11-05", "projectName": "水分", "projectResult": "15.5", "itemAmount": "100.00"},
    {"id": "2", "refNo": "ASN2511010001", "postDate": "2025-11-05", "projectName": "杂质", "itemAmount": "50.00"},
    {"id": "3", "refNo": "ASN2511010002", "postDate": "2025-12-02", "projectName": "杂质", "itemAmount": "20.00"},
    {"id": "4", "refNo": "ASN2511020004", "postDate": "2025-11-06", "projectName": "水分", "itemAmount": "30.00"},
]


def srm_sender(page_size_seen=None, calls=None):
    """假装是扣款接口：分页返回 SRM_ROWS。page_size_seen 模拟「系统把每页条数压到自己的上限」。"""

    def send(method, url, params, body_type, headers):
        if calls is not None:
            calls.append({"method": method, "url": url, "params": dict(params), "body_type": body_type})
        size = int(params["rows"]) if page_size_seen is None else page_size_seen
        page = int(params["page"])
        chunk = SRM_ROWS[(page - 1) * size: page * size]
        return {"status": 200, "url": url,
                "text": json.dumps({"success": True, "total": len(SRM_ROWS), "rows": chunk}, ensure_ascii=False)}

    return send
