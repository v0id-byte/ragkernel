"""本地模型生命周期：下载 embedding / reranker，探测缓存完整性。

从 cli.cmd_models 抽出来，让 setup 向导 / doctor / 未来的 API 都能调同一层，
且可单测（cmd_models 原先逻辑+print 混在一起）。

按角色拆函数（download_embedding / download_reranker），不把「有哪些模型」写死在
一个上帝函数里——将来 CAD 线的 OCR/vision 模型平行加即可。

get_cache_status() 只探不下、纯文件系统扫描，**不 import huggingface_hub / torch**，
所以 checks 层能安全复用它（诊断层不该依赖模型下载 SDK）。

**「完整性」判定的几条硬规矩**（都是下载中断会造成的假 cached）：
- 跟随符号链接实际验证文件可达（悬空软链 = 中断）；
- tokenizer 要有**真的分词器产物**（tokenizer.json / sentencepiece 等），不是只有
  小小的 tokenizer_config.json；
- sharded 权重必须**每个分片都在**（读 index.json 的 weight_map 逐一核对）；
- 多快照时校验 **refs/main 指向的那个 revision**（运行时加载的就是它），而不是
  「随便找一个完整快照」——旧完整 + 新中断的组合会骗过后者。
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# missing=从未装过 · incomplete=装了一半（下载中断）· error=尝试下载但失败
ModelStatus = Literal["cached", "downloaded", "missing", "incomplete", "error"]

_SHARD_RE = re.compile(r"-\d+-of-\d+\.(safetensors|bin)$")
# 真正的分词器数据文件（tokenizer_config.json 只是元数据，不算）
_TOKENIZER_FILES = ("tokenizer.json", "sentencepiece.bpe.model", "spiece.model",
                    "tokenizer.model", "vocab.txt")


@dataclass
class ModelResult:
    name: str
    role: str  # "embedding" | "reranker"
    status: ModelStatus
    path: str | None = None
    size_bytes: int | None = None
    source: str | None = None  # "huggingface-cache" | "local-path" | ...
    error: str | None = None


# ------------------------------------------------------------------ 配置读取

def _embed_model_name() -> str:
    from . import config

    return (config.settings().get("embed") or {}).get("model", "BAAI/bge-m3")


def _rerank_model_name() -> str:
    from . import config

    return ((config.settings().get("retrieval") or {}).get("rerank") or {}).get(
        "model", "BAAI/bge-reranker-v2-m3")


def _rerank_enabled() -> bool:
    from . import config

    # 与 tools.py 同源：rerank.enabled=false 时运行时根本不加载 reranker，
    # doctor 就不该报缺失、也不该建议下这 2GB 模型
    return ((config.settings().get("retrieval") or {}).get("rerank") or {}).get("enabled", True)


# ------------------------------------------------------------------ 缓存根解析

def _cache_roots() -> list[Path]:
    """所有可能的缓存根，按优先级去重。

    sentence-transformers 用 SENTENCE_TRANSFORMERS_HOME 作 cache_folder，模型落在
    `<ST_HOME>/models--...`（**不是** /hub 下）——Docker 里 HF_HOME 与
    SENTENCE_TRANSFORMERS_HOME 都设成 /models，只查 HF hub 路径会漏掉。
    """
    roots: list[Path] = []
    if os.environ.get("SENTENCE_TRANSFORMERS_HOME"):
        roots.append(Path(os.environ["SENTENCE_TRANSFORMERS_HOME"]))
    if os.environ.get("HF_HUB_CACHE"):
        roots.append(Path(os.environ["HF_HUB_CACHE"]))
    if os.environ.get("HF_HOME"):
        roots.append(Path(os.environ["HF_HOME"]) / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")

    seen, out = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ------------------------------------------------------------------ 完整性判定

def _has_tokenizer(snapshot: Path) -> bool:
    # tokenizer_config.json 只是元数据；真加载需要 tokenizer.json / sentencepiece 等
    if any((snapshot / f).exists() for f in _TOKENIZER_FILES):
        return True
    # GPT 风格 BPE：vocab.json + merges.txt
    return (snapshot / "vocab.json").exists() and (snapshot / "merges.txt").exists()


def _has_weights(snapshot: Path) -> bool:
    # 1) sharded：有 index 就必须 weight_map 里每个分片都在
    for index_name in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
        idx = snapshot / index_name
        if idx.exists():
            try:
                shards = set(json.loads(idx.read_text()).get("weight_map", {}).values())
            except (OSError, json.JSONDecodeError):
                return False
            return bool(shards) and all((snapshot / s).exists() for s in shards)
    # 2) 有分片文件却没 index = 残缺（中断的 sharded 下载）
    if any(_SHARD_RE.search(f.name) for f in snapshot.glob("*")):
        return False
    # 3) 单文件权重（.exists() 跟随软链，悬空即缺）
    for pattern in ("*.safetensors", "pytorch_model*.bin"):
        if any(f.exists() for f in snapshot.glob(pattern)):
            return True
    return False


def validate_model_artifact(snapshot: Path) -> bool:
    """快照是否真的能用——config + 真分词器 + 完整权重三者齐全。

    只判目录存在远远不够：HF 缓存目录在下载中断后照样在，里头一堆悬空软链，
    doctor 会报 ✓ 而运行时加载才炸。这里跟随软链实际验证文件可达。
    """
    if not (snapshot / "config.json").exists():
        return False
    return _has_tokenizer(snapshot) and _has_weights(snapshot)


def _snapshot_size(snapshot: Path) -> int:
    total = 0
    for f in snapshot.rglob("*"):
        if f.is_file():  # 跟随软链；悬空软链 is_file()=False，自动跳过
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


# ------------------------------------------------------------------ 探测

def _cached(name, role, snapshot) -> ModelResult:
    return ModelResult(name=name, role=role, status="cached", path=str(snapshot),
                       size_bytes=_snapshot_size(snapshot), source="huggingface-cache")


def _incomplete(name, role, model_dir) -> ModelResult:
    return ModelResult(name=name, role=role, status="incomplete", path=str(model_dir),
                       source="huggingface-cache",
                       error="快照文件不全（下载可能中断），建议清缓存后重下")


def _probe_in_root(root: Path, role: str, name: str) -> ModelResult | None:
    """在单个缓存根里探一个模型；不在这个根返回 None。"""
    model_dir = root / f"models--{name.replace('/', '--')}"
    if not model_dir.exists():
        return None

    # 优先校验 refs/main 指向的 revision——运行时加载的就是它。
    # 旧完整快照 + 新中断快照的组合，只有盯住 refs/main 才不会误报 cached。
    ref = model_dir / "refs" / "main"
    if ref.exists():
        try:
            head = ref.read_text().strip()
        except OSError:
            head = ""
        if head:
            target = model_dir / "snapshots" / head
            if validate_model_artifact(target):
                return _cached(name, role, target)
            return _incomplete(name, role, model_dir)  # 当前 revision 不完整/缺失

    # 无 refs/main（无 revision 信息）→ 退回「任一完整快照」
    snap_root = model_dir / "snapshots"
    snaps = [d for d in snap_root.glob("*") if d.is_dir()] if snap_root.exists() else []
    complete = next((s for s in snaps if validate_model_artifact(s)), None)
    if complete:
        return _cached(name, role, complete)
    return _incomplete(name, role, model_dir)


def _probe(role: str, name: str) -> ModelResult:
    # 本地路径（少见：config 指向本地模型目录）——存在即信任
    if os.sep in name and Path(name).is_dir():
        return ModelResult(name=name, role=role, status="cached", path=name, source="local-path")

    incomplete: ModelResult | None = None
    for root in _cache_roots():
        r = _probe_in_root(root, role, name)
        if r is None:
            continue
        if r.status == "cached":
            return r
        incomplete = r  # 某个根里装了一半——记下，但别的根可能有完整的
    return incomplete or ModelResult(name=name, role=role, status="missing",
                                     source="huggingface-cache")


def get_cache_status() -> list[ModelResult]:
    """只探不下，供向导与 doctor 复用。rerank 关闭时不探 reranker（运行时不加载它）。"""
    results = [_probe("embedding", _embed_model_name())]
    if _rerank_enabled():
        results.append(_probe("reranker", _rerank_model_name()))
    return results


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
    """编排。缺哪个下哪个，已缓存的秒返回。rerank 关闭时不下 reranker。"""
    results = [download_embedding()]
    if _rerank_enabled():
        results.append(download_reranker())
    return results
