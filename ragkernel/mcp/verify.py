"""verify_engineering_claim：核验一条工程 claim 是否被知识库证据支持。

抗幻觉三道闸〔P0-4/P1-1/P1-2〕：
1. 证据只走 collect_evidence 的**范围过滤检索**（top-k 原子片），绝不整篇 read_document 灌进 LLM；
2. 先确定性判断——无证据直接 unsupported，不烧 provider 预算；
3. LLM 只从服务端分配的 evidence_id 里选，输出经 Pydantic 校验，引用由服务端按 id 回填（编造的 id 直接丢）。
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ValidationError

from .. import backends, config

MAX_CLAIM_CHARS = 2000
MAX_EVIDENCE_ITEMS = 10
MAX_EVIDENCE_CHARS = 24000
MAX_SCOPE_DOCUMENTS = 20

SPEC = {
    "name": "verify_engineering_claim",
    "description": (
        "核验一条工程结论是否被知识库证据支持。先在（可选）范围内检索原子证据，"
        "再由模型判定 supported/contradicted/unsupported，返回带真实页码引用的证据。"
        "适合写硬件/嵌入式代码时核对引脚能力、电气参数、故障码含义等——阻止基于错误假设的代码。"
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claim": {"type": "string", "minLength": 1, "maxLength": MAX_CLAIM_CHARS,
                      "description": "待核验的工程结论，如 'ESP32-S3 GPIO46 支持输出模式'"},
            "scope": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "model": {"type": "string", "maxLength": 200, "description": "限定设备/芯片型号"},
                    "document_ids": {"type": "array", "maxItems": MAX_SCOPE_DOCUMENTS,
                                     "items": {"type": "integer", "minimum": 1},
                                     "description": "限定在这些文档内检索证据（范围过滤，不是读全文）"},
                },
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8},
        },
        "required": ["claim"],
    },
}

_SYS = (
    "你是工程结论核验器。只依据给定的编号证据判断 claim 是否成立，绝不使用证据之外的知识、绝不编造。\n"
    "只输出一个 JSON 对象，不要任何多余文字或 code fence：\n"
    '{"verdict":"supported|contradicted|unsupported","confidence":"high|medium|low",'
    '"rationale":"简述依据","evidence_ids":["E1", ...]}\n'
    "规则：verdict 只能取这三个之一；evidence_ids 只能从给定证据编号里选，不得编造编号；"
    "证据不足或与 claim 无关 → unsupported 且 evidence_ids 为空；"
    "supported/contradicted 必须至少给一个真正相关的 evidence_id。"
)


class _LLMOut(BaseModel):
    verdict: Literal["supported", "contradicted", "unsupported"]
    confidence: Literal["high", "medium", "low"]
    rationale: str = ""
    evidence_ids: list[str] = []


def _parse_json(text: str) -> dict | None:
    """容错：剥掉 code fence，取第一个 {...} 再 json.loads。"""
    t = re.sub(r"```(?:json)?|```", "", (text or "").strip(), flags=re.I).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _dump(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def build(tb):
    """返回 (spec, handler)；handler 闭包持有当前 session 的 Toolbox。"""

    def verify_engineering_claim(claim: str, scope: dict | None = None, top_k: int = 8) -> str:
        scope = scope or {}
        claim = (claim or "").strip()[:MAX_CLAIM_CHARS]
        doc_ids = [int(d) for d in (scope.get("document_ids") or [])][:MAX_SCOPE_DOCUMENTS]
        model = scope.get("model") or None
        top_k = max(1, min(int(top_k or 8), 12))
        scope_out = {"model": model, "document_ids": doc_ids}
        if not claim:
            return _dump(verdict="unsupported", confidence="low", rationale="claim 为空",
                         evidence=[], scope=scope_out, warnings=["empty_claim"])

        evidence = tb.collect_evidence(claim, document_ids=doc_ids or None, model=model,
                                       top_k=top_k, max_chars=MAX_EVIDENCE_CHARS)[:MAX_EVIDENCE_ITEMS]
        tb.audit("tool:verify", {"claim": claim, "scope": scope, "n_evidence": len(evidence)})

        # 〔P1-2〕无证据 → 直接 unsupported，不调 LLM
        if not evidence:
            return _dump(verdict="unsupported", confidence="low",
                         rationale="知识库中未检索到相关证据", evidence=[],
                         scope=scope_out, warnings=["no_evidence"])

        by_id = {e["id"]: e for e in evidence}
        listing = "\n\n".join(f'{e["id"]} {e["ref"]}\n{e["text"]}' for e in evidence)
        warnings: list[str] = []

        try:
            be = backends.get_backend(config.provider())
            turn = be.step(system=_SYS, tools=[],
                           messages=[be.user_message(f"claim：{claim}\n\n证据：\n{listing}")])
        except Exception as exc:
            # provider 未配 / 超时 / 报错 —— 回退证据但不硬判定，不影响其它工具
            return _dump(verdict="unsupported", confidence="low",
                         rationale=f"判定后端不可用：{type(exc).__name__}",
                         evidence=[{"ref": e["ref"], "text": e["text"]} for e in evidence[:3]],
                         scope=scope_out, warnings=["provider_unavailable"])

        raw = _parse_json(turn.text)
        try:
            parsed = _LLMOut.model_validate(raw) if raw is not None else None
        except ValidationError:
            parsed = None
        if parsed is None:
            return _dump(verdict="unsupported", confidence="low",
                         rationale="模型未返回可解析的判定",
                         evidence=[{"ref": e["ref"], "text": e["text"]} for e in evidence[:3]],
                         scope=scope_out, warnings=["llm_output_unparseable"])

        # 〔P1-1〕服务端 grounding：只认真实存在的 evidence_id，编造的丢弃
        picked = [by_id[eid] for eid in parsed.evidence_ids if eid in by_id]
        if len(picked) != len(parsed.evidence_ids):
            warnings.append("llm_cited_unknown_ids")

        verdict, confidence = parsed.verdict, parsed.confidence
        if verdict in ("supported", "contradicted") and not picked:
            verdict, confidence = "unsupported", "low"
            warnings.append("no_valid_evidence_downgraded")

        return _dump(verdict=verdict, confidence=confidence, rationale=parsed.rationale,
                     evidence=[{"ref": e["ref"], "text": e["text"]} for e in picked],
                     scope=scope_out, warnings=warnings)

    return SPEC, verify_engineering_claim
