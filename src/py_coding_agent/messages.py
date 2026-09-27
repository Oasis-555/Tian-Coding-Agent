from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict


Role = Literal["system", "user", "assistant", "tool"]


class ToolCallFunction(TypedDict):
    name: str
    arguments: str


class ToolCall(TypedDict):
    id: str
    type: Literal["function"]
    function: ToolCallFunction


class Message(TypedDict):
    role: Role
    content: str | list[dict[str, Any]] | None
    tool_call_id: NotRequired[str]
    tool_calls: NotRequired[list[ToolCall]]
    reasoning_content: NotRequired[str]


class ToolSpec(TypedDict):
    type: Literal["function"]
    function: dict[str, Any]


class ToolResult(TypedDict):
    content: str
    is_error: bool


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    source: str = "provider"
