"""Runtime configuration."""
from __future__ import annotations

import os
from pathlib import Path

# readflow/ 项目根目录（qflow/config.py -> qflow -> readflow）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

# 可用环境变量覆盖，方便把库放到服务器挂载盘
DEFAULT_DB = Path(
    os.environ.get("QFLOW_DB") or (DEFAULT_DATA_DIR / "queryflow.db")
).expanduser()

TZ_NAME = os.environ.get("QFLOW_TZ", "Asia/Shanghai")

# 服务端鉴权：设置后除 /health 外的所有接口必须带 X-API-Key 或 Bearer Token；
# 页面接口 /desk/api/* 还可以用登录后的会话 cookie
DEFAULT_TOKEN = os.environ.get("QFLOW_TOKEN", "").strip()
# 页面登录口令（给人输入）；不设就用 QFLOW_TOKEN 本身。只在设了 QFLOW_TOKEN 时生效
DESK_PASS = os.environ.get("QFLOW_DESK_PASS", "").strip()

# 实时模式：查询时登录 SRM 现场读取，不落业务数据；缓存模式只用于测试/演示
LIVE_MODE = os.environ.get("QFLOW_LIVE", "0") == "1"
LIVE_TTL = float(os.environ.get("QFLOW_LIVE_TTL", "120"))

# voiceAI 侧对外的默认端口（只监听 127.0.0.1 时由本机 voiceAI 调用）
DEFAULT_HOST = os.environ.get("QFLOW_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("QFLOW_PORT", "8790"))

# 服务器端 VoiceLab 语音引擎地址（ASR/TTS 统一中枢）
# 本地小模型（OpenAI 兼容的 chat/completions 地址）。不设就只用规则。
# 只用来做一道闭集选择题，见 qflow/understand.py。
LLM_URL = os.environ.get("QFLOW_LLM_URL", "").strip()
LLM_MODEL = os.environ.get("QFLOW_LLM_MODEL", "qwen3")
LLM_TIMEOUT = float(os.environ.get("QFLOW_LLM_TIMEOUT", "3"))

VOICELAB_URL = os.environ.get("VOICELAB_URL", "http://127.0.0.1:8040").rstrip("/")
VOICELAB_AGENT_ID = os.environ.get("VOICELAB_AGENT_ID", "snxt_deduction")


def resolve_db(path: str | os.PathLike | None = None) -> Path:
    """把 CLI/API 传入的 db 路径统一成绝对路径。"""
    if path:
        return Path(path).expanduser().resolve()
    return DEFAULT_DB
