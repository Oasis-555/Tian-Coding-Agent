from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol

from .agent import Agent
from .tools import Tool


class ExtensionModule(Protocol):
    def register(self, api: "ExtensionAPI") -> None: ...


@dataclass
class ExtensionAPI:
    agent: Agent

    def register_tool(self, tool: Tool) -> None:
        self.agent.registry.register(tool)
        self.agent.refresh_tools()

    def add_system_prompt(self, text: str) -> None:
        self.agent.extra_system_prompts.append(text)


def load_extensions(agent: Agent, extension_dir: Path) -> list[str]:
    if not extension_dir.is_dir():
        return []
    loaded: list[str] = []
    for path in sorted(extension_dir.glob("*.py")):
        module = _load_module(path)
        register = getattr(module, "register", None)
        if callable(register):
            register(ExtensionAPI(agent))
            loaded.append(path.stem)
    return loaded


def _load_module(path: Path) -> ModuleType:
    name = f"py_coding_agent_extension_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load extension: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
