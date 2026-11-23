"""Runtime configuration."""
from __future__ import annotations

import os
from pathlib import Path

# readflow/ 项目根目录（qflow/config.py -> qflow -> readflow）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

# 账号、登录态、页面导入的图谱都放这里；可用环境变量挪到服务器挂载盘
DATA_DIR = Path(os.environ.get("QFLOW_DATA") or DEFAULT_DATA_DIR).expanduser()
SYSTEMS_DIR = PROJECT_ROOT / "systems"

TZ_NAME = os.environ.get("QFLOW_TZ", "Asia/Shanghai")

# 服务端鉴权：设置后除 /health 外的所有接口必须带 X-API-Key 或 Bearer Token；
# 页面接口 /desk/api/* 还可以用登录后的会话 cookie
DEFAULT_TOKEN = os.environ.get("QFLOW_TOKEN", "").strip()
# 页面登录口令（给人输入）；不设就用 QFLOW_TOKEN 本身。只在设了 QFLOW_TOKEN 时生效
DESK_PASS = os.environ.get("QFLOW_DESK_PASS", "").strip()

# 同一个请求多少秒内不重复发给业务系统（连着追问时不用反复拉全量）
CACHE_TTL = float(os.environ.get("QFLOW_CACHE_TTL", "120"))

# 登录用的浏览器：默认不显示窗口。设 QFLOW_BROWSER_HEADLESS=0 并提供 DISPLAY（例如 Xvfb）就用真窗口
BROWSER_HEADLESS = os.environ.get("QFLOW_BROWSER_HEADLESS", "1") != "0"
CHROME_BINARY = os.environ.get("QFLOW_CHROME_BINARY", "")
CHROME_DRIVER = os.environ.get("QFLOW_CHROME_DRIVER", "")

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
