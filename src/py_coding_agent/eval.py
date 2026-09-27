from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agent import Agent
from .config import Config
from .context import estimate_tokens
from .messages import Message, TokenUsage, ToolResult, ToolSpec
from .providers.base import ProviderError
from .session import Session


@dataclass
class EvalTask:
    id: str
    prompt: str
    checker: str = ""
    fixture: str = ""
    mock_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LlmCallTrace:
    duration_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    token_source: str
    tool_specs: int


@dataclass
class ToolCallTrace:
    name: str
    args: dict[str, Any]
    is_error: bool
    duration_ms: int
    repeated: bool = False
    changed_files: list[str] = field(default_factory=list)
    rolled_back_files: list[str] = field(default_factory=list)


@dataclass
class EvalRunTrace:
    task_id: str
    run_index: int
    success: bool
    mode: str
    duration_ms: int
    llm_calls: list[LlmCallTrace]
    tool_calls: list[ToolCallTrace]
    context_events: list[str]
    checker_command: str
    checker_exit_code: int | None
    checker_output: str
    final_answer: str
    error: str = ""
    failure_type: str = ""


class RequestRateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        self.requests_per_minute = max(0, requests_per_minute)
        self.timestamps: deque[float] = deque()

    def acquire(self) -> None:
        if self.requests_per_minute <= 0:
            return
        while True:
            now = time.monotonic()
            while self.timestamps and now - self.timestamps[0] >= 60:
                self.timestamps.popleft()
            if len(self.timestamps) < self.requests_per_minute:
                self.timestamps.append(now)
                return
            time.sleep(max(0.05, 60 - (now - self.timestamps[0]) + 0.1))


class MockEvalClient:
    def __init__(self, mock_calls: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self.mock_calls = mock_calls or []
        self.last_usage: TokenUsage | None = None

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        self.calls += 1
        last_user = _last_user_text(messages).lower()
        tool_names = {tool["function"]["name"] for tool in tools}
        if self.calls <= len(self.mock_calls):
            call = self.mock_calls[self.calls - 1]
            name = str(call.get("name", ""))
            arguments = call.get("arguments", {})
            if name not in tool_names or not isinstance(arguments, dict):
                result = {"role": "assistant", "content": "Mock action is unavailable.", "tool_calls": []}
            else:
                result = _tool_call(f"mock_call_{self.calls}", name, arguments)
        elif self.calls == 1 and "write_file" in tool_names:
            path = "random_number.py" if "random" in last_user else "answer.txt"
            content = (
                "import random\n\n"
                "def generate_random_number() -> int:\n"
                "    return random.randint(1, 100)\n\n"
                "if __name__ == \"__main__\":\n"
                "    print(generate_random_number())\n"
            )
            if path == "answer.txt":
                content = "done\n"
            result = _tool_call("call_write", "write_file", {"path": path, "content": content})
        else:
            result = {"role": "assistant", "content": "Task completed.", "tool_calls": []}
        input_tokens = self.count_input_tokens(messages, tools)
        output_tokens = estimate_tokens([result])
        self.last_usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            source="mock_estimate",
        )
        return result

    def count_input_tokens(self, messages: list[Message], tools: list[ToolSpec]) -> int:
        return estimate_tokens(messages, tools)


class TracingClient:
    def __init__(self, client: Any, trace: EvalRunTrace, rate_limiter: RequestRateLimiter | None = None) -> None:
        self.client = client
        self.trace = trace
        self.rate_limiter = rate_limiter
        self.last_usage: TokenUsage | None = None

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        if self.rate_limiter is not None:
            self.rate_limiter.acquire()
        started = time.perf_counter()
        result = self.client.complete(messages, tools)
        duration_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(self.client, "last_usage", None)
        if not isinstance(usage, TokenUsage):
            input_tokens = estimate_tokens(messages, tools)
            output_tokens = estimate_tokens([result])
            usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                source="local_estimate",
            )
        self.last_usage = usage
        self.trace.llm_calls.append(
            LlmCallTrace(
                duration_ms=duration_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                token_source=usage.source,
                tool_specs=len(tools),
            )
        )
        return result

    def count_input_tokens(self, messages: list[Message], tools: list[ToolSpec]) -> int:
        counter = getattr(self.client, "count_input_tokens", None)
        if callable(counter):
            return int(counter(messages, tools))
        return estimate_tokens(messages, tools)


def load_tasks(path: Path) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        tasks.append(EvalTask(**item))
    return tasks


