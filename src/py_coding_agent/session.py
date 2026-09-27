from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .messages import Message


class Session:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: list[Message] = []

    @classmethod
    def create(cls, session_dir: Path) -> "Session":
        session_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return cls(session_dir / f"{stamp}-{uuid.uuid4().hex[:8]}.jsonl")

    @classmethod
    def load(cls, path: Path) -> "Session":
        session = cls(path)
        if not path.exists():
            return session
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line:
                    session.messages.append(json.loads(line))
        return session

    @classmethod
    def latest(cls, session_dir: Path) -> "Session":
        sessions = sorted(session_dir.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not sessions:
            return cls.create(session_dir)
        return cls.load(sessions[0])

    def append(self, message: Message) -> None:
        self.messages.append(message)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            for message in self.messages:
                file.write(json.dumps(message, ensure_ascii=False) + "\n")

    def export_markdown(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# Session Export\n\nSource: `{self.path}`\n"]
        for message in self.messages:
            role = message["role"]
            content = message.get("content") or ""
            lines.append(f"\n## {role}\n\n{content}\n")
            for tool_call in message.get("tool_calls", []):
                name = tool_call["function"]["name"]
                args = tool_call["function"].get("arguments", "{}")
                lines.append(f"\n```tool-call\n{name} {args}\n```\n")
        path.write_text("\n".join(lines), encoding="utf-8")

    def compact(self, keep: int = 8, summary: str | None = None) -> str:
        if len(self.messages) <= keep:
            return "session is already small"
        older = self.messages[:-keep]
        recent = self.messages[-keep:]
        summary = summary or mechanical_summary(older)
        self.messages = [{"role": "user", "content": summary}, *recent]
        self.save()
        return f"compacted session to {len(self.messages)} messages"

    def fork(self, session_dir: Path, upto: int | None = None) -> "Session":
        forked = Session.create(session_dir)
        if upto is None:
            forked.messages = list(self.messages)
        else:
            end = max(0, min(upto + 1, len(self.messages)))
            forked.messages = list(self.messages[:end])
        forked.save()
        return forked

    def tree_lines(self) -> list[str]:
        lines = [f"session: {self.path}"]
        for index, message in enumerate(self.messages):
            role = message["role"]
            content = str(message.get("content") or "").replace("\n", " ")
            if len(content) > 80:
                content = content[:80] + "..."
            lines.append(f"{index:03d} {role}: {content}")
            for tool_call in message.get("tool_calls", []):
                name = tool_call["function"]["name"]
                lines.append(f"    tool_call: {name}")
        return lines


def mechanical_summary(messages: list[Message]) -> str:
    summary_lines = ["Previous conversation was compacted. Important prior messages:"]
    for message in messages:
        content = str(message.get("content") or "").replace("\n", " ")
        if len(content) > 300:
            content = content[:300] + "..."
        summary_lines.append(f"- {message['role']}: {content}")
    return "\n".join(summary_lines)
