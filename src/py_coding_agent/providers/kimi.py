from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..messages import Message, TokenUsage, ToolSpec
from .base import ProviderError
from .openai import estimate_request_tokens


class KimiCodingClient:
    def __init__(self, base_url: str, api_key: str, model: str, max_output_tokens: int = 4096) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.last_usage: TokenUsage | None = None

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        if not self.api_key:
            raise ProviderError("KIMI_API_KEY or PY_CODING_AGENT_API_KEY is not set")

        system, kimi_messages = _to_anthropic_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": system,
            "messages": kimi_messages,
            "tools": [_to_anthropic_tool(tool) for tool in tools],
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"kimi returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"kimi request failed: {exc.reason}") from exc

        self.last_usage = _parse_anthropic_usage(payload.get("usage"))
        return _from_anthropic_message(payload)

    def count_input_tokens(self, messages: list[Message], tools: list[ToolSpec]) -> int:
        system, converted = _to_anthropic_messages(messages)
        request_messages: list[Message] = [{"role": "system", "content": system}]
        request_messages.extend(converted)  # type: ignore[arg-type]
        return estimate_request_tokens(request_messages, tools)


def _to_anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    function = tool["function"]
    return {
        "name": function["name"],
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {"type": "object"}),
    }


def _to_anthropic_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message.get("content")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            continue
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id", ""),
                            "content": content or "",
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for tool_call in message.get("tool_calls", []):
                arguments = tool_call["function"].get("arguments", "{}")
                try:
                    tool_input = json.loads(arguments)
                except json.JSONDecodeError:
                    tool_input = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call["id"],
                        "name": tool_call["function"]["name"],
                        "input": tool_input,
                    }
                )
            converted.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
        converted.append({"role": "user", "content": content or ""})
    return "\n\n".join(system_parts), converted


def _from_anthropic_message(payload: dict[str, Any]) -> Message:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in payload.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        if block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
    return {
        "role": "assistant",
        "content": "\n".join(part for part in text_parts if part),
        "tool_calls": tool_calls,
    }


def _parse_anthropic_usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    input_tokens = int(raw.get("input_tokens", 0) or 0)
    output_tokens = int(raw.get("output_tokens", 0) or 0)
    cached_tokens = int(raw.get("cache_read_input_tokens", 0) or 0)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cached_input_tokens=cached_tokens,
    )
