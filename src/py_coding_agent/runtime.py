from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .mcp_client import McpStdioClient


@dataclass
class TaskRecord:
    id: str
    subject: str
    description: str
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    worktree: str | None = None


@dataclass
class BackgroundJob:
    id: str
    command: str
    status: str = "running"
    exit_code: int | None = None
    output: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


@dataclass
class CronRecord:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = True
    enabled: bool = True
    pending_delivery: bool = False
    last_fired: str | None = None


@dataclass
class ProtocolRequest:
    id: str
    type: str
    sender: str
    target: str
    payload: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)


@dataclass
class MemoryRecord:
    name: str
    description: str
    type: str
    body: str
    filename: str
    mtime: float
    fact_key: str = ""
    source: str = "user_explicit"
    confidence: float = 1.0
    status: str = "active"
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str = ""
    created_at: str = ""
    updated_at: str = ""


MEMORY_TYPES = {"user", "feedback", "project", "reference"}
MEMORY_SOURCES = {"user_explicit", "llm_extracted", "assistant_observed", "imported"}
MEMORY_STATUSES = {"active", "pending", "archived", "conflicted", "rejected"}


class TodoManager:
    def __init__(self, max_items: int = 20) -> None:
        self.max_items = max_items
        self.items: list[dict[str, str]] = []

    def update(self, todos: list[dict[str, Any]]) -> str:
        if len(todos) > self.max_items:
            return f"Error: todos must contain at most {self.max_items} items"
        normalized: list[dict[str, str]] = []
        in_progress = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return f"Error: todos[{index}] must be an object"
            content = str(todo.get("content", "")).strip()
            status = str(todo.get("status", "pending"))
            if not content:
                return f"Error: todos[{index}] missing content"
            if status not in {"pending", "in_progress", "completed"}:
                return f"Error: todos[{index}] has invalid status: {status}"
            if status == "in_progress":
                in_progress += 1
            normalized.append({"content": content, "status": status})
        if in_progress > 1:
            return "Error: only one todo can be in_progress at a time"
        self.items = normalized
        return f"Updated {len(self.items)} todos"

    def render(self) -> str:
        if not self.items:
            return "No todos."
        return "\n".join(f"- [{todo['status']}] {todo['content']}" for todo in self.items)


