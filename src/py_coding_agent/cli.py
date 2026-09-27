from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from .agent import Agent
from .config import Config
from .extensions import load_extensions
from .llm import LlmError
from .resources import load_prompts, load_skills, parse_key_values, render_prompt
from .session import Session


HELP = """Commands:
  /help             show this help
  /session          show current session file
  /tree             show indexed session messages
  /fork [index]     fork current session, optionally up to message index
  /todos            show current todo plan
  /memory [query]   search durable memories
  /tasks            list durable tasks
  /jobs             list background jobs
  /crons            list scheduled cron prompts
  /mcp              list connected MCP manifest tools
  /models           list supported Kimi models
  /model [name]     show or switch the current model
  /new              start a new session
  /continue         load the latest session
  /export [path]    export session to markdown
  /compact [keep]   compact older session messages with LLM summary
  /compact-raw [keep] compact older messages without LLM
  /skills           list skills
  /skill:name       load a skill from skills/name/SKILL.md
  /clear-skills     unload all skills
  /prompts          list prompt templates
  /prompt:name ...  expand prompts/name.md with key=value args
  /quit             exit
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="py-coding-agent")
    parser.add_argument("message", nargs="*", help="initial message")
    parser.add_argument("-p", "--print", action="store_true", help="print mode and exit")
    parser.add_argument("--workspace", default=".", help="workspace directory")
    parser.add_argument("--session", help="load or write a specific JSONL session file")
    parser.add_argument("-c", "--continue-session", action="store_true", help="continue latest session")
    parser.add_argument("--tools", help="comma-separated tools to enable")
    parser.add_argument("--no-confirm-edits", action="store_true", help="apply write/edit tool changes without diff confirmation")
    parser.add_argument("--no-confirm-bash", action="store_true", help="run bash tool commands without confirmation")
    parser.add_argument("--read-only", action="store_true", help="disable write/edit/bash tools")
    parser.add_argument("--no-stream", action="store_true", help="print final answers all at once")
    parser.add_argument("--auto-compact", action="store_true", help="compact old session messages automatically")
    parser.add_argument("--auto-compact-threshold", type=int, help="message count that triggers automatic compaction")
    parser.add_argument("--auto-compact-keep", type=int, help="recent message count kept after automatic compaction")
    parser.add_argument("--context-window-tokens", type=int, help="model context window in tokens")
    parser.add_argument("--max-output-tokens", type=int, help="tokens reserved for one model response")
    parser.add_argument("--context-trigger-ratio", type=float, help="fraction of the input budget that triggers L4 compaction")
    parser.add_argument("--context-safety-tokens", type=int, help="token safety reserve below the context window")
    parser.add_argument("--compact-max-messages", type=int, help="message count limit for L1 snip compaction")
    parser.add_argument("--keep-recent-tool-results", type=int, help="recent tool result count kept before L2 micro compaction")
    parser.add_argument("--tool-result-token-limit", type=int, help="tool result token count that triggers L3 persistence")
    parser.add_argument("--provider", choices=["openai", "kimi-coding"], help="provider to use")
    parser.add_argument("--model", help="override PY_CODING_AGENT_MODEL")
    return parser


def create_agent(args: argparse.Namespace) -> Agent:
    enabled_tools = [tool.strip() for tool in args.tools.split(",") if tool.strip()] if args.tools else None
    config = Config.from_env(
        Path(args.workspace),
        provider=args.provider,
        model=args.model,
        confirm_edits=False if args.no_confirm_edits else None,
        confirm_bash=False if args.no_confirm_bash else None,
        read_only=True if args.read_only else None,
        stream_output=False if args.no_stream else None,
        auto_compact=True if args.auto_compact else None,
        auto_compact_threshold=args.auto_compact_threshold,
        auto_compact_keep=args.auto_compact_keep,
        context_window_tokens=args.context_window_tokens,
        max_output_tokens=args.max_output_tokens,
        context_trigger_ratio=args.context_trigger_ratio,
        context_safety_tokens=args.context_safety_tokens,
        compact_max_messages=args.compact_max_messages,
        keep_recent_tool_results=args.keep_recent_tool_results,
        tool_result_token_limit=args.tool_result_token_limit,
        enabled_tools=enabled_tools,
    )
    if args.session:
        session = Session.load(Path(args.session).resolve())
    elif args.continue_session:
        session = Session.latest(config.session_dir)
    else:
        session = Session.create(config.session_dir)
    agent = Agent(config, session, confirm=confirm_diff, confirm_command=confirm_command, skills=load_skills(config.workspace))
    load_extensions(agent, config.workspace / "extensions")
    return agent


def confirm_diff(diff: str) -> bool:
    if threading.current_thread() is not threading.main_thread():
        print("diff confirmation denied: scheduled turns cannot request interactive approval")
        return False
    print(diff)
    answer = input("Apply this change? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def confirm_command(command: str) -> bool:
    if threading.current_thread() is not threading.main_thread():
        print("command confirmation denied: scheduled turns cannot request interactive approval")
        return False
    print(command)
    answer = input("Run this command? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def run_prompt(agent: Agent, text: str) -> int:
    try:
        answer = agent.prompt(text)
    except LlmError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if answer and agent.config.stream_output:
        for char in answer:
            print(char, end="", flush=True)
            time.sleep(0.001)
        print()
    elif answer:
        print(answer)
    return 0


def interactive(agent: Agent) -> int:
    skills = agent.available_skills
    prompts = load_prompts(agent.config.workspace)
    turn_lock = threading.Lock()
    stop_event = threading.Event()

    def cron_scheduler_loop() -> None:
        while not stop_event.wait(1.0):
            due = agent.runtime.poll_due_crons()
            for cron in due:
                print(f"\n[cron] due {cron.id}: {cron.prompt[:80]}")

    def cron_queue_loop() -> None:
        while not stop_event.wait(0.2):
            if not agent.runtime.has_cron_queue():
                continue
            if not turn_lock.acquire(blocking=False):
                continue
            jobs = []
            try:
                jobs = agent.runtime.consume_cron_queue()
                for job in jobs:
                    print(f"\n[cron] delivered {job.id}: {job.prompt[:80]}")
                    answer = agent.prompt(f"[Scheduled] {job.prompt}")
                    if answer:
                        print(answer)
                    print()
                agent.runtime.acknowledge_cron_jobs(jobs)
            except LlmError as exc:
                if jobs:
                    agent.runtime.restore_cron_jobs(jobs)
                print(f"scheduled cron failed: {exc}", file=sys.stderr)
            finally:
                turn_lock.release()

    scheduler_thread = threading.Thread(target=cron_scheduler_loop, name="py-coding-agent-cron-scheduler", daemon=True)
    queue_thread = threading.Thread(target=cron_queue_loop, name="py-coding-agent-cron-queue", daemon=True)
    scheduler_thread.start()
    queue_thread.start()
    print(f"py-coding-agent using {agent.config.provider}/{agent.config.model}")
    print(
        f"context: {agent.config.context_window_tokens:,} tokens; "
        f"max output: {agent.config.max_output_tokens:,} tokens"
    )
    print(f"workspace: {agent.config.workspace}")
    print("type /help for commands")
    try:
        while True:
            try:
                text = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            if text == "/quit":
                return 0
            if text == "/help":
                print(HELP)
                continue
            if text == "/session":
                print(agent.session.path)
                continue
            if text == "/todos":
                print(agent.runtime.todo_read())
                continue
            if text.startswith("/memory"):
                parts = text.split(maxsplit=1)
                query = parts[1] if len(parts) > 1 else ""
                print(agent.runtime.search_memory(query=query))
                continue
            if text == "/tasks":
                print(agent.runtime.list_tasks())
                continue
            if text == "/jobs":
                print(agent.runtime.list_background_jobs())
                continue
            if text == "/crons":
                print(agent.runtime.list_crons())
                continue
            if text == "/mcp":
                print(agent.runtime.list_mcp_tools())
                continue
            if text == "/models":
                print(agent.list_models())
                continue
            if text == "/model":
                print(
                    f"{agent.config.provider}/{agent.config.model}; "
                    f"context={agent.config.context_window_tokens:,} tokens; "
                    f"max_output={agent.config.max_output_tokens:,}"
                )
                continue
            if text.startswith("/model "):
                model = text.split(maxsplit=1)[1].strip().lower()
                with turn_lock:
                    print(agent.switch_model(model))
                continue
            if text == "/tree":
                print("\n".join(agent.session.tree_lines()))
                continue
            if text.startswith("/fork"):
                parts = text.split(maxsplit=1)
                upto = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                agent.session = agent.session.fork(agent.config.session_dir, upto=upto)
                print(agent.session.path)
                continue
            if text == "/new":
                agent.session = Session.create(agent.config.session_dir)
                print(agent.session.path)
                continue
            if text == "/continue":
                agent.session = Session.latest(agent.config.session_dir)
                print(agent.session.path)
                continue
            if text.startswith("/export"):
                parts = text.split(maxsplit=1)
                target = Path(parts[1]).resolve() if len(parts) > 1 else agent.session.path.with_suffix(".md")
                agent.session.export_markdown(target)
                print(target)
                continue
            if text.startswith("/compact"):
                parts = text.split(maxsplit=1)
                keep = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 8
                use_llm = not text.startswith("/compact-raw")
                try:
                    print(agent.compact_session(keep, use_llm=use_llm))
                except LlmError as exc:
                    print(f"LLM compact failed, using raw compaction: {exc}")
                    print(agent.compact_session(keep, use_llm=False))
                continue
            if text == "/skills":
                if not skills:
                    print("no skills found")
                else:
                    for skill in skills.values():
                        description = f" - {skill.description}" if skill.description else ""
                        print(f"{skill.name}{description}")
                continue
            if text.startswith("/skill:"):
                name = text.removeprefix("/skill:").strip()
                skill = skills.get(name)
                if skill is None:
                    print(f"skill not found: {name}")
                else:
                    agent.activate_skill(skill)
                    print(f"loaded skill: {name}")
                continue
            if text == "/clear-skills":
                agent.deactivate_skills()
                print("cleared skills")
                continue
            if text == "/prompts":
                print("\n".join(prompts) if prompts else "no prompts found")
                continue
            if text.startswith("/prompt:"):
                command = text.removeprefix("/prompt:").strip()
                parts = command.split()
                name = parts[0] if parts else ""
                prompt = prompts.get(name)
                if prompt is None:
                    print(f"prompt not found: {name}")
                    continue
                text = render_prompt(prompt.text, parse_key_values(parts[1:]))
            with turn_lock:
                code = run_prompt(agent, text)
            if code != 0:
                return code
    finally:
        stop_event.set()
        scheduler_thread.join(timeout=1)
        queue_thread.join(timeout=1)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    agent = create_agent(args)
    initial = " ".join(args.message).strip()
    if args.print or initial:
        if not initial:
            initial = sys.stdin.read().strip()
        if not initial:
            parser.error("print mode requires a message or stdin")
        return run_prompt(agent, initial)
    return interactive(agent)


if __name__ == "__main__":
    raise SystemExit(main())
