"""从一句中文里取时间。只有规则，不问模型（实测小模型会漏、会编时间）。和具体系统无关。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .dates import month_bounds, now_local


def _mk_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _month_of(year: int, month: int) -> tuple[str, str] | None:
    if not 1 <= month <= 12:
        return None
    return month_bounds(f"{year:04d}-{month:02d}")


_CN_DIGIT = {c: i for i, c in enumerate("零一二三四五六七八九")}
_CN_NUM_RE = re.compile(r"(?<![上这每近个第])(三十一|三十|二十[一二三四五六七八九]?|十[一二三四五六七八九]?|[一二两三四五六七八九])(?=月|号|日(?!子))")
_SPOKEN = (
    ("这个礼拜", "这周"), ("这礼拜", "这周"), ("这个星期", "这周"), ("本礼拜", "这周"),
    ("上个礼拜", "上周"), ("上礼拜", "上周"), ("上个星期", "上周"), ("上星期", "上周"),
)


def _cn_number(word: str) -> int:
    if word == "两":
        return 2
    if "十" not in word:
        return _CN_DIGIT[word]
    tens, _, ones = word.partition("十")
    return (_CN_DIGIT[tens] if tens else 1) * 10 + (_CN_DIGIT[ones] if ones else 0)


def normalize_time_words(text: str) -> str:
    """语音识别常把月份、日期写成汉字（十一月、三号）；规则只认数字，这里先换掉。"""
    t = text.replace(" ", "")
    for spoken, standard in _SPOKEN:
        t = t.replace(spoken, standard)
    return _CN_NUM_RE.sub(lambda m: str(_cn_number(m.group(1))), t)


def extract_time(text: str, now: datetime | None = None, hint_year: int | None = None) -> dict:
    """一句话里的时间 → {"start": ..., "end": ...}；没说时间返回 {}。

    hint_year：上一问查的是哪一年。只说「12 月」时沿用它；没有上一问就取最近的那个 12 月（不取还没到的）。
    """
    now = now or now_local()
    today = now.date()
    t = normalize_time_words(text)
    year_of = {"去年": today.year - 1, "今年": today.year, "前年": today.year - 2, "": today.year}

    # 0) 一段月份：今年3月到5月 / 3月至5月；上半年 / 去年下半年
    m = re.search(r"(去年|今年|前年|)(\d{1,2})月(?:份)?(?:到|至|[-~—])(\d{1,2})月", t)
    if m:
        first = _month_of(year_of[m.group(1)], int(m.group(2)))
        last = _month_of(year_of[m.group(1)], int(m.group(3)))
        if first and last and first[0] <= last[1]:
            return {"start": first[0], "end": last[1]}
    m = re.search(r"(去年|今年|前年|)(上|下)半年", t)
    if m:
        year = year_of[m.group(1)]
        return (
            {"start": f"{year}-01-01", "end": f"{year}-06-30"}
            if m.group(2) == "上"
            else {"start": f"{year}-07-01", "end": f"{year}-12-31"}
        )

    # 1) 明确日期：2025-11-21 / 2025/11/21 / 2025.11.21
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        value = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if value:
            return {"start": value, "end": value}

    # 2) 2025年11月21日
    m = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})[日号]", t)
    if m:
        value = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if value:
            return {"start": value, "end": value}

    # 3) 2025年11月
    m = re.search(r"(20\d{2})年(\d{1,2})月", t)
    if m:
        bounds = _month_of(int(m.group(1)), int(m.group(2)))
        if bounds:
            return {"start": bounds[0], "end": bounds[1]}

    # 4) 去年11月 / 今年3月 / 前年12月
    m = re.search(r"(去年|今年|前年)(\d{1,2})月", t)
    if m:
        offset = {"去年": -1, "今年": 0, "前年": -2}[m.group(1)]
        bounds = _month_of(today.year + offset, int(m.group(2)))
        if bounds:
            return {"start": bounds[0], "end": bounds[1]}

    # 5) 本月 / 这个月 / 当月 / 上月 / 上个月
    if any(word in t for word in ("本月", "这个月", "当月", "这月")):
        bounds = _month_of(today.year, today.month)
        return {"start": bounds[0], "end": bounds[1]}
    if any(word in t for word in ("上月", "上个月")):
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        bounds = _month_of(year, month)
        return {"start": bounds[0], "end": bounds[1]}

    # 6) 今天 / 昨天 / 前天
    if any(word in t for word in ("今天", "今日", "当天")):
        value = today.isoformat()
        return {"start": value, "end": value}
    if any(word in t for word in ("昨天", "昨日")):
        value = (today - timedelta(days=1)).isoformat()
        return {"start": value, "end": value}
    if "前天" in t:
        value = (today - timedelta(days=2)).isoformat()
        return {"start": value, "end": value}

    # 7) 本周 / 上周
    if any(word in t for word in ("本周", "这周", "本星期", "这星期")):
        monday = today - timedelta(days=today.weekday())
        return {"start": monday.isoformat(), "end": today.isoformat()}
    if "上周" in t:
        monday = today - timedelta(days=today.weekday() + 7)
        return {"start": monday.isoformat(), "end": (monday + timedelta(days=6)).isoformat()}

    # 8) 今年 / 去年 / 前年（整年）
    if "今年" in t or "本年" in t:
        return {"start": f"{today.year}-01-01", "end": f"{today.year}-12-31"}
    if "去年" in t:
        year = today.year - 1
        return {"start": f"{year}-01-01", "end": f"{year}-12-31"}
    if "前年" in t:
        year = today.year - 2
        return {"start": f"{year}-01-01", "end": f"{year}-12-31"}

    # 9) 2025年（整年）
    m = re.search(r"(20\d{2})年", t)
    if m:
        year = int(m.group(1))
        return {"start": f"{year}-01-01", "end": f"{year}-12-31"}

    # 10) 最近 7 天 / 近 30 天
    m = re.search(r"(?:最近|近|过去)(\d{1,4})天", t)
    if m:
        days = max(1, int(m.group(1)))
        return {
            "start": (today - timedelta(days=days - 1)).isoformat(),
            "end": today.isoformat(),
        }

    # 11) 只写 11月（默认今年）
    m = re.search(r"(?<![0-9A-Za-z])(\d{1,2})月(?:份)?", t)
    if m:
        month = int(m.group(1))
        year = hint_year or (today.year - 1 if month > today.month else today.year)
        bounds = _month_of(year, month)
        if bounds:
            return {"start": bounds[0], "end": bounds[1]}

    return {}
