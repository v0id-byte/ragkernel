"""检索质量评测：Recall@k / MRR，rerank on/off 对比（可复现）。

读 eval/qa.jsonl，每行 {"question": "...", "expect": "应命中的原文子串"}；
自动摄取 eval/corpus/ 下的语料（幂等），无需手工准备。跑：
  uv run python scripts/eval_retrieval.py          # on/off 都跑
  uv run python scripts/eval_retrieval.py off       # 仅纯 RRF
  uv run python scripts/eval_retrieval.py on        # 仅带 reranker
"""

import json
import sys
from pathlib import Path

from ragkernel import config, connectors, embed, pipeline, rerank, search, store

config.load_env()

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "eval" / "qa.jsonl"
CORPUS = ROOT / "eval" / "corpus"
K = 8


def _ensure_corpus() -> None:
    """幂等摄取 eval/corpus 下的语料（sha256 命中即跳过），让评测自足可复现。"""
    if not CORPUS.exists():
        return
    exts = connectors.supported_exts()
    for f in sorted(CORPUS.rglob("*")):
        if f.suffix.lower() in exts:
            pipeline.ingest_file(f)


def _run(use_rerank: bool) -> dict:
    db = store.connect()
    rr = rerank.get() if use_rerank else None
    items = [json.loads(x) for x in QA.read_text(encoding="utf-8").splitlines() if x.strip()]
    hit = 0
    mrr = 0.0
    for it in items:
        qvec = embed.embed([it["question"]])[0]
        rows = search.hybrid_search(db, it["question"], qvec, k=K, reranker=rr, candidates=40)
        rank = None
        for i, r in enumerate(rows, 1):
            if it["expect"] in (r["text"] or ""):
                rank = i
                break
        if rank:
            hit += 1
            mrr += 1.0 / rank
    n = max(len(items), 1)
    return {"n": len(items), "recall@k": round(hit / n, 3), "mrr": round(mrr / n, 3)}


def main():
    if not QA.exists():
        print(f"缺 {QA}。先建标注集：每行 {{'question':..,'expect':..}}，并摄取对应语料。")
        return
    _ensure_corpus()
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode in ("off", "both"):
        print("纯 RRF   ", _run(False))
    if mode in ("on", "both"):
        print("+reranker", _run(True))


if __name__ == "__main__":
    main()
