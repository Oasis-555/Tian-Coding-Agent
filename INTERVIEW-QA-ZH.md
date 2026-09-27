# TianForge Agent 面试问答

本文回答以当前 `py-coding-agent` 源码为准。回答时应区分三类内容：已经实现的机制、已验证但仍是教学级的实现、尚未实现的生产化方案。

## 一、记忆系统

### 1. 选一个具体功能，说明它怎样从想法一步步变成实现

我会选择“长期记忆冲突治理”。最初的方案只是把长期信息写入 Markdown，再在后续会话中读回来。问题是用户可能先说“偏好 GPT-4.1”，后来改成“偏好 Kimi K3”；如果两条都注入模型，模型可能随机选择，长期记忆反而成为噪声。

我的实现过程分为六步：

1. 先定义不变量：用户本轮明确指令优先；同一个事实槽默认只能有一条有效记忆；模型推断不能直接覆盖用户明确事实。
2. 为记忆增加结构化字段：`fact_key`、`source`、`confidence`、`status`、`supersedes`、`superseded_by` 和时间字段。
3. 使用 `fact_key` 表示事实槽，例如不同的模型偏好都使用 `preferred_model`。
4. 写入时执行状态转换：用户明确更新会把同槽旧记忆归档，新记忆设为 `active`；模型提取的信息与现有事实冲突时设为 `conflicted`。
5. 召回时默认只读取 `active`，避免旧事实和冲突事实同时进入上下文。
6. 用单元测试验证显式更新、模型冲突、索引预算、自动提取和按需召回。

核心代码位于 `runtime.py` 的 `write_memory_file()`、`select_memories()` 和 `rebuild_memory_index()`，调用编排位于 `agent.py` 的 `_extract_memories()`、`_select_relevant_memories()` 和 `_system_prompt()`。

### 2. 记忆方向参考过哪些开源项目或产品

我实际参考的是三个来源：

- `learn-claude-code` 旧版 s09：结构化文件记忆、索引、按需读取和整理思路。
- Claude Code：`CLAUDE.md`、自动记忆、`MEMORY.md` 索引和主题文件按需读取。
- pi-mono：文件式上下文、可审查指令和轻量 Agent Harness 思想。

我的代码是 Python 重写，并没有直接复制这些项目。`fact_key + source + confidence + status + supersedes` 的冲突状态机，是在这些文件式记忆思路上增加的治理层。

### 3. Claude Code 的长期记忆怎样实现

Claude Code 公开文档描述了两套互补机制：

- `CLAUDE.md`：由用户或团队维护，保存项目规则、命令、架构和编码约定。它有组织、用户、项目和本地等作用域。
- 自动记忆：由 Claude 在工作中写入，保存构建命令、调试经验、架构笔记和偏好，不是每轮都写。

