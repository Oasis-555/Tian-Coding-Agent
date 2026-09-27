# Py Coding Agent

一个基于 Python 实现的轻量级 Coding Agent 原型，参考 `pi-mono` 和 `learn-claude-code` 的核心思想。

项目重点不是“训练一个 Agent”，而是实现 Agent Harness：给大模型提供可操作的本地环境，包括工具、上下文、权限、记忆、任务系统、Skills、扩展点和会话管理。

## 核心能力

- 支持 OpenAI-compatible API。
- 支持 Kimi / Moonshot 模型调用。
- 支持在运行时切换 `kimi-k2.6`、`kimi-k2.7-code` 和 `kimi-k3`。
- 支持 Kimi Coding 专用接口。
- 实现 Agent Loop：用户输入、LLM 推理、工具调用、工具结果反馈、多轮循环直到最终回答。
- 实现本地代码操作工具：文件读取、文件写入、精确编辑、目录浏览、正则搜索、文件查找和 shell 命令执行。
- 实现工具注册表，可按需启用工具。
- 支持 workspace 路径校验，防止文件工具访问工作区外路径。
- 文件修改前展示 unified diff，并要求人工确认。
- shell 命令默认需要人工确认。
- 支持只读模式，过滤高风险工具。
- 支持 JSONL 会话持久化、继续会话、导出 Markdown、session tree 和 fork。
- 支持 LLM 总结式上下文压缩和自动压缩。
- 使用 Kimi Token API 和 Provider usage 管理上下文与评测指标，不再按字符数判断容量。
- 支持 Skills 按需加载。
- 支持 Prompt Templates。
- 支持 Python extension hook。
- 支持 `learn-claude-code` s01-s20 风格的 Harness 机制。

## 项目结构

```text
py-coding-agent/
  pyproject.toml
  settings.example.json
  README.md
  README-zh.md
  prompts/
    fix.md
    review.md
  skills/
    code-review/SKILL.md
    python-maintainer/SKILL.md
  src/py_coding_agent/
    agent.py        # Agent Loop、系统提示词、子 Agent、错误恢复
    cli.py          # 命令行入口和交互命令
    config.py       # 配置读取：CLI、环境变量、.env、settings.json
    extensions.py   # Python 扩展加载
    llm.py          # Provider 兼容层
    messages.py     # Message / ToolSpec 类型结构
    resources.py    # Skills 和 Prompt Templates 加载
    mcp_client.py   # MCP stdio JSON-RPC client
    runtime.py      # todo、memory、task、job、cron、team、worktree、MCP
    session.py      # JSONL 会话持久化
    tools.py        # 工具注册表和内置工具
    providers/
      openai.py     # OpenAI-compatible /chat/completions
      kimi.py       # Kimi Coding /v1/messages
      factory.py    # Provider 工厂
  tests/
    test_core.py
```

## 安装

```powershell
cd D:\pi-mono\pi-mono-main\py-coding-agent
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
```

## 模型配置

项目没有硬编码模型白名单。只要目标服务支持对应协议和 Tool Calling，就可以通过配置切换模型。

### Moonshot / Kimi OpenAI-compatible API

`.env` 示例：

```text
PY_CODING_AGENT_PROVIDER=openai
PY_CODING_AGENT_BASE_URL=https://api.moonshot.cn/v1
PY_CODING_AGENT_API_KEY=<你的 Moonshot API Key>
PY_CODING_AGENT_MODEL=kimi-k2.6
```

这里的 `provider=openai` 表示使用 OpenAI-compatible 协议，不表示调用 OpenAI 公司的模型。真正请求的服务商由 `base_url` 决定，真正调用的模型由 `model` 决定。

运行时查看和切换内置模型：

```text
/models
/model kimi-k2.6
/model kimi-k2.7-code
/model kimi-k3
```

三个模型都通过 `https://api.moonshot.cn/v1` 调用，并复用同一个 Moonshot API Key。切换时会重建 Provider Client，并同步切换上下文窗口与最大输出 Token；当前 Session 会保留。

### Kimi Coding 专用接口

```text
PY_CODING_AGENT_PROVIDER=kimi-coding
PY_CODING_AGENT_BASE_URL=https://api.kimi.com/coding
KIMI_API_KEY=<你的 Kimi API Key>
PY_CODING_AGENT_MODEL=kimi-for-coding
```

