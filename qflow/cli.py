"""命令行入口：sync / ask / serve / overview / datasets / tools / login / discover."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import capabilities, config, db, engine, graph, importer, nlu, service

logger = logging.getLogger("qflow.cli")


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=None, help=f"SQLite 路径（默认 {config.DEFAULT_DB}）")


def _open(args):
    return db.connect(config.resolve_db(args.db))


def cmd_sync(args) -> int:
    conn = _open(args)
    try:
        paths = [Path(p) for p in args.paths]
        if paths:
            results = importer.sync_paths(conn, paths)
        else:
            directory = Path(args.dir).expanduser().resolve() if args.dir else config.PROJECT_ROOT.parent
            if not directory.is_dir():
                print(f"目录不存在: {directory}", file=sys.stderr)
                return 2
            print(f"扫描目录: {directory}")
            results = importer.sync_dir(conn, directory)
        total = {"success": 0, "skipped": 0, "error": 0}
        for item in results:
            status = item["status"]
            total[status] = total.get(status, 0) + 1
            error = f"  <- {item['error']}" if item.get("error") else ""
            print(
                f"[{status:7}] {Path(item['source_file']).name:22} "
                f"kind={item['kind']:10} read={item['rows_read']:4} "
                f"written={item['rows_written']:4} skipped={item['skipped']:3}{error}"
            )
        print(
            f"完成：成功 {total['success']}，跳过 {total['skipped']}，失败 {total['error']}；"
            f"库内发货 {conn.execute('SELECT COUNT(*) FROM shipments').fetchone()[0]} 单，"
            f"扣款 {conn.execute('SELECT COUNT(*) FROM deductions').fetchone()[0]} 笔。"
        )
        return 0 if total.get("error", 0) == 0 else 1
    finally:
        conn.close()


def cmd_ask(args) -> int:
    params = json.loads(args.params) if args.params else None
    if params is not None and not isinstance(params, dict):
        print("--params 需要是 JSON 对象", file=sys.stderr)
        return 2
    conn = _open(args)
    try:
        result = capabilities.query(conn, text=args.text, intent=args.intent, params=params)
    except ValueError as exc:
        print(f"查询失败: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(result["answer"])
        if args.verbose:
            print(
                f"\n[intent={result['intent']} confidence={result['confidence']} "
                f"elapsed={result['elapsed_ms']}ms as_of={result['data_as_of']}]"
            )
            for note in result.get("notes") or []:
                print(f"[note] {note}")
    return 0


def cmd_serve(args) -> int:
    token = args.token if args.token is not None else config.DEFAULT_TOKEN
    live = args.live if args.live is not None else config.LIVE_MODE
    service.serve(args.host, args.port, config.resolve_db(args.db), token, live)
    return 0


def cmd_overview(args) -> int:
    conn = _open(args)
    try:
        result = capabilities.query(conn, intent="overview")
    finally:
        conn.close()
    print(json.dumps(result["data"], ensure_ascii=False, indent=2, default=str) if args.json else result["answer"])
    return 0


def cmd_datasets(args) -> int:
    conn = _open(args)
    try:
        rows = engine.datasets(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"{'id':>3}  {'dataset':18} {'kind':10} {'rows':>5} {'status':8} finished_at")
    for row in rows:
        print(
            f"{row['id']:>3}  {str(row['dataset'])[:18]:18} {row['kind']:10} "
            f"{row['rows_written'] or 0:>5} {row['status']:8} {row['finished_at'] or ''}"
        )
    return 0


def cmd_tools(args) -> int:
    conn = _open(args)
    try:
        payload = capabilities.tools_payload(conn)
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_graph(args) -> int:
    g = graph.Graph.load()
    if args.json:
        payload = {
            "stats": g.stats(),
            "nodes": list(g.nodes.values()),
            "edges": g.edges,
            "path": str(g.path) if g.path else None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(g.summary())
    return 0


def cmd_plan(args) -> int:
    if not args.text and not args.intent:
        print("需要 text 或 --intent", file=sys.stderr)
        return 2
    parsed = nlu.parse(args.text or "")
    intent = args.intent or parsed.intent
    params = dict(parsed.params)
    if args.params:
        extra = json.loads(args.params)
        if not isinstance(extra, dict):
            print("--params 需要是 JSON 对象", file=sys.stderr)
            return 2
        params.update(extra)
    try:
        plan = graph.Graph.load().plan(intent, params)
    except graph.GraphError as exc:
        print(f"生成计划失败: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    else:
        print(graph.human_plan(plan))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qflow",
        description="readflow：圣农SRM 只读查询数据流（供 voiceAI 调用）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试信息")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="导入 Excel 导出文件")
    p.add_argument("paths", nargs="*", help="指定文件；不填则扫描 --dir")
    p.add_argument("--dir", default=None, help="扫描目录（默认 snxt 根目录）")
    _add_db(p)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("ask", help="自然语言查询")
    p.add_argument("text", nargs="?", default=None, help="例如：去年11月扣款合计")
    p.add_argument("--intent", default=None, choices=list(capabilities.INTENTS))
    p.add_argument("--params", default=None, help='JSON 参数，例如 \'{"month":"2025-11"}\'')
    p.add_argument("--json", action="store_true", help="输出完整 JSON")
    _add_db(p)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("serve", help="启动只读 HTTP 服务")
    p.add_argument("--host", default=config.DEFAULT_HOST)
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    p.add_argument("--token", default=None, help="API Key；设置后请求需带 X-API-Key")
    p.add_argument("--live", action="store_true", default=None, help="实时模式：登录 SRM 现场读取，不落库")
    _add_db(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("overview", help="数据概览")
    p.add_argument("--json", action="store_true")
    _add_db(p)
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser("datasets", help="导入审计")
    p.add_argument("--json", action="store_true")
    _add_db(p)
    p.set_defaults(func=cmd_datasets)

    p = sub.add_parser("tools", help="输出 voiceAI 工具 schema")
    _add_db(p)
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("graph", help="查看查询图谱（节点/边/覆盖率）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("plan", help="查看一个问题会走哪条图谱路径")
    p.add_argument("text", nargs="?", default=None)
    p.add_argument("--intent", default=None, choices=list(capabilities.INTENTS))
    p.add_argument("--params", default=None, help='JSON 参数，例如 \'{"month":"2025-11"}\'')
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)



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
