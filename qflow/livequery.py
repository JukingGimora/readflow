"""实时查询：用圣农的登录态，按图谱读取质量扣款明细 → 内存汇总。

不落库：结果只存在内存快照里，TTL 过期重新读。
服务启动时后台预热 + 周期刷新，查询命中的是最近一次实时抓取。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .dates import resolve_range

logger = logging.getLogger("qflow.livequery")

DEDUCTION_OP = "op.deduction.list"
_GRAPH = None


def _graph():
    global _GRAPH
    if _GRAPH is None:
        from . import config, runner

        _GRAPH = runner.load(config.PROJECT_ROOT / "systems" / "sunner_srm" / "graph.json")
    return _GRAPH


class LiveDeduction:
    """扣款明细实时读取。"""

    def __init__(self, ttl: float = 120.0):
        self.ttl = ttl
        self._lock = threading.RLock()
        self.cookies: list[dict] | None = None
        self._session_mtime = 0.0
        self._last_auto_login = 0.0
        self.rows: list[dict] = []
        self.page_total: int | None = None
        self.fetched_at: float | None = None
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = False

    AUTO_LOGIN_INTERVAL = 600.0  # 自动 OCR 登录最多每 10 分钟试一次，避免触发风控

    # ---------- 生命周期 ----------

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop:
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.exception("live refresh failed")
            time.sleep(max(10.0, self.ttl))

    def close(self):
        self._stop = True
        try:
            from . import srmlogin

            srmlogin.get_session().close()
        except Exception:
            pass

    def _sync_session(self) -> None:
        """session.json 被人工登录更新过就重载 cookie。"""
        from . import srmapi

        mtime = srmapi.session_mtime()
        if mtime != self._session_mtime:
            self._session_mtime = mtime
            self.cookies = srmapi.load_session()

    @property
    def age(self) -> float | None:
        return (time.time() - self.fetched_at) if self.fetched_at else None

    def fresh(self) -> bool:
        return self.rows and self.age is not None and self.age < self.ttl

    # ---------- 抓取 ----------

    def _fetch_api(self) -> tuple[list[dict], int | None]:
        """纯 HTTP 拉取：cookie 有效直接用；失效先自动 OCR，失败抛 AuthError 等人工。

        接口地址、翻页、字段对应都来自图谱（systems/sunner_srm/graph.json），
        由通用执行器执行；这里只剩圣农的登录态管理（T5 之后也会搬走）。
        """
        from . import runner, srmapi, srmlogin

        def fetch(cookies):
            send = runner.http_sender(srmapi.opener_from_cookies(cookies))
            rows, trace = runner.fetch(_graph(), DEDUCTION_OP, {}, send)
            return rows, trace["reported_total"]

        self._sync_session()
        cookies = self.cookies or srmapi.load_session()
        if cookies:
            try:
                return fetch(cookies)
            except (runner.AuthExpired, runner.RunError) as exc:
                self.last_error = f"会话失效：{exc}"
                logger.warning("api auth failed: %s", exc)

        # 自动 OCR 登录：限频，避免验证码接口被风控
        if time.time() - self._last_auto_login > self.AUTO_LOGIN_INTERVAL:
            self._last_auto_login = time.time()
            try:
                if srmlogin.get_session().auto_login():
                    self._session_mtime = 0.0
                    self._sync_session()
                    cookies = self.cookies
                    if cookies:
                        return fetch(cookies)
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto_login error: %s", exc)

        raise srmapi.AuthError("SRM 需要人工登录：打开查询页面完成验证")

    def refresh(self) -> list[dict]:
        with self._lock:
            # 排队等锁期间别人可能已经刷好了
            if self.fresh():
                return list(self.rows)
            self._sync_session()
            rows, page_total = self._fetch_api()
            self.rows = rows
            self.page_total = page_total
            self.fetched_at = time.time()
            self.last_error = None
            logger.info("live refresh: %d rows (page_total=%s)", len(rows), page_total)
            return rows

    def snapshot(self) -> list[dict]:
        if not self.fresh():
            try:
                self.refresh()
            except Exception as exc:
                self.last_error = str(exc)
                if self.rows:
                    # 刷新失败：先用上一次快照兜底，不崩
                    logger.warning("refresh failed, serving stale snapshot: %s", exc)
                    return list(self.rows)
                raise RuntimeError(str(exc)) from exc
        return list(self.rows)

    # ---------- 查询 ----------

    def summary(self, start=None, end=None, item=None, asn=None, group_by="item", limit=20) -> dict:
        rows = self.snapshot()
        filtered = self._filter(rows, start, end, item, asn)
        groups: dict[str, dict] = {}
        for record in filtered:
            bucket = {
                "item": record["item"] or "未注明",
                "month": (record["biz_date"] or "")[:7] or "未知",
                "asn": record["asn"],
            }.get(group_by if group_by in ("item", "month", "asn") else "item", record["item"] or "未注明")
            slot = groups.setdefault(bucket, {"n": 0, "total": 0.0, "max": 0.0, "asns": set()})
            slot["n"] += 1
            slot["total"] += record["amount"] or 0.0
            slot["max"] = max(slot["max"], record["amount"] or 0.0)
            slot["asns"].add(record["asn"])
        group_rows = sorted(
            (
                {
                    "bucket": key,
                    "n": value["n"],
                    "total": round(value["total"], 2),
                    "avg": round(value["total"] / value["n"], 2),
                    "max_amount": round(value["max"], 2),
                    "asn_count": len(value["asns"]),
                }
                for key, value in groups.items()
            ),
            key=lambda row: -row["total"],
        )[: max(1, min(int(limit), 200))]
        return {
            "start": start,
            "end": end,
            "range_label": resolve_range({"start": start, "end": end})[2],
            "group_by": group_by if group_by in ("item", "month", "asn") else "item",
            "filters": {"asn": asn, "item": item},
            "count": len(filtered),
            "total_amount": round(sum(record["amount"] or 0.0 for record in filtered), 2),
            "asn_count": len({record["asn"] for record in filtered}),
            "groups": group_rows,
            "live_fetched_at": self.fetched_iso(),
        }

    def detail(self, start=None, end=None, item=None, asn=None, limit=50) -> dict:
        rows = self.snapshot()
        filtered = self._filter(rows, start, end, item, asn)
        ordered = sorted(
            filtered,
            key=lambda record: (record["biz_date"] or "", record["amount"] or 0.0),
            reverse=True,
        )
        limit = max(1, min(int(limit), 500))
        result_rows = [
            {
                "asn": record.get("asn"),
                "biz_date": record.get("biz_date") or None,
                "item": record.get("item"),
                "order_no": record.get("order_no"),
                "result_text": record.get("result_text"),
                "range_text": record.get("range_text"),
                "formula": record.get("formula"),
                "amount": record.get("amount"),
                "qty": record.get("qty"),
                "source_file": "live:srm",
                "dataset": "live",
            }
            for record in ordered[:limit]
        ]
        return {
            "start": start,
            "end": end,
            "range_label": resolve_range({"start": start, "end": end})[2],
            "filters": {"asn": asn, "item": item},
            "count": len(filtered),
            "total_amount": round(sum(record["amount"] or 0.0 for record in filtered), 2),
            "returned": len(result_rows),
            "rows": result_rows,
            "live_fetched_at": self.fetched_iso(),
        }

    def overview(self) -> dict:
        rows = self.snapshot()
        dates = sorted(record["biz_date"] or "" for record in rows if record["biz_date"])
        return {
            "mode": "live",
            "count": len(rows),
            "page_total": self.page_total,
            "min_date": dates[0] if dates else None,
            "max_date": dates[-1] if dates else None,
            "total_amount": round(sum(record["amount"] or 0.0 for record in rows), 2),
            "live_fetched_at": self.fetched_iso(),
        }

    @staticmethod
    def _filter(rows, start, end, item, asn):
        result = []
        for record in rows:
            date = record["biz_date"] or ""
            if start and date < start:
                continue
            if end and date > end:
                continue
            if asn and record["asn"] != asn:
                continue
            if item and (not record["item"] or item not in record["item"]):
                continue
            result.append(record)
        return result

    def fetched_iso(self) -> str | None:
        if self.fetched_at is None:
            return None
        return datetime.fromtimestamp(self.fetched_at, timezone.utc).astimezone().isoformat(timespec="seconds")


_client: LiveDeduction | None = None
_client_lock = threading.Lock()


def get_client() -> LiveDeduction:
    global _client
    with _client_lock:
        if _client is None:
            import os

            ttl = float(os.environ.get("QFLOW_LIVE_TTL", "120"))
            _client = LiveDeduction(ttl=ttl)
            _client.start()
        return _client
