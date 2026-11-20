"""系统注册表：systems/*.json 描述每个业务系统，页面/认证层按它驱动。

每个系统卡片需要的最小信息：
    id / label / base_url / auth_kind / status / data_as_of
"""
from __future__ import annotations

import json

from . import config

SYSTEMS_DIR = config.PROJECT_ROOT / "systems"


def _auth_kind(spec: dict) -> str:
    auth = spec.get("auth") or {}
    captcha = auth.get("captcha")
    if isinstance(captcha, dict) and captcha:
        # image / sms / slider ...
        return str(captcha.get("type") or "image_captcha")
    if captcha:
        return "image_captcha"
    mode = str(auth.get("mode") or "")
    if "sms" in mode:
        return "sms"
    if mode == "form":
        return "form"
    return "unknown"


def load_specs() -> dict[str, dict]:
    specs: dict[str, dict] = {}
    if not SYSTEMS_DIR.exists():
        return specs
    for path in sorted(SYSTEMS_DIR.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = spec.get("name") or path.stem
        specs[sid] = {
            "id": sid,
            "label": spec.get("label") or sid,
            "base_url": spec.get("base_url"),
            "auth_kind": _auth_kind(spec),
            "has_crawl": bool(spec.get("crawl")),
        }
    return specs


def list_systems() -> list[dict]:
    return list(load_specs().values())
