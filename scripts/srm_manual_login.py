"""人工登录 SRM：弹出可见 Chrome，等人完成登录（验证码/短信都行），cookie 留在 profile 里。

用法：
    .venv/bin/python scripts/srm_manual_login.py

登录成功后直接退出；livequery 下次刷新会复用同一 profile，无需再登录。
也可以被服务端 POST /desk/api/srm-login 拉起。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from qflow import srm  # noqa: E402

TIMEOUT = 10 * 60  # 等人操作的最长时间


def main() -> int:
    driver = srm.new_driver(headless=False)
    try:
        driver.set_window_size(1280, 860)
        driver.get(srm.URL)
        print("浏览器已打开，请在窗口里完成登录（账号/密码/验证码）。", flush=True)
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            if not srm.is_login_page(driver):
                print("LOGIN OK — 会话已保存在 Chrome profile，窗口即将关闭。", flush=True)
                time.sleep(2)
                return 0
            time.sleep(2)
        print("超时：10 分钟内没有检测到登录成功。", flush=True)
        return 1
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
