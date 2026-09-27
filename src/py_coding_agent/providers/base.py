from __future__ import annotations

from typing import Protocol

from ..messages import Message, TokenUsage, ToolSpec


class ProviderError(RuntimeError):
    pass


class ProviderClient(Protocol):
    last_usage: TokenUsage | None

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        pass

    def count_input_tokens(self, messages: list[Message], tools: list[ToolSpec]) -> int:
        pass
