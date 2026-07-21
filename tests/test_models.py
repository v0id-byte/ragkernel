"""models 生命周期 + check_models 单测。

用 tmp_path 造假 HF 缓存（HF_HUB_CACHE 指过去），绝不碰真实 ~/.cache 或下真模型。
重点锁死：缓存完整性判定不能只看目录存在——下载中断的悬空软链要被识别为 incomplete。
"""

import json

import pytest

from ragkernel import models
from ragkernel.models import ModelResult


def _make_model(cache, name, *, weights="model.safetensors", with_weights=True,
                dangling=False, snap="abc123", write_ref=True):
    base = cache / f"models--{name.replace('/', '--')}"
    d = base / "snapshots" / snap
    d.mkdir(parents=True)
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")  # 真分词器产物
    if with_weights:
        if dangling:
            (d / weights).symlink_to(cache / "nonexistent-blob")  # 悬空软链 = 下载中断
        else:
            (d / weights).write_text("W" * 100)
    if write_ref:  # 真实 HF 缓存有 refs/main 指向当前 revision
        (base / "refs").mkdir(parents=True, exist_ok=True)
        (base / "refs" / "main").write_text(snap)
    return d


# ------------------------------------------------------------------ validate

def test_validate_requires_config_tokenizer_and_weights(tmp_path):
    d = tmp_path / "snap"
    d.mkdir()
    assert not models.validate_model_artifact(d)          # 空
    (d / "config.json").write_text("{}")
    assert not models.validate_model_artifact(d)          # 缺 tokenizer + 权重
    (d / "tokenizer.json").write_text("{}")
    assert not models.validate_model_artifact(d)          # 缺权重
    (d / "model.safetensors").write_text("W")
    assert models.validate_model_artifact(d)              # 齐了


def test_validate_accepts_pytorch_bin(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")     # 真分词器
    (d / "tokenizer_config.json").write_text("{}")
    (d / "pytorch_model.bin").write_text("W")
    assert models.validate_model_artifact(d)


def test_validate_rejects_tokenizer_config_only(tmp_path):
    """tokenizer_config.json 只是元数据——缺真分词器（tokenizer.json/sentencepiece）时
    加载会炸，不能当 cached。"""
    d = tmp_path / "s"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "tokenizer_config.json").write_text("{}")  # 只有元数据，无真分词器
    (d / "model.safetensors").write_text("W")
    assert not models.validate_model_artifact(d)


def test_validate_accepts_complete_sharded(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")
    (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {
        "a": "model-00001-of-00002.safetensors", "b": "model-00002-of-00002.safetensors"}}))
    (d / "model-00001-of-00002.safetensors").write_text("W")
    (d / "model-00002-of-00002.safetensors").write_text("W")
    assert models.validate_model_artifact(d)


def test_validate_rejects_partial_shards(tmp_path):
    """index 列了 2 个分片，只下到 1 个——transformers 读 index 会去开缺的那个而炸。"""
    d = tmp_path / "s"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")
    (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {
        "a": "model-00001-of-00002.safetensors", "b": "model-00002-of-00002.safetensors"}}))
    (d / "model-00001-of-00002.safetensors").write_text("W")  # 只有 1/2
    assert not models.validate_model_artifact(d)


def test_validate_rejects_shards_without_index(tmp_path):
    """有分片文件却没 index.json = 中断的 sharded 下载。"""
    d = tmp_path / "s"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")
    (d / "model-00001-of-00002.safetensors").write_text("W")
    assert not models.validate_model_artifact(d)


def test_validate_rejects_dangling_weight_symlink(tmp_path):
    """下载中断留下指向缺失 blob 的悬空软链——必须判为不可用。"""
    d = tmp_path / "s"
    d.mkdir()
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")
    (d / "model.safetensors").symlink_to(tmp_path / "missing-blob")
    assert not models.validate_model_artifact(d)