def run_eval(
    tasks: list[EvalTask],
    workspace: Path,
    mode: str,
    k: int,
    results_dir: Path,
    requests_per_minute: int = 0,
) -> list[EvalRunTrace]:
    results_dir.mkdir(parents=True, exist_ok=True)
    base_config = Config.from_env(workspace=workspace) if mode == "live" else None
    rate_limiter = RequestRateLimiter(requests_per_minute) if mode == "live" else None
    traces: list[EvalRunTrace] = []
    for task in tasks:
        for run_index in range(1, k + 1):
            trace = run_one(
                task,
                workspace,
                mode,
                run_index,
                base_config=base_config,
                rate_limiter=rate_limiter,
            )
            traces.append(trace)
            output_path = results_dir / f"{task.id}_{run_index}.json"
            output_path.write_text(json.dumps(_to_json(trace), ensure_ascii=False, indent=2), encoding="utf-8")
    report = build_report(traces, k)
    (results_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return traces


def run_one(
    task: EvalTask,
    workspace: Path,
    mode: str,
    run_index: int,
    base_config: Config | None = None,
    rate_limiter: RequestRateLimiter | None = None,
) -> EvalRunTrace:
    with tempfile.TemporaryDirectory(prefix=f"py-coding-agent-eval-{task.id}-") as directory:
        run_workspace = Path(directory) / "workspace"
        _prepare_workspace(workspace, run_workspace, task.fixture)
        config = Config.from_env(
            workspace=run_workspace,
            provider=base_config.provider if base_config else None,
            model=base_config.model if base_config else None,
            base_url=base_config.base_url if base_config else None,
            api_key=base_config.api_key if base_config else "mock",
            confirm_edits=False,
            confirm_bash=False,
            stream_output=False,
            auto_compact=False,
        )
        session = Session.create(run_workspace / ".py-coding-agent" / "sessions")
        trace = EvalRunTrace(
            task_id=task.id,
            run_index=run_index,
            success=False,
            mode=mode,
            duration_ms=0,
            llm_calls=[],
            tool_calls=[],
            context_events=[],
            checker_command=task.checker,
            checker_exit_code=None,
            checker_output="",
            final_answer="",
        )
        started = time.perf_counter()
        try:
            agent = Agent(config, session, output=lambda _text: None)
            client = MockEvalClient(task.mock_calls) if mode == "mock" else agent.client
            agent.client = TracingClient(client, trace, rate_limiter=rate_limiter)
            _instrument_agent(agent, trace)
            trace.final_answer = agent.prompt(task.prompt)
            trace.success = _run_checker(task.checker, run_workspace, trace)
            if not trace.success:
                trace.failure_type = "checker"
        except ProviderError as exc:
            trace.error = f"{type(exc).__name__}: {exc}"
            trace.failure_type = "provider"
        except subprocess.TimeoutExpired as exc:
            trace.error = f"{type(exc).__name__}: {exc}"
            trace.failure_type = "checker_timeout"
        except Exception as exc:
            trace.error = f"{type(exc).__name__}: {exc}"
            trace.failure_type = "harness"
        finally:
            trace.duration_ms = int((time.perf_counter() - started) * 1000)
        return trace


def build_report(traces: list[EvalRunTrace], k: int) -> dict[str, Any]:
    by_task: dict[str, list[EvalRunTrace]] = defaultdict(list)
    for trace in traces:
        by_task[trace.task_id].append(trace)
    task_successes = {task_id: [trace.success for trace in runs] for task_id, runs in by_task.items()}
    total_runs = len(traces)
    successful_runs = sum(1 for trace in traces if trace.success)
    measured_traces = [trace for trace in traces if trace.success] or traces
    durations = [trace.duration_ms for trace in measured_traces]
    tool_calls = [call for trace in traces for call in trace.tool_calls]
    repeated = sum(1 for call in tool_calls if call.repeated)
    invalid = sum(1 for call in tool_calls if call.is_error)
    rollbacks = sum(len(call.rolled_back_files) for call in tool_calls)
    modifications = sum(len(call.changed_files) for call in tool_calls)
    failure_counts: dict[str, int] = defaultdict(int)
    for trace in traces:
        if not trace.success:
            failure_counts[trace.failure_type or "unknown"] += 1
    return {
        "tasks": len(by_task),
        "runs": total_runs,
        "k": k,
        "result": {
            "pass_rate": successful_runs / total_runs if total_runs else 0.0,
            "pass_at_k": sum(any(values) for values in task_successes.values()) / len(by_task) if by_task else 0.0,
            "pass_power_k": sum(all(values) for values in task_successes.values()) / len(by_task) if by_task else 0.0,
        },
        "efficiency": {
            "scope": "successful_runs" if successful_runs else "all_runs",
            "avg_input_tokens": _mean(sum(call.input_tokens for call in trace.llm_calls) for trace in measured_traces),
            "avg_output_tokens": _mean(sum(call.output_tokens for call in trace.llm_calls) for trace in measured_traces),
            "avg_total_tokens": _mean(sum(call.total_tokens for call in trace.llm_calls) for trace in measured_traces),
            "avg_cached_input_tokens": _mean(
                sum(call.cached_input_tokens for call in trace.llm_calls) for trace in measured_traces
            ),
            "avg_reasoning_tokens": _mean(
                sum(call.reasoning_tokens for call in trace.llm_calls) for trace in measured_traces
            ),
            "avg_duration_ms": _mean(durations),
            "p50_duration_ms": _percentile(durations, 50),
            "p95_duration_ms": _percentile(durations, 95),
            "avg_llm_calls": _mean(len(trace.llm_calls) for trace in measured_traces),
            "avg_tool_calls": _mean(len(trace.tool_calls) for trace in measured_traces),
        },
        "process": {
            "invalid_tool_call_rate": invalid / len(tool_calls) if tool_calls else 0.0,
            "repeated_tool_call_rate": repeated / len(tool_calls) if tool_calls else 0.0,
            "rollback_rate": rollbacks / modifications if modifications else 0.0,
            "compact_events": sum(len(trace.context_events) for trace in traces),
        },
        "failures": dict(sorted(failure_counts.items())),
    }


def _instrument_agent(agent: Agent, trace: EvalRunTrace) -> None:
    original_execute = agent.registry.execute
    seen_calls: set[str] = set()
    file_hashes = _file_hashes(agent.config.workspace)
    hash_history: dict[str, set[str]] = defaultdict(set)
    for path, digest in file_hashes.items():
        hash_history[path].add(digest)

    def execute(name: str, raw_arguments: str) -> ToolResult:
        try:
            args = json.loads(raw_arguments or "{}")
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        signature = json.dumps({"name": name, "args": args}, ensure_ascii=False, sort_keys=True)
        repeated = signature in seen_calls
        seen_calls.add(signature)
        before = dict(file_hashes)
        started = time.perf_counter()
        result = original_execute(name, raw_arguments)
        duration_ms = int((time.perf_counter() - started) * 1000)
        after = _file_hashes(agent.config.workspace)
        changed = sorted(path for path, digest in after.items() if before.get(path) != digest)
        rolled_back = [path for path in changed if after[path] in hash_history[path]]
        for path, digest in after.items():
            hash_history[path].add(digest)
        file_hashes.clear()
        file_hashes.update(after)
        trace.tool_calls.append(
            ToolCallTrace(
                name=name,
                args=args,
                is_error=bool(result.get("is_error")),
                duration_ms=duration_ms,
                repeated=repeated,
                changed_files=changed,
                rolled_back_files=rolled_back,
            )
        )
        return result

    agent.registry.execute = execute  # type: ignore[method-assign]

    original_output = agent.output

    def output(text: str) -> None:
        if text.startswith("[context] "):
            events = text.removeprefix("[context] ").split("; ")
            trace.context_events.extend(event for event in events if event)
        original_output(text)

    agent.output = output


def _prepare_workspace(base: Path, run_workspace: Path, fixture: str) -> None:
    if fixture:
        source = (base / fixture).resolve()
        shutil.copytree(source, run_workspace)
    else:
        run_workspace.mkdir(parents=True, exist_ok=True)


def _run_checker(command: str, cwd: Path, trace: EvalRunTrace) -> bool:
    if not command.strip():
        trace.checker_exit_code = 0
        trace.checker_output = "No checker configured."
        return True
    if command.startswith("python "):
        command = subprocess.list2cmdline([sys.executable]) + command[len("python") :]
        trace.checker_command = command
    completed = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True, timeout=120, check=False)
    trace.checker_exit_code = completed.returncode
    trace.checker_output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
    return completed.returncode == 0


