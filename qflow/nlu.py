"""轻量中文意图解析（voiceAI 已做 NLU 时可直接传 intent，本模块作兜底）.

支持：
- 时间：本月/上个月/今年/去年/去年11月/2025年11月/今天/昨天/本周/上周/最近7天
- 实体：ASN 单号、车牌、检测项目
- 意图：扣款汇总/明细、发货汇总/明细、对账、数据概览

不追求通用 NLP，只覆盖"老板问一句"的高频句式；解析结果永远带 confidence，
低置信度时服务端可以要求 voiceAI 澄清。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .dates import month_bounds, now_local

ASN_RE = re.compile(r"(?<![A-Za-z0-9])(ASN\d{6,})(?![0-9])", re.IGNORECASE)
PLATE_RE = re.compile(
    r"([京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]"
    r"[A-Za-z][A-Za-z0-9]{5,6})"
)
ITEM_NAMES = [
    "玉米赤霉烯酮",
    "黄曲霉毒素",
    "不完善粒",
    "霉变粒",
    "生霉粒",
    "破损粒",
    "水分",
    "杂质",
    "容重",
    "色泽",
    "气味",
]

# 口语里的叫法 → 标准名。TODO(T3): 换成图谱 entities[].fields[].synonyms。
ITEM_ALIASES = {"水份": "水分", "含水": "水分", "黄曲霉": "黄曲霉毒素", "赤霉烯酮": "玉米赤霉烯酮", "霉变": "霉变粒", "生霉": "生霉粒"}

RECON_KEYS = ("对账", "差额", "净额", "抵扣", "扣完", "结算差")
SHIP_KEYS = ("发货", "收货", "到货", "物流", "车牌", "车号", "实收", "装车", "运输")
DED_KEYS = ("扣款", "扣罚", "罚款", "扣钱", "检测", "扣了", "扣多少", "订单号")
OVERVIEW_KEYS = ("概览", "总体", "数据情况", "多少数据", "数据量", "统计一下")
DETAIL_KEYS = ("明细", "列表", "哪几笔", "哪几单", "详情", "具体", "查一下", "看看", "列出", "有哪些", "哪些")
# 明确在指「某一条 / 每一条」的说法；出现了就按明细，除非要总数的词更多。
POINTING_KEYS = (
    "明细", "哪几", "哪天", "哪一", "每一笔", "每笔", "每车", "每一车", "每单", "一条条", "一条一条", "一笔笔", "一笔一笔", "逐笔", "逐车",
    "那车", "那单", "那趟", "那笔", "那几笔", "为什么", "为啥", "单号", "念一下", "报一下", "列一下", "列出",
)
SUMMARY_KEYS = ("多少", "合计", "总额", "总计", "统计", "汇总", "分布", "排名", "一共", "总共", "合计")


@dataclass
class ParsedQuery:
    text: str
    intent: str
    params: dict = field(default_factory=dict)
    confidence: float = 0.5
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "intent": self.intent,
            "params": self.params,
            "confidence": self.confidence,
            "notes": self.notes,
        }


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


def extract_time(text: str, now: datetime | None = None) -> dict:
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
        bounds = _month_of(today.year, int(m.group(1)))
        if bounds:
            return {"start": bounds[0], "end": bounds[1]}

    return {}


def extract_entities(text: str) -> dict:
    entities: dict = {}
    asn = ASN_RE.search(text)
    if asn:
        entities["asn"] = asn.group(1).upper()
    plate = PLATE_RE.search(text)
    if plate:
        entities["plate"] = plate.group(1).upper()
    item = next((name for name in ITEM_NAMES if name in text), None)
    if not item:
        item = next((name for alias, name in ITEM_ALIASES.items() if alias in text), None)
    if item:
        entities["item"] = item
    return entities


def wants_detail(text: str, params: dict | None = None) -> bool:
    """要逐条记录还是要总数。只看提示词，不问模型（实测小模型在这件事上偏向明细）。"""
    params = params or {}
    if "asn" in params or "plate" in params:
        return True
    summary = sum(1 for key in set(SUMMARY_KEYS) if key in text)
    pointing = sum(1 for key in POINTING_KEYS if key in text)
    if pointing:
        return pointing >= summary
    return sum(1 for key in DETAIL_KEYS if key in text) > summary


def _score(text: str) -> dict[str, int]:
    scores = {"reconcile": 0, "shipment": 0, "deduction": 0, "overview": 0}
    for key in RECON_KEYS:
        if key in text:
            scores["reconcile"] += 3
    for key in DED_KEYS:
        if key in text:
            scores["deduction"] += 2
    for key in SHIP_KEYS:
        if key in text:
            scores["shipment"] += 2
    for key in OVERVIEW_KEYS:
        if key in text:
            scores["overview"] += 3
    return scores


def parse(text: str, now: datetime | None = None) -> ParsedQuery:
    text = (text or "").strip()
    if not text:
        return ParsedQuery(
            text=text,
            intent="overview",
            confidence=0.2,
            notes=["空请求，返回数据概览"],
        )

    params: dict = {}
    params.update(extract_time(text, now))
    entities = extract_entities(text)
    params.update(entities)

    scores = _score(text)
    if "item" in entities:
        scores["deduction"] += 1  # 检测项目只出现在扣款里
    best = max(scores, key=scores.get)
    confidence = 0.55
    notes: list[str] = []

    if scores[best] == 0:
        intent = "overview"
        confidence = 0.3
        notes.append("未识别具体业务意图，返回数据概览")
    elif best == "reconcile":
        intent = "reconcile"
        confidence = 0.9
    elif best == "deduction":
        summary_hits = sum(1 for key in SUMMARY_KEYS if key in text)
        if "asn" in params:
            intent = "deduction.detail"
            confidence = 0.95
        elif wants_detail(text, params):
            intent = "deduction.detail"
            confidence = 0.85
        else:
            intent = "deduction.summary"
            confidence = 0.85 if summary_hits else 0.75
    elif best == "shipment":
        if wants_detail(text, params):
            intent = "shipment.detail"
        else:
            intent = "shipment.summary"
        confidence = 0.85
    else:
        intent = "overview"
        confidence = 0.8

    if intent in {"deduction.detail", "deduction.summary"} and any(
        key in text for key in ("扣款", "扣罚", "罚款")
    ):
        confidence = min(0.98, confidence + 0.05)
    if intent != "overview" and not params.get("start") and not params.get("end"):
        notes.append("未指定时间范围，按全部数据查询")
    if "item" in params and intent.startswith("shipment"):
        notes.append("检测项目仅对扣款数据有效，已忽略")

    return ParsedQuery(
        text=text,
        intent=intent,
        params=params,
        confidence=round(confidence, 2),
        notes=notes,
    )
