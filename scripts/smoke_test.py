"""端到端冒烟：造样例文档 → 摄取 → 断言已嵌入 → 检索命中 →（有 LLM key 时）问答带引用。

用法：uv run python scripts/smoke_test.py
"""

import os
import sys

from ragkernel import config, embed, pipeline, search, store

config.load_env()

SAMPLE = """# 员工手册

## 年假政策
入职满一年的员工每年享有 15 天带薪年假。年假须提前 3 个工作日申请。

## 差旅报销
差旅报销需在出差结束后 30 天内提交发票，逾期不予受理。
"""


def main():
    sample = config.data_dir() / "sample_smoke.md"
    sample.write_text(SAMPLE, encoding="utf-8")
    db = store.connect()

    r = pipeline.ingest_file(sample, db=db)
    doc_id = r["document_id"]
    row = store.get_document(db, store.file_sha256(sample))
    assert row["status"] == "embedded", f"期望 status=embedded，实际 {row['status']}"
    n_vec = db.execute(
        "SELECT COUNT(*) FROM chunks_vec cv JOIN chunks c ON c.id=cv.chunk_id WHERE c.document_id=?",
        (doc_id,),
    ).fetchone()[0]
    assert n_vec > 0, "chunks_vec 为空"
    print(f"✓ 摄取+嵌入：doc D{doc_id}，{n_vec} 块已向量化")

    qvec = embed.embed(["年假有多少天"])[0]
    hits = search.hybrid_search(db, "年假有多少天", qvec, k=3)
    assert any("15 天" in (h["text"] or "") for h in hits), "检索未命中年假条款"
    print("✓ 检索命中年假条款")

    prov = config.provider()
    if os.environ.get(prov["api_key_env"]):
        from ragkernel import agent

        ans, _, tb, model = agent.ask("入职满一年有多少天年假？依据文档回答。")
        refs = sorted({t["ref"] for t in tb.touched})
        print(f"\n答（{model}）：{ans}\n引用：{refs}")
        assert tb.touched, "答案无检索引用（touched 为空）"
        print("✓ 问答带引用")
    else:
        print(f"（跳过问答：未配置 {prov['api_key_env']}；检索/嵌入链路已验证）")

    print("\n冒烟通过。")


if __name__ == "__main__":
    sys.exit(main())