### PowerShell 临时配置

```powershell
$env:PY_CODING_AGENT_PROVIDER="openai"
$env:PY_CODING_AGENT_BASE_URL="https://api.moonshot.cn/v1"
$env:PY_CODING_AGENT_API_KEY="<你的 Key>"
$env:PY_CODING_AGENT_MODEL="kimi-k2.6"
py-coding-agent
```

注意：PowerShell 中应该使用 `$env:NAME="value"`，不要使用 `set NAME=value`。

## 基本使用

启动交互模式：

```powershell
py-coding-agent
```

一次性提问：

```powershell
py-coding-agent -p "总结这个项目的功能"
```

指定工作区：

```powershell
py-coding-agent --workspace D:\pi-mono\pi-mono-main
```

只读模式：

```powershell
py-coding-agent --read-only
```

开启自动上下文压缩：

```powershell
py-coding-agent --auto-compact --auto-compact-threshold 40 --auto-compact-keep 8
```

只启用部分工具：

```powershell
py-coding-agent --tools read_file,grep,list_dir
```

## 交互命令

```text
/help                  显示帮助
/session               显示当前 session 文件
/tree                  显示当前 session 消息索引
/fork [index]          从当前 session 分叉
/new                   创建新 session
/continue              继续最近 session
/export [path]         导出 Markdown
/compact [keep]        使用 LLM 总结压缩旧上下文
/compact-raw [keep]    使用机械摘要压缩旧上下文
/skills                查看 Skills
/skill:name            手动加载 Skill
/clear-skills          清除手动加载的 Skill
/prompts               查看 Prompt Templates
/prompt:name key=value 展开 Prompt Template
/todos                 查看当前 todo plan
/memory [query]        搜索长期记忆
/tasks                 查看任务记录
/jobs                  查看后台任务
/crons                 查看定时任务
/mcp                   查看 MCP 工具
/models                查看可切换的 Kimi 模型
/model [name]          查看或切换当前模型
/quit                  退出
```

## Eval 评测

项目提供独立的 Eval Harness，用来评测 Agent 的结果层、效率层和过程层指标。

Sim Eval 不调用真实 LLM，使用任务中配置的 `mock_calls` 产生确定性工具调用，适合验证 Agent Loop、工具执行、Trace 和 Checker 流程：

```powershell
py-coding-agent-eval --mode mock --tasks evals/tasks.jsonl -k 3
```

Real Eval 使用当前 `.env` / `settings.json` / 环境变量中的真实模型配置，适合评估真实模型与 Agent Harness 组合后的任务完成能力。CLI 中仍使用 `--mode live`：

```powershell
py-coding-agent-eval --mode live --tasks evals/tasks.jsonl -k 3 --requests-per-minute 3
```

`--requests-per-minute` 为整个 Real Eval 共享滑动窗口限速器，适合遵守 Provider 的 RPM 配额并减少连续 Trial 触发 HTTP 429。报告会区分 `checker`、`provider`、`checker_timeout` 和 `harness` 失败，效率指标默认只统计成功 Trial，避免失败请求的零 Token 和短耗时扭曲平均值。

任务文件是 JSONL，每行一个任务：

```json
{"id":"create_random_number","prompt":"Create random_number.py with generate_random_number().","checker":"python -c \"import random_number; assert 1 <= random_number.generate_random_number() <= 100\"","mock_calls":[{"name":"write_file","arguments":{"path":"random_number.py","content":"..."}}]}
```

运行后会在 `evals/results/` 写入每次运行的 trace 和汇总报告：

```text
evals/results/
  create_random_number_1.json
  create_random_number_2.json
  report.json
```

报告包含：

```text
结果层：pass_rate、pass_at_k、pass_power_k
效率层：成功 Trial 的平均输入/输出/总 Token、缓存与推理 Token、平均耗时、p50/p95、平均 LLM/工具调用次数
过程层：无效工具调用率、重复工具调用率、回滚率、上下文压缩事件数
失败层：Checker、Provider、Checker 超时和 Harness 异常数量
```

仓库内置基准包含 10 个小型 Python 任务，包括 3 个从零创建任务和 7 个缺陷修复任务。每个任务通过独立临时工作区执行，并由确定性代码 Checker 验收结果。

## Agent Loop 运行流程

用户输入一段话后，执行顺序如下：

