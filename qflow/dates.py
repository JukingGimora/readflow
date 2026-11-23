"""日期小工具：当前时间、把各种写法的日期统一成 YYYY-MM-DD、一个月的首尾。"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import TZ_NAME

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


def to_date_str(value) -> str | None:
    """把字符串或日期对象统一成 YYYY-MM-DD；认不出来返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
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
