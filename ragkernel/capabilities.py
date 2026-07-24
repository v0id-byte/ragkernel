"""运行时能力探测。

**独立成模块**，因为 Web UI、MCP `get_server_info`、`ragkernel version` 三处都要用——
写三套探测迟早不一致。

**必须运行时探测，不能写死列表。** CAD 是 optional extra（cadquery-ocp-novtk / trimesh），
装没装是部署决定的；写死等于宣称一个可能根本没装的能力，agent 照着调就是一次必然失败的
工具调用。

**返回嵌套结构而非扁平列表**，这样能表达能力分级（支持 STEP 导入但尚不支持拓扑抽取），
日后加一档不必改契约——而契约是给 MCP agent 的，改一次所有 agent 都要跟。
"""

import time

# OCP / trimesh / rapidocr 的 import 都是重量级的（OCP 尤其），而 get_server_info 是
# agent 可能反复调的只读工具——不缓存的话每问一次版本就触发一轮重 import。
# 探测结果在进程生命周期内几乎不变，缓存零风险。
_TTL = 300.0
_cache: tuple[float, dict] | None = None


def _probe(module: str) -> bool:
    """只看能不能导入，不碰功能。失败的原因（没装 / ABI 不匹配 / 缺系统库）
    在这里等价——都意味着这个能力用不了。"""
    try:
        __import__(module)
    except Exception:  # noqa: BLE001
        return False
    return True


def _cad() -> dict:
    return {
        # STEP 走 OpenCASCADE XDE，STL 走 trimesh——两个独立的可选依赖，分别探测
        "step": _probe("OCP.STEPCAFControl"),
        "stl": _probe("trimesh"),
        # 拓扑抽取（孔识别、GD&T）明确未支持。诚实声明比让 agent 试出来强
        "topology": False,
    }


def _ocr() -> dict:
    ok = _probe("rapidocr_onnxruntime")
    return {"enabled": ok, "provider": "rapidocr" if ok else None}


def _retrieval() -> dict:
    from . import config

    rr = ((config.settings().get("retrieval") or {}).get("rerank") or {})
    return {
        "hybrid": True,                       # BM25 + 向量是内核能力，不可关
        "rerank": bool(rr.get("enabled", False)),
        "rerank_model": rr.get("model") if rr.get("enabled") else None,
    }


def detect(*, force: bool = False) -> dict:
    global _cache
    now = time.monotonic()
    if not force and _cache and now - _cache[0] < _TTL:
        return _cache[1]

    caps = {
        "cad": _cad(),
        "ocr": _ocr(),
        "retrieval": _retrieval(),
        "claim_verify": {"enabled": True},
    }
    _cache = (now, caps)
    return caps
