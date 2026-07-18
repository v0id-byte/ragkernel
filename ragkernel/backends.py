"""生成后端抽象：Anthropic（Claude / MiniMax）与 OpenAI 兼容（vLLM / Ollama / Xinference / 本地）。

一套 tool loop（agent.py），两种消息方言收在这里：
- Anthropic：content-blocks + `{"role":"user","content":[{tool_result}]}`；
- OpenAI：`tool_calls` + 每个结果一条 `{"role":"tool", tool_call_id, content}`，参数是 JSON 字符串。
本地引擎几乎都是 OpenAI 兼容而非 Anthropic 兼容，故 OpenAI 通路是私有化的一等公民。
"""

import json
import os
from dataclasses import dataclass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    text: str
    tool_calls: list
    assistant_message: dict


def anthropic_to_openai_tools(specs: list[dict]) -> list[dict]:
    """Anthropic 工具定义 → OpenAI function 定义（工具只定义一次）。"""
    return [
        {"type": "function", "function": {
            "name": s["name"],
            "description": s.get("description", ""),
            "parameters": s.get("input_schema", {"type": "object", "properties": {}}),
        }}
        for s in specs
    ]


class AnthropicBackend:
    def __init__(self, prov: dict):
        import anthropic

        key = os.environ.get(prov["api_key_env"], "")
        if not key:
            raise RuntimeError(f"缺少 {prov['api_key_env']}，请在 .env 中配置")
        self.client = anthropic.Anthropic(api_key=key, base_url=prov.get("base_url") or None)
        self.model = prov["model"]
        self.max_tokens = int(prov.get("max_tokens", 8000))

    def convert_tools(self, specs: list[dict]) -> list[dict]:
        return specs

    def step(self, system: str, tools: list, messages: list) -> Turn:
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, tools=tools, messages=messages
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, b.input or {}) for b in resp.content if b.type == "tool_use"]
        # 回传完整 content 块（MiniMax 要求 thinking 块随历史返回；Claude 亦兼容）
        return Turn(text, calls, {"role": "assistant", "content": resp.content})

    def tool_result_messages(self, results: list[tuple[str, str, str]]) -> list[dict]:
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": out} for tid, _, out in results
        ]}]


class OpenAIBackend:
    def __init__(self, prov: dict):
        from openai import OpenAI

        key = os.environ.get(prov["api_key_env"], "") or "EMPTY"  # 本地 vLLM 忽略 key，但 SDK 要非空
        self.client = OpenAI(api_key=key, base_url=prov.get("base_url") or None)
        self.model = prov["model"]
        self.max_tokens = int(prov.get("max_tokens", 8000))

    def convert_tools(self, specs: list[dict]) -> list[dict]:
        return anthropic_to_openai_tools(specs)

    def step(self, system: str, tools: list, messages: list) -> Turn:
        msgs = [{"role": "system", "content": system}] + messages
        resp = self.client.chat.completions.create(
            model=self.model, max_tokens=self.max_tokens, messages=msgs, tools=tools or None
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        # 干净重建 assistant 消息（避免个别本地服务对多余字段敏感）
        am = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            am["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        return Turn(text, calls, am)

    def tool_result_messages(self, results: list[tuple[str, str, str]]) -> list[dict]:
        return [{"role": "tool", "tool_call_id": tid, "content": out} for tid, _, out in results]


def get_backend(prov: dict):
    """按 provider.kind 选后端（默认 anthropic）。"""
    if prov.get("kind", "anthropic") == "openai":
        return OpenAIBackend(prov)
    return AnthropicBackend(prov)
