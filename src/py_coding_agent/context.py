from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .messages import Message, ToolSpec
from .session import mechanical_summary


Summarizer = Callable[[list[Message]], str]
TokenCounter = Callable[[list[Message], list[ToolSpec]], int]


@dataclass
class ContextPipelineResult:
    messages: list[Message]
    events: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    input_token_budget: int = 0


def apply_context_pipeline(
    messages: list[Message],
    workspace: Path,
    summarizer: Summarizer | None = None,
    tools: list[ToolSpec] | None = None,
    fixed_messages: list[Message] | None = None,
    token_counter: TokenCounter | None = None,
    context_window_tokens: int = 128_000,
    max_output_tokens: int = 4_096,
    context_trigger_ratio: float = 0.85,
    context_safety_tokens: int = 4_096,
    max_messages: int = 50,
    keep_recent_tool_results: int = 3,
    tool_result_token_limit: int = 12_000,
    force_summary: bool = False,
    reactive: bool = False,
) -> ContextPipelineResult:
    current = _copy_messages(messages)
    events: list[str] = []
    tool_specs = tools or []
    prefix = fixed_messages or []

    hard_input_budget = max(1, context_window_tokens - max_output_tokens - context_safety_tokens)
    input_token_budget = max(1, int(hard_input_budget * min(max(context_trigger_ratio, 0.1), 1.0)))

    def count_tokens(candidate: list[Message]) -> int:
        complete_messages = [*prefix, *candidate]
        if token_counter is not None:
            try:
                return max(1, int(token_counter(complete_messages, tool_specs)))
            except Exception:
                pass
        return estimate_tokens(complete_messages, tool_specs)

    current, count = tool_result_budget(current, workspace, tool_result_token_limit)
    if count:
        events.append(f"L3 persisted {count} large tool result(s)")

    before = len(current)
    current = snip_compact(current, max_messages=max_messages)
    if len(current) < before:
        events.append(f"L1 snipped {before - len(current)} message(s)")

    current, count = micro_compact(current, keep_recent_tool_results=keep_recent_tool_results)
    if count:
        events.append(f"L2 compacted {count} old tool result(s)")

    estimated_tokens = count_tokens(current)
    if force_summary or estimated_tokens > input_token_budget:
        current = compact_history(current, workspace, summarizer=summarizer, reactive=reactive)
        events.append("L4 summarized history" if not reactive else "reactive compact summarized history")
        estimated_tokens = count_tokens(current)

    return ContextPipelineResult(current, events, estimated_tokens, input_token_budget)


def estimate_tokens(messages: list[Message], tools: list[ToolSpec] | None = None) -> int:
    payload = {"messages": messages, "tools": tools or []}
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, math.ceil(len(serialized.encode("utf-8")) / 4 * 1.1))


def tool_result_budget(messages: list[Message], workspace: Path, token_limit: int) -> tuple[list[Message], int]:
    root = workspace / ".py-coding-agent" / "tool-results"
    count = 0
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        if estimate_tokens([{"role": "tool", "content": content}]) <= token_limit:
            continue
        tool_call_id = str(message.get("tool_call_id", "unknown")).replace("/", "_").replace("\\", "_")
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{tool_call_id}.txt"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        rel = path.relative_to(workspace)
        message["content"] = f"[Large tool result persisted to {rel}. Re-read that file if needed.]"
        count += 1
    return messages, count


def snip_compact(messages: list[Message], max_messages: int = 50) -> list[Message]:
    if len(messages) <= max_messages:
        return messages
    groups = _message_groups(messages)
    if len(groups) <= max_messages:
        return messages
    keep_head = 3
    keep_tail = max(1, max_messages - keep_head)
    head_end = min(keep_head, len(groups))
    tail_start = max(head_end, len(groups) - keep_tail)
    if head_end >= tail_start:
        return messages
    snipped_messages = sum(len(group) for group in groups[head_end:tail_start])
    compacted_groups = groups[:head_end] + [[{"role": "user", "content": f"[snipped {snipped_messages} messages]"}]] + groups[tail_start:]
    return [message for group in compacted_groups for message in group]


def micro_compact(messages: list[Message], keep_recent_tool_results: int = 3) -> tuple[list[Message], int]:
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    if len(tool_messages) <= keep_recent_tool_results:
        return messages, 0
    count = 0
    for message in tool_messages[:-keep_recent_tool_results]:
        content = str(message.get("content") or "")
        if len(content) > 120 and not content.startswith("[Large tool result persisted"):
            message["content"] = "[Earlier tool result compacted. Re-run the relevant tool if needed.]"
            count += 1
    return messages, count


def compact_history(
    messages: list[Message],
    workspace: Path,
    summarizer: Summarizer | None = None,
    reactive: bool = False,
) -> list[Message]:
    transcript = write_transcript(messages, workspace)
    try:
        summary = summarizer(messages) if summarizer is not None else mechanical_summary(messages)
    except Exception:
        summary = mechanical_summary(messages)
    if reactive:
        tail = _safe_tail(messages, keep_groups=5)
        return [{"role": "user", "content": f"[Reactive compact]\nTranscript: {transcript}\n\n{summary}"}, *tail]
    return [{"role": "user", "content": f"[Compacted]\nTranscript: {transcript}\n\n{summary}"}]


def write_transcript(messages: list[Message], workspace: Path) -> Path:
    root = workspace / ".py-coding-agent" / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as file:
        for message in messages:
            file.write(json.dumps(message, ensure_ascii=False) + "\n")
    return path.relative_to(workspace)


def _copy_messages(messages: list[Message]) -> list[Message]:
    return json.loads(json.dumps(messages, ensure_ascii=False, default=str))


def _message_groups(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected = len(message.get("tool_calls", []))
            index += 1
            while index < len(messages) and expected > 0 and messages[index].get("role") == "tool":
                group.append(messages[index])
                expected -= 1
                index += 1
            groups.append(group)
            continue
        groups.append(group)
        index += 1
    return groups


def _safe_tail(messages: list[Message], keep_groups: int) -> list[Message]:
    groups = _message_groups(messages)
    return [message for group in groups[-keep_groups:] for message in group]
