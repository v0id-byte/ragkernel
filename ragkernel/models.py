"""本地模型生命周期：下载 embedding / reranker，探测缓存完整性。

从 cli.cmd_models 抽出来，让 setup 向导 / doctor / 未来的 API 都能调同一层，
且可单测（cmd_models 原先逻辑+print 混在一起）。

按角色拆函数（download_embedding / download_reranker），不把「有哪些模型」写死在
一个上帝函数里——将来 CAD 线的 OCR/vision 模型平行加即可。

get_cache_status() 只探不下、纯文件系统扫描，**不 import huggingface_hub / torch**，
所以 checks 层能安全复用它（诊断层不该依赖模型下载 SDK）。
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# missing=从未装过 · incomplete=装了一半（下载中断）· error=尝试下载但失败
# ——三条排障路径不同，不能笼统一个 failed
ModelStatus = Literal["cached", "downloaded", "missing", "incomplete", "error"]


@dataclass
class ModelResult:
    name: str
    role: str  # "embedding" | "reranker"
    status: ModelStatus
    path: str | None = None
    size_bytes: int | None = None
    source: str | None = None  # "huggingface-cache" | "local-path" | ...
    error: str | None = None


# ------------------------------------------------------------------ 模型名（与运行时同一来源）

def _embed_model_name() -> str:
    from . import config

    return (config.settings().get("embed") or {}).get("model", "BAAI/bge-m3")


def _rerank_model_name() -> str:
    from . import config

    rr = (config.settings().get("retrieval") or {}).get("rerank") or {}
    return rr.get("model", "BAAI/bge-reranker-v2-m3")


# ------------------------------------------------------------------ 缓存探测（纯 FS）

def _hf_cache() -> Path:
    """HF hub 缓存根。sentence-transformers>=3 走 HF hub 缓存。
    优先级同 huggingface_hub：HF_HUB_CACHE > HF_HOME/hub > ~/.cache/huggingface/hub。"""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _has_weights(snapshot: Path) -> bool:
    # .exists() 跟随符号链接：HF 快照里是指向 blobs 的软链，下载中断→悬空软链→exists() False。
    # *.safetensors 同时覆盖单文件与 sharded 分片；pytorch_model*.bin 同理。
    for pattern in ("*.safetensors", "pytorch_model*.bin"):
        if any(f.exists() for f in snapshot.glob(pattern)):
            return True
    return False


def validate_model_artifact(snapshot: Path) -> bool:
    """快照是否真的能用——config + tokenizer + 权重三者齐全。

    只判目录存在远远不够：HF 缓存目录在下载中断后照样在，里头一堆悬空软链，
    doctor 会报 ✓ 而运行时加载才炸。这里跟随软链实际验证文件可达。
    """
    if not (snapshot / "config.json").exists():
        return False
    has_tokenizer = (snapshot / "tokenizer.json").exists() or (snapshot / "tokenizer_config.json").exists()
    return has_tokenizer and _has_weights(snapshot)


def _snapshot_size(snapshot: Path) -> int:
    total = 0
    for f in snapshot.rglob("*"):
        if f.is_file():  # 跟随软链；悬空软链 is_file()=False，自动跳过
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _probe(role: str, name: str) -> ModelResult:
    # 本地路径（少见：config 指向本地模型目录）——存在即信任
    if os.sep in name and Path(name).is_dir():
        return ModelResult(name=name, role=role, status="cached", path=name, source="local-path")

    model_dir = _hf_cache() / f"models--{name.replace('/', '--')}"
    if not model_dir.exists():
        return ModelResult(name=name, role=role, status="missing", source="huggingface-cache")

    snap_root = model_dir / "snapshots"
    snaps = [d for d in snap_root.glob("*") if d.is_dir()] if snap_root.exists() else []
    complete = next((s for s in snaps if validate_model_artifact(s)), None)
    if complete:
        return ModelResult(name=name, role=role, status="cached", path=str(complete),
                           size_bytes=_snapshot_size(complete), source="huggingface-cache")
    # 目录在、但没有一个完整快照 = 下载中断
    return ModelResult(name=name, role=role, status="incomplete", path=str(model_dir),
                       source="huggingface-cache", error="快照文件不全（下载可能中断），建议清缓存后重下")


def get_cache_status() -> list[ModelResult]:
    """只探不下，供向导与 doctor 复用。"""
    return [_probe("embedding", _embed_model_name()), _probe("reranker", _rerank_model_name())]


# ------------------------------------------------------------------ 下载（触发重依赖，惰性导入）

def download_embedding() -> ModelResult:
    name = _embed_model_name()
    was_cached = _probe("embedding", name).status == "cached"
    try:
        from . import embed

        embed.embed(["warmup"])  # 触发下载 + 加载
    except Exception as e:
        return ModelResult(name=name, role="embedding", status="error",
                           source="huggingface-cache", error=f"{type(e).__name__}: {e}")
    r = _probe("embedding", name)
    if r.status == "cached" and not was_cached:
        r.status = "downloaded"
    return r


def download_reranker() -> ModelResult:
    name = _rerank_model_name()
    was_cached = _probe("reranker", name).status == "cached"
    from . import rerank

    rk = rerank.get(name)  # rerank.get 内部吞异常、失败返回 None（不 raise）
    if rk is None:
        return ModelResult(name=name, role="reranker", status="error",
                           source="huggingface-cache", error="加载失败（见 [rerank] 日志）")
    r = _probe("reranker", name)
    if r.status == "cached" and not was_cached:
        r.status = "downloaded"
    return r


def download() -> list[ModelResult]:
    """编排两个角色。缺哪个下哪个，已缓存的秒返回。"""
    return [download_embedding(), download_reranker()]
