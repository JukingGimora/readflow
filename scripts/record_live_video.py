"""录制真实浏览器操作：登录 SRM → 质量扣款明细 → 查询 → 汇总视频。

用 Chrome DevTools Page.startScreencast 抓浏览器画面（无头也有效），
不依赖 macOS 录屏权限，也不保存业务数据到库。

输出：
    docs/demo_live.mp4
    docs/demo_live_frames/（录制帧，可删）
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import ddddocr
import websocket
from PIL import Image, ImageDraw, ImageFont
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

HERE = Path(__file__).resolve().parent
READFLOW = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(READFLOW))

from live_login_auto import captcha_images, creds, extract_table, messages, next_page  # noqa: E402

LOGIN_URL = "https://srm.sunnercn.com:10001"
DEDUCTION_URL = LOGIN_URL + "/#/quajud/qua-jud-vendor-list"
DRIVER = READFLOW / "data" / "drivers" / "chromedriver-mac-arm64" / "chromedriver"
OUT = READFLOW / "docs"
FRAMES = OUT / "demo_live_frames"
VIDEO = OUT / "demo_live.mp4"

W, H = 1280, 800
FONT_BOLD = "/System/Library/Fonts/PingFang.ttc"


class Screencast:
    def __init__(self, debugger_address: str, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        targets = json.load(urllib.request.urlopen(f"http://{debugger_address}/json", timeout=5))
        page = next(t for t in targets if t.get("type") == "page")
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=10, suppress_origin=True
        )
        self.ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        self.ws.send(json.dumps({
            "id": 2, "method": "Page.startScreencast",
            "params": {"format": "jpeg", "quality": 85, "maxWidth": W, "maxHeight": H, "everyNthFrame": 1},
        }))
        self.frames: list[tuple[Path, float]] = []
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.running:
            try:
                raw = self.ws.recv()
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("method") != "Page.screencastFrame":
                continue
            params = msg["params"]
            index = len(self.frames) + 1
            path = self.out_dir / f"frame_{index:05d}.jpg"
            path.write_bytes(base64.b64decode(params["data"]))
            ts = float((params.get("metadata") or {}).get("timestamp") or time.time())
            self.frames.append((path, ts))
            try:
                self.ws.send(json.dumps({
                    "id": 1000 + index, "method": "Page.screencastFrameAck",
                    "params": {"sessionId": params["sessionId"]},
                }))
            except Exception:
                break

    def stop(self):
        self.running = False
        try:
            self.ws.close()
        except Exception:
            pass
        self.thread.join(timeout=5)


def overlay(driver, record: bool = True):
    """REC 角标 + 账号打码；持续闪烁让 screencast 持续出帧。"""
    driver.execute_script(
        """
        (() => {
          if (!document.getElementById('demo-rec')) {
            const style = document.createElement('style');
            style.textContent = 'input[placeholder*="账号"]{filter:blur(6px)!important}';
            document.head.appendChild(style);
            const el = document.createElement('div');
            el.id = 'demo-rec';
            el.textContent = '● REC  只读演示';
            el.style.cssText = 'position:fixed;left:14px;top:12px;z-index:99999;' +
              'background:rgba(190,30,30,.95);color:#fff;font:600 13px -apple-system;' +
              'padding:6px 10px;border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.3)';
            document.body.appendChild(el);
            setInterval(() => { el.style.opacity = (el.style.opacity === '0.35' ? '1' : '0.35'); }, 500);
          }
        })()
        """
    )


def recognize_more(driver, ocr, rounds: int = 12):
    """比 live_login_auto 的 4 轮更宽容；识别不到 4 位就刷新验证码。"""
    for round_no in range(1, rounds + 1):
        candidates = captcha_images(driver)
        if not candidates:
            raise RuntimeError("没有找到验证码图片")
        for image in candidates:
            text = "".join(ch for ch in (ocr.classification(image.screenshot_as_png) or "") if ch.isalnum())
            print(f"  OCR round={round_no} raw={text!r}", flush=True)
            if len(text) == 4:
                return image, text
        driver.execute_script("arguments[0].click()", candidates[-1])
        time.sleep(0.7)
    raise RuntimeError(f"刷新 {rounds} 轮后仍没有得到 4 位验证码")


def type_slow(element, value: str, delay: float = 0.045):
    element.clear()
    for ch in value:
        element.send_keys(ch)
        time.sleep(delay)


def run() -> dict:
    options = Options()
    for arg in (
        "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--window-size=1280,800", "--ignore-certificate-errors", "--lang=zh-CN",
        "--remote-allow-origins=*",
    ):
        options.add_argument(arg)
    driver = webdriver.Chrome(service=Service(str(DRIVER)), options=options)
    driver.set_page_load_timeout(40)
    cast = None
    try:
        driver.get(LOGIN_URL)
        time.sleep(2)
        overlay(driver)
        time.sleep(0.5)

        ocr = ddddocr.DdddOcr(show_ad=False)
        account = creds()
        fields = {
            "账号/邮箱": next(x for x in driver.find_elements(By.CSS_SELECTOR, "input") if "账号/邮箱" in (x.get_attribute("placeholder") or "")),
            "密码": next(x for x in driver.find_elements(By.CSS_SELECTOR, "input") if "密码" in (x.get_attribute("placeholder") or "")),
            "验证码": next(x for x in driver.find_elements(By.CSS_SELECTOR, "input") if "验证码" in (x.get_attribute("placeholder") or "")),
        }
        # 先把验证码解出来，再开始录；避免把多次刷新录进视频
        _, code = recognize_more(driver, ocr)
        debugger = driver.capabilities["goog:chromeOptions"]["debuggerAddress"]
        shutil.rmtree(FRAMES, ignore_errors=True)
        cast = Screencast(debugger, FRAMES)
        time.sleep(0.8)
        type_slow(fields["账号/邮箱"], account["username"])
        time.sleep(0.6)
        type_slow(fields["密码"], account["password"])
        time.sleep(0.6)
        type_slow(fields["验证码"], code, 0.08)
        time.sleep(0.6)
        button = driver.find_element(By.CSS_SELECTOR, "button.login-btn")
        button.click()
        found = messages(driver)
        time.sleep(2)
        still_login = [
            x for x in driver.find_elements(By.CSS_SELECTOR, "input")
            if x.is_displayed() and any(s in (x.get_attribute("placeholder") or "") for s in ("账号/邮箱", "验证码"))
        ]
        if still_login:
            raise RuntimeError(f"登录失败: {found}")
        print("LOGIN OK", flush=True)

        time.sleep(1.2)
        driver.get(DEDUCTION_URL)
        time.sleep(5)
        overlay(driver)
        time.sleep(2)
        driver.execute_script(
            "const w=document.querySelector('.el-table__body-wrapper'); if(w) w.scrollTop += 420;"
        )
        time.sleep(1.4)
        driver.execute_script(
            "const w=document.querySelector('.el-table__body-wrapper'); if(w) w.scrollTop += 520;"
        )
        time.sleep(1.4)
        driver.execute_script(
            "const w=document.querySelector('.el-table__body-wrapper'); if(w) w.scrollTop = 0;"
        )
        time.sleep(1.0)
        for _ in range(3):
            if not next_page(driver):
                break
            time.sleep(1.3)
        time.sleep(1.0)

        frames = list(cast.frames)
        cast.stop()
        cast = None
        stats = page_stats(driver)
        print("STATS", stats, flush=True)
        return frames, stats
    finally:
        if cast:
            cast.stop()
        driver.quit()


def page_stats(driver) -> dict:
    """在已登录会话里翻完质量扣款明细，从页面表格汇总；不落库。"""
    driver.get(DEDUCTION_URL)
    time.sleep(4)
    # 清掉上一次查询留下的筛选状态
    for x in driver.find_elements(By.CSS_SELECTOR, "button,.el-button"):
        if (x.text or "").strip() == "重置":
            x.click()
            break
    time.sleep(2)
    headers = None
    raw_rows = []
    pages = 0
    while True:
        page = extract_table(driver)
        headers = headers or page["headers"]
        raw_rows.extend(page["rows"])
        pages += 1
        if not next_page(driver):
            break
        time.sleep(1.0)
    if not headers:
        raise RuntimeError("没有表头")
    unique_headers = []
    for h in headers:
        if h not in unique_headers:
            unique_headers.append(h)
    idx = {name: i for i, name in enumerate(unique_headers)}
    amount_i, date_i = idx["扣罚金额"], idx["制单日期"]
    seen = set()
    rows = []
    for raw in raw_rows:
        cells = list(raw)
        if len(cells) == len(unique_headers) + 1:
            cells = cells[1:]
        if len(cells) < len(unique_headers):
            continue
        key = tuple(cells)
        if key in seen:
            continue
        seen.add(key)
        rows.append(cells)
    def total(prefix: str) -> tuple[int, float]:
        subset = [r for r in rows if r[date_i].startswith(prefix)]
        amount = sum(float(r[amount_i]) for r in subset if r[amount_i])
        return len(subset), round(amount, 2)
    print("SAMPLE dates:", [r[date_i] for r in rows[:5]], "amounts:", [r[amount_i] for r in rows[:5]], flush=True)
    may_n, may_total = total("2026-05")
    jun_n, jun_total = total("2026-06")
    return {"total": len(rows), "pages": pages, "may_n": may_n, "may_total": may_total,
            "jun_n": jun_n, "jun_total": jun_total}


def card(title: str, lines: list[str], path: Path, accent: str = "#4f8cff"):
    im = Image.new("RGB", (W, H), "#0b1220")
    draw = ImageDraw.Draw(im)
    draw.rectangle([(0, 0), (W, 8)], fill=accent)
    title_font = ImageFont.truetype(FONT_BOLD, 46)
    body_font = ImageFont.truetype(FONT_BOLD, 27)
    small = ImageFont.truetype(FONT_BOLD, 20)
    draw.text((70, 150), title, font=title_font, fill="#f4f7ff")
    y = 260
    for line in lines:
        draw.text((72, y), line, font=body_font, fill="#c9d6ee")
        y += 58
    draw.text((72, H - 70), "readflow · 只读演示 · 不保存原始表格", font=small, fill="#7f91b2")
    im.save(path, quality=92)


def build_video(frames: list[tuple[Path, float]], stats: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    intro = OUT / "demo_intro.jpg"
    outro = OUT / "demo_outro.jpg"
    card("模拟人操作：登录 → 查询", [
        "1. 打开圣农 SRM，输入账号、密码、验证码",
        "2. 进入「采购协同 → 质量扣款明细」",
        "3. 查询并翻页，读取表格",
        "只读：不保存账号、不写库、不导出原始表格",
    ], intro)
    card("查询结果（从页面表格汇总）", [
        f"质量扣款明细：共 {stats['total']} 条（{stats['pages']} 页）",
        f"2026 年 5 月：{stats['may_n']} 笔，合计 {stats['may_total']:,.2f} 元",
        f"2026 年 6 月：{stats['jun_n']} 笔，合计 {stats['jun_total']:,.2f} 元",
        "来源：SRM 质量扣款明细页面",
    ], outro, accent="#22c55e")

    normalized = FRAMES / "norm"
    normalized.mkdir(exist_ok=True)
    # 归一化到 1280x800，避免 CDP 帧尺寸不一致
    frame_paths = []
    for index, (path, ts) in enumerate(frames):
        try:
            im = Image.open(path).convert("RGB")
        except Exception:
            continue
        canvas = Image.new("RGB", (W, H), "#0b1220")
        im.thumbnail((W, H), Image.LANCZOS)
        canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
        target = normalized / f"n_{index:05d}.jpg"
        canvas.save(target, quality=88)
        frame_paths.append((target, ts))

    lines = [f"file '{intro.as_posix()}'", "duration 3.0"]
    if frame_paths:
        for i, (path, ts) in enumerate(frame_paths):
            if i + 1 < len(frame_paths):
                dur = max(0.08, min(2.0, frame_paths[i + 1][1] - ts))
            else:
                dur = 0.5
            lines += [f"file '{path.as_posix()}'", f"duration {dur:.3f}"]
    lines += [f"file '{outro.as_posix()}'", "duration 5.0", f"file '{outro.as_posix()}'", "duration 0.1"]
    concat = OUT / "demo_concat.txt"
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-vf", "fps=30,format=yuv420p", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart",
        str(VIDEO),
    ]
    subprocess.run(cmd, check=True)
    print("VIDEO", VIDEO, VIDEO.stat().st_size // 1024, "KB", flush=True)


def main() -> int:
    try:
        frames, stats = run()
    except Exception as exc:
        print("RECORD FAILED:", type(exc).__name__, exc, flush=True)
        return 1
    print("FRAME COUNT", len(frames), flush=True)
    if not frames:
        print("NO FRAMES", flush=True)
        return 1
    print("STATS", stats, flush=True)
    build_video(frames, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
