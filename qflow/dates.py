"""日期解析与区间计算.

数据里最麻烦的是"日期不一定有"：
- 新导出（deduct.xls）有 制单日期；
- 老导出（(1).xls 等）没有日期，但 ASN 号里嵌了 YYMMDD；
所以统一用 biz_date = 制单日期 or ASN 内嵌日期，保证按月查询可用。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import TZ_NAME

ASN_DATE_RE = re.compile(r"^ASN(\d{2})(\d{2})(\d{2})\d+$", re.IGNORECASE)
_DATE_RE = re.compile(
    r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)
_CN_DATE_RE = re.compile(
    r"^(\d{4})年(\d{1,2})月(\d{1,2})日"
    r"(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ_NAME)
    except Exception:  # pragma: no cover - 极端环境无 tzdata
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(tz())


def asn_date(asn: str | None) -> str | None:
    """ASN2511210605 -> 2025-11-21；解析失败返回 None。"""
    if not asn:
        return None
    m = ASN_DATE_RE.match(str(asn).strip())
    if not m:
        return None
    yy, mm, dd = (int(x) for x in m.groups())
    try:
        return date(2000 + yy, mm, dd).isoformat()
    except ValueError:
        return None


def to_date_str(value) -> str | None:
    """把 Excel/字符串/日期统一成 YYYY-MM-DD。"""
    if value is None:
        return None
    if isinstance(value, datetime):  # pandas.Timestamp 也是 datetime 子类
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if text.lower() in {"", "nan", "nat", "none", "null", "na", "<na>"}:
        return None

    m = _DATE_RE.match(text)
    if not m:
        m = _CN_DATE_RE.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def to_datetime_str(value) -> str | None:
    """保留发货时间（如有），否则退化为日期字符串。"""
    if isinstance(value, datetime):
        if value.hour or value.minute or value.second:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip() if value is not None else ""
    if text.lower() in {"", "nan", "nat", "none", "null", "na", "<na>"}:
        return None

    m = _DATE_RE.match(text) or _CN_DATE_RE.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            base = date(year, month, day).isoformat()
        except ValueError:
            return None
        if m.group(4) is None:
            return base
        hour, minute = int(m.group(4)), int(m.group(5))
        second = int(m.group(6) or 0)
        return f"{base} {hour:02d}:{minute:02d}:{second:02d}"

    try:
        parsed = datetime.fromisoformat(text)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def month_bounds(month: str) -> tuple[str, str]:
    """'2025-11' -> ('2025-11-01', '2025-11-30')。"""
    m = re.match(r"^(\d{4})-(\d{1,2})$", str(month).strip())
    if not m:
        raise ValueError(f"month 需为 YYYY-MM，收到: {month!r}")
    year, mon = int(m.group(1)), int(m.group(2))
    if not 1 <= mon <= 12:
        raise ValueError(f"月份越界: {month!r}")
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def shift_month(month: str, delta: int) -> str:
    year, mon = (int(x) for x in month.split("-"))
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def resolve_range(params: dict, now: datetime | None = None) -> tuple[str | None, str | None, str]:
    """把查询参数里的 start/end/month/days 归一成闭区间。

    返回 (start, end, label)。start/end 为 None 表示不按时间过滤。
    """
    now = now or now_local()
    start = to_date_str(params.get("start"))
    end = to_date_str(params.get("end"))
    month = str(params.get("month") or "").strip()
    days = params.get("days")

    if month:
        start, end = month_bounds(month)
    elif days not in (None, "", 0, "0"):
        count = max(1, min(int(days), 3660))
        end = now.date().isoformat()
        start = (now.date() - timedelta(days=count - 1)).isoformat()

    if start and not end:
        end = start
    if end and not start:
        start = end
    if start and end and start > end:
        start, end = end, start

    if not start and not end:
        label = "全部数据"
    elif start == end:
        label = start
    else:
        label = f"{start} 至 {end}"
    return start, end, label
