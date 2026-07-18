"""本地 cross-encoder 重排：BAAI/bge-reranker-v2-m3（语料不出机，MPS 优先，退回 CPU）。

对 hybrid_search 融合出的候选集（几十条）做 (query, chunk) 精排。
单例加载，全程复用；加载失败则 available()=False，调用方优雅回退到纯 RRF。
"""

_model = None
_tried = False
_ok = False


def _load(model_name: str):
    global _model, _tried, _ok
    if _tried:
        return _model
    _tried = True
    try:
        import torch
        from sentence_transformers import CrossEncoder

        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _model = CrossEncoder(model_name, device=device, max_length=512)
        _ok = True
        print(f"[rerank] 已加载 {model_name}（{device}）")
    except Exception as e:  # 缺模型/缺依赖/加载异常 → 回退纯 RRF
        print(f"[rerank] 不可用，回退纯 RRF：{type(e).__name__}: {e}")
        _model = None
    return _model


class Reranker:
    """一个进程共享一个实例。rerank(query, texts) 返回与 texts 等长的相关性分数。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
        self.model_name = model_name

    def available(self) -> bool:
        _load(self.model_name)
        return _ok

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        m = _load(self.model_name)
        if m is None or not texts:
            # 回退：保持原顺序（分数递减）
            return [float(len(texts) - i) for i in range(len(texts))]
        scores = m.predict([(query, t) for t in texts])
        return [float(s) for s in scores]


_shared: Reranker | None = None


def get(model_name: str = "BAAI/bge-reranker-v2-m3") -> Reranker | None:
    """按需返回共享 Reranker；不可用时返回 None（调用方据此回退）。"""
    global _shared
    if _shared is None:
        _shared = Reranker(model_name)
    return _shared if _shared.available() else None