自动记忆按项目存放在 `~/.claude/projects/<project>/memory/`，包含入口 `MEMORY.md` 和主题文件。每次会话启动加载 `MEMORY.md` 前 200 行或 25KB，主题文件需要时再读取；用户可以通过 `/memory` 查看、编辑、删除或关闭自动记忆。同一 Git 仓库的 worktrees 共享自动记忆目录。[Claude Code 官方记忆文档](https://code.claude.com/docs/zh-CN/memory)

需要注意：Claude Code 把这些内容当上下文，不是强制配置。官方文档明确指出，冲突指令可能被模型任意选择，因此仍建议人工审查；必须强制执行的规则应放在权限设置、沙箱或 Hook 中。

### 4. 弱事实怎样避免被永久当成确定事实

例如“我可能要使用 Redis”只表示候选方案，不能写成“项目使用 Redis”。正确策略是：

- `source=llm_extracted`，而不是 `user_explicit`。
- 使用较低置信度，例如 `confidence=0.4`。
- 状态设为 `pending`，默认不参与召回。
- 保存原始证据和会话位置，后续出现重复证据或用户确认后才能晋升为 `active`。
- 若用户否认，则转成 `rejected`；若与现有事实冲突，则转成 `conflicted`。

当前项目的提取 Prompt 已要求不确定推断使用 `pending/conflicted`，且无效状态会降级为 `pending`。但代码还没有强制“低置信度不得 active”的阈值门禁，这是可以继续完善的点。生产版应在 `write_memory_file()` 中加入确定性规则，而不能只相信模型正确填写状态。

### 5. 新旧偏好冲突时，怎样决定 pending、active 和 superseded

当前状态机是：

- 用户明确表达，且没有同槽事实：新记忆直接 `active`。
- 用户明确修改，且存在相同 `fact_key` 的 active 事实：新记忆 `active`，旧记忆变成 `archived`，旧记录写入 `superseded_by`，新记录写入 `supersedes`。
- LLM 推断与同槽 active 事实冲突：新记忆变成 `conflicted`，不能覆盖用户事实。
- 证据不足：应该保持 `pending`，等待确认或更多证据。

本项目没有名为 `superseded` 的状态；`supersedes` 是关系，旧记录实际状态是 `archived`。面试时应准确说明这一点。

### 6. 从对话结束到写入，再到下一次召回的完整流程

1. `Agent.prompt()` 发现 Assistant 没有继续返回 Tool Call，认为当前轮结束。
2. `_extract_memories()` 截取最近 10 条 Session 消息，并附上现有记忆摘要。
3. LLM 以 JSON 数组返回候选记忆及元数据。
4. Agent 校验必要字段，调用 `write_memory_file()`。
5. Runtime 去重、查找相同 `fact_key`、执行冲突状态转换，写入 `.memory/*.md`。
6. `rebuild_memory_index()` 重建 `.memory/MEMORY.md`。
7. active 记忆达到 10 条时，`_maybe_consolidate_memories()` 可执行 Dream 合并；它有锁和 24 小时冷却。
8. 下一轮开始时，`_select_relevant_memories()` 先获取 active 记忆目录，让 LLM 选择最多 5 个文件；失败时使用关键词匹配降级。
9. 选中的正文受单条 4096 字符、总计 12000 字符预算限制，被注入本轮系统提示词。

### 7. 怎样证明新规则优于原始方案，而不是只针对个例调参

当前证据是规则级单元测试：明确更新会归档旧事实、LLM 冲突不被默认召回、索引与正文预算生效、提取和召回链路可运行。这证明实现符合不变量，但还不能证明在真实分布上普遍更好。

更完整的验证应建立 Memory Eval：

- 构造多种事实槽：模型偏好、语言偏好、环境路径、项目依赖、临时方案。
- 每类包含确认、否认、弱表达、同义改写、跨会话更新和恶意注入。
- 划分开发集和留出测试集，留出集不能用于调规则。
- A/B 对比“全部注入”与“状态治理”两种方案。
- 指标包括冲突注入率、过时记忆召回率、有效记忆召回率、错误永久化率、更新收敛轮数和 Token 开销。
- 对规则做变异测试，例如删除 `source` 优先级或 active 过滤，确认指标确实下降。

### 8. 是否在真实产品中遇到过错误或过时记忆

诚实回答是：这个项目还没有部署到大规模生产环境，我不能声称处理过真实线上记忆事故。我在日常使用和项目测试中观察到的风险是，旧偏好在后续会话仍被采用、模型把讨论中的候选方案写成既定事实，以及摘要把“可能”压缩成“确定”。这些现象启发我把来源、置信度和生命周期从自然语言提升为显式数据字段。

如果面试官继续追问，我会强调这是基于可复现风险构造的工程设计，而不是虚构生产经历。

## 二、上下文管理

### 9. Codex 或 Claude Code 如何管理上下文

Claude Code 通过会话历史、`CLAUDE.md`、自动记忆、路径范围规则、Skills 和压缩共同管理上下文。官方文档说明，根项目 `CLAUDE.md` 在 `/compact` 后会重新从磁盘注入，子目录指令在再次读取该目录时加载。

关于 Codex，我只依据公开机制回答，不推测产品内部实现：Codex 使用项目指令文件和会话上下文；OpenAI API 为长任务提供 conversation state 和 compaction，压缩结果可用于后续请求。官方还建议压缩时保留已完成动作、关键假设、标识符、工具结果、阻塞项和下一目标。[OpenAI Compaction 文档](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.5)

### 10. 项目的四层上下文管理分别怎样做

入口是 `context.py::apply_context_pipeline()`，执行顺序固定为 L3、L1、L2，然后按 Token 判断是否执行 L4：

- L3 大结果落盘：单条工具结果超过 `tool_result_token_limit`，写入 `.py-coding-agent/tool-results/`，上下文只保留路径提示。
- L1 截断：消息组超过 `max_messages` 时保留前 3 组和最近若干组，中间替换成 `[snipped N messages]`。Assistant Tool Call 与对应 Tool Result 作为整体分组，避免破坏协议。
- L2 旧结果处理：只保留最近若干条完整 Tool Result，更早且较长的结果替换成可重新执行工具的占位符。
- L4 历史摘要：前三层之后计算 Token；超过输入预算或显式强制压缩时，将完整历史落盘，再由 LLM 总结目标、决策、文件、命令、错误和待办。

Provider 报 context/token 错误时还会触发 Reactive Compact，并保留最近 5 个消息组。

### 11. 上下文压缩会带来什么问题

- 摘要遗漏约束、文件名、错误细节或尚未完成的任务。
- 把不确定陈述压缩为确定结论，改变语义强度。
- 丢失 Tool Call 与结果之间的因果关系。
- 旧摘要不断摘要，产生累积失真。
- 压缩后模型重复已完成操作，或者忘记为什么做出某个决策。
- LLM 摘要本身增加 Token、延迟和一次失败机会。

因此项目在摘要前保存完整 transcript，并保留工具协议分组；但它仍不能保证语义零损失。

### 12. 上下文已经很长，为什么仍需要压缩

长窗口解决的是“能放进去”，没有解决：

- 输入 Token 成本和延迟随历史增长。
- 大量旧工具输出稀释当前目标，降低注意力质量。
- Coding Agent 的日志、测试输出和搜索结果增长非常快。
- 模型在长上下文中仍可能遗漏中间信息。
- 需要为下一次模型输出和工具 Schema 预留空间。

所以压缩既是容量控制，也是相关性和信噪比管理。

### 13. 是否遇到过目标漂移或上下文噪声

项目评测中观察到过过程噪声：Real Eval 的重复工具调用率为 14.56%，主要是重复 `read_file` 和 `bash`；上下文管线共触发 5 次压缩。这不能直接证明发生了目标漂移，但说明工具轨迹会快速膨胀并产生重复信息。

面试时应说“观察到噪声和重复调用，目标漂移尚未建立专门指标”，不要把尚未量化的现象说成确定结论。

### 14. 怎样判断继续当前会话还是新开会话

继续当前会话适用于：目标相同、仍依赖之前的决策和工具结果、修改同一批文件。新开会话适用于：目标已经改变、之前历史大部分无关、需要不同权限或角色、旧上下文包含冲突假设，或者压缩后仍然噪声很高。

简单判断标准是：新任务需要引用的历史不到当前会话的 20% 时，通常更适合新建 Session，并用一段明确 handoff 摘要传递必要状态。这是经验规则，不是项目当前硬编码阈值。

### 15. 除新开会话外，怎样减少噪声和目标漂移

- 使用 Todo 和任务 DAG 固定当前目标、验收条件与未完成项。
- 大工具结果落盘，只保留引用。
- 按需加载 Skill、Memory 和 MCP 工具 Schema。
- 在里程碑处生成结构化 handoff 摘要。
- 删除或归档失效记忆，避免新旧事实并存。
- 限制重复工具签名，连续失败后改变策略。
- 将探索任务交给 SubAgent，只回收结论而不是完整轨迹。

### 16. 是否考虑 Prompt Cache，提示词怎样排列

项目已经解析 Provider 返回的 `cached_input_tokens`，但没有显式配置 `prompt_cache_key`，也没有完整缓存策略。

更好的排列是：稳定且完全一致的系统规则和常用 Tool Schema放在最前；相对稳定的项目指令随后；动态的 Memory、Todo、任务状态和当前用户输入放在末尾。不要在稳定前缀里加入时间戳、随机 ID 或顺序不稳定的工具列表。OpenAI 官方也建议静态内容在前、动态内容在后，并持续记录 cached tokens。[OpenAI Prompt Caching 指南](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.5)

### 17. Token 预算怎样计算，为什么在阈值触发压缩

当前公式是：

```text
hard_input_budget = context_window - max_output_tokens - safety_tokens
input_budget = hard_input_budget * context_trigger_ratio
```

默认 `context_trigger_ratio=0.85`、`safety_tokens=4096`。Moonshot 消息通过 Token API 估算，再加上本地估算的 Tool Schema；不支持的 Provider 使用 UTF-8 字节数除以 4 并乘 1.1 的保守估算。

85% 不是理论最优值，而是为 Token 估算误差、下一轮 Tool Result、系统提示变化和模型输出留余量。正确做法是通过不同任务长度的 Eval 调整，而不是凭感觉固定。

### 18. Token 预算管理容量还是调用成本

当前主要管理上下文容量和溢出风险，同时会间接降低成本。它还不是成本预算器，因为没有配置单价、每任务人民币上限或模型路由策略。生产版应同时维护 `context_budget` 和 `cost_budget`，不能把两者混为一谈。

### 19. 重要信息被压掉怎么办，结构化摘要能完全避免吗

不能完全避免。项目通过完整 transcript 落盘、保留最近消息组、提示摘要必须保存目标/决策/文件/错误/待办，以及把长期事实放进独立 Memory 来降低风险。

进一步可把摘要改成可校验 Schema，例如 `goal`、`constraints`、`completed`、`files_changed`、`commands`、`errors`、`open_tasks` 和 `evidence_refs`，并在压缩后运行一致性 Checker。即使如此，结构化摘要只是降低丢失概率，不是无损压缩。

### 20. JSONL 是做会话持久化还是摘要，为什么选择 JSONL

JSONL 主要用于 Session 持久化：每行保存一条 user、assistant 或 tool 消息，便于逐行读取、追加、调试、导出和部分恢复。压缩后会把摘要作为一条新的 user 消息写回同一个 JSONL，但 JSONL 本身不是摘要算法。

选择 JSONL 是因为格式简单、流式友好、单行损坏影响范围较小，而且能保留 Tool Call 的结构字段。当前 `Session.save()` 每次会重写整个文件，严格来说还没有发挥纯追加日志的全部优势。

## 三、工具执行与 Agent Loop

### 21. 工具执行前怎样校验参数、权限、目录和网络

当前流程是：模型先看到 JSON Schema；`ToolRegistry.execute()` 再把参数解析为 JSON object；Pre Hook 检查危险命令；具体工具检查必要字段和类型转换；`_resolve()` 与 `_base_workspace()` 使用解析后的绝对路径验证目标仍在 workspace；写操作受只读模式和 diff 确认控制；bash 默认需要人工确认并有超时。

边界是：当前没有通用 JSON Schema Runtime Validator，部分参数依靠工具函数访问时校验；网络没有隔离，bash 会继承系统环境并拥有宿主机允许的网络能力。生产版应使用真实进程沙箱、网络 allowlist、资源上限和按工具发放的 capability。

### 22. 工具调用失败怎样反馈给模型

工具异常被 `ToolRegistry` 捕获并转换成 `{"content": error, "is_error": true}`。Agent 将内容封装成带原 `tool_call_id` 的 `role=tool` 消息，追加到 Session 和当前 messages；下一轮 LLM 可以根据错误换参数、换工具或停止。错误不会直接抛给用户，除非达到最大循环或 Provider 本身失败。

### 23. Agent 怎样判断任务完成，还是继续调用工具

当前决定权主要在模型：Assistant 返回 `tool_calls` 时继续执行；没有 Tool Call 时把文本视为最终回答。`max_steps` 是硬停止条件。交互模式没有自动运行任务级 Checker，因此“模型说完成”不等于代码一定正确；Eval 模式才会在循环结束后运行确定性 Checker 验收结果。

### 24. 怎样防止重复调用错误工具进入死循环

当前只有两层基础保护：工具错误作为证据反馈给模型，Agent Loop 最多执行 `max_steps`。Eval Harness 能统计相同工具与参数签名的重复率，但主运行时还没有重复调用断路器。

推荐升级为：对标准化的 `tool_name + args + error` 计算签名；同一失败签名连续两次时注入策略提醒，三次时禁用该调用并要求换方案；连续无文件变化、无新信息的轮次也计为 no-progress，达到阈值后停止或请求用户。

### 25. 为什么单 Agent 不够，什么时候创建 SubAgent

单 Agent 的上下文会混入探索细节，顺序执行独立任务也浪费时间。适合创建 SubAgent 的情况包括：独立代码调研、一次性代码审查、可并行的多个模块、需要不同角色提示词，或者希望隔离大量搜索轨迹。

单文件小改、强依赖共享上下文、任务间频繁交互时不应创建 SubAgent，因为委派本身有 Token、延迟和结果整合成本。

### 26. 主 Agent 怎样创建子 Agent、传递上下文并回收结果

主模型调用 `run_subagent(prompt)`；实现创建新的 messages，只包含专用 system prompt 和任务 prompt，不复制主 Session；提供 `bash/read/write/edit/find/grep/list` 白名单工具，不提供递归代理工具；子 Agent 最多运行 30 轮；没有 Tool Call 时返回摘要。该摘要作为 `run_subagent` 的 Tool Result 回到主 Agent，主 Agent再继续推理。

当前传递的是任务描述，不是自动筛选后的主上下文包；如果子任务依赖背景，主 Agent 必须把必要约束、文件和验收条件写入 prompt。

### 27. SubAgent 与 Agent Team 有什么区别

SubAgent 是同步、一次性、隔离上下文的函数调用，适合聚焦子任务，结束后只返回结果。Agent Team 的 Teammate 在后台线程中运行自己的循环，有名称、角色、Inbox、WORK/IDLE 生命周期、任务认领、计划审批和 shutdown 协议，适合持续协作。

当前 Team 是线程级教学实现，不是跨机器、跨进程的分布式 Agent 系统。

## 四、多 Agent、任务与 Worktree

### 28. Worker 怎样领取 ready 任务，如何避免重复领取

任务有 `pending/in_progress/completed`、`owner` 和 `blocked_by`。Worker 空闲时调用 `first_claimable_task()` 找到依赖已完成的 pending 任务，再调用 `claim_task()`。

`claim_task()` 在 `_task_lock` 内重新读取任务，并再次检查状态、owner 和依赖，然后写成 `in_progress`。因此同一 Python 进程的多个线程不会同时领取成功。这个锁不是跨进程锁；如果未来使用多进程或多机器，需要数据库原子更新、文件锁或 compare-and-swap。

### 29. Agent 如何通信，Inbox、确认和审计日志分别做什么

`send_message()` 把消息追加到接收者的 mailbox JSONL；`consume_inbox()` 读取后删除文件，并把带 `request_id` 的协议消息交给状态机；计划审批和 shutdown response 用 `request_id` 匹配请求。

当前没有逐消息 ACK，也没有消费后的持久审计日志；Inbox 是简化版 at-most-once 交付，进程在读取后、处理前崩溃可能丢消息。`teammates.jsonl` 和 worktree `events.jsonl` 只记录部分事件。生产版应增加 `message_id`、pending/acked 状态、原子 rename、重投递和 append-only audit log。

### 30. 两个 Agent 同时修改同一份代码怎样处理

设计上应先按模块拆任务，并为 Worker 分配不同 worktree。这样它们不会直接写同一工作目录。如果两个分支修改同一行，冲突推迟到合并阶段处理。

当前如果两个线程被配置到同一个 cwd，系统没有文件锁，也不能防止覆盖。因此 Worktree 是主要隔离手段，而不是完整冲突解决方案。

### 31. Worktree 成果怎样审查、合并和处理冲突

当前项目能创建、保留和删除 worktree，并将任务绑定到 worktree；Worker 认领任务后把 `_cwd` 切换到对应目录。它还没有自动 Review/Merge Queue。

现阶段应由 Lead 或用户检查 `git diff`、运行测试，再手动 merge/cherry-pick；发生冲突时回到相关 Worker 或人工解决。生产升级方案是：Worker 提交分支、Verifier 运行验收、Lead 审批、Merge Queue 按依赖顺序 rebase/merge，冲突时创建专门解决任务。

### 32. 与 Codex/Claude Code 相似度高，自己真正增加或调整了什么

我不会把 Agent Loop、Tool Calling、MCP、SubAgent 或 Worktree 本身说成原创。我的工作主要是：

- 用 Python 从零组合成可阅读、可测试的轻量 Harness。
- 在文件记忆上增加 `fact_key/source/confidence/status/supersedes` 冲突治理。
- 将 L3/L1/L2/L4 管线与真实 Token API、Provider Usage 和模型配置关联。
- 将 Team 的任务 DAG、mailbox、计划审批、自动领取和 worktree cwd 串成完整时序。
- 建立 10 类任务、每类 3 次的 Sim/Real Eval，并区分 Checker、Provider 和 Harness 失败。

真实评测达到 96.7% Pass Rate、100% `pass@3`、90% `pass^3`；同时发现 14.56% 重复工具调用率和 Provider 网络恢复不足，这些数据用于继续优化 Harness。

### 33. Worker 宕机后，被占用任务怎样恢复

当前没有自动恢复。Worker 异常退出后，任务可能停留在 `in_progress + owner`，需要人工重置；这属于明确的生产化缺口。

升级方案是租约机制：任务保存 `lease_owner`、`lease_expires_at` 和 `heartbeat_at`；Worker 定期续租；调度器发现租约过期后，把任务转回 pending 或进入 recovery；重领前检查 worktree 和副作用，避免重复执行不可幂等操作。

### 34. 哪些操作自动允许、询问用户或直接拒绝

当前策略可以这样回答：

| 决策 | 操作 |
| --- | --- |
| 自动允许 | workspace 内读取、目录浏览、grep、文件查找、查看任务和记忆 |
| 询问用户 | 默认的文件写入/精确编辑 diff、bash 命令、删除有未提交修改的 worktree |
| 直接拒绝 | workspace 路径逃逸、只读模式下的写入/bash、静态 deny-list 中的高危命令、未知工具和非法 JSON 参数 |

当前 deny-list 只能挡住少量明显命令，不是安全沙箱。更成熟的方案应根据文件敏感度、命令副作用、可逆性和网络访问计算风险：低风险自动允许，中风险确认，高风险隔离执行，凭证读取、系统目录写入和破坏性命令直接拒绝。

## 五、面试总结话术

可以用下面这段作为两分钟回答：

> 我实现的不是一个新模型，而是一个 Python Coding Agent Harness。核心是 Tool Calling 驱动的 Agent Loop，外面组合了本地代码工具、权限 Hook、JSONL Session、Token 感知的分层上下文压缩、结构化长期记忆、SubAgent、Agent Teams、Worktree 和 MCP。我的重点改造是长期记忆冲突治理：使用 fact_key 表示事实槽，通过来源、置信度、状态和替代关系管理弱事实、显式更新与过时记忆，召回时只注入 active 记录。我还建立了 10 类任务、每类 3 次的真实模型评测，得到 96.7% Pass Rate、100% pass@3 和 90% pass^3，并根据 Trace 发现重复工具调用和 Provider 恢复仍需优化。对于尚未实现的跨进程锁、Worker 租约、网络沙箱和自动合并队列，我会明确说明边界和下一步方案。
