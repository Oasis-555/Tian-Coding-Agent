from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .messages import ToolResult, ToolSpec
from .resources import NamedText
from .runtime import HarnessRuntime


Confirm = Callable[[str], bool]
ConfirmCommand = Callable[[str], bool]
ToolHandler = Callable[[dict[str, Any]], ToolResult]
PreToolHook = Callable[[str, dict[str, Any]], ToolResult | None]
PostToolHook = Callable[[str, dict[str, Any], ToolResult], None]


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    execute: ToolHandler

    @property
    def name(self) -> str:
        return self.spec["function"]["name"]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._pre_hooks: list[PreToolHook] = []
        self._post_hooks: list[PostToolHook] = []

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def add_pre_hook(self, hook: PreToolHook) -> None:
        self._pre_hooks.append(hook)

    def add_post_hook(self, hook: PostToolHook) -> None:
        self._post_hooks.append(hook)

    def enabled(self, names: list[str] | None = None) -> list[Tool]:
        if names is None:
            return list(self._tools.values())
        return [self._tools[name] for name in names if name in self._tools]

    def execute(self, name: str, raw_arguments: str) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return _err(f"unknown tool: {name}")
        try:
            args = json.loads(raw_arguments or "{}")
            if not isinstance(args, dict):
                return _err("tool arguments must be a JSON object")
            for hook in self._pre_hooks:
                result = hook(name, args)
                if result is not None:
                    return result
            result = tool.execute(args)
            for hook in self._post_hooks:
                hook(name, args, result)
            return result
        except Exception as exc:
            return _err(str(exc))