class HarnessRuntime:
    def __init__(self, workspace: Path, output=print) -> None:
        self.workspace = workspace.resolve()
        self.output = output
        self.root = self.workspace / ".py-coding-agent"
        self.tasks_dir = self.root / "tasks"
        self.jobs_dir = self.root / "background"
        self.mailbox_dir = self.root / "mailboxes"
        self.worktree_dir = self.root / "worktrees"
        self.mcp_dir = self.root / "mcp"
        self.memory_dir = self.workspace / ".memory"
        for path in [self.tasks_dir, self.jobs_dir, self.mailbox_dir, self.worktree_dir, self.mcp_dir, self.memory_dir]:
            path.mkdir(parents=True, exist_ok=True)
        self.todo_manager = TodoManager()
        self.background_jobs: dict[str, BackgroundJob] = {}
        self.protocol_requests: dict[str, ProtocolRequest] = {}
        self._task_lock = threading.Lock()
        self._cron_lock = threading.RLock()
        self._cron_queue: list[CronRecord] = []
        self.crons = self._load_crons()
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        self.mcp_stdio_clients: dict[str, McpStdioClient] = {}
        self.mcp_stdio_tools: dict[str, list[dict[str, Any]]] = {}

    def todo_write(self, todos: list[dict[str, Any]]) -> str:
        return self.todo_manager.update(todos)

    def todo_read(self) -> str:
        return self.todo_manager.render()

    def remember(self, text: str, category: str = "general", fact_key: str = "") -> str:
        mem_type = category if category in MEMORY_TYPES else "reference"
        description = text.strip().replace("\n", " ")
        if len(description) > 120:
            description = description[:117] + "..."
        name = f"{mem_type}-{_slugify(description) or int(time.time())}"
        return self.write_memory_file(
            name,
            mem_type,
            description or "Durable memory",
            text,
            fact_key=fact_key,
            source="user_explicit",
            confidence=1.0,
            status="active",
        )

    def search_memory(self, query: str = "", limit: int = 10) -> str:
        records = self.select_memories(query, limit=limit, include_body=True)
        if not records:
            return "No matching memories."
        return self.render_memories(records, max_chars=12000)

    def relevant_memories(self, query: str = "", limit: int = 5) -> str:
        records = self.select_memories(query, limit=limit, include_body=True)
        if not records:
            return "No matching memories."
        return self.render_memories(records, max_chars=12000)

    def write_memory_file(
        self,
        name: str,
        mem_type: str,
        description: str,
        body: str,
        fact_key: str = "",
        source: str = "user_explicit",
        confidence: float = 1.0,
        status: str = "active",
        supersedes: list[str] | None = None,
    ) -> str:
        mem_type = mem_type.strip().lower()
        if mem_type not in MEMORY_TYPES:
            return f"Error: invalid memory type: {mem_type}"
        slug = _slugify(name)
        if not slug:
            return "Error: memory name must contain letters or numbers"
        source = source.strip().lower()
        if source not in MEMORY_SOURCES:
            source = "llm_extracted"
        status = status.strip().lower()
        if status not in MEMORY_STATUSES:
            status = "pending" if source != "user_explicit" else "active"
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.6 if source != "user_explicit" else 1.0
        fact_key = _slugify(fact_key or name, max_len=80)
        supersedes = [_memory_ref(item) for item in (supersedes or []) if _memory_ref(item)]
        filename = f"{slug}.md"
        path = self.memory_dir / filename

        duplicate = self._find_duplicate_memory(slug, description, body, fact_key)
        if duplicate is not None:
            return f"Memory skipped: duplicate of {duplicate.filename}"

        related_active = [
            record
            for record in self.list_memory_files(include_inactive=True)
            if record.status == "active" and record.fact_key and record.fact_key == fact_key and record.filename != filename
        ]
        if related_active and source == "user_explicit" and status == "active":
            supersedes = sorted(set([*supersedes, *[record.filename for record in related_active]]))
            for record in related_active:
                self._update_memory_status(record, "archived", superseded_by=filename)
        elif related_active and source != "user_explicit" and status == "active":
            status = "conflicted"

        now = _now_iso()
        existing = self._read_memory_file(path) if path.exists() else None
        created_at = existing.created_at if existing else now
        text = _format_memory_file(
            MemoryRecord(
                name=slug,
                description=description,
                type=mem_type,
                body=body.strip(),
                filename=filename,
                mtime=time.time(),
                fact_key=fact_key,
                source=source,
                confidence=confidence,
                status=status,
                supersedes=supersedes,
                superseded_by="",
                created_at=created_at,
                updated_at=now,
            )
        )
        path.write_text(text, encoding="utf-8")
        self.rebuild_memory_index()
        suffix = f" [{status}]"
        if supersedes:
            suffix += f" supersedes={', '.join(supersedes)}"
        return f"Memory saved: {filename}{suffix}"

    def list_memory_files(self, include_inactive: bool = False) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            record = self._read_memory_file(path)
            if record is not None and (include_inactive or record.status == "active"):
                records.append(record)
        return sorted(records, key=lambda record: record.mtime, reverse=True)

    def memory_catalog(self, max_items: int = 200, max_chars: int = 25000) -> str:
        rows = [
            f"{index}: {record.filename} | {record.status} | {record.type} | {record.fact_key} | "
            f"{record.source} | {record.confidence:.2f} | {record.name} | {record.description}"
            for index, record in enumerate(self.list_memory_files()[:max_items])
        ]
        text = "\n".join(rows)
        return text[:max_chars]

    def memory_index(self, max_chars: int = 25000) -> str:
        self.rebuild_memory_index()
        if not self.list_memory_files():
            return "No memories."
        index_path = self.memory_dir / "MEMORY.md"
        if not index_path.exists():
            return "No memories."
        text = index_path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + "\n\n[MEMORY.md truncated by budget]"
        return text

    def select_memories(self, query: str = "", limit: int = 5, include_body: bool = True, filenames: list[str] | None = None) -> list[MemoryRecord]:
        records = self.list_memory_files()
        if filenames is not None:
            wanted = {Path(filename).name for filename in filenames}
            records = [record for record in records if record.filename in wanted]
            return records[:limit]
        query_lower = query.lower().strip()
        if not query_lower:
            return records[:limit]
        terms = [term for term in re.split(r"\W+", query_lower) if term]

        def score(record: MemoryRecord) -> int:
            haystack = " ".join([record.name, record.description, record.type, record.body]).lower()
            return sum(1 for term in terms if term in haystack)

        ranked = [(score(record), record) for record in records]
        selected = [record for item_score, record in sorted(ranked, key=lambda item: (item[0], item[1].mtime), reverse=True) if item_score > 0]
        return selected[:limit]

    def render_memories(self, records: list[MemoryRecord], max_chars: int = 12000, per_memory_chars: int = 4096) -> str:
        rows: list[str] = []
        used = 0
        for record in records:
            body = record.body.strip()
            if len(body) > per_memory_chars:
                body = body[:per_memory_chars] + "\n[truncated by per-memory budget]"
            block = (
                f"## {record.name}\n"
                f"- file: {record.filename}\n"
                f"- type: {record.type}\n"
                f"- fact_key: {record.fact_key}\n"
                f"- source: {record.source}\n"
                f"- confidence: {record.confidence:.2f}\n"
                f"- status: {record.status}\n"
                f"- description: {record.description}\n\n"
                f"{body}"
            )
            if used + len(block) > max_chars:
                rows.append("[memory injection truncated by total budget]")
                break
            rows.append(block)
            used += len(block)
        return "\n\n".join(rows) if rows else "No matching memories."

    def rebuild_memory_index(self) -> None:
        records = self.list_memory_files(include_inactive=True)
        rows = ["# MEMORY", ""]
        for record in records:
            if record.status != "active":
                rows.append(
                    f"- [{record.name}]({record.filename}) — {record.description} "
                    f"({record.type}, {record.status}, fact_key={record.fact_key})"
                )
                continue
            rows.append(
                f"- [{record.name}]({record.filename}) — {record.description} "
                f"({record.type}, active, fact_key={record.fact_key}, source={record.source}, confidence={record.confidence:.2f})"
            )
        text = "\n".join(rows).strip() + "\n"
        if len(text) > 25000:
            text = text[:25000] + "\n\n[MEMORY.md truncated by index budget]\n"
        (self.memory_dir / "MEMORY.md").write_text(text, encoding="utf-8")

    def replace_memories(self, memories: list[dict[str, str]]) -> str:
        for path in self.memory_dir.glob("*.md"):
            path.unlink()
        saved = 0
        for item in memories:
            name = str(item.get("name", "")).strip()
            mem_type = str(item.get("type", "reference")).strip()
            description = str(item.get("description", "")).strip()
            body = str(item.get("body", "")).strip()
            if name and description and body and mem_type in MEMORY_TYPES:
                self.write_memory_file(
                    name,
                    mem_type,
                    description,
                    body,
                    fact_key=str(item.get("fact_key", name)).strip(),
                    source=str(item.get("source", "llm_extracted")).strip(),
                    confidence=float(item.get("confidence", 0.8)),
                    status=str(item.get("status", "active")).strip(),
                    supersedes=[str(value) for value in item.get("supersedes", [])] if isinstance(item.get("supersedes", []), list) else [],
                )
                saved += 1
        self.rebuild_memory_index()
        return f"Consolidated {saved} memories"

    def create_task(self, subject: str, description: str = "", blocked_by: list[str] | None = None) -> str:
        task = TaskRecord(
            id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
            subject=subject,
            description=description,
            blocked_by=blocked_by or [],
        )
        self._save_task(task)
        return json.dumps(asdict(task), ensure_ascii=False, indent=2)

    def list_tasks(self) -> str:
        tasks = self._load_tasks()
        if not tasks:
            return "No tasks."
        return "\n".join(
            f"{task.id}: {task.subject} [{task.status}]"
            + (f" owner={task.owner}" if task.owner else "")
            + (f" blocked_by={task.blocked_by}" if task.blocked_by else "")
            + (f" worktree={task.worktree}" if task.worktree else "")
            for task in tasks
        )

    def get_task(self, task_id: str) -> str:
        return json.dumps(asdict(self._load_task(task_id)), ensure_ascii=False, indent=2)

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        with self._task_lock:
            task = self._load_task(task_id)
            if task.status != "pending":
                return f"Task {task_id} is {task.status}, cannot claim"
            if task.owner:
                return f"Task {task_id} is already owned by {task.owner}"
            blocked = [dep for dep in task.blocked_by if not self._task_completed(dep)]
            if blocked:
                return f"Task {task_id} is blocked by {blocked}"
            task.status = "in_progress"
            task.owner = owner
            self._save_task(task)
        return f"Claimed {task_id}"

    def complete_task(self, task_id: str) -> str:
        task = self._load_task(task_id)
        task.status = "completed"
        self._save_task(task)
        unblocked = [task.subject for task in self._load_tasks() if task.status == "pending" and self._can_start(task)]
        suffix = f"\nUnblocked: {', '.join(unblocked)}" if unblocked else ""
        return f"Completed {task_id}{suffix}"

    def start_background_command(self, command: str, timeout: int = 120) -> str:
        job = BackgroundJob(id=f"job_{int(time.time())}_{random.randint(0, 9999):04d}", command=command)
        self.background_jobs[job.id] = job
        self._save_job(job)

        def run() -> None:
            try:
                completed = subprocess.run(
                    command,
                    cwd=self.workspace,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
                job.exit_code = completed.returncode
                job.output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
                job.status = "completed" if completed.returncode == 0 else "failed"
            except subprocess.TimeoutExpired as exc:
                job.status = "timeout"
                job.exit_code = None
                job.output = str(exc)
            finally:
                job.finished_at = time.time()
                self._save_job(job)
                self.output(f"[background] {job.id} {job.status}")

        threading.Thread(target=run, daemon=True).start()
        return f"Started background job {job.id}"

    def list_background_jobs(self) -> str:
        jobs = self._load_jobs()
        if not jobs:
            return "No background jobs."
        return "\n".join(f"{job.id}: {job.command} [{job.status}]" for job in jobs)

    def read_background_job(self, job_id: str) -> str:
        job = self._load_job(job_id)
        return json.dumps(asdict(job), ensure_ascii=False, indent=2)

    def schedule_cron(self, cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> str:
        error = validate_cron(cron)
        if error:
            return f"Error: {error}"
        if not prompt.strip():
            return "Error: prompt cannot be empty"
        with self._cron_lock:
            record = CronRecord(
                id=f"cron_{random.randint(0, 99999999):08d}",
                cron=cron,
                prompt=prompt,
                recurring=recurring,
                durable=durable,
            )
            while any(item.id == record.id for item in self.crons):
                record.id = f"cron_{random.randint(0, 99999999):08d}"
            self.crons.append(record)
            self._save_crons()
        return f"Scheduled {record.id}: {cron} -> {prompt}"

    def list_crons(self) -> str:
        with self._cron_lock:
            crons = list(self.crons)
        if not crons:
            return "No crons."
        rows = []
        for cron in crons:
            frequency = "recurring" if cron.recurring else "one-shot"
            storage = "durable" if cron.durable else "session"
            pending = ", pending" if cron.pending_delivery else ""
            rows.append(f"{cron.id}: {cron.cron} -> {cron.prompt[:80]} [{frequency}, {storage}, enabled={cron.enabled}{pending}]")
        return "\n".join(rows)

    def cancel_cron(self, cron_id: str) -> str:
        with self._cron_lock:
            for cron in self.crons:
                if cron.id == cron_id:
                    cron.enabled = False
                    self._cron_queue = [queued for queued in self._cron_queue if queued.id != cron_id]
                    self._save_crons()
                    return f"Cancelled {cron_id}"
        return f"Cron not found: {cron_id}"

    def poll_due_crons(self, moment: datetime | None = None) -> list[CronRecord]:
        moment = moment or datetime.now()
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        due: list[CronRecord] = []
        with self._cron_lock:
            for cron in self.crons:
                if not cron.enabled or cron.pending_delivery or cron.last_fired == minute_marker:
                    continue
                if cron_matches(cron.cron, moment):
                    cron.pending_delivery = True
                    cron.last_fired = minute_marker
                    self._cron_queue.append(cron)
                    due.append(cron)
            if due:
                self._save_crons()
        return due

    def has_cron_queue(self) -> bool:
        with self._cron_lock:
            return bool(self._cron_queue)

    def consume_cron_queue(self) -> list[CronRecord]:
        with self._cron_lock:
            queued = list(self._cron_queue)
            self._cron_queue.clear()
            return queued

    def acknowledge_cron_jobs(self, jobs: list[CronRecord]) -> None:
        with self._cron_lock:
            changed = False
            for delivered in jobs:
                current = self._find_cron(delivered.id)
                if current is None:
                    continue
                if current.recurring:
                    current.pending_delivery = False
                else:
                    self.crons = [cron for cron in self.crons if cron.id != current.id]
                changed = True
            if changed:
                self._save_crons()

    def restore_cron_jobs(self, jobs: list[CronRecord]) -> None:
        with self._cron_lock:
            queued_ids = {cron.id for cron in self._cron_queue}
            changed = False
            for delivered in jobs:
                current = self._find_cron(delivered.id)
                if current is None:
                    continue
                current.pending_delivery = True
                if current.id not in queued_ids:
                    self._cron_queue.append(current)
                    queued_ids.add(current.id)
                changed = True
            if changed:
                self._save_crons()

    def due_cron_prompts(self) -> list[str]:
        due = self.poll_due_crons()
        jobs = self.consume_cron_queue()
        self.acknowledge_cron_jobs(jobs)
        return [f"[cron {job.id}] {job.prompt}" for job in due]

    def _find_cron(self, cron_id: str) -> CronRecord | None:
        for cron in self.crons:
            if cron.id == cron_id:
                return cron
        return None

    def _load_crons(self) -> list[CronRecord]:
        path = self.root / "crons.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        crons: list[CronRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                if "cron" not in item and "interval_seconds" in item:
                    interval = max(1, int(item.get("interval_seconds", 60)))
                    cron_expr = f"*/{interval} * * * *" if interval <= 59 else "* * * * *"
                    item = {
                        "id": str(item.get("id", f"cron_{random.randint(0, 99999999):08d}")),
                        "cron": cron_expr,
                        "prompt": str(item.get("prompt", "")),
                        "recurring": True,
                        "durable": True,
                        "enabled": bool(item.get("enabled", True)),
                        "pending_delivery": False,
                        "last_fired": None,
                    }
                record = CronRecord(**item)
                if validate_cron(record.cron) or not record.prompt.strip():
                    continue
                crons.append(record)
                if record.pending_delivery:
                    self._cron_queue.append(record)
            except (TypeError, ValueError):
                continue
        return crons

    def _save_crons(self) -> None:
        payload = [asdict(cron) for cron in self.crons if cron.durable]
        path = self.root / "crons.json"
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def spawn_teammate(self, name: str, role: str = "agent") -> str:
        self._safe_name(name)
        profile = {"name": name, "role": role, "status": "idle", "ts": time.time()}
        (self.root / "teammates.jsonl").parent.mkdir(parents=True, exist_ok=True)
        self._append_jsonl(self.root / "teammates.jsonl", profile)
        return f"Teammate {name} registered as {role}"

    def send_message(
        self,
        to: str,
        content: str,
        from_agent: str = "lead",
        msg_type: str = "message",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._safe_name(to)
        message = {
            "from": from_agent,
            "to": to,
            "type": msg_type,
            "content": content,
            "metadata": metadata or {},
            "ts": time.time(),
        }
        self._append_jsonl(self.mailbox_dir / f"{to}.jsonl", message)
        return f"Sent message to {to}"

    def check_inbox(self, agent: str = "lead", consume: bool = True) -> str:
        self._safe_name(agent)
        path = self.mailbox_dir / f"{agent}.jsonl"
        messages = self._read_jsonl(path)
        if consume and path.exists():
            path.unlink()
        if not messages:
            return "Inbox empty."
        return json.dumps(messages, ensure_ascii=False, indent=2)

    def consume_inbox(self, agent: str = "lead", route_protocol: bool = True) -> list[dict[str, Any]]:
        self._safe_name(agent)
        path = self.mailbox_dir / f"{agent}.jsonl"
        messages = self._read_jsonl(path)
        if path.exists():
            path.unlink()
        if route_protocol:
            for message in messages:
                self.dispatch_protocol_message(agent, message)
        return messages

    def dispatch_protocol_message(self, agent: str, message: dict[str, Any]) -> None:
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        request_id = str(metadata.get("request_id", ""))
        msg_type = str(message.get("type", "message"))
        if not request_id:
            return
        if msg_type in {"shutdown_response", "plan_approval_response"}:
            approve = bool(metadata.get("approve", True))
            self.match_protocol_response(request_id, msg_type, approve)

    def match_protocol_response(self, request_id: str, response_type: str, approve: bool) -> str:
        request = self.protocol_requests.get(request_id)
        if request is None:
            return f"Unknown protocol request: {request_id}"
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }.get(request.type)
        if response_type != expected:
            return f"Protocol type mismatch: expected {expected}, got {response_type}"
        if request.status not in {"pending", "submitted"}:
            return f"Protocol request {request_id} is already {request.status}"
        request.status = "approved" if approve else "rejected"
        return f"Protocol request {request_id} {request.status}"

    def request_shutdown(self, teammate: str) -> str:
        request = self._new_protocol("shutdown", "lead", teammate, "Please shut down.")
        self.send_message(teammate, request.payload, msg_type="shutdown_request", metadata={"request_id": request.id})
        return f"Shutdown request {request.id} sent"

    def request_plan(self, teammate: str, task: str) -> str:
        request = self._new_protocol("plan_approval", teammate, "lead", task)
        self.send_message(teammate, f"Submit a plan for: {task}", msg_type="plan_request", metadata={"request_id": request.id})
        return f"Plan request {request.id} sent"

    def review_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        request = self.protocol_requests.get(request_id)
        if request is None:
            return f"Request not found: {request_id}"
        request.status = "approved" if approve else "rejected"
        self.send_message(
            request.sender,
            feedback or request.status,
            from_agent="lead",
            msg_type="plan_approval_response",
            metadata={"request_id": request_id, "approve": approve},
        )
        return f"Request {request_id} {request.status}"

    def submit_plan(self, request_id: str, teammate: str, plan: str) -> str:
        request = self.protocol_requests.get(request_id)
        if request is None:
            return f"Request not found: {request_id}"
        if request.type != "plan_approval":
            return f"Request {request_id} is not a plan approval request"
        if request.sender != teammate:
            return f"Request {request_id} belongs to {request.sender}, not {teammate}"
        request.payload = plan
        request.status = "submitted"
        self.send_message(
            "lead",
            f"Plan from {teammate}:\n{plan}",
            from_agent=teammate,
            msg_type="plan_approval_request",
            metadata={"request_id": request_id},
        )
        return f"Submitted plan {request_id}"

    def autonomous_claim(self, owner: str = "agent") -> str:
        task = self.first_claimable_task()
        if task is not None:
            return self.claim_task(task.id, owner=owner)
        return "No claimable tasks."

    def first_claimable_task(self) -> TaskRecord | None:
        with self._task_lock:
            for task in self._load_tasks():
                if task.status == "pending" and not task.owner and self._can_start(task):
                    return task
        return None

    def worktree_path_for_task(self, task_id: str) -> Path | None:
        task = self._load_task(task_id)
        if not task.worktree:
            return None
        path = (self.worktree_dir / task.worktree).resolve()
        if path.exists():
            return path
        return None

    def create_worktree(self, name: str, task_id: str | None = None) -> str:
        self._safe_name(name)
        path = self.worktree_dir / name
        if path.exists():
            return f"Worktree exists: {path}"
        ok, output = self._run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
        if not ok:
            path.mkdir(parents=True, exist_ok=True)
            self._append_jsonl(self.worktree_dir / "events.jsonl", {"type": "create_directory", "worktree": name, "task_id": task_id, "ts": time.time()})
        else:
            self._append_jsonl(self.worktree_dir / "events.jsonl", {"type": "create", "worktree": name, "task_id": task_id, "ts": time.time()})
        if task_id:
            task = self._load_task(task_id)
            task.worktree = name
            self._save_task(task)
        suffix = "" if ok else f"\nGit worktree unavailable, created directory fallback:\n{output}"
        return f"Created isolated worktree directory {path}{suffix}"

    def remove_worktree(self, name: str, discard_changes: bool = False) -> str:
        self._safe_name(name)
        path = self.worktree_dir / name
        if not path.exists():
            return f"Worktree not found: {name}"
        if self._has_uncommitted_changes(path) and not discard_changes:
            return "Worktree has uncommitted changes. Pass discard_changes=true to remove, or keep_worktree."
        ok, output = self._run_git(["worktree", "remove", str(path), "--force"])
        if not ok and path.exists():
            shutil.rmtree(path)
        self._run_git(["branch", "-D", f"wt/{name}"])
        self._append_jsonl(self.worktree_dir / "events.jsonl", {"type": "remove", "worktree": name, "ts": time.time()})
        return f"Removed worktree {name}"

    def keep_worktree(self, name: str) -> str:
        self._safe_name(name)
        path = self.worktree_dir / name
        if not path.exists():
            return f"Worktree not found: {name}"
        self._append_jsonl(self.worktree_dir / "events.jsonl", {"type": "keep", "worktree": name, "ts": time.time()})
        return f"Kept worktree {name} at {path}"

    def connect_mcp(self, name: str, manifest_path: str) -> str:
        self._safe_name(name)
        path = self._safe_workspace_path(manifest_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        tools = data.get("tools", {})
        if not isinstance(tools, dict):
            return "Error: MCP manifest must contain a tools object"
        self.mcp_servers[name] = {"path": str(path), "tools": tools}
        return f"Connected MCP manifest {name} with {len(tools)} tool(s)"

    def connect_mcp_stdio(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        self._safe_name(name)
        client = McpStdioClient(command=command, args=args or [], env=env or {}, cwd=str(self.workspace))
        tools = client.list_tools()
        old = self.mcp_stdio_clients.pop(name, None)
        if old is not None:
            old.close()
        self.mcp_stdio_clients[name] = client
        self.mcp_stdio_tools[name] = [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
            for tool in tools
        ]
        return f"Connected MCP stdio server {name} with {len(tools)} tool(s)"

    def list_mcp_tools(self) -> str:
        rows: list[str] = []
        for server, data in self.mcp_servers.items():
            for tool_name, tool in data.get("tools", {}).items():
                rows.append(f"mcp__{server}__{tool_name}: {tool.get('description', '')} [manifest]")
        for server, tools in self.mcp_stdio_tools.items():
            for tool in tools:
                rows.append(f"mcp__{server}__{tool['name']}: {tool.get('description', '')} [stdio]")
        return "\n".join(rows) if rows else "No MCP tools connected."

    def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        parts = tool_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return "Error: tool name must be mcp__server__tool"
        _, server, name = parts
        client = self.mcp_stdio_clients.get(server)
        if client is not None:
            return client.call_tool(name, arguments or {})
        data = self.mcp_servers.get(server)
        if data is None:
            return f"Error: MCP server not connected: {server}"
        tool = data.get("tools", {}).get(name)
        if tool is None:
            return f"Error: MCP tool not found: {tool_name}"
        command = tool.get("command")
        if not command:
            return json.dumps({"tool": tool_name, "arguments": arguments or {}}, ensure_ascii=False)
        env = {**os.environ, "MCP_TOOL_ARGUMENTS": json.dumps(arguments or {}, ensure_ascii=False)}
        completed = subprocess.run(
            str(command),
            cwd=self.workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=int(tool.get("timeout", 30)),
            env=env,
            check=False,
        )
        output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
        output += f"\n[exit code: {completed.returncode}]"
        return output

    def close_mcp(self) -> None:
        for client in self.mcp_stdio_clients.values():
            client.close()
        self.mcp_stdio_clients.clear()
        self.mcp_stdio_tools.clear()

    def _new_protocol(self, request_type: str, sender: str, target: str, payload: str) -> ProtocolRequest:
        request = ProtocolRequest(
            id=f"req_{random.randint(0, 999999):06d}",
            type=request_type,
            sender=sender,
            target=target,
            payload=payload,
        )
        self.protocol_requests[request.id] = request
        return request

    def _save_task(self, task: TaskRecord) -> None:
        (self.tasks_dir / f"{task.id}.json").write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_task(self, task_id: str) -> TaskRecord:
        return TaskRecord(**json.loads((self.tasks_dir / f"{task_id}.json").read_text(encoding="utf-8")))

    def _load_tasks(self) -> list[TaskRecord]:
        return [TaskRecord(**json.loads(path.read_text(encoding="utf-8"))) for path in sorted(self.tasks_dir.glob("task_*.json"))]

    def _task_completed(self, task_id: str) -> bool:
        path = self.tasks_dir / f"{task_id}.json"
        return path.exists() and self._load_task(task_id).status == "completed"

    def _can_start(self, task: TaskRecord) -> bool:
        return all(self._task_completed(task_id) for task_id in task.blocked_by)

    def _save_job(self, job: BackgroundJob) -> None:
        (self.jobs_dir / f"{job.id}.json").write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_job(self, job_id: str) -> BackgroundJob:
        in_memory = self.background_jobs.get(job_id)
        if in_memory is not None:
            return in_memory
        return BackgroundJob(**json.loads((self.jobs_dir / f"{job_id}.json").read_text(encoding="utf-8")))

    def _load_jobs(self) -> list[BackgroundJob]:
        jobs: dict[str, BackgroundJob] = {}
        for path in sorted(self.jobs_dir.glob("job_*.json")):
            job = BackgroundJob(**json.loads(path.read_text(encoding="utf-8")))
            jobs[job.id] = job
        jobs.update(self.background_jobs)
        return list(jobs.values())

    def _safe_workspace_path(self, raw_path: str) -> Path:
        path = (self.workspace / raw_path).resolve()
        path.relative_to(self.workspace)
        return path

    def _safe_name(self, name: str) -> None:
        if not name or len(name) > 64 or name in {".", ".."} or any(char in name for char in "\\/:*?\"<>|"):
            raise ValueError(f"invalid name: {name}")

    def _run_git(self, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd or self.workspace,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            return False, str(exc)
        output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
        return completed.returncode == 0, output

    def _has_uncommitted_changes(self, path: Path) -> bool:
        ok, output = self._run_git(["status", "--porcelain"], cwd=path)
        if ok:
            return bool(output.strip())
        return any(path.iterdir())

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _load_json_list(self, path: Path, cls):
        if not path.exists():
            return []
        return [cls(**item) for item in json.loads(path.read_text(encoding="utf-8"))]

    def _read_memory_file(self, path: Path) -> MemoryRecord | None:
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter: dict[str, Any] = {}
        body = text
        if text.startswith("---\n"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                _, raw_frontmatter, body = parts
            else:
                raw_frontmatter = ""
            for raw_line in raw_frontmatter.splitlines():
                if ":" not in raw_line:
                    continue
                key, value = raw_line.split(":", 1)
                frontmatter[key.strip()] = _parse_frontmatter_value(value.strip())
        name = frontmatter.get("name") or path.stem
        description = frontmatter.get("description") or (body.strip().splitlines()[0] if body.strip() else path.stem)
        mem_type = frontmatter.get("type", "reference")
        if mem_type not in MEMORY_TYPES:
            mem_type = "reference"
        source = str(frontmatter.get("source", "user_explicit"))
        if source not in MEMORY_SOURCES:
            source = "llm_extracted"
        status = str(frontmatter.get("status", "active"))
        if status not in MEMORY_STATUSES:
            status = "active"
        confidence = frontmatter.get("confidence", 1.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 1.0
        supersedes = frontmatter.get("supersedes", [])
        if not isinstance(supersedes, list):
            supersedes = [str(supersedes)] if supersedes else []
        return MemoryRecord(
            name=str(name),
            description=str(description),
            type=str(mem_type),
            body=body.strip(),
            filename=path.name,
            mtime=path.stat().st_mtime,
            fact_key=str(frontmatter.get("fact_key", "")),
            source=source,
            confidence=confidence,
            status=status,
            supersedes=[str(item) for item in supersedes],
            superseded_by=str(frontmatter.get("superseded_by", "")),
            created_at=str(frontmatter.get("created_at", "")),
            updated_at=str(frontmatter.get("updated_at", "")),
        )

    def _find_duplicate_memory(self, slug: str, description: str, body: str, fact_key: str) -> MemoryRecord | None:
        wanted_description = _normalize_memory_text(description)
        wanted_body = _normalize_memory_text(body)
        for record in self.list_memory_files(include_inactive=True):
            if record.name == slug or record.filename == f"{slug}.md":
                continue
            if record.status in {"archived", "rejected"}:
                continue
            same_fact = bool(fact_key and record.fact_key == fact_key)
            if same_fact and _normalize_memory_text(record.description) == wanted_description:
                return record
            if same_fact and _normalize_memory_text(record.body) == wanted_body:
                return record
        return None

    def _update_memory_status(self, record: MemoryRecord, status: str, superseded_by: str = "") -> None:
        if status not in MEMORY_STATUSES:
            return
        record.status = status
        record.superseded_by = superseded_by
        record.updated_at = _now_iso()
        (self.memory_dir / record.filename).write_text(_format_memory_file(record), encoding="utf-8")


def _slugify(text: str, max_len: int = 64) -> str:
    slug = re.sub(r"[^\w-]+", "-", text.lower(), flags=re.UNICODE).strip("-_")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:max_len].strip("-_")


def _frontmatter_scalar(text: str) -> str:
    return str(text).replace("\r", " ").replace("\n", " ").replace('"', "'").strip()


def _frontmatter_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _parse_frontmatter_value(text: str) -> Any:
    value = text.strip().strip('"').strip("'")
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value) if "." in value else int(value)
    return value


def _memory_ref(text: str) -> str:
    value = str(text).strip()
    if not value:
        return ""
    if value.endswith(".md"):
        return Path(value).name
    slug = _slugify(value)
    return f"{slug}.md" if slug else ""


def _normalize_memory_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _format_memory_file(record: MemoryRecord) -> str:
    return (
        "---\n"
        f"name: {record.name}\n"
        f"description: {_frontmatter_scalar(record.description)}\n"
        f"type: {record.type}\n"
        f"fact_key: {_frontmatter_scalar(record.fact_key)}\n"
        f"source: {record.source}\n"
        f"confidence: {record.confidence:.2f}\n"
        f"status: {record.status}\n"
        f"supersedes: {_frontmatter_list(record.supersedes)}\n"
        f"superseded_by: {_frontmatter_scalar(record.superseded_by)}\n"
        f"created_at: {_frontmatter_scalar(record.created_at)}\n"
        f"updated_at: {_frontmatter_scalar(record.updated_at)}\n"
        "---\n\n"
        f"{record.body.strip()}\n"
    )


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    cron_weekday = (moment.weekday() + 1) % 7
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        return False
    day_matches = _cron_field_matches(day, moment.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"invalid range: {field}"
        start_value = int(start)
        end_value = int(end)
        if start_value > end_value:
            return f"range start is greater than end: {field}"
        if start_value < minimum or end_value > maximum:
            return f"range {field} is outside [{minimum}-{maximum}]"
        return None
    if not field.isdigit():
        return f"invalid field: {field}"
    value = int(field)
    if value < minimum or value > maximum:
        return f"value {value} is outside [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"expected 5 fields, got {len(fields)}"
    field_rules = [
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 6),
    ]
    for field, (name, minimum, maximum) in zip(fields, field_rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None