```text
CLI 接收用户输入
  -> 调用 Agent.prompt(text)
  -> 将用户消息保存到 JSONL Session
  -> 运行时组装 system prompt
  -> 将 system prompt、历史消息和工具 Schema 发给 LLM
  -> LLM 判断是否需要调用工具
  -> 如果返回 tool_call，ToolRegistry 执行对应 Python 函数
  -> 工具结果作为 tool message 写回 Session
  -> 再次请求 LLM
  -> 直到 LLM 不再调用工具，返回最终回答
```

核心代码在 `src/py_coding_agent/agent.py` 的 `Agent.prompt()`。

## 本地工具原理

每个工具由两部分组成：

```text
ToolSpec：提供给 LLM 的 JSON Schema 说明
execute：真正执行本地操作的 Python 函数
```

例如 `write_file`：

```text
LLM 返回 write_file tool_call
  -> ToolRegistry 解析 JSON 参数
  -> 校验目标路径是否在 workspace 内
  -> 读取旧文件内容
  -> 生成 unified diff
  -> 用户确认
  -> 写入磁盘
  -> 返回 wrote xxx
```

模型本身不会直接操作文件，真正执行的是 Python 工具函数。

## Skills 机制

Skill 文件放在：

```text
skills/<name>/SKILL.md
```

文件示例：

```markdown
---
name: python-maintainer
description: Use when modifying or reviewing Python code.
---

具体规则正文
```

启动时只将 Skill 的 `name` 和 `description` 放入 system prompt。当任务匹配时，模型调用 `load_skill` 工具读取完整正文。

这样可以实现渐进式加载：

```text
先加载少量 Skill 描述
  -> LLM 判断是否相关
  -> 需要时再加载完整 Skill 正文
```

## Prompt Templates 机制

Prompt Templates 放在：

```text
prompts/*.md
```

调用方式：

```text
/prompt:review target=src/app.py focus=security
```

模板中的变量会被替换：

```text
{{target}} -> src/app.py
{{focus}}  -> security
```

Prompt Template 是用户输入模板；Skill 是给模型看的任务执行规则。

## 上下文压缩

大模型 API 通常是无状态的。每次请求模型时，项目都会重新发送当前需要保留的历史消息。

随着 Session 变长，Token 消耗会增加，因此项目参考 `learn-claude-code` s08/s20，实现了分层压缩管线。Moonshot 模型通过 `/v1/tokenizers/estimate-token-count` 在请求前估算输入 Token，调用完成后读取 Provider `usage` 记录真实输入、输出、缓存和推理 Token；其他 Provider 使用 UTF-8 字节保守估算作为降级方案。

```text
L3 tool_result_budget
  -> 大工具结果落盘到 .py-coding-agent/tool-results/
  -> tool message 中只保留文件引用

L1 snip_compact
  -> 消息数量过多时裁剪中间历史
  -> 保留开头和最近消息
  -> 针对 OpenAI-compatible 消息结构保护 assistant.tool_calls + tool result 配对

L2 micro_compact
  -> 将较旧的长工具结果替换为占位符
  -> 保留最近若干个工具结果原文

L4 compact_history
  -> 如果上下文仍超过 Token 输入预算，调用 LLM 总结整段历史
  -> 保存 transcript 到 .py-coding-agent/transcripts/
```

手动触发：

```text
/compact 8
```

自动触发：

```powershell
py-coding-agent --auto-compact --auto-compact-threshold 40 --auto-compact-keep 8
```

当前自动压缩按消息数量触发，不是精确 token 预算。

也可以调整分层管线阈值：

```powershell
py-coding-agent `
  --context-window-tokens 262144 `
  --max-output-tokens 8192 `
  --context-trigger-ratio 0.85 `
  --context-safety-tokens 4096 `
  --compact-max-messages 50 `
  --keep-recent-tool-results 3 `
  --tool-result-token-limit 12000
