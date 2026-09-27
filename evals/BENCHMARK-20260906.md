# Coding Agent Benchmark - 2026-09-06

## Setup

- Model: `kimi-k2.6`
- Tasks: 10 small Python coding tasks
- Trials per task: 3
- Total trials per mode: 30
- Task mix: 3 file-creation tasks and 7 bug-fix tasks
- Isolation: a fresh temporary workspace per trial
- Grading: deterministic Python checker commands
- Real-model pacing: shared 3 RPM sliding-window limiter

## Sim Eval

| Metric | Value |
| --- | ---: |
| Pass rate | 100% (30/30) |
| pass@3 | 100% |
| pass^3 | 100% |
| Average estimated total tokens | 7,942.9 |
| Average duration | 0.301 s |
| p50 / p95 duration | 0.194 s / 1.009 s |
| Average LLM calls | 3.00 |
| Average tool calls | 1.00 |
| Invalid / repeated tool-call rate | 0% / 0% |
| Rollback rate / compact events | 0% / 0 |

Sim Eval uses deterministic task-specific mock tool calls. Token counts are local estimates and do not represent provider billing.

## Real Eval

| Metric | Value |
| --- | ---: |
| Pass rate | 96.7% (29/30) |
| pass@3 | 100% |
| pass^3 | 90% |
| Average input / output tokens | 12,011.6 / 2,056.2 |
| Average total tokens | 14,067.8 |
| Average cached / reasoning tokens | 8,792.3 / 1,608.8 |
| Average duration | 115.1 s |
| p50 / p95 duration | 117.6 s / 195.1 s |
| Average LLM calls | 5.31 |
| Average tool calls | 3.48 |
| Invalid tool-call rate | 2.91% (3/103) |
| Repeated tool-call rate | 14.56% (15/103) |
| Rollback rate | 0% |
| Context compact events | 5 |

Efficiency values use the 29 successful trials. Provider token values come from Moonshot usage fields.

## Failure Analysis

The only failed trial was `fix_inventory_3`. The agent successfully called `find_files` and `read_file`, then the provider request failed with DNS error `getaddrinfo failed`. This is classified as a provider failure rather than a checker failure.

The three invalid tool calls were `bash` verification commands. The agent recovered and passed their tasks. The 15 repeated calls consisted of 12 repeated `read_file` calls and 3 repeated `bash` calls, indicating an opportunity to improve tool-use efficiency.

## Interpretation

- All 10 tasks succeeded at least once, producing 100% `pass@3`.
- Nine tasks succeeded in all three trials, producing 90% `pass^3`.
- Deterministic Sim Eval confirms the harness, tool execution, trace, and checker paths across the full task set.
- Real Eval shows strong outcome reliability on this small benchmark, while repeated reads, verification retries, latency, and provider-network recovery remain optimization targets.
- The benchmark is project-specific and small; it is not comparable to SWE-bench or a production workload.
