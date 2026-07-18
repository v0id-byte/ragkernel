"""本地 embedding：默认 BAAI/bge-m3（唯一可换点，只暴露 embed()）。model 从 config 读。"""

_model = None


def _get_model():
    global _model
    if _model is None:
        import torch
        from sentence_transformers import SentenceTransformer

        from . import config

        name = (config.settings().get("embed") or {}).get("model", "BAAI/bge-m3")
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _model = SentenceTransformer(name, device=device)
    return _model


def embed(texts: list[str], batch_size: int = 16):
    """返回 L2 归一化的 float32 ndarray (n, 1024)。"""
    m = _get_model()
    return m.encode(
        list(texts),
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=len(texts) > 64,
    )
