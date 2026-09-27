# Py Coding Agent

A small Python coding agent inspired by the Pi monorepo structure.

It is intentionally independent from the TypeScript packages in the parent
repository. The implementation focuses on the core coding-agent loop:

- OpenAI-compatible chat completions client
- Kimi For Coding client
- runtime switching between `kimi-k2.6`, `kimi-k2.7-code`, and `kimi-k3`
- provider token usage plus Kimi's input-token estimation API
- tool calling loop
- tool registry with configurable enabled tools
- optional diff confirmation before file edits
- optional bash confirmation and read-only mode
- skills discovered from `skills/*/SKILL.md`, with descriptions listed first and full instructions loaded on demand
- prompt templates loaded from `prompts/*.md`
- lightweight streamed final-answer rendering
- built-in tools for reading, writing, editing, searching, listing, and shell commands
- JSONL session persistence, continue, export, session tree/fork, and LLM summary compaction commands
- automatic session compaction
- learn-claude-code style L3/L1/L2/L4 context compaction pipeline
- Python extension hooks from `extensions/*.py`
- interactive and one-shot CLI modes
- learn-claude-code inspired harness mechanisms from s01-s20

## Layout

```text
py-coding-agent/
  pyproject.toml
  src/py_coding_agent/
    agent.py       # agent loop and tool execution
    cli.py         # command line interface
    config.py      # environment/config loading
    llm.py         # OpenAI-compatible and Kimi provider clients
    messages.py    # shared typed message structures
    providers/     # provider clients
    resources.py   # skills and prompt templates
    context.py     # layered context compaction pipeline
    runtime.py     # todos, memory, tasks, jobs, crons, teams, worktrees, MCP manifests
    session.py     # JSONL session storage
    tools.py       # tool registry and built-in coding tools
    extensions.py  # extension hook loader
```

## Setup

