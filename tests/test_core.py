from __future__ import annotations

import tempfile
import time
import unittest
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

from py_coding_agent.agent import Agent
from py_coding_agent.config import Config
from py_coding_agent.context import apply_context_pipeline
from py_coding_agent.eval import EvalTask, build_report, run_eval
from py_coding_agent.extensions import load_extensions
from py_coding_agent.resources import load_skills
from py_coding_agent.runtime import HarnessRuntime, cron_matches, validate_cron
from py_coding_agent.session import Session
from py_coding_agent.tools import Tool, build_registry
from py_coding_agent.providers.openai import _parse_openai_usage


class FakeSubagentClient:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: list[str] = []

    def complete(self, messages, tools):
        self.calls += 1
        self.tool_names.extend(tool["function"]["name"] for tool in tools)
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
                    }
                ],
            }
        return {"role": "assistant", "content": "Subagent read the note.", "tool_calls": []}


class FakeTeammateClient:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: list[str] = []

    def complete(self, messages, tools):
        self.calls += 1
        self.tool_names.extend(tool["function"]["name"] for tool in tools)
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_send",
                        "type": "function",
                        "function": {
                            "name": "send_message",
                            "arguments": '{"to":"lead","content":"review complete"}',
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "done", "tool_calls": []}


class FakeTodoReminderClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        if self.calls <= 3:
            path = f"note{self.calls}.txt"
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_read_{self.calls}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"' + path + '"}'},
                    }
                ],
            }
        return {"role": "assistant", "content": "done", "tool_calls": []}


class FakeMemoryClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, messages, tools):
        system = messages[0]["content"]
        self.calls.append(system)
        if "Select durable memories" in system:
            return {"role": "assistant", "content": '["existing.md"]', "tool_calls": []}
        if "extract durable coding-agent memory" in system.lower():
            return {
                "role": "assistant",
                "content": json.dumps(
                    [
                        {
                            "name": "user-prefers-tabs",
                            "type": "user",
                            "description": "User prefers tabs for indentation",
                            "body": "Use tabs for indentation when writing code.",
                        }
                    ]
                ),
                "tool_calls": [],
            }
        return {"role": "assistant", "content": "answer", "tool_calls": []}


class FakeDreamClient:
    def complete(self, messages, tools):
        return {
            "role": "assistant",
            "content": json.dumps(
                [
                    {
                        "name": "merged-preference",
                        "type": "user",
                        "description": "Merged user preference",
                        "body": "Keep the consolidated useful preference.",
                    }
                ]
            ),
            "tool_calls": [],
        }


