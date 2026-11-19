"""认证桥：把各系统的登录挑战统一成 challenge/submit/refresh/status。

页面只面对这一个接口，不关心背后是图片验证码、短信还是滑块。
新系统接入：在 systems/<id>.json 描述 + 这里注册一个 bridge。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("qflow.authbridge")


class _SrmAuth:
    """圣农 SRM：图片验证码接力（复用 srmlogin 的 headless 会话）。"""

    system_id = "sunner_srm"

    def _session(self):
        from . import srmlogin

        return srmlogin.get_session()

    def challenge(self) -> dict:
        result = self._session().captcha()
        if result.get("already_logged_in"):
            return {"ok": True, "already_solved": True}
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error")}
        return {
            "ok": True,
            "kind": "image_captcha",
            "image": result.get("image"),
            "hint": "输入图片中的 4 位字符",
        }

    def refresh(self) -> dict:
        result = self._session().refresh_captcha()
        if result.get("already_logged_in"):
            return {"ok": True, "already_solved": True}
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error")}
        return {
            "ok": True,
            "kind": "image_captcha",
            "image": result.get("image"),
        }

    def submit(self, payload: dict) -> dict:
        result = self._session().submit(str(payload.get("code") or ""))
        if result.get("ok"):
            return {"ok": True}
        return {"ok": False, "error": result.get("error") or "验证失败"}

    def status(self, live=None) -> dict:
        """live 是 livequery client（可为 None，缓存模式）。"""
        if live is not None:
            rows = len(getattr(live, "rows", []) or [])
            error = getattr(live, "last_error", None)
            if rows:
                return {"status": "online", "rows": rows, "error": None}
            return {"status": "needs_auth", "rows": 0, "error": error}
        return {"status": "cache", "rows": None, "error": None}


_BRIDGES = {
    "sunner_srm": _SrmAuth,
}


def get_bridge(system_id: str):
    factory = _BRIDGES.get(system_id)
    return factory() if factory else None