def build_registry(
    workspace: Path,
    confirm: Confirm | None = None,
    confirm_edits: bool = True,
    confirm_command: ConfirmCommand | None = None,
    confirm_bash: bool = True,
    read_only: bool = False,
    skills: dict[str, NamedText] | None = None,
    runtime: HarnessRuntime | None = None,
) -> ToolRegistry:
    workspace = workspace.resolve()
    registry = ToolRegistry()
    runtime = runtime or HarnessRuntime(workspace)

    def approve(path: Path, old: str, new: str) -> bool:
        if not confirm_edits:
            return True
        diff = "\n".join(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile=str(path.relative_to(workspace)),
                tofile=str(path.relative_to(workspace)),
                lineterm="",
            )
        )
        if not diff:
            return True
        if confirm is None:
            return False
        return confirm(diff)

    def read_file(args: dict[str, Any]) -> ToolResult:
        base = _base_workspace(workspace, args)
        path = _resolve(base, str(args["path"]))
        if not path.is_file():
            return _err(f"file not found: {path}")
        limit = int(args.get("limit", 20000))
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > limit:
            text = text[:limit] + f"\n\n[truncated to {limit} characters]"
        return _ok(text)

    def write_file(args: dict[str, Any]) -> ToolResult:
        if read_only:
            return _err("write_file is disabled in read-only mode")
        base = _base_workspace(workspace, args)
        path = _resolve(base, str(args["path"]))
        old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        new = str(args["content"])
        if not approve(path, old, new):
            return _err("write rejected by user")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        return _ok(f"wrote {_display_path(workspace, path)}")

    def edit_file(args: dict[str, Any]) -> ToolResult:
        if read_only:
            return _err("edit_file is disabled in read-only mode")
        base = _base_workspace(workspace, args)
        path = _resolve(base, str(args["path"]))
        if not path.is_file():
            return _err(f"file not found: {path}")
        old_text = path.read_text(encoding="utf-8", errors="replace")
        old = str(args["old"])
        new = str(args["new"])
        count = int(args.get("count", 1))
        if old not in old_text:
            return _err("old text was not found")
        updated = old_text.replace(old, new, count)
        if not approve(path, old_text, updated):
            return _err("edit rejected by user")
        path.write_text(updated, encoding="utf-8")
        return _ok(f"edited {_display_path(workspace, path)}")

    def list_dir(args: dict[str, Any]) -> ToolResult:
        base = _base_workspace(workspace, args)
        path = _resolve(base, str(args.get("path", ".")))
        if not path.is_dir():
            return _err(f"directory not found: {path}")
        entries = []
        for entry in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{entry.name}{suffix}")
        return _ok("\n".join(entries))

    def grep(args: dict[str, Any]) -> ToolResult:
        base = _base_workspace(workspace, args)
        root = _resolve(base, str(args.get("path", ".")))
        pattern = re.compile(str(args["pattern"]))
        max_results = int(args.get("max_results", 100))
        results: list[str] = []
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in files:
            if len(results) >= max_results:
                break
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    rel = _display_path(workspace, path)
                    results.append(f"{rel}:{number}: {line}")
                    if len(results) >= max_results:
                        break
        return _ok("\n".join(results) if results else "no matches")

    def find_files(args: dict[str, Any]) -> ToolResult:
        base = _base_workspace(workspace, args)
        root = _resolve(base, str(args.get("path", ".")))
        glob = str(args.get("glob", "*"))
        max_results = int(args.get("max_results", 100))
        matches = []
        for path in root.rglob(glob):
            matches.append(_display_path(workspace, path))
            if len(matches) >= max_results:
                break
        return _ok("\n".join(matches) if matches else "no matches")

    def bash(args: dict[str, Any]) -> ToolResult:
        if read_only:
            return _err("bash is disabled in read-only mode")
        command = str(args["command"])
        if confirm_bash:
            if confirm_command is None:
                return _err("bash command rejected because no command confirmation callback is available")
            if not confirm_command(command):
                return _err("bash command rejected by user")
        timeout = int(args.get("timeout", 30))
        base = _base_workspace(workspace, args)
        completed = subprocess.run(
            command,
            cwd=base,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=os.environ.copy(),
            check=False,
        )
        output = completed.stdout
        if completed.stderr:
            output += ("\n" if output else "") + completed.stderr
        output += f"\n[exit code: {completed.returncode}]"
        return {"content": output, "is_error": completed.returncode != 0}

    def load_skill(args: dict[str, Any]) -> ToolResult:
        name = str(args["name"])
        skill = (skills or {}).get(name)
        if skill is None:
            available = ", ".join(sorted((skills or {}).keys()))
            return _err(f"skill not found: {name}. Available skills: {available}")
        body = skill.body or skill.text
        return _ok(f"# Skill: {skill.name}\n\nPath: {skill.path}\n\n{body}")

    def todo_write(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.todo_write(list(args["todos"])))

    def todo_read(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.todo_read())

    def remember(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.remember(str(args["text"]), str(args.get("category", "general")), str(args.get("fact_key", ""))))

    def search_memory(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.search_memory(str(args.get("query", "")), int(args.get("limit", 10))))

    def create_task(args: dict[str, Any]) -> ToolResult:
        blocked_by = args.get("blocked_by", [])
        if not isinstance(blocked_by, list):
            return _err("blocked_by must be a list")
        return _ok(runtime.create_task(str(args["subject"]), str(args.get("description", "")), [str(item) for item in blocked_by]))

    def list_tasks_tool(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.list_tasks())

    def get_task(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.get_task(str(args["task_id"])))

    def claim_task(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.claim_task(str(args["task_id"]), str(args.get("owner", "agent"))))

    def complete_task(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.complete_task(str(args["task_id"])))

    def start_background_command(args: dict[str, Any]) -> ToolResult:
        if read_only:
            return _err("background command is disabled in read-only mode")
        command = str(args["command"])
        if confirm_bash:
            if confirm_command is None or not confirm_command(command):
                return _err("background command rejected by user")
        return _ok(runtime.start_background_command(command, int(args.get("timeout", 120))))

    def list_background_jobs(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.list_background_jobs())

    def read_background_job(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.read_background_job(str(args["job_id"])))

    def schedule_cron(args: dict[str, Any]) -> ToolResult:
        return _ok(
            runtime.schedule_cron(
                str(args["cron"]),
                str(args["prompt"]),
                bool(args.get("recurring", True)),
                bool(args.get("durable", True)),
            )
        )

    def list_crons(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.list_crons())

    def cancel_cron(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.cancel_cron(str(args["cron_id"])))

    def spawn_teammate(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.spawn_teammate(str(args["name"]), str(args.get("role", "agent"))))

    def send_message(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.send_message(str(args["to"]), str(args["content"]), str(args.get("from_agent", "lead")), str(args.get("msg_type", "message"))))

    def check_inbox(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.check_inbox(str(args.get("agent", "lead")), bool(args.get("consume", True))))

    def request_shutdown(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.request_shutdown(str(args["teammate"])))

    def request_plan(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.request_plan(str(args["teammate"]), str(args["task"])))

    def review_plan(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.review_plan(str(args["request_id"]), bool(args["approve"]), str(args.get("feedback", ""))))

    def submit_plan(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.submit_plan(str(args["request_id"]), str(args.get("teammate", "agent")), str(args["plan"])))

    def autonomous_claim(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.autonomous_claim(str(args.get("owner", "agent"))))

    def create_worktree(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.create_worktree(str(args["name"]), str(args["task_id"]) if args.get("task_id") else None))

    def remove_worktree(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.remove_worktree(str(args["name"]), bool(args.get("discard_changes", False))))

    def keep_worktree(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.keep_worktree(str(args["name"])))

    def connect_mcp(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.connect_mcp(str(args["name"]), str(args["manifest_path"])))

    def connect_mcp_stdio(args: dict[str, Any]) -> ToolResult:
        command_args = args.get("args", [])
        if not isinstance(command_args, list):
            return _err("args must be a list")
        env = args.get("env", {})
        if not isinstance(env, dict):
            return _err("env must be an object")
        return _ok(
            runtime.connect_mcp_stdio(
                str(args["name"]),
                str(args["command"]),
                [str(item) for item in command_args],
                {str(key): str(value) for key, value in env.items()},
            )
        )

    def list_mcp_tools(args: dict[str, Any]) -> ToolResult:
        return _ok(runtime.list_mcp_tools())

    def call_mcp_tool(args: dict[str, Any]) -> ToolResult:
        arguments = args.get("arguments", {})
        if not isinstance(arguments, dict):
            return _err("arguments must be an object")
        return _ok(runtime.call_mcp_tool(str(args["tool_name"]), arguments))

    def permission_pre_hook(name: str, args: dict[str, Any]) -> ToolResult | None:
        if name == "bash":
            command = str(args.get("command", ""))
            deny_list = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
            for pattern in deny_list:
                if pattern in command:
                    return _err(f"permission denied: '{pattern}' is blocked")
        return None

    def logging_post_hook(name: str, args: dict[str, Any], result: ToolResult) -> None:
        runtime.output(f"[hook] {name} {'error' if result.get('is_error') else 'ok'}")

    registry.add_pre_hook(permission_pre_hook)
    registry.add_post_hook(logging_post_hook)

    for tool in [
        Tool(
            _tool_spec(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                {"path": {"type": "string"}, "limit": {"type": "integer", "default": 20000}},
                ["path"],
            ),
            read_file,
        ),
        Tool(
            _tool_spec(
                "write_file",
                "Create or overwrite a UTF-8 text file in the workspace.",
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            write_file,
        ),
        Tool(
            _tool_spec(
                "edit_file",
                "Replace text in a file. Use exact old text.",
                {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "count": {"type": "integer", "default": 1},
                },
                ["path", "old", "new"],
            ),
            edit_file,
        ),
        Tool(_tool_spec("list_dir", "List files and directories under a workspace path.", {"path": {"type": "string", "default": "."}}, []), list_dir),
        Tool(
            _tool_spec(
                "grep",
                "Search file contents with a regular expression.",
                {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 100},
                },
                ["pattern"],
            ),
            grep,
        ),
        Tool(
            _tool_spec(
                "find_files",
                "Find files by glob under a workspace path.",
                {
                    "glob": {"type": "string", "default": "*"},
                    "path": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 100},
                },
                [],
            ),
            find_files,
        ),
        Tool(
            _tool_spec(
                "bash",
                "Run a shell command in the workspace and return stdout/stderr.",
                {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 30}},
                ["command"],
            ),
            bash,
        ),
        Tool(
            _tool_spec(
                "load_skill",
                "Load the full instructions for an available skill by name. Use when a task matches a listed skill description.",
                {"name": {"type": "string"}},
                ["name"],
            ),
            load_skill,
        ),
        Tool(_tool_spec("todo_write", "Create or update the visible todo plan.", {"todos": {"type": "array"}}, ["todos"]), todo_write),
        Tool(_tool_spec("todo_read", "Read the current todo plan.", {}, []), todo_read),
        Tool(
            _tool_spec(
                "remember",
                "Store a durable Markdown memory for future turns. Category may be user, feedback, project, or reference. Use fact_key for replaceable facts such as preferred_model.",
                {"text": {"type": "string"}, "category": {"type": "string", "default": "reference"}, "fact_key": {"type": "string", "default": ""}},
                ["text"],
            ),
            remember,
        ),
        Tool(_tool_spec("search_memory", "Search durable Markdown memory records.", {"query": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 10}}, []), search_memory),
        Tool(_tool_spec("create_task", "Create a durable task record with optional dependencies.", {"subject": {"type": "string"}, "description": {"type": "string"}, "blocked_by": {"type": "array"}}, ["subject"]), create_task),
        Tool(_tool_spec("list_tasks", "List durable task records.", {}, []), list_tasks_tool),
        Tool(_tool_spec("get_task", "Read one task record.", {"task_id": {"type": "string"}}, ["task_id"]), get_task),
        Tool(_tool_spec("claim_task", "Claim a pending task when dependencies are complete.", {"task_id": {"type": "string"}, "owner": {"type": "string", "default": "agent"}}, ["task_id"]), claim_task),
        Tool(_tool_spec("complete_task", "Mark an in-progress task completed.", {"task_id": {"type": "string"}}, ["task_id"]), complete_task),
        Tool(_tool_spec("start_background_command", "Run a shell command in a background thread.", {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 120}}, ["command"]), start_background_command),
        Tool(_tool_spec("list_background_jobs", "List background command jobs.", {}, []), list_background_jobs),
        Tool(_tool_spec("read_background_job", "Read one background job output.", {"job_id": {"type": "string"}}, ["job_id"]), read_background_job),
        Tool(
            _tool_spec(
                "schedule_cron",
                "Schedule a prompt with a 5-field cron expression. The CLI scheduler delivers due prompts automatically while the CLI is active.",
                {
                    "cron": {"type": "string"},
                    "prompt": {"type": "string"},
                    "recurring": {"type": "boolean", "default": True},
                    "durable": {"type": "boolean", "default": True},
                },
                ["cron", "prompt"],
            ),
            schedule_cron,
        ),
        Tool(_tool_spec("list_crons", "List scheduled cron prompts.", {}, []), list_crons),
        Tool(_tool_spec("cancel_cron", "Disable a scheduled cron prompt.", {"cron_id": {"type": "string"}}, ["cron_id"]), cancel_cron),
        Tool(_tool_spec("spawn_teammate", "Register a mailbox-backed teammate.", {"name": {"type": "string"}, "role": {"type": "string", "default": "agent"}}, ["name"]), spawn_teammate),
        Tool(_tool_spec("send_message", "Send an async mailbox message to a teammate.", {"to": {"type": "string"}, "content": {"type": "string"}, "from_agent": {"type": "string", "default": "lead"}, "msg_type": {"type": "string", "default": "message"}}, ["to", "content"]), send_message),
        Tool(_tool_spec("check_inbox", "Read a mailbox inbox.", {"agent": {"type": "string", "default": "lead"}, "consume": {"type": "boolean", "default": True}}, []), check_inbox),
        Tool(_tool_spec("request_shutdown", "Send a shutdown request to a teammate.", {"teammate": {"type": "string"}}, ["teammate"]), request_shutdown),
        Tool(_tool_spec("request_plan", "Ask a teammate for a plan.", {"teammate": {"type": "string"}, "task": {"type": "string"}}, ["teammate", "task"]), request_plan),
        Tool(_tool_spec("review_plan", "Approve or reject a pending teammate plan.", {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, ["request_id", "approve"]), review_plan),
        Tool(_tool_spec("submit_plan", "Submit a teammate plan to lead for approval.", {"request_id": {"type": "string"}, "teammate": {"type": "string"}, "plan": {"type": "string"}}, ["request_id", "plan"]), submit_plan),
        Tool(_tool_spec("autonomous_claim", "Claim the first unblocked task for an autonomous worker.", {"owner": {"type": "string", "default": "agent"}}, []), autonomous_claim),
        Tool(_tool_spec("create_worktree", "Create an isolated task directory and optionally bind it to a task.", {"name": {"type": "string"}, "task_id": {"type": "string"}}, ["name"]), create_worktree),
        Tool(_tool_spec("remove_worktree", "Remove an isolated task directory.", {"name": {"type": "string"}, "discard_changes": {"type": "boolean", "default": False}}, ["name"]), remove_worktree),
        Tool(_tool_spec("keep_worktree", "Keep an isolated task directory for review.", {"name": {"type": "string"}}, ["name"]), keep_worktree),
        Tool(_tool_spec("connect_mcp", "Connect a teaching MCP manifest from the workspace.", {"name": {"type": "string"}, "manifest_path": {"type": "string"}}, ["name", "manifest_path"]), connect_mcp),
        Tool(
            _tool_spec(
                "connect_mcp_stdio",
                "Start and connect a real MCP stdio server, then discover its tools with tools/list.",
                {
                    "name": {"type": "string"},
                    "command": {"type": "string"},
                    "args": {"type": "array"},
                    "env": {"type": "object"},
                },
                ["name", "command"],
            ),
            connect_mcp_stdio,
        ),
        Tool(_tool_spec("list_mcp_tools", "List tools exposed by connected MCP manifests.", {}, []), list_mcp_tools),
        Tool(_tool_spec("call_mcp_tool", "Call a tool exposed by a connected MCP manifest.", {"tool_name": {"type": "string"}, "arguments": {"type": "object"}}, ["tool_name"]), call_mcp_tool),
    ]:
        registry.register(tool)
    return registry


def _tool_spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> ToolSpec:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _ok(content: str) -> ToolResult:
    return {"content": content, "is_error": False}


def _err(content: str) -> ToolResult:
    return {"content": content, "is_error": True}


def _resolve(workspace: Path, raw_path: str) -> Path:
    path = (workspace / raw_path).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {raw_path}") from exc
    return path


def _base_workspace(workspace: Path, args: dict[str, Any]) -> Path:
    override = args.get("_cwd")
    if not override:
        return workspace
    path = Path(str(override)).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"cwd escapes workspace: {override}") from exc
    return path


def _display_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)