class CoreTests(unittest.TestCase):
    def test_load_skill_frontmatter_description(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "skills" / "reviewer"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: reviewer\ndescription: Review Python changes\n---\nBody text\n",
                encoding="utf-8",
            )

            skills = load_skills(root)

            self.assertEqual(skills["reviewer"].description, "Review Python changes")
            self.assertEqual(skills["reviewer"].body, "Body text")

    def test_read_only_blocks_mutating_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = build_registry(root, read_only=True)

            result = registry.execute("write_file", '{"path":"x.txt","content":"hello"}')

            self.assertTrue(result["is_error"])
            self.assertFalse((root / "x.txt").exists())

    def test_session_compact_and_fork(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.create(root)
            for index in range(5):
                session.append({"role": "user", "content": f"message {index}"})

            compact_result = session.compact(keep=2, summary="summary")
            forked = session.fork(root, upto=1)

            self.assertEqual(compact_result, "compacted session to 3 messages")
            self.assertEqual(len(forked.messages), 2)
            self.assertIn("000 user", "\n".join(session.tree_lines()))

    def test_extension_can_register_tool_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extension_dir = root / "extensions"
            extension_dir.mkdir()
            (extension_dir / "hello.py").write_text(
                "\n".join(
                    [
                        "from py_coding_agent.tools import Tool",
                        "",
                        "def register(api):",
                        "    api.add_system_prompt('Extension prompt')",
                        "    api.register_tool(Tool({",
                        "        'type': 'function',",
                        "        'function': {",
                        "            'name': 'hello_extension',",
                        "            'description': 'Return hello.',",
                        "            'parameters': {'type': 'object', 'properties': {}, 'required': []},",
                        "        },",
                        "    }, lambda args: {'content': 'hello', 'is_error': False}))",
                    ]
                ),
                encoding="utf-8",
            )
            config = Config.from_env(workspace=root, api_key="test", auto_compact=False)
            agent = Agent(config, Session.create(root / "sessions"))

            loaded = load_extensions(agent, extension_dir)
            result = agent.registry.execute("hello_extension", "{}")

            self.assertEqual(loaded, ["hello"])
            self.assertEqual(result["content"], "hello")
            self.assertIn("Extension prompt", agent._system_prompt())

    def test_runtime_tools_cover_planning_memory_tasks_and_mcp_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "mcp.json"
            manifest.write_text(
                '{"tools":{"echo":{"description":"Echo args"}}}',
                encoding="utf-8",
            )
            registry = build_registry(root, confirm_edits=False, confirm_bash=False)

            todos = registry.execute(
                "todo_write",
                '{"todos":[{"content":"inspect files","status":"pending"}]}',
            )
            memory = registry.execute("remember", '{"text":"prefer focused tests","category":"testing"}')
            task = registry.execute("create_task", '{"subject":"add feature","description":"demo"}')
            mcp = registry.execute("connect_mcp", '{"name":"demo","manifest_path":"mcp.json"}')

            self.assertFalse(todos["is_error"])
            self.assertFalse(memory["is_error"])
            self.assertIn("task_", task["content"])
            self.assertIn("1 tool", mcp["content"])
            self.assertIn("mcp__demo__echo", registry.execute("list_mcp_tools", "{}")["content"])

    def test_memory_uses_markdown_frontmatter_index_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = HarnessRuntime(root)

            saved = runtime.write_memory_file(
                "User Prefers Tabs",
                "user",
                "User prefers tabs",
                "Use tabs for indentation." + ("x" * 5000),
            )
            index = runtime.memory_index()
            rendered = runtime.render_memories(runtime.select_memories("tabs"), max_chars=200, per_memory_chars=80)

            self.assertIn("Memory saved", saved)
            self.assertTrue((root / ".memory" / "user-prefers-tabs.md").exists())
            self.assertIn("description: User prefers tabs", (root / ".memory" / "user-prefers-tabs.md").read_text(encoding="utf-8"))
            self.assertIn("[user-prefers-tabs](user-prefers-tabs.md)", index)
            self.assertIn("truncated", rendered)

    def test_memory_explicit_update_archives_old_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = HarnessRuntime(root)

            old_result = runtime.write_memory_file(
                "preferred-model-kimi",
                "user",
                "User prefers Kimi",
                "Use Kimi by default.",
                fact_key="preferred_model",
                source="user_explicit",
                confidence=1.0,
                status="active",
            )
            new_result = runtime.write_memory_file(
                "preferred-model-gpt41",
                "user",
                "User prefers GPT-4.1",
                "Use GPT-4.1 by default.",
                fact_key="preferred_model",
                source="user_explicit",
                confidence=1.0,
                status="active",
            )

            records = {record.filename: record for record in runtime.list_memory_files(include_inactive=True)}
            active = runtime.select_memories("preferred model", limit=10)

            self.assertIn("Memory saved", old_result)
            self.assertIn("supersedes=preferred-model-kimi.md", new_result)
            self.assertEqual(records["preferred-model-kimi.md"].status, "archived")
            self.assertEqual(records["preferred-model-kimi.md"].superseded_by, "preferred-model-gpt41.md")
            self.assertEqual(records["preferred-model-gpt41.md"].status, "active")
            self.assertEqual(records["preferred-model-gpt41.md"].supersedes, ["preferred-model-kimi.md"])
            self.assertEqual([record.filename for record in active], ["preferred-model-gpt41.md"])

    def test_memory_llm_conflict_is_not_loaded_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = HarnessRuntime(root)

            runtime.write_memory_file(
                "preferred-model-kimi",
                "user",
                "User prefers Kimi",
                "Use Kimi by default.",
                fact_key="preferred_model",
                source="user_explicit",
                confidence=1.0,
                status="active",
            )
            conflict_result = runtime.write_memory_file(
                "preferred-model-gpt41",
                "user",
                "User may prefer GPT-4.1",
                "Use GPT-4.1 by default.",
                fact_key="preferred_model",
                source="llm_extracted",
                confidence=0.55,
                status="active",
            )

            records = {record.filename: record for record in runtime.list_memory_files(include_inactive=True)}
            active = runtime.select_memories("preferred model", limit=10)

            self.assertIn("[conflicted]", conflict_result)
            self.assertEqual(records["preferred-model-kimi.md"].status, "active")
            self.assertEqual(records["preferred-model-gpt41.md"].status, "conflicted")
            self.assertEqual([record.filename for record in active], ["preferred-model-kimi.md"])

    def test_agent_selects_extracts_and_injects_structured_memories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config.from_env(workspace=root, api_key="test", confirm_edits=False, confirm_bash=False)
            agent = Agent(config, Session.create(root / "sessions"))
            agent.runtime.write_memory_file("existing", "project", "Existing project fact", "Existing fact body.")
            fake = FakeMemoryClient()
            agent.client = fake

            answer = agent.prompt("remember that I prefer tabs")

            self.assertEqual(answer, "answer")
            self.assertTrue((root / ".memory" / "user-prefers-tabs.md").exists())
            first_system = fake.calls[1]
            self.assertIn("MEMORY", first_system)
            self.assertIn("Existing fact body", first_system)

    def test_dream_consolidates_markdown_memories_with_lock_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config.from_env(workspace=root, api_key="test", confirm_edits=False, confirm_bash=False)
            agent = Agent(config, Session.create(root / "sessions"))
            for index in range(10):
                agent.runtime.write_memory_file(f"memory {index}", "reference", f"Memory {index}", f"Body {index}")
            agent.client = FakeDreamClient()

            agent._maybe_consolidate_memories(threshold=10)

            files = sorted(path.name for path in (root / ".memory").glob("*.md"))
            self.assertIn("merged-preference.md", files)
            self.assertIn("MEMORY.md", files)
            self.assertEqual(len([name for name in files if name != "MEMORY.md"]), 1)

    def test_cron_expression_scheduler_queue_ack_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = HarnessRuntime(root)

            self.assertIsNone(validate_cron("*/5 9-17 * * 1-5"))
            self.assertTrue(cron_matches("30 9 * * 1-5", datetime(2026, 8, 27, 9, 30)))
            self.assertFalse(cron_matches("30 9 * * 1-5", datetime(2026, 8, 27, 9, 31)))

            recurring = runtime.schedule_cron("* * * * *", "run recurring check")
            one_shot = runtime.schedule_cron("* * * * *", "run once", recurring=False)
            self.assertIn("Scheduled", recurring)
            self.assertIn("Scheduled", one_shot)

            due = runtime.poll_due_crons(datetime(2026, 8, 27, 9, 30))
            queued = runtime.consume_cron_queue()
            self.assertEqual(len(due), 2)
            self.assertEqual(len(queued), 2)
            self.assertFalse(runtime.has_cron_queue())

            runtime.restore_cron_jobs(queued)
            self.assertTrue(runtime.has_cron_queue())
            queued_again = runtime.consume_cron_queue()
            runtime.acknowledge_cron_jobs(queued_again)

            listed = runtime.list_crons()
            self.assertIn("run recurring check", listed)
            self.assertNotIn("run once", listed)

            next_due_same_minute = runtime.poll_due_crons(datetime(2026, 8, 27, 9, 30, 30))
            next_due_next_minute = runtime.poll_due_crons(datetime(2026, 8, 27, 9, 31))
            self.assertEqual(next_due_same_minute, [])
            self.assertEqual(len(next_due_next_minute), 1)

    def test_schedule_cron_tool_uses_five_field_expression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = build_registry(root, confirm_edits=False, confirm_bash=False)

            invalid = registry.execute("schedule_cron", '{"cron":"bad","prompt":"x"}')
            valid = registry.execute("schedule_cron", '{"cron":"0 9 * * *","prompt":"run tests","recurring":true,"durable":true}')

            self.assertIn("Error", invalid["content"])
            self.assertIn("Scheduled", valid["content"])

    def test_todo_write_enforces_new_s05_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = build_registry(root, confirm_edits=False, confirm_bash=False)

            too_many = [{"content": f"task {index}", "status": "pending"} for index in range(21)]
            too_many_result = registry.execute("todo_write", json.dumps({"todos": too_many}))
            multi_progress = registry.execute(
                "todo_write",
                '{"todos":[{"content":"a","status":"in_progress"},{"content":"b","status":"in_progress"}]}',
            )
            valid = registry.execute(
                "todo_write",
                '{"todos":[{"content":"a","status":"in_progress"},{"content":"b","status":"pending"}]}',
            )

            self.assertIn("at most 20", too_many_result["content"])
            self.assertIn("only one todo", multi_progress["content"])
            self.assertEqual(valid["content"], "Updated 2 todos")

    def test_todo_reminder_is_appended_after_three_non_todo_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(1, 4):
                (root / f"note{index}.txt").write_text(f"note {index}", encoding="utf-8")
            config = Config.from_env(
                workspace=root,
                api_key="test",
                confirm_edits=False,
                confirm_bash=False,
                max_steps=5,
            )
            agent = Agent(config, Session.create(root / "sessions"))
            agent.client = FakeTodoReminderClient()

            answer = agent.prompt("read three notes")
            tool_contents = [str(message.get("content", "")) for message in agent.session.messages if message["role"] == "tool"]

            self.assertEqual(answer, "done")
            self.assertEqual(len(tool_contents), 3)
            self.assertNotIn("<reminder>", tool_contents[0])
            self.assertNotIn("<reminder>", tool_contents[1])
            self.assertIn("<reminder>Update your todos", tool_contents[2])

    def test_context_pipeline_persists_snips_and_micro_compacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages = [{"role": "user", "content": f"message {index}"} for index in range(8)]
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_old",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_old", "content": "z" * 150},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_big",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_big", "content": "x" * 600},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_recent",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_recent", "content": "y" * 20},
                ]
            )

            result = apply_context_pipeline(
                messages,
                root,
                context_window_tokens=100000,
                max_messages=10,
                keep_recent_tool_results=1,
                tool_result_token_limit=100,
            )

            text = "\n".join(str(message.get("content", "")) for message in result.messages)
            self.assertIn("snipped", text)
            self.assertIn("Earlier tool result compacted", text)
            self.assertIn("Large tool result persisted", text)
            self.assertTrue((root / ".py-coding-agent" / "tool-results" / "call_big.txt").exists())

    def test_context_pipeline_uses_token_budget_and_counts_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages = [{"role": "user", "content": "keep this goal"}]
            tools = [
                {
                    "type": "function",
                    "function": {"name": "large_tool", "description": "x" * 500, "parameters": {"type": "object"}},
                }
            ]

            def counter(counted_messages, counted_tools):
                return 900 if counted_tools else 10

            result = apply_context_pipeline(
                messages,
                root,
                tools=tools,
                token_counter=counter,
                context_window_tokens=1000,
                max_output_tokens=100,
                context_safety_tokens=100,
                context_trigger_ratio=1.0,
                summarizer=lambda _messages: "goal summary",
            )

            self.assertIn("L4 summarized history", result.events)
            self.assertIn("goal summary", str(result.messages[0]["content"]))
            self.assertEqual(result.input_token_budget, 800)

    def test_builtin_model_switch_updates_token_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config.from_env(
                workspace=root,
                api_key="test",
                model="kimi-k2.6",
                confirm_edits=False,
                confirm_bash=False,
            )
            agent = Agent(config, Session.create(root / "sessions"))

            switched = agent.switch_model("kimi-k3")

            self.assertIn("kimi-k2.6 -> kimi-k3", switched)
            self.assertEqual(agent.config.model, "kimi-k3")
            self.assertEqual(agent.config.context_window_tokens, 1_048_576)
            self.assertIn("kimi-k2.7-code", agent.list_models())

    def test_openai_usage_parser_records_real_token_fields(self) -> None:
        usage = _parse_openai_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
                "prompt_tokens_details": {"cached_tokens": 25},
                "completion_tokens_details": {"reasoning_tokens": 12},
            }
        )

        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 40)
        self.assertEqual(usage.cached_input_tokens, 25)
        self.assertEqual(usage.reasoning_tokens, 12)

    def test_subagent_gets_coding_tools_without_recursive_agent_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("hello", encoding="utf-8")
            config = Config.from_env(
                workspace=root,
                api_key="test",
                confirm_edits=False,
                confirm_bash=False,
                max_steps=4,
            )
            agent = Agent(config, Session.create(root / "sessions"))
            fake = FakeSubagentClient()
            agent.client = fake

            result = agent.registry.execute("run_subagent", '{"prompt":"read note.txt"}')

            self.assertFalse(result["is_error"])
            self.assertIn("Subagent read", result["content"])
            self.assertIn("read_file", fake.tool_names)
            self.assertNotIn("run_subagent", fake.tool_names)
            self.assertNotIn("create_task", fake.tool_names)

    def test_spawn_teammate_runs_thread_with_tools_and_lead_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config.from_env(
                workspace=root,
                api_key="test",
                confirm_edits=False,
                confirm_bash=False,
                max_steps=4,
            )
            agent = Agent(config, Session.create(root / "sessions"))
            fake = FakeTeammateClient()
            agent.client = fake

            result = agent.registry.execute(
                "spawn_teammate",
                '{"name":"reviewer","role":"reviewer","prompt":"review the project"}',
            )

            self.assertFalse(result["is_error"])
            for _ in range(20):
                inbox = agent.runtime.check_inbox("lead", consume=False)
                if "review complete" in inbox and "done" in inbox:
                    break
                time.sleep(0.05)
            else:
                self.fail("lead inbox did not receive teammate messages")

            self.assertIn("send_message", fake.tool_names)
            self.assertIn("submit_plan", fake.tool_names)
            self.assertIn("read_file", fake.tool_names)
            self.assertNotIn("spawn_teammate", fake.tool_names)

    def test_protocol_request_plan_review_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = HarnessRuntime(root)

            request_text = runtime.request_plan("alice", "Refactor auth safely")
            request_id = request_text.split()[2]
            alice_inbox = runtime.consume_inbox("alice")

            self.assertEqual(alice_inbox[0]["type"], "plan_request")
            self.assertEqual(alice_inbox[0]["metadata"]["request_id"], request_id)

            submitted = runtime.submit_plan(request_id, "alice", "1. inspect\n2. edit\n3. test")
            lead_inbox = runtime.consume_inbox("lead")
            reviewed = runtime.review_plan(request_id, approve=True, feedback="approved")
            alice_response = runtime.consume_inbox("alice")

            self.assertIn("Submitted plan", submitted)
            self.assertEqual(lead_inbox[0]["type"], "plan_approval_request")
            self.assertEqual(reviewed, f"Request {request_id} approved")
            self.assertEqual(alice_response[0]["type"], "plan_approval_response")
            self.assertEqual(runtime.protocol_requests[request_id].status, "approved")

    def test_worktree_cwd_override_and_teammate_plan_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = HarnessRuntime(root)
            registry = build_registry(root, confirm_edits=False, confirm_bash=False, runtime=runtime)
            task = runtime.create_task("isolated edit")
            task_id = task.split('"id": "')[1].split('"')[0]
            runtime.create_worktree("alice-task", task_id)
            worktree = runtime.worktree_path_for_task(task_id)
            self.assertIsNotNone(worktree)

            result = registry.execute(
                "write_file",
                '{"path":"client.py","content":"print(1)","_cwd":"' + str(worktree).replace("\\", "\\\\") + '"}',
            )

            self.assertFalse(result["is_error"])
            self.assertTrue((worktree / "client.py").exists())
            self.assertFalse((root / "client.py").exists())

            config = Config.from_env(workspace=root, api_key="test", confirm_edits=False, confirm_bash=False)
            agent = Agent(config, Session.create(root / "sessions"))
            state = {"cwd": str(root), "approved_plan_ids": set(), "pending_plan_ids": {"req_1"}, "last_reported": ""}
            blocked = agent._teammate_handlers("alice", state)["write_file"]({"path": "x.py", "content": "x"})

            self.assertIn("Plan approval required", blocked)
            self.assertFalse((root / "x.py").exists())

    def test_real_mcp_stdio_server_tools_list_and_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "fake_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "import json",
                        "import sys",
                        "",
                        "def read_msg():",
                        "    headers = {}",
                        "    while True:",
                        "        line = sys.stdin.buffer.readline()",
                        "        if not line:",
                        "            raise SystemExit",
                        "        if line in (b'\\r\\n', b'\\n'):",
                        "            break",
                        "        key, value = line.decode().split(':', 1)",
                        "        headers[key.lower()] = value.strip()",
                        "    body = sys.stdin.buffer.read(int(headers['content-length']))",
                        "    return json.loads(body.decode())",
                        "",
                        "def write_msg(msg):",
                        "    body = json.dumps(msg, separators=(',', ':')).encode()",
                        "    sys.stdout.buffer.write(f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode() + body)",
                        "    sys.stdout.buffer.flush()",
                        "",
                        "while True:",
                        "    msg = read_msg()",
                        "    if 'id' not in msg:",
                        "        continue",
                        "    if msg['method'] == 'initialize':",
                        "        write_msg({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}})",
                        "    elif msg['method'] == 'tools/list':",
                        "        write_msg({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':[{'name':'echo','description':'Echo text','inputSchema':{'type':'object','properties':{'text':{'type':'string'}}}}]}})",
                        "    elif msg['method'] == 'tools/call':",
                        "        text = msg['params']['arguments'].get('text', '')",
                        "        write_msg({'jsonrpc':'2.0','id':msg['id'],'result':{'content':[{'type':'text','text':'echo:' + text}]}})",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = HarnessRuntime(root)

            try:
                connected = runtime.connect_mcp_stdio("fake", sys.executable, [str(server)])
                listed = runtime.list_mcp_tools()
                called = runtime.call_mcp_tool("mcp__fake__echo", {"text": "hello"})
            finally:
                runtime.close_mcp()

            self.assertIn("1 tool", connected)
            self.assertIn("mcp__fake__echo", listed)
            self.assertEqual(called, "echo:hello")

    def test_mock_eval_harness_records_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = EvalTask(
                id="create_random",
                prompt="Create random_number.py with generate_random_number returning 1 to 100.",
                checker='python -c "import random_number; value=random_number.generate_random_number(); assert 1 <= value <= 100"',
                mock_calls=[
                    {
                        "name": "write_file",
                        "arguments": {
                            "path": "random_number.py",
                            "content": "def generate_random_number():\n    return 42\n",
                        },
                    }
                ],
            )

            traces = run_eval([task], root, mode="mock", k=2, results_dir=root / "results")
            report = build_report(traces, k=2)

            self.assertEqual(len(traces), 2)
            self.assertTrue(all(trace.success for trace in traces))
            self.assertEqual(report["result"]["pass_rate"], 1.0)
            self.assertEqual(report["result"]["pass_at_k"], 1.0)
            self.assertEqual(report["result"]["pass_power_k"], 1.0)
            self.assertGreaterEqual(report["efficiency"]["avg_tool_calls"], 1.0)
            self.assertEqual(report["efficiency"]["scope"], "successful_runs")
            self.assertGreaterEqual(report["efficiency"]["avg_llm_calls"], 2.0)
            self.assertEqual(report["process"]["invalid_tool_call_rate"], 0.0)
            self.assertEqual(report["failures"], {})
            self.assertGreater(report["efficiency"]["avg_input_tokens"], 0)
            self.assertGreater(report["efficiency"]["avg_total_tokens"], 0)
            self.assertTrue((root / "results" / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
