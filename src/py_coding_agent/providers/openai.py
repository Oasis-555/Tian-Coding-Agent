from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any

from ..messages import Message, TokenUsage, ToolSpec
from .base import ProviderError


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_output_tokens: int = 4096,
        reasoning_effort: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.last_usage: TokenUsage | None = None

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        if not self.api_key:
            raise ProviderError("OPENAI_API_KEY or PY_CODING_AGENT_API_KEY is not set")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": _prepare_messages(messages, self.base_url, self.model),
            "tools": tools,
            "tool_choice": "auto",
        }
        if self.model == "kimi-k3":
            body["max_completion_tokens"] = self.max_output_tokens
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
        else:
            body["max_tokens"] = self.max_output_tokens
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"provider returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"provider request failed: {exc.reason}") from exc

        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected provider response: {payload}") from exc

        self.last_usage = _parse_openai_usage(payload.get("usage"))
        return {
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls", []),
            "reasoning_content": message.get("reasoning_content", ""),
        }

    def count_input_tokens(self, messages: list[Message], tools: list[ToolSpec]) -> int:
        prepared = _prepare_messages(messages, self.base_url, self.model)
        if "api.moonshot.cn" not in self.base_url or not self.api_key:
            return estimate_request_tokens(prepared, tools)
        body = {"model": self.model, "messages": prepared}
        request = urllib.request.Request(
            f"{self.base_url}/tokenizers/estimate-token-count",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            message_tokens = int(payload["data"]["total_tokens"])
            return message_tokens + estimate_value_tokens(tools)
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return estimate_request_tokens(prepared, tools)


def _prepare_messages(messages: list[Message], base_url: str, model: str) -> list[dict[str, Any]]:
    needs_reasoning_content = "api.moonshot.cn" in base_url or model.startswith("kimi-")
    prepared: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = dict(message)
        if item.get("tool_calls") == []:
            item.pop("tool_calls")
        if needs_reasoning_content and item.get("role") == "assistant" and item.get("tool_calls"):
            item.setdefault("reasoning_content", "")
        prepared.append(item)
    return prepared


def estimate_request_tokens(messages: list[dict[str, Any]] | list[Message], tools: list[ToolSpec]) -> int:
    return estimate_value_tokens({"messages": messages, "tools": tools})


def estimate_value_tokens(value: Any) -> int:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, math.ceil(len(serialized.encode("utf-8")) / 4 * 1.1))


def _parse_openai_usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    input_tokens = int(raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0)
    output_tokens = int(raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0)
    total_tokens = int(raw.get("total_tokens", input_tokens + output_tokens) or input_tokens + output_tokens)
    input_details = raw.get("prompt_tokens_details", raw.get("input_tokens_details", {}))
    output_details = raw.get("completion_tokens_details", raw.get("output_tokens_details", {}))
    cached_tokens = int(input_details.get("cached_tokens", 0) or 0) if isinstance(input_details, dict) else 0
    reasoning_tokens = int(output_details.get("reasoning_tokens", 0) or 0) if isinstance(output_details, dict) else 0
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
    )
