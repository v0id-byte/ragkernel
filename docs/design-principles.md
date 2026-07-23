# 设计原则与理念

> RagKernel 在取舍时依据的原则，以及它对「工程知识」的基本立场。

[← Back to documentation](README.md)

## Design Principles

- **Verifiable by default** — every claim traces back to a citation or a measured value.
- **Preserve structure before flattening** — tables, entities, provenance, and geometry remain structured wherever the source and parser support it.
- **Honest uncertainty** — when something can't be verified, it says so instead of guessing.
- **Provider independent** — Claude / MiniMax / local vLLM · Ollama, swappable.
- **Local-first storage and retrieval** — the corpus, index, embeddings, and reranker stay local; retrieved context is sent only to the configured generation provider. Use a local OpenAI-compatible backend for fully private operation.
- **Read-only by default** — agent tools observe knowledge, never mutate it; diagnostics inspect the system without changing it.
- **Operable by default** — one-line deployment, guided setup, and self-diagnosis (`doctor`); configuration issues are surfaced early instead of discovered during first use.

## Philosophy

> RagKernel prefers evidence over confidence.
>
> When information cannot be verified, it reports uncertainty instead of guessing.
>
> Structured engineering data is preserved whenever possible rather than flattened into free text.
>
> AI agents should retrieve engineering evidence, not fabricate engineering facts.
