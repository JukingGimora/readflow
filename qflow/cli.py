"""命令行入口：python -m qflow <命令>

    serve                     启动服务（页面 /desk）
    systems                   这台机器接入了哪些系统、各自的状态
    validate <图谱文件>        校验外部模型交来的图谱
    login <系统 id>            在终端里登录一个系统（验证码图片存成文件，人看了输入）
    ask "一句话"               和页面走同一条路查询
    run <图谱文件> ...         不经过已接入的系统，直接拿一份图谱文件执行（验收新图谱用）
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import logging
import sys

from . import agent as agents
from . import config, online, runner, spec

logger = logging.getLogger("qflow")


def cmd_serve(args) -> int:
    from . import service

    service.serve(args.host, args.port, config.DEFAULT_TOKEN if args.token is None else args.token)
    return 0


def cmd_systems(args) -> int:
    rows = [a.status() for a in agents.all().values()]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    words = {"no_account": "还没填账号", "needs_login": "需要登录", "online": "可以查"}
    for row in rows:
        print(f"{row['id']}  {row['label']}  {words[row['status']]}  能力：{'、'.join(row['capabilities']) or '无'}")
    for path, why in agents.problems.items():
        print(f"没加载 {path}：{why}")
    return 0


def cmd_validate(args) -> int:
    result = spec.validate_file(args.file)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else spec.format_report(result))
    return 0 if result["ok"] else 1


def cmd_login(args) -> int:
    from . import login

    target = agents.get(args.system)
    if target is None:
        print(f"没有这个系统：{args.system}", file=sys.stderr)
        return 2
    if args.account or not target.account():
        target.save_account(input("账号："), getpass.getpass("密码（不显示）："))
    out = login.handle(target, "start")
    while out["state"] == "challenge":
        image = target.store / "challenge.png"
        image.write_bytes(base64.b64decode(out["image"].split(",", 1)[1]))
        if out.get("error"):
            print(out["error"])
        out = login.handle(target, "answer", {"text": input(f"验证码图片在 {image}，请输入：")})
    if out["state"] == "window":
        login.handle(target, "cancel")
        print("这个系统需要人在浏览器窗口里操作（滑块、扫码等），请在页面上登录：管理 → 点这个系统。", file=sys.stderr)
        return 3
    print("登录成功" if out["state"] == "done" else f"没登录上：{out.get('error')}")
    return 0 if out["state"] == "done" else 1


def cmd_ask(args) -> int:
    out = agents.ask(args.text)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str) if args.json else out["reply"])
    return 0


def cmd_run(args) -> int:
    try:
        graph = runner.load(args.file)
        send = runner.http_sender(runner.opener_from_cookie_file(args.cookies) if args.cookies else None)
        if args.ask:  # 从一句话开始：听懂 → 选能力 → 填条件 → 执行
            from . import ask

            out = ask.ask(graph, args.ask, None, send)
            result = out.get("result") or {"answer": out["reply"]}
            if out.get("capability"):
                print(f"[{out['capability']} 由{'规则' if out['by'] == 'rules' else '本地模型'}选出，条件 "
                      f"{json.dumps(out['state']['values'], ensure_ascii=False)}]", file=sys.stderr)
        elif args.capability:
            result = runner.run(graph, args.capability, json.loads(args.params or "{}"), send, limit=args.limit)
        else:
            print("需要能力 id 或 --ask 一句话", file=sys.stderr)
            return 2
    except runner.AuthExpired as exc:
        print(f"登录态失效：{exc}", file=sys.stderr)
        return 3
    except (runner.RunError, online.ReadOnlyViolation) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["answer"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qflow", description="readflow：按系统图谱只读查询业务系统")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试信息")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="启动服务")
    p.add_argument("--host", default=config.DEFAULT_HOST)
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    p.add_argument("--token", default=None, help="API Key；设置后请求需带 X-API-Key，页面要口令")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("systems", help="接入了哪些系统")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_systems)

    p = sub.add_parser("validate", help="校验外部模型产出的系统图谱（docs/graph-spec.md）")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("login", help="在终端里登录一个系统")
    p.add_argument("system")
    p.add_argument("--account", action="store_true", help="重新输入账号密码")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("ask", help="用一句话查询（走已接入的系统和保存的登录态）")
    p.add_argument("text")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("run", help="直接拿一份图谱文件执行一个能力（只读），用来验收新图谱")
    p.add_argument("file")
    p.add_argument("capability", nargs="?", default=None)
    p.add_argument("--ask", default=None, help="不指定能力，直接给一句话")
    p.add_argument("--params", default=None, help='条件的 JSON，例如 {"period": {"start": "2025-11-01", "end": "2025-11-30"}}')
    p.add_argument("--cookies", default=None, help="登录态 cookie 文件；不给就不带登录态")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        logger.exception("命令失败: %s", exc)
        print(f"错误: {exc}", file=sys.stderr)
        return 1
