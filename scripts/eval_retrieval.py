"""检索质量评测：Recall@k / MRR，rerank on/off 对比（可复现）。

读 eval/qa.jsonl，每行 {"question": "...", "expect": "应命中的原文子串"}。
先摄取对应语料（如 smoke 样例），再跑：
  uv run python scripts/eval_retrieval.py          # on/off 都跑
  uv run python scripts/eval_retrieval.py off       # 仅纯 RRF
  uv run python scripts/eval_retrieval.py on        # 仅带 reranker
"""

import json
import sys
from pathlib import Path

from ragkernel import config, embed, rerank, search, store

config.load_env()

QA = Path(__file__).resolve().parent.parent / "eval" / "qa.jsonl"
K = 8


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
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode in ("off", "both"):
        print("纯 RRF   ", _run(False))
    if mode in ("on", "both"):
        print("+reranker", _run(True))


if __name__ == "__main__":
    main()