# ------------------------------------------------------------------ get_cache_status / _probe

def test_probe_cached_with_size(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    _make_model(tmp_path, "BAAI/bge-m3")
    r = models._probe("embedding", "BAAI/bge-m3")
    assert r.status == "cached"
    assert r.size_bytes and r.size_bytes > 0
    assert r.source == "huggingface-cache"


def test_probe_missing_when_no_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    r = models._probe("embedding", "BAAI/bge-m3")
    assert r.status == "missing"


def test_probe_incomplete_when_no_weights(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    _make_model(tmp_path, "BAAI/bge-m3", with_weights=False)
    r = models._probe("embedding", "BAAI/bge-m3")
    assert r.status == "incomplete"


def test_probe_incomplete_when_weights_dangling(monkeypatch, tmp_path):
    """核心回归：只判目录存在会把中断的下载误报 cached。"""
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    _make_model(tmp_path, "BAAI/bge-m3", dangling=True)
    r = models._probe("embedding", "BAAI/bge-m3")
    assert r.status == "incomplete"


def test_probe_local_path_is_trusted(monkeypatch, tmp_path):
    local = tmp_path / "my-local-model"
    local.mkdir()
    r = models._probe("embedding", str(local))
    assert r.status == "cached" and r.source == "local-path"


def test_get_cache_status_covers_both_roles(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    monkeypatch.setattr(models, "_embed_model_name", lambda: "BAAI/bge-m3")
    monkeypatch.setattr(models, "_rerank_model_name", lambda: "BAAI/bge-reranker-v2-m3")
    _make_model(tmp_path, "BAAI/bge-m3", weights="pytorch_model.bin")
    _make_model(tmp_path, "BAAI/bge-reranker-v2-m3")
    by_role = {r.role: r for r in models.get_cache_status()}
    assert by_role["embedding"].status == "cached"
    assert by_role["reranker"].status == "cached"


def test_rerank_disabled_skips_reranker_probe(monkeypatch, tmp_path):
    """rerank.enabled=false 时运行时不加载 reranker——doctor 就不该报缺、也不该建议下它。"""
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    monkeypatch.setattr(models, "_embed_model_name", lambda: "BAAI/bge-m3")
    monkeypatch.setattr(models, "_rerank_enabled", lambda: False)
    _make_model(tmp_path, "BAAI/bge-m3")
    assert [r.role for r in models.get_cache_status()] == ["embedding"]


def test_probe_refs_main_complete_is_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    _make_model(tmp_path, "BAAI/bge-m3", snap="good999")
    r = models._probe("embedding", "BAAI/bge-m3")
    assert r.status == "cached" and "good999" in (r.path or "")


def test_probe_refs_main_incomplete_shadows_old_complete(monkeypatch, tmp_path):
    """旧完整快照 + refs/main 指向的新中断快照——运行时加载新 revision 会炸，必须报 incomplete，
    不能因为「还有个旧的完整快照」就误报 cached。"""
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    _make_model(tmp_path, "BAAI/bge-m3", snap="old111", write_ref=False)     # 老的完整
    _make_model(tmp_path, "BAAI/bge-m3", snap="new222", with_weights=False)  # 新的中断 + refs/main→new222
    r = models._probe("embedding", "BAAI/bge-m3")
    assert r.status == "incomplete"


def test_probe_honors_sentence_transformers_home(monkeypatch, tmp_path):
    """Docker 里 SENTENCE_TRANSFORMERS_HOME 作 cache_folder，模型落 <ST_HOME>/models--...
    只查 HF hub 路径会漏。这里测真实 _cache_roots，故隔离所有相关 env + HOME。"""
    for v in ("HF_HUB_CACHE", "HF_HOME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # 兜底根也落 tmp，隔离真缓存
    st = tmp_path / "st"
    st.mkdir()
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", str(st))
    _make_model(st, "BAAI/bge-m3")
    assert models._probe("embedding", "BAAI/bge-m3").status == "cached"


# ------------------------------------------------------------------ download

def test_download_embedding_reports_downloaded_when_new(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    monkeypatch.setattr(models, "_embed_model_name", lambda: "BAAI/bge-m3")
    import ragkernel.embed as embed_mod

    def fake_embed(texts, **kw):
        _make_model(tmp_path, "BAAI/bge-m3")  # 模拟下载落盘
    monkeypatch.setattr(embed_mod, "embed", fake_embed)

    r = models.download_embedding()
    assert r.status == "downloaded"


def test_download_embedding_reports_cached_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    monkeypatch.setattr(models, "_embed_model_name", lambda: "BAAI/bge-m3")
    _make_model(tmp_path, "BAAI/bge-m3")
    import ragkernel.embed as embed_mod
    monkeypatch.setattr(embed_mod, "embed", lambda texts, **kw: None)

    r = models.download_embedding()
    assert r.status == "cached"


def test_download_embedding_error_on_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_cache_roots", lambda: [tmp_path])
    monkeypatch.setattr(models, "_embed_model_name", lambda: "BAAI/bge-m3")
    import ragkernel.embed as embed_mod

    def boom(texts, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(embed_mod, "embed", boom)

    r = models.download_embedding()
    assert r.status == "error"
    assert "network down" in r.error


# ------------------------------------------------------------------ check_models

def test_check_models_all_cached_passes(monkeypatch):
    monkeypatch.setattr(models, "get_cache_status", lambda: [
        ModelResult("BAAI/bge-m3", "embedding", "cached", size_bytes=2 * 1024**3),
        ModelResult("BAAI/x", "reranker", "cached", size_bytes=1024**3),
    ])
    from ragkernel.checks.models import check_models
    r = check_models()
    assert r.status == "passed"


def test_check_models_missing_is_warning_not_unhealthy(monkeypatch):
    """模型没缓存是「功能缺失但系统健康」——degraded，不是 unhealthy。"""
    monkeypatch.setattr(models, "get_cache_status", lambda: [
        ModelResult("BAAI/bge-m3", "embedding", "missing"),
        ModelResult("BAAI/x", "reranker", "cached", size_bytes=1),
    ])
    from ragkernel.checks.models import check_models
    r = check_models()
    assert r.status == "failed" and r.severity == "warning"
    assert r.fix == "ragkernel models"


def test_check_models_incomplete_is_flagged(monkeypatch):
    monkeypatch.setattr(models, "get_cache_status", lambda: [
        ModelResult("BAAI/bge-m3", "embedding", "incomplete"),
        ModelResult("BAAI/x", "reranker", "cached", size_bytes=1),
    ])
    from ragkernel.checks.models import check_models
    r = check_models()
    assert r.status == "failed" and r.severity == "warning"
    assert "incomplete" in r.summary


# ------------------------------------------------------------------ cmd_models 退出码

def test_cmd_models_exits_nonzero_on_error(monkeypatch):
    """ragkernel models 既是预载、也是 doctor 的修复命令——失败必须非零退出，
    否则自动化（含 install 向导）会把失败当成功。"""
    import ragkernel.cli as cli

    monkeypatch.setattr("ragkernel.models.download", lambda: [
        ModelResult("BAAI/bge-m3", "embedding", "error", error="boom"),
        ModelResult("BAAI/x", "reranker", "cached", size_bytes=1),
    ])
    with pytest.raises(SystemExit) as ei:
        cli.cmd_models()
    assert ei.value.code == 1


def test_cmd_models_success_exits_zero(monkeypatch):
    import ragkernel.cli as cli

    monkeypatch.setattr("ragkernel.models.download", lambda: [
        ModelResult("BAAI/bge-m3", "embedding", "cached", size_bytes=1),
    ])
    cli.cmd_models()  # 不抛 SystemExit 即通过