```

对应 `.env` 配置：

```text
PY_CODING_AGENT_CONTEXT_WINDOW_TOKENS=262144
PY_CODING_AGENT_MAX_OUTPUT_TOKENS=8192
PY_CODING_AGENT_CONTEXT_TRIGGER_RATIO=0.85
PY_CODING_AGENT_CONTEXT_SAFETY_TOKENS=4096
PY_CODING_AGENT_COMPACT_MAX_MESSAGES=50
PY_CODING_AGENT_KEEP_RECENT_TOOL_RESULTS=3
PY_CODING_AGENT_TOOL_RESULT_TOKEN_LIMIT=12000
```

## learn-claude-code s01-s20 对应关系

| 章节 | 本项目实现 |
| --- | --- |
| s01 Agent Loop | `Agent.prompt()` 中的多轮消息、工具调用和结果反馈循环 |
| s02 Tool Use | `ToolRegistry` 注册工具和分发调用 |
| s03 Permission System | workspace 路径校验、diff 确认、bash 确认、只读模式、deny-list hook |
| s04 Hook System | `ToolRegistry` 支持 pre/post tool hooks |
| s05 TodoWrite | `todo_write`、`todo_read` 维护可见计划；`TodoManager` 校验最多 20 项、非空内容、合法状态和单一 `in_progress`，连续 3 次非 todo 工具调用后注入 reminder |
| s06 Subagent | `run_subagent` 使用干净上下文执行子任务，提供 coding 工具但不提供递归子代理工具 |
| s07 Skill Loading | Skill description 先加载，完整正文按需加载 |
| s08 Context Compact | L3 工具结果落盘、L1 中间消息裁剪、L2 旧工具结果占位、L4 LLM 总结、reactive compact |
| s09 Memory System | `.memory/*.md` + YAML frontmatter 存储长期记忆，`MEMORY.md` 自动索引；每轮开始 LLM/关键词选择相关 active 记忆并注入，结束后自动提取新记忆；通过 `fact_key`、`source`、`confidence`、`status`、`supersedes` 管理冲突和更新，达到阈值后触发 Dream 合并去重，并带索引/内容预算控制 |
| s10 System Prompt | `_system_prompt()` 每轮动态组装系统提示词 |
| s11 Error Recovery | 瞬时错误重试，context/token 压力时触发压缩 |
| s12 Task System | `create_task`、`claim_task`、`complete_task` 持久化任务图 |
| s13 Background Tasks | `start_background_command` 后台线程执行慢命令 |
| s14 Cron Scheduler | `schedule_cron` 支持五段式 cron 表达式，CLI 活跃时后台线程扫描到期任务，经 cron queue 自动送入 Agent Loop，并支持持久化、ack 和失败 restore |
| s15 Agent Teams | `spawn_teammate` 启动后台 teammate 线程，teammate 通过 mailbox 与 lead 通信 |
| s16 Team Protocols | `request_plan`、`submit_plan`、`review_plan`、`request_shutdown` 使用 `request_id` 关联请求和响应，Lead 消费 inbox 时会先路由协议消息 |
| s17 Autonomous Agents | teammate 使用 WORK/IDLE 生命周期，空闲时优先读 inbox，再自动扫描并领取未阻塞任务 |
| s18 Worktree Isolation | `create_worktree`、`remove_worktree`、`keep_worktree` 管理隔离目录；teammate 领取绑定 worktree 的任务后，文件和 shell 工具会在该目录执行 |
| s19 MCP Plugin | `connect_mcp_stdio` 连接真实 MCP stdio server，执行 `initialize`、`tools/list`、`tools/call`；`connect_mcp` 保留 manifest 兼容模式 |
| s20 Comprehensive Agent | 所有机制都挂在同一个 Agent Loop 周围 |

## Team / Protocol / Worktree 使用

Lead 可以直接让模型使用这些工具，也可以在对话里自然描述目标：

```text
创建两个任务：后端 schema、前端 API client。
为每个任务创建 worktree。
启动 alice 作为 backend dev，启动 bob 作为 frontend dev。
让他们空闲时自动领取任务并完成。
```

运行时大致流程：

```text
Lead -> create_task
Lead -> create_worktree(name, task_id)
Lead -> spawn_teammate(alice / bob)
teammate WORK: 调 LLM，执行工具
teammate IDLE: 先 consume inbox，再 scan unclaimed tasks
teammate -> claim_task
如果任务绑定 worktree，teammate 的 cwd 切到该 worktree
teammate -> bash / read_file / write_file
teammate -> complete_task
teammate -> send_message("lead", result)
Lead 下一轮 Agent.prompt() 会把 lead inbox 注入上下文
```

计划审批流程：

```text
Lead -> request_plan("alice", "重构 auth 模块")
alice inbox 收到 plan_request
alice -> submit_plan(request_id, plan)
Lead inbox 收到 plan_approval_request
Lead -> review_plan(request_id, approve=true, feedback="approved")
alice inbox 收到 plan_approval_response
alice 获批后再执行 bash/write_file/edit_file
```

关闭流程：

```text
Lead -> request_shutdown("alice")
alice 在 WORK 或 IDLE 阶段读到 shutdown_request
alice -> shutdown_response
Lead 消费 inbox 后更新协议状态
alice 线程退出
```

## MCP 使用

项目现在支持两种 MCP 接入：

1. `connect_mcp_stdio`：连接真实 MCP stdio server，走 MCP JSON-RPC framing。
2. `connect_mcp`：兼容旧的 JSON manifest 命令包装。

### 真实 MCP stdio server

连接一个 stdio MCP Server：

```text
connect_mcp_stdio({
  "name": "filesystem",
  "command": "python",
  "args": ["path/to/server.py"],
  "env": {}
})
```

连接后查看工具：

```text
list_mcp_tools({})
```

调用工具：

```text
call_mcp_tool({
  "tool_name": "mcp__filesystem__read_file",
  "arguments": {"path": "README.md"}
})
```

内部协议流程：

```text
启动 MCP server 子进程
-> 发送 initialize
-> 发送 notifications/initialized
-> 调用 tools/list 获取工具 schema
-> call_mcp_tool 时发送 tools/call
-> 解析 content 文本返回给 Agent
```

### Manifest 兼容模式

示例文件：

```json
{
  "tools": {
    "echo": {
      "description": "Echo arguments.",
      "command": "python scripts/echo_tool.py",
      "timeout": 30
    }
  }
}
```

连接：

```text
connect_mcp({"name":"demo","manifest_path":"mcp.json"})
```

调用：

```text
call_mcp_tool({"tool_name":"mcp__demo__echo","arguments":{"text":"hello"}})
```

工具命令会通过环境变量 `MCP_TOOL_ARGUMENTS` 接收 JSON 参数。

## Extension Hook

可以在：

```text
extensions/*.py
```

中定义：

```python
def register(api):
    api.add_system_prompt("Always mention changed files.")
    api.register_tool(...)
```

扩展可以追加 system prompt，也可以注册新工具。

## 测试

```powershell
cd D:\pi-mono\pi-mono-main\py-coding-agent
.\.venv\Scripts\python.exe -m unittest tests.test_core
```

当前测试覆盖：

- Skill frontmatter 解析
- 只读模式阻止写入
- Session compact 和 fork
- Extension 注册工具
- Todo 新版约束、reminder 注入、Memory、Task、MCP manifest 工具
- MCP stdio server 的 `initialize`、`tools/list`、`tools/call`
- Subagent 工具隔离
- Team protocol request/response
- Worktree cwd 覆盖和 teammate plan gate

## 当前边界

这是一个学习型 Coding Agent 原型，不是完整 Claude Code 复刻。

当前边界：

- 流式输出是 CLI 逐字渲染，不是 Provider SSE 真流式。
- MCP 已支持 stdio transport 的工具主链路：`initialize`、`tools/list`、`tools/call`。暂未实现 HTTP/SSE transport、resources、prompts 和 sampling。
- teammate 是线程级教学实现，支持 WORK/IDLE、协议消息、自动认领和 idle 通知，但不是独立进程级长期团队。
- worktree 优先尝试 `git worktree add/remove`，失败时退化为隔离目录；不是完整 Claude Code worktree 生命周期。
- 上下文压缩使用近似字符大小估算，不是真实 tokenizer token 预算。
- RAG 和向量数据库没有实现。
- 权限系统是轻量版，生产环境还需要更严格沙箱。

## 面试项目总结

可以这样介绍：

> 我基于 Python 实现了一个轻量级 Coding Agent Harness。核心是 LLM Tool Calling 驱动的 Agent Loop，模型可以按需调用本地文件读写、精确编辑、搜索和 shell 工具。项目通过 workspace 路径校验、diff 人工确认、bash 确认和只读模式控制执行风险。同时实现了 Provider 解耦、JSONL 会话持久化、learn-claude-code 风格的 L3/L1/L2/L4 分层上下文压缩、Skills 按需加载、Prompt Templates、扩展 Hook，以及 todo、memory、task、background、cron、team mailbox、worktree 和 MCP stdio 工具接入等 Harness 机制。
