"""ragkernel CLI：ingest / embed / ask / serve / watch / models / stats。"""

import argparse
import sys

from . import config


def cmd_ingest(path: str, no_embed: bool):
    from . import pipeline

    results = pipeline.ingest_path(path, do_embed=not no_embed, on_stage=lambda s, d: print(f"  [{s}] {d}"))
    n = sum(r["chunks"] for r in results)
    skipped = sum(1 for r in results if r.get("skipped"))
    print(f"完成：{len(results)} 个文件（跳过 {skipped}），共 {n} 块。")


def cmd_embed():
    from . import pipeline, store

    db = store.connect()
    n = pipeline.embed_missing(db, on_progress=lambda done, total: print(f"  {done}/{total}"))
    pipeline._mark_embedded(db)
    print("嵌入已是最新。" if n == 0 else f"嵌入完成 {n} 块。")


def cmd_stats():
    from . import store

    for k, v in store.stats(store.connect()).items():
        print(f"  {k}: {v}")


def cmd_models():
    from . import embed, rerank

    print("预载 embedding 模型（首次会下载 ~2GB，之后走缓存）…")
    embed.embed(["warmup"])
    print("预载 reranker 模型…")
    rerank.get()
    print("模型就绪。")


def cmd_ask(question: str):
    from . import agent, audit

    aud = audit.Audit(client="cli")
    answer, _, tb, model = agent.ask(
        question, audit=aud, on_tool=lambda name, inp: print(f"  ↳ {name} {inp}", file=sys.stderr)
    )
    print(f"\n{answer}\n")
    if tb.touched:
        refs = sorted({t["ref"] for t in tb.touched})
        print(f"—— 触达 {len(refs)} 处引用 · 模型 {model}")


def cmd_watch(folder: str):
    from . import watch

    watch.run(folder)


def main():
    config.load_env()
    ap = argparse.ArgumentParser(prog="ragkernel", description="本地优先的企业 RAG 内核")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="摄取文件或目录（幂等，可反复跑）")
    p.add_argument("--path", required=True, help="文件或目录")
    p.add_argument("--no-embed", action="store_true")

    sub.add_parser("embed", help="补齐缺失的向量")
    sub.add_parser("stats", help="知识库统计")
    sub.add_parser("models", help="预载/下载本地模型")
    sub.add_parser("serve", help="启动 Web 服务（上传 + 带引用问答）")

    p = sub.add_parser("ask", help="命令行问一个问题")
    p.add_argument("question")

    p = sub.add_parser("watch", help="监听落盘文件夹，自动索引")
    p.add_argument("--dir", required=True)

    args = ap.parse_args()
    if args.cmd == "ingest":
        cmd_ingest(args.path, args.no_embed)
    elif args.cmd == "embed":
        cmd_embed()
    elif args.cmd == "stats":
        cmd_stats()
    elif args.cmd == "models":
        cmd_models()
    elif args.cmd == "ask":
        cmd_ask(args.question)
    elif args.cmd == "watch":
        cmd_watch(args.dir)
    elif args.cmd == "serve":
        from . import webapp

        webapp.main()


if __name__ == "__main__":
    main()
