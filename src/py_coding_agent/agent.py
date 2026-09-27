from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .config import Config
from .context import apply_context_pipeline, estimate_tokens
from .llm import LlmError, create_client
from .messages import Message, ToolSpec
from .models import model_profile, supported_model_names
from .resources import NamedText
from .runtime import HarnessRuntime
from .session import Session, mechanical_summary
from .tools import Tool, _tool_spec, build_registry


SYSTEM_PROMPT = """You are a coding agent working in a local workspace.
Use tools to inspect and modify files when needed.
Keep final answers concise and include changed file paths when you edit files.
Do not invent file contents; read files before making targeted edits.
"""


class Agent:
    def __init__(
        self,
        config: Config,
        session: Session,
        output: Callable[[str], None] = print,
        confirm: Callable[[str], bool] | None = None,
        confirm_command: Callable[[str], bool] | None = None,
        skills: dict[str, NamedText] | None = None,
    ) -> None:
        self.config = config
        self.session = session
        self.output = output
        self.runtime = HarnessRuntime(config.workspace, output=output)
        self.rounds_since_todo = 0
        self.active_teammates: dict[str, threading.Thread] = {}
        self.available_skills = skills or {}
        self.active_skills: dict[str, NamedText] = {}
        self.extra_system_prompts: list[str] = []
        profile = model_profile(config.model)
        self.client = create_client(
            config.provider,
            config.base_url,
            config.api_key,
            config.model,
            max_output_tokens=config.max_output_tokens,
            reasoning_effort=profile.reasoning_effort if profile else None,
        )
        self.registry = build_registry(
            config.workspace,
            confirm=confirm,
            confirm_edits=config.confirm_edits,
            confirm_command=confirm_command,
            confirm_bash=config.confirm_bash,
            read_only=config.read_only,
            skills=self.available_skills,
            runtime=self.runtime,
        )
        self._register_agent_tools()
        self.tools: list[Tool] = []
        self.refresh_tools()

    def activate_skill(self, skill: NamedText) -> None:
        self.active_skills[skill.name] = skill

    def deactivate_skills(self) -> None:
        self.active_skills.clear()

    def refresh_tools(self) -> None:
        self.tools = self.registry.enabled(self.config.enabled_tools)
        if self.config.read_only:
            blocked = {"write_file", "edit_file", "bash", "start_background_command"}
            self.tools = [tool for tool in self.tools if tool.name not in blocked]

    def list_models(self) -> str:
        rows: list[str] = []
        for name in supported_model_names():
            profile = model_profile(name)
            if profile is None:
                continue
            marker = "*" if name == self.config.model else " "
            rows.append(
                f"{marker} {name}: {profile.display_name}, "
                f"context={profile.context_window_tokens:,} tokens, max_output={profile.max_output_tokens:,}"
            )
        return "\n".join(rows)

    def switch_model(self, model: str) -> str:
        profile = model_profile(model)
        if profile is None:
            return f"Unknown model: {model}. Available: {', '.join(supported_model_names())}"
        previous = self.config.model
        self.config = replace(
            self.config,
            provider="openai",
            model=profile.model,
            context_window_tokens=profile.context_window_tokens,
            max_output_tokens=profile.max_output_tokens,
        )
        self.client = create_client(
            self.config.provider,
            self.config.base_url,
            self.config.api_key,
            self.config.model,
            max_output_tokens=self.config.max_output_tokens,
            reasoning_effort=profile.reasoning_effort,
        )
        return (
            f"Switched model: {previous} -> {profile.model}; "
            f"context={profile.context_window_tokens:,} tokens; current session retained"
        )

    def prompt(self, text: str) -> str:
        lead_messages = self.runtime.consume_inbox("lead")
        if lead_messages:
            lead_inbox = json.dumps(lead_messages, ensure_ascii=False, indent=2)
            text = f"<inbox>{lead_inbox}</inbox>\n\n{text}"

        selected_memories = self._select_relevant_memories(text)
        user_message: Message = {"role": "user", "content": text}
        self.session.append(user_message)

        messages: list[Message] = [{"role": "system", "content": self._system_prompt(selected_memories)}, *self.session.messages]
        tool_specs = [tool.spec for tool in self.tools]

        for _ in range(self.config.max_steps):
            messages = self._apply_context_pipeline(
                messages,
                tool_specs=tool_specs,
                force_summary=self.config.auto_compact and len(self.session.messages) >= self.config.auto_compact_threshold,
            )
            assistant = self._complete_with_recovery(messages, tool_specs)
            self.session.append(assistant)
            messages.append(assistant)

            tool_calls = assistant.get("tool_calls", [])
            if not tool_calls:
                content = assistant.get("content") or ""
                self._extract_memories()
                self._maybe_consolidate_memories()
                return content if isinstance(content, str) else str(content)

            for tool_call in tool_calls:
                name = tool_call["function"]["name"]
                args = tool_call["function"].get("arguments", "{}")
                self.output(f"[tool] {name}")
                result = self.registry.execute(name, args)
                content = self._apply_todo_reminder(name, result["content"])
                tool_message: Message = {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": content,
                }
                self.session.append(tool_message)
                messages.append(tool_message)

        return "Stopped because the maximum tool loop step count was reached."

    def _apply_todo_reminder(self, tool_name: str, content: str) -> str:
        if tool_name == "todo_write":
            self.rounds_since_todo = 0
            return content
        self.rounds_since_todo += 1
        if self.rounds_since_todo < 3:
            return content
        self.rounds_since_todo = 0
        return content + "\n\n<reminder>Update your todos with todo_write before continuing.</reminder>"

    def _register_agent_tools(self) -> None:
        def spawn_teammate(args: dict) -> dict[str, object]:
            name = str(args["name"])
            role = str(args.get("role", "agent"))
            prompt = str(args.get("prompt", "Wait for inbox messages and help the lead agent."))
            return {"content": self._spawn_teammate_thread(name, role, prompt), "is_error": False}

        def run_subagent(args: dict) -> dict[str, object]:
            prompt = str(args["prompt"])
            sub_tools = self._subagent_tools()
            sub_tool_specs = [tool.spec for tool in sub_tools]
            system = (
                "You are a focused coding subagent. Work in an isolated context. "
                "Use the available local coding tools when needed. "
                "Do not delegate further and do not try to spawn another agent. "
                "Complete the task, then return a concise final summary for the lead agent."
            )
            messages: list[Message] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

            for _ in range(30):
                messages = self._apply_context_pipeline(messages, tool_specs=sub_tool_specs, persist=False)
                assistant = self._complete_with_recovery(messages, sub_tool_specs)
                messages.append(assistant)
                tool_calls = assistant.get("tool_calls", [])
                if not tool_calls:
                    content = assistant.get("content") or ""
                    return {"content": content if isinstance(content, str) else str(content), "is_error": False}

                for tool_call in tool_calls:
                    name = tool_call["function"]["name"]
                    args_text = tool_call["function"].get("arguments", "{}")
                    self.output(f"[subagent tool] {name}")
                    result = self.registry.execute(name, args_text)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": result["content"],
                        }
                    )

            return {"content": "Subagent stopped after 30 turns without a final answer.", "is_error": True}

        def compact_context(args: dict) -> dict[str, object]:
            use_llm = bool(args.get("use_llm", True))
            if use_llm:
                return {"content": self.compact_session(), "is_error": False}
            return {"content": self.compact_session(use_llm=False), "is_error": False}

        self.registry.register(
            Tool(
                _tool_spec(
                    "spawn_teammate",
                    "Spawn a teammate agent in a background thread. The teammate has its own loop and sends results to lead.",
                    {
                        "name": {"type": "string"},
                        "role": {"type": "string", "default": "agent"},
                        "prompt": {"type": "string"},
                    },
                    ["name", "prompt"],
                ),
                spawn_teammate,
            )
        )
        self.registry.register(
            Tool(
                _tool_spec(
                    "run_subagent",
                    "Run a focused one-shot subagent with a fresh context and return its summary.",
                    {"prompt": {"type": "string"}},
                    ["prompt"],
                ),
                run_subagent,
            )
        )
        self.registry.register(
            Tool(
                _tool_spec(
                    "compact_context",
                    "Compact the current session history.",
                    {"keep": {"type": "integer", "default": 8}, "use_llm": {"type": "boolean", "default": True}},
                    [],
                ),
                compact_context,
            )
        )

    def _spawn_teammate_thread(self, name: str, role: str, prompt: str) -> str:
        if name in self.active_teammates and self.active_teammates[name].is_alive():
            return f"Teammate '{name}' is already running"
        self.runtime.spawn_teammate(name, role)

        def run() -> None:
            self.output(f"[teammate] {name} spawned as {role}")
            summary = self._run_teammate_loop(name, role, prompt)
            self.runtime.send_message("lead", summary, from_agent=name, msg_type="result")
            self.active_teammates.pop(name, None)
            self.output(f"[teammate] {name} finished")

        thread = threading.Thread(target=run, daemon=True, name=f"py-coding-agent-teammate-{name}")
        self.active_teammates[name] = thread
        thread.start()
        return f"Teammate '{name}' spawned as {role}"

    def _run_teammate_loop(self, name: str, role: str, prompt: str) -> str:
        system = (
            f"You are '{name}', a {role}. Use tools to complete tasks. "
            "Read your inbox when it appears in context. Send progress or final results to 'lead' with send_message. "
            "When asked for a plan, call submit_plan and wait for approval before mutating files or running bash. "
            "When idle, claim available tasks yourself. Do not spawn other agents."
        )
        messages: list[Message] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        tools = self._teammate_tools()
        tool_specs = [tool.spec for tool in tools]
        state: dict[str, Any] = {
            "cwd": str(self.config.workspace),
            "approved_plan_ids": set(),
            "pending_plan_ids": set(),
            "last_reported": "",
        }
        handlers = self._teammate_handlers(name, state)
        last_text = ""

        idle_timeouts = 0
        while idle_timeouts < 3:
            inbox_result = self._inject_teammate_inbox(name, messages, state)
            if inbox_result == "shutdown":
                return "Shutting down."

            for _ in range(10):
                try:
                    messages = self._apply_context_pipeline(messages, tool_specs=tool_specs, persist=False)
                    assistant = self._complete_with_recovery(messages, tool_specs)
                except Exception as exc:
                    return f"Teammate error: {type(exc).__name__}: {exc}"

                messages.append(assistant)
                content = assistant.get("content") or ""
                if isinstance(content, str) and content.strip():
                    last_text = content.strip()

                tool_calls = assistant.get("tool_calls", [])
                if not tool_calls:
                    if last_text and state.get("last_reported") != last_text:
                        self.runtime.send_message("lead", last_text, from_agent=name, msg_type="idle_notification")
                        state["last_reported"] = last_text
                    break

                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"]
                    raw_args = tool_call["function"].get("arguments", "{}")
                    try:
                        parsed_args = json.loads(raw_args or "{}")
                        if not isinstance(parsed_args, dict):
                            parsed_args = {}
                    except json.JSONDecodeError:
                        parsed_args = {}
                    handler = handlers.get(tool_name)
                    output = handler(parsed_args) if handler else f"Unknown teammate tool: {tool_name}"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": output,
                        }
                    )

            idle_result = self._teammate_idle_poll(name, role, messages, state)
            if idle_result == "shutdown":
                return "Shutting down."
            if idle_result == "work":
                idle_timeouts = 0
                continue
            idle_timeouts += 1

        return last_text or "Teammate idle timeout."

    def _teammate_tools(self) -> list[Tool]:
        allowed = {"bash", "read_file", "write_file", "list_tasks", "claim_task", "complete_task", "submit_plan"}
        if self.config.read_only:
            allowed = {"read_file", "list_tasks"}
        tools = [tool for tool in self.tools if tool.name in allowed]
        tools.append(
            Tool(
                _tool_spec(
                    "send_message",
                    "Send a message to another agent.",
                    {"to": {"type": "string"}, "content": {"type": "string"}},
                    ["to", "content"],
                ),
                lambda args: {"content": self.runtime.send_message(str(args["to"]), str(args["content"])), "is_error": False},
            )
        )
        return tools

    def _teammate_handlers(self, name: str, state: dict[str, Any]) -> dict[str, Callable[[dict[str, Any]], str]]:
        def execute(tool_name: str, args: dict[str, Any]) -> str:
            if tool_name in {"bash", "write_file", "edit_file"} and state["pending_plan_ids"] and not state["approved_plan_ids"]:
                return "Plan approval required before mutating files or running bash. Call submit_plan, then wait for approval."
            if tool_name in {"bash", "read_file", "write_file", "edit_file", "grep", "find_files", "list_dir"}:
                args = {**args, "_cwd": state["cwd"]}
            return self.registry.execute(tool_name, json.dumps(args, ensure_ascii=False))["content"]

        def claim_task(args: dict[str, Any]) -> str:
            task_id = str(args["task_id"])
            output = execute("claim_task", {**args, "owner": name})
            if output.startswith("Claimed "):
                path = self.runtime.worktree_path_for_task(task_id)
                if path is not None:
                    state["cwd"] = str(path)
                    output += f"\nEntered worktree {path}"
            return output

        def submit_plan(args: dict[str, Any]) -> str:
            request_id = str(args["request_id"])
            output = self.runtime.submit_plan(request_id, name, str(args["plan"]))
            state["pending_plan_ids"].add(request_id)
            return output

        return {
            "bash": lambda args: execute("bash", args),
            "read_file": lambda args: execute("read_file", args),
            "write_file": lambda args: execute("write_file", args),
            "list_tasks": lambda args: execute("list_tasks", args),
            "claim_task": claim_task,
            "complete_task": lambda args: execute("complete_task", args),
            "submit_plan": submit_plan,
            "send_message": lambda args: self.runtime.send_message(str(args["to"]), str(args["content"]), from_agent=name),
        }

    def _inject_teammate_inbox(self, name: str, messages: list[Message], state: dict[str, Any]) -> str:
        inbox = self.runtime.consume_inbox(name)
        if not inbox:
            return "empty"
        for message in inbox:
            msg_type = message.get("type")
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            request_id = str(metadata.get("request_id", ""))
            if msg_type == "shutdown_request":
                self.runtime.send_message(
                    "lead",
                    "Shutdown approved.",
                    from_agent=name,
                    msg_type="shutdown_response",
                    metadata={"request_id": request_id, "approve": True},
                )
                return "shutdown"
            if msg_type == "plan_request" and request_id:
                state["pending_plan_ids"].add(request_id)
            if msg_type == "plan_approval_response" and request_id:
                if metadata.get("approve", True):
                    state["approved_plan_ids"].add(request_id)
                else:
                    state["approved_plan_ids"].discard(request_id)
        messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, ensure_ascii=False, indent=2)}</inbox>"})
        return "work"

    def _teammate_idle_poll(self, name: str, role: str, messages: list[Message], state: dict[str, Any]) -> str:
        for _ in range(10):
            inbox_result = self._inject_teammate_inbox(name, messages, state)
            if inbox_result in {"work", "shutdown"}:
                return inbox_result
            task = self.runtime.first_claimable_task()
            if task is not None:
                claim_result = self.runtime.claim_task(task.id, owner=name)
                if claim_result.startswith("Claimed "):
                    path = self.runtime.worktree_path_for_task(task.id)
                    if path is not None:
                        state["cwd"] = str(path)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"You autonomously claimed task {task.id}: {task.subject}\n"
                                f"Description: {task.description}\n"
                                f"Role: {role}\n"
                                f"Current cwd: {state['cwd']}"
                            ),
                        }
                    )
                    return "work"
            time.sleep(0.1)
        return "timeout"

    def _subagent_tools(self) -> list[Tool]:
        allowed = {"bash", "read_file", "write_file", "edit_file", "find_files", "grep", "list_dir"}
        tools = [tool for tool in self.tools if tool.name in allowed]
        if self.config.read_only:
            tools = [tool for tool in tools if tool.name not in {"bash", "write_file", "edit_file"}]
        return tools

    def _complete_with_recovery(self, messages: list[Message], tool_specs: list[ToolSpec]) -> Message:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self.client.complete(messages, tool_specs)
            except LlmError as exc:
                last_error = exc
                text = str(exc).lower()
                if "context" in text or "token" in text:
                    self.output("[recovery] provider reported context pressure; running reactive compact")
                    has_system = bool(messages and messages[0].get("role") == "system")
                    system_messages = messages[:1] if has_system else []
                    body = messages[1:] if has_system else messages
                    result = apply_context_pipeline(
                        body,
                        self.config.workspace,
                        summarizer=self._summarize_messages,
                        tools=tool_specs,
                        fixed_messages=system_messages,
                        token_counter=self._count_input_tokens,
                        context_window_tokens=self.config.context_window_tokens,
                        max_output_tokens=self.config.max_output_tokens,
                        context_trigger_ratio=self.config.context_trigger_ratio,
                        context_safety_tokens=self.config.context_safety_tokens,
                        max_messages=self.config.compact_max_messages,
                        keep_recent_tool_results=self.config.keep_recent_tool_results,
                        tool_result_token_limit=self.config.tool_result_token_limit,
                        force_summary=True,
                        reactive=True,
                    )
                    messages[:] = [*system_messages, *result.messages]
                elif any(code in text for code in ["429", "500", "502", "503", "529", "timeout"]):
                    delay = 0.5 * (attempt + 1)
                    self.output(f"[recovery] transient provider error; retrying in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise
        if last_error is not None:
            raise last_error
        raise LlmError("provider call failed")

    def compact_session(self, keep: int = 8, use_llm: bool = True) -> str:
        if not self.session.messages:
            return "session is already small"
        summarizer = self._summarize_messages if use_llm else mechanical_summary
        result = apply_context_pipeline(
            self.session.messages,
            self.config.workspace,
            summarizer=summarizer,
            token_counter=self._count_input_tokens,
            context_window_tokens=self.config.context_window_tokens,
            max_output_tokens=self.config.max_output_tokens,
            context_trigger_ratio=self.config.context_trigger_ratio,
            context_safety_tokens=self.config.context_safety_tokens,
            max_messages=self.config.compact_max_messages,
            keep_recent_tool_results=self.config.keep_recent_tool_results,
            tool_result_token_limit=self.config.tool_result_token_limit,
            force_summary=True,
        )
        self.session.messages = result.messages
        self.session.save()
        events = "; ".join(result.events) if result.events else "compacted"
        return f"{events}; session now has {len(self.session.messages)} messages"

    def _apply_context_pipeline(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        force_summary: bool = False,
        persist: bool = True,
    ) -> list[Message]:
        if not messages:
            return messages
        has_system = messages[0].get("role") == "system"
        system_messages = messages[:1] if has_system else []
        body = messages[1:] if has_system else messages
        result = apply_context_pipeline(
            body,
            self.config.workspace,
            summarizer=self._summarize_messages,
            tools=tool_specs or [],
            fixed_messages=system_messages,
            token_counter=self._count_input_tokens,
            context_window_tokens=self.config.context_window_tokens,
            max_output_tokens=self.config.max_output_tokens,
            context_trigger_ratio=self.config.context_trigger_ratio,
            context_safety_tokens=self.config.context_safety_tokens,
            max_messages=self.config.compact_max_messages,
            keep_recent_tool_results=self.config.keep_recent_tool_results,
            tool_result_token_limit=self.config.tool_result_token_limit,
            force_summary=force_summary,
        )
        if result.events:
            self.output("[context] " + "; ".join(result.events))
            if persist and (body == self.session.messages or has_system):
                self.session.messages = result.messages
                self.session.save()
        return [*system_messages, *result.messages]

    def _count_input_tokens(self, messages: list[Message], tool_specs: list[ToolSpec]) -> int:
        counter = getattr(self.client, "count_input_tokens", None)
        if not callable(counter):
            return estimate_tokens(messages, tool_specs)
        return int(counter(messages, tool_specs))

    def _summarize_messages(self, messages: list[Message]) -> str:
        transcript = _format_messages_for_summary(messages)
        summary_prompt = (
            "Summarize the following coding-agent conversation for future continuation.\n"
            "Preserve user goals, important decisions, files inspected or changed, commands run, errors, "
            "and remaining tasks. Be concise but specific.\n\n"
            f"{transcript}"
        )
        response = self.client.complete(
            [
                {
                    "role": "system",
                    "content": "You summarize coding-agent session history for context compaction.",
                },
                {"role": "user", "content": summary_prompt},
            ],
            [],
        )
        content = response.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            return mechanical_summary(messages)
        return "Previous conversation was compacted by the LLM. Summary:\n\n" + content.strip()

    def _select_relevant_memories(self, query: str, limit: int = 5) -> str:
        catalog = self.runtime.memory_catalog()
        if not catalog:
            return ""
        fallback = self.runtime.relevant_memories(query=query, limit=limit)
        try:
            response = self.client.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Select durable memories relevant to the user's current coding-agent request. "
                            "Return only a JSON array of memory filenames. Select at most 5. "
                            "If none are useful, return []."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Current request:\n{query}\n\n"
                            f"Memory catalog:\n{catalog}"
                        ),
                    },
                ],
                [],
            )
            filenames = _parse_json_array(response.get("content") or "")
            selected = [str(item) for item in filenames if isinstance(item, str)]
            records = self.runtime.select_memories(limit=limit, filenames=selected)
            if records:
                return self.runtime.render_memories(records, max_chars=12000)
        except Exception:
            return fallback if fallback != "No matching memories." else ""
        return fallback if fallback != "No matching memories." else ""

    def _extract_memories(self) -> None:
        records = self.runtime.list_memory_files()
        existing = "\n".join(f"- {record.name}: {record.description} ({record.type})" for record in records[:100])
        dialogue = _format_messages_for_summary(self.session.messages[-10:])
        if not dialogue.strip():
            return
        prompt = (
            "Extract durable memories from the recent coding-agent dialogue.\n"
            "Save only facts useful across future sessions or future context compactions: user preferences, "
            "repeated feedback, project facts, and reference locations.\n"
            "Return only JSON array items with fields: name, type, description, body, fact_key, source, confidence, status.\n"
            "Allowed type values: user, feedback, project, reference.\n"
            "Allowed source values: user_explicit, llm_extracted, assistant_observed, imported.\n"
            "Allowed status values: active, pending, conflicted. Use user_explicit + confidence 1.0 only when "
            "the user clearly stated the memory. Use pending/conflicted for uncertain inferences. "
            "Use the same fact_key for memories that describe the same durable fact slot, such as preferred_model.\n"
            "If nothing new or already covered, return [].\n\n"
            f"Existing memories:\n{existing or '(none)'}\n\n"
            f"Recent dialogue:\n{dialogue[:6000]}"
        )
        try:
            response = self.client.complete(
                [
                    {"role": "system", "content": "You extract durable coding-agent memory records."},
                    {"role": "user", "content": prompt},
                ],
                [],
            )
            items = _parse_json_array(response.get("content") or "")
        except Exception:
            return
        saved = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            mem_type = str(item.get("type", "")).strip()
            description = str(item.get("description", "")).strip()
            body = str(item.get("body", "")).strip()
            fact_key = str(item.get("fact_key", name)).strip()
            source = str(item.get("source", "llm_extracted")).strip()
            confidence = item.get("confidence", 0.6)
            status = str(item.get("status", "pending")).strip()
            if not name or not description or not body:
                continue
            result = self.runtime.write_memory_file(
                name,
                mem_type,
                description,
                body,
                fact_key=fact_key,
                source=source,
                confidence=float(confidence),
                status=status,
            )
            if result.startswith("Memory saved"):
                saved += 1
        if saved:
            self.output(f"[memory] extracted {saved} new memor{'y' if saved == 1 else 'ies'}")

    def _maybe_consolidate_memories(self, threshold: int = 10) -> None:
        records = self.runtime.list_memory_files()
        if len(records) < threshold:
            return
        lock_path = self.runtime.memory_dir / ".consolidate-lock"
        now = time.time()
        if lock_path.exists() and now - lock_path.stat().st_mtime < 3600:
            return
        last_path = self.runtime.memory_dir / ".last-consolidated"
        if last_path.exists() and now - last_path.stat().st_mtime < 24 * 60 * 60:
            return
        lock_path.write_text(str(now), encoding="utf-8")
        try:
            corpus = self.runtime.render_memories(records, max_chars=50000, per_memory_chars=4096)
            response = self.client.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Consolidate durable coding-agent memories. Deduplicate, merge overlapping facts, "
                            "resolve obvious obsolete duplicates conservatively, and keep useful details."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Return only a JSON array of memory records with fields: name, type, description, body. "
                            "Include fact_key, source, confidence, and status when known. "
                            "Allowed type values: user, feedback, project, reference.\n\n"
                            f"Existing memories:\n{corpus}"
                        ),
                    },
                ],
                [],
            )
            items = _parse_json_array(response.get("content") or "")
            valid_items = [item for item in items if isinstance(item, dict)]
            if valid_items:
                result = self.runtime.replace_memories(valid_items)
                last_path.write_text(str(now), encoding="utf-8")
                self.output(f"[memory] {result}")
        except Exception:
            return
        finally:
            if lock_path.exists():
                lock_path.unlink()

    def _system_prompt(self, selected_memories: str = "") -> str:
        tool_names = ", ".join(tool.name for tool in self.tools) if self.tools else "(none)"
        parts = [
            SYSTEM_PROMPT,
            "Runtime harness mechanisms: tool dispatch, permission hooks, todo planning, subagent isolation, "
            "skill loading, context compaction, durable memory, runtime system prompt assembly, error recovery, "
            "task graph, background jobs, cron prompts, mailbox teams, team protocols, autonomous claiming, "
            "worktree directories, and MCP manifest routing.",
            "Context priority: the user's current explicit instruction overrides current-session context, "
            "active durable memory, and compacted history. Durable memory can be stale; when the user clearly "
            "updates an old preference or fact, follow the current message this turn and store the update so "
            "future turns stop loading the obsolete memory.",
            f"Workspace: {self.config.workspace}",
            f"Available tool names: {tool_names}",
        ]
        todos = self.runtime.todo_read()
        if todos != "No todos.":
            parts.append(f"Current todos:\n{todos}")
        memory_index = self.runtime.memory_index()
        if memory_index != "No memories.":
            parts.append(
                "Durable memory index is always available below. Use `search_memory` for extra lookup "
                "or `remember` to store stable preferences, feedback, project facts, and references.\n\n"
                f"{memory_index}"
            )
        if selected_memories:
            parts.append(f"Relevant memories loaded for this request:\n{selected_memories}")
        tasks = self.runtime.list_tasks()
        if tasks != "No tasks.":
            parts.append(f"Durable tasks:\n{tasks}")
        crons = self.runtime.list_crons()
        if crons != "No crons.":
            parts.append(f"Scheduled cron prompts:\n{crons}")
        if self.available_skills:
            descriptions = []
            for skill in self.available_skills.values():
                description = skill.description or "No description provided."
                descriptions.append(f"- {skill.name}: {description}")
            parts.append(
                "Available skills are listed below. If a task matches a skill, call the `load_skill` tool "
                "with the skill name before following the skill instructions.\n\n" + "\n".join(descriptions)
            )
        if self.active_skills:
            skill_text = "\n\n".join(f"## Skill: {skill.name}\n\n{skill.body or skill.text}" for skill in self.active_skills.values())
            parts.append(f"Manually loaded skills:\n\n{skill_text}")
        parts.extend(self.extra_system_prompts)
        return "\n\n".join(parts)


def _format_messages_for_summary(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        content = str(message.get("content") or "")
        if len(content) > 4000:
            content = content[:4000] + "\n[truncated]"
        lines.append(f"## {message['role']}\n{content}")
        for tool_call in message.get("tool_calls", []):
            name = tool_call["function"]["name"]
            args = tool_call["function"].get("arguments", "{}")
            lines.append(f"```tool-call\n{name} {args}\n```")
    return "\n\n".join(lines)


def _parse_json_array(text: object) -> list[Any]:
    if not isinstance(text, str):
        return []
    stripped = text.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", stripped)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []
