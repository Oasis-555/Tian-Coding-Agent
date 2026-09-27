from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpStdioClient:
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self._next_id = 1
        self._lock = threading.Lock()
        merged_env = {**os.environ, **self.env}
        self._process = subprocess.Popen(
            [self.command, *self.args],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=merged_env,
        )
        self._initialize()

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        if self._process.stdin is not None:
            self._process.stdin.close()
        if self._process.stdout is not None:
            self._process.stdout.close()

    def list_tools(self) -> list[McpTool]:
        result = self._request("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            return []
        parsed: list[McpTool] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            if not name:
                continue
            schema = item.get("inputSchema", {})
            parsed.append(
                McpTool(
                    name=name,
                    description=str(item.get("description", "")),
                    input_schema=schema if isinstance(schema, dict) else {},
                )
            )
        return parsed

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content", [])
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        chunks.append(str(item.get("text", "")))
                    else:
                        chunks.append(json.dumps(item, ensure_ascii=False))
                else:
                    chunks.append(str(item))
            text = "\n".join(chunk for chunk in chunks if chunk)
        else:
            text = json.dumps(content, ensure_ascii=False)
        if result.get("isError"):
            return f"Error from MCP tool {name}:\n{text}"
        return text

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "py-coding-agent", "version": "0.1.0"},
            },
        )
        self._notification("notifications/initialized", {})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                message = self._read()
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                result = message.get("result", {})
                return result if isinstance(result, dict) else {"result": result}

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise RuntimeError("MCP server stdin is closed")
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + body)
        self._process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        if self._process.stdout is None:
            raise RuntimeError("MCP server stdout is closed")
        headers: dict[str, str] = {}
        while True:
            line = self._process.stdout.readline()
            if not line:
                raise RuntimeError("MCP server exited")
            stripped = line.strip()
            if not stripped:
                break
            key, _, value = stripped.decode("ascii", errors="replace").partition(":")
            headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            raise RuntimeError("MCP response missing Content-Length")
        payload = self._process.stdout.read(length)
        return json.loads(payload.decode("utf-8"))
