"""只读护栏：判断一个请求或一次点击是不是写操作。

当前运行层只做读这一级；任何要发往业务系统的请求都应先过
`assert_readonly_request`，写路径直接拒绝。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger("qflow.online")

WRITE_URL_RE = re.compile(
    r"(?:^|/)(?:save|submit|delete|remove|update|edit|add|create|approve|audit|"
    r"ship|deliver|confirm|commit|pay|settle|return|cancel|void|export|print|upload)"
    r"(?:[/?#]|$)",
    re.IGNORECASE,
)
ACTION_WORDS = (
    "保存", "提交", "删除", "移除", "发货", "审批", "审核", "确认", "确定",
    "新增", "编辑", "修改", "作废", "退回", "付款", "结算", "导出", "打印",
    "上传", "停用", "启用",
)
# 菜单/导航通常以这些词结尾，不能因为包含"发货/结算"就被当成写按钮
NAV_SUFFIXES = (
    "管理", "记录", "看板", "清单", "列表", "明细", "查询", "统计", "报表",
    "详情", "首页", "设置", "概览", "中心", "分析", "日志", "信息", "档案",
)


class ReadOnlyViolation(RuntimeError):
    """尝试执行写操作时抛出。"""


def is_write_request(method: str, url: str) -> bool:
    method = (method or "").upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return False
    # DELETE / PUT / PATCH 一律视为写；POST 允许查询类接口（很多后台列表用 POST）
    if method in {"DELETE", "PUT", "PATCH"}:
        return True
    if method != "POST":
        return True
    path = urlparse(url).path or ""
    return bool(WRITE_URL_RE.search(path))


def is_dangerous_click(text: str | None) -> bool:
    """按钮/菜单文本是否可能触发写操作。

    导航项（如"发货管理""发货记录""结算管理"）放行；真正动作按钮
    （"保存""发货""确认发货""删除记录"）拦截。
    """
    if not text:
        return False
    normalized = re.sub(r"\s+", "", str(text))
    if normalized.startswith(("删除", "移除", "清空", "作废")):
        return True
    if normalized.endswith(NAV_SUFFIXES):
        return False
    return any(word in normalized for word in ACTION_WORDS)


def assert_readonly_request(method: str, url: str) -> None:
    if is_write_request(method, url):
        raise ReadOnlyViolation(f"已拦截写请求: {method} {url}")