def _file_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel.startswith(".py-coding-agent/") or rel.startswith(".memory/"):
            continue
        try:
            hashes[rel] = str(hash(path.read_bytes()))
        except OSError:
            continue
    return hashes


def _tool_call(tool_call_id: str, name: str, args: dict[str, Any]) -> Message:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


def _last_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _mean(values: Any) -> float:
    items = list(values)
    return float(sum(items) / len(items)) if items else 0.0


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = round((percentile / 100) * (len(ordered) - 1))
    return ordered[index]


def _to_json(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _to_json(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_json(item) for key, item in value.items()}
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="py-coding-agent-eval")
    parser.add_argument("--tasks", default="evals/tasks.jsonl", help="JSONL task file")
    parser.add_argument("--workspace", default=".", help="base workspace for fixtures")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock", help="eval mode")
    parser.add_argument("-k", type=int, default=1, help="runs per task")
    parser.add_argument("--results-dir", default="evals/results", help="directory for traces and report")
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        default=0,
        help="shared live-model request limit; 0 disables pacing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tasks = load_tasks(Path(args.tasks).resolve())
    traces = run_eval(
        tasks,
        Path(args.workspace).resolve(),
        args.mode,
        max(1, args.k),
        Path(args.results_dir).resolve(),
        requests_per_minute=max(0, args.requests_per_minute),
    )
    report = build_report(traces, max(1, args.k))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(trace.success for trace in traces) else 1


if __name__ == "__main__":
    raise SystemExit(main())