```bash
cd py-coding-agent
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## Usage

OpenAI:

```bash
$env:OPENAI_API_KEY="sk-..."
py-coding-agent
```

Settings can also be stored in `settings.json`. See `settings.example.json`.

Kimi OpenAI-compatible API:

```text
PY_CODING_AGENT_PROVIDER=openai
PY_CODING_AGENT_BASE_URL=https://api.moonshot.cn/v1
PY_CODING_AGENT_API_KEY=...
PY_CODING_AGENT_MODEL=kimi-k2.6
```

Kimi For Coding:

```powershell
$env:KIMI_API_KEY="..."
$env:PY_CODING_AGENT_PROVIDER="kimi-coding"
$env:PY_CODING_AGENT_MODEL="kimi-for-coding"
py-coding-agent
```

Or pass flags:

```powershell
$env:KIMI_API_KEY="..."
py-coding-agent --provider kimi-coding --model kimi-k2-thinking -p "Summarize this project"
```

You can also create `.env` in this directory:

```text
KIMI_API_KEY=...
PY_CODING_AGENT_PROVIDER=kimi-coding
PY_CODING_AGENT_MODEL=kimi-for-coding
```

OpenAI-compatible local server:

```powershell
$env:OPENAI_BASE_URL="http://localhost:11434/v1"
$env:OPENAI_API_KEY="dummy"
$env:PY_CODING_AGENT_MODEL="qwen2.5-coder"
py-coding-agent -p "List the files in this project"
```

Interactive commands:

- `/help` shows commands
- `/session` shows current session path
- `/tree` shows indexed session messages
- `/fork [index]` creates a new session copied from the current session
- `/todos` shows the visible todo plan
- `/memory [query]` searches durable memories
- `/tasks` lists durable task records
- `/jobs` lists background jobs
- `/crons` lists scheduled cron prompts
- `/mcp` lists connected MCP manifest tools
- `/models` lists the built-in Kimi model profiles
- `/model [name]` shows or switches the active model
- `/new` starts a new session
- `/continue` loads the latest session
- `/export [path]` exports the session to Markdown
- `/compact [keep]` compacts older messages using the active LLM
- `/compact-raw [keep]` compacts older messages without calling the LLM
- `/skills` lists skills
- `/skill:name` loads `skills/name/SKILL.md`
- `/clear-skills` unloads skills
- `/prompts` lists prompt templates
- `/prompt:name key=value` expands `prompts/name.md`
- `/quit` exits

Useful flags:

```powershell
py-coding-agent --workspace D:\pi-mono\pi-mono-main
py-coding-agent --continue-session
py-coding-agent --tools read_file,grep,list_dir
py-coding-agent --no-confirm-edits
py-coding-agent --read-only
py-coding-agent --auto-compact --auto-compact-threshold 40 --auto-compact-keep 8
py-coding-agent --context-window-tokens 262144 --max-output-tokens 8192
```

Context compaction follows the `learn-claude-code` layered order:

1. `L3 tool_result_budget`: persist large tool results to `.py-coding-agent/tool-results/`.
2. `L1 snip_compact`: trim middle history while preserving OpenAI-compatible tool-call/result groups.
3. `L2 micro_compact`: replace older long tool results with placeholders.
4. `L4 compact_history`: call the LLM for a full summary only if the context is still too large or compact is explicitly requested.

Reactive compaction also runs when the provider reports context or token pressure. Moonshot requests use `/v1/tokenizers/estimate-token-count`; completed calls record the provider's real usage fields. Other providers fall back to a conservative UTF-8 byte estimate.

## learn-claude-code s01-s20 Mapping

This project keeps one Agent Loop and layers harness mechanisms around it:

| Chapter | Mechanism in this project |
| --- | --- |
| s01 Agent Loop | `Agent.prompt()` sends messages, executes tools, appends results, and repeats. |
| s02 Tool Use | `ToolRegistry` registers dispatch-map tools. |
| s03 Permission | workspace path checks, diff confirmation, bash confirmation, read-only mode, and deny-list hook. |
| s04 Hooks | `ToolRegistry` has pre/post tool hooks for permission and logging. |
| s05 TodoWrite | `todo_write` and `todo_read` maintain a visible plan; `TodoManager` enforces at most 20 items, non-empty content, valid statuses, one `in_progress` item, and a reminder after 3 non-todo tool calls. |
| s06 Subagent | `run_subagent` runs a focused subagent loop with fresh context and coding tools, but no recursive subagent/task tools. |
| s07 Skill Loading | skill descriptions are listed first; `load_skill` loads full instructions on demand. |
| s08 Context Compact | L3 tool result persistence, L1 snip compact, L2 micro compact, L4 LLM summary, reactive compact. |
| s09 Memory | `.memory/*.md` files with YAML frontmatter, generated `MEMORY.md` index, LLM/keyword relevant-memory loading, stop-time memory extraction, Dream consolidation, and memory budget limits. |
| s10 System Prompt | `_system_prompt()` assembles runtime sections each turn. |
| s11 Error Recovery | transient LLM retries and raw compaction on context/token pressure. |
| s12 Task System | `create_task`, `list_tasks`, `claim_task`, and `complete_task` persist task graph records. |
| s13 Background Tasks | `start_background_command` runs slow shell commands in a background thread. |
| s14 Cron Scheduler | `schedule_cron` supports 5-field cron expressions; active CLI sessions run a background scanner, deliver due prompts through a cron queue, and use persistence plus ack/restore delivery. |
| s15 Agent Teams | `spawn_teammate` starts a background teammate thread that communicates with lead through mailboxes. |
| s16 Team Protocols | `request_plan`, `review_plan`, and `request_shutdown` create protocol messages and approval records. |
| s17 Autonomous Agents | `autonomous_claim` lets a worker claim the first unblocked task. |
| s18 Worktree Isolation | `create_worktree`, `remove_worktree`, and `keep_worktree` manage task directories. |
| s19 MCP Plugin | `connect_mcp` loads a workspace JSON manifest and exposes `mcp__server__tool` routing. |
| s20 Comprehensive | all mechanisms remain attached to the same loop. |

The implementation is intentionally lightweight. MCP supports the stdio tools path plus a manifest compatibility mode, but not HTTP/SSE, resources, prompts, or sampling. Teammates run as background teaching threads with simplified agent loops and mailbox communication, not as isolated long-running processes. `run_subagent` is the model-backed isolated execution primitive; it can use a bounded coding-tool list, but it is still a synchronous one-shot worker. Token counting is exact for Moonshot message content and provider usage; tool schemas and unsupported providers use a conservative local estimate.

## Extensions

Create `extensions/name.py` and export `register(api)`. Extensions can add tools
or append system prompt text:

```python
from py_coding_agent.tools import Tool


def register(api):
    api.add_system_prompt("Always mention changed files in final answers.")
    api.register_tool(
        Tool(
            {
                "type": "function",
                "function": {
                    "name": "hello",
                    "description": "Return hello.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            lambda args: {"content": "hello", "is_error": False},
        )
    )
```
