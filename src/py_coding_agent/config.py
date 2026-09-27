from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import model_profile


@dataclass(frozen=True)
class Config:
    provider: str
    model: str
    base_url: str
    api_key: str
    workspace: Path
    session_dir: Path
    max_steps: int
    confirm_edits: bool
    confirm_bash: bool
    read_only: bool
    stream_output: bool
    auto_compact: bool
    auto_compact_threshold: int
    auto_compact_keep: int
    context_window_tokens: int
    max_output_tokens: int
    context_trigger_ratio: float
    context_safety_tokens: int
    compact_max_messages: int
    keep_recent_tool_results: int
    tool_result_token_limit: int
    enabled_tools: list[str] | None

    @staticmethod
    def from_env(
        workspace: Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        session_dir: Path | None = None,
        max_steps: int | None = None,
        confirm_edits: bool | None = None,
        confirm_bash: bool | None = None,
        read_only: bool | None = None,
        stream_output: bool | None = None,
        auto_compact: bool | None = None,
        auto_compact_threshold: int | None = None,
        auto_compact_keep: int | None = None,
        context_window_tokens: int | None = None,
        max_output_tokens: int | None = None,
        context_trigger_ratio: float | None = None,
        context_safety_tokens: int | None = None,
        compact_max_messages: int | None = None,
        keep_recent_tool_results: int | None = None,
        tool_result_token_limit: int | None = None,
        enabled_tools: list[str] | None = None,
    ) -> "Config":
        root = (workspace or Path.cwd()).resolve()
        settings = _load_settings(root / "settings.json")
        env_file = _load_env_file(root / ".env")
        resolved_provider = provider or _setting(env_file, settings, "provider", "PY_CODING_AGENT_PROVIDER", "openai")
        if resolved_provider == "kimi-coding":
            default_model = "kimi-for-coding"
            resolved_base_url = base_url or _setting(
                env_file, settings, "baseUrl", "PY_CODING_AGENT_BASE_URL", "https://api.kimi.com/coding"
            )
            default_api_key = _secret(_setting(env_file, settings, "kimiApiKey", "KIMI_API_KEY", ""))
        else:
            default_model = "gpt-4o-mini"
            resolved_base_url = base_url or _setting(
                env_file,
                settings,
                "baseUrl",
                "PY_CODING_AGENT_BASE_URL",
                _setting(env_file, settings, "openaiBaseUrl", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
            )
            default_api_key = _secret(_setting(env_file, settings, "openaiApiKey", "OPENAI_API_KEY", ""))
        resolved_session_dir = session_dir or Path(
            _setting(
                env_file,
                settings,
                "sessionDir",
                "PY_CODING_AGENT_SESSION_DIR",
                str(root / ".py-coding-agent" / "sessions"),
            )
        )
        tools_setting = enabled_tools
        if tools_setting is None:
            raw_tools = _setting(env_file, settings, "tools", "PY_CODING_AGENT_TOOLS", "")
            tools_setting = [tool.strip() for tool in raw_tools.split(",") if tool.strip()] or None
        resolved_model = model or _setting(env_file, settings, "model", "PY_CODING_AGENT_MODEL", default_model)
        profile = model_profile(resolved_model)
        default_context_window = profile.context_window_tokens if profile else 128_000
        default_max_output = profile.max_output_tokens if profile else 4_096
        return Config(
            provider=resolved_provider,
            model=resolved_model,
            base_url=resolved_base_url.rstrip("/"),
            api_key=api_key or _secret(_setting(env_file, settings, "apiKey", "PY_CODING_AGENT_API_KEY", default_api_key)),
            workspace=root,
            session_dir=resolved_session_dir.resolve(),
            max_steps=max_steps
            if max_steps is not None
            else int(_setting(env_file, settings, "maxSteps", "PY_CODING_AGENT_MAX_STEPS", "8")),
            confirm_edits=confirm_edits
            if confirm_edits is not None
            else _bool(_setting(env_file, settings, "confirmEdits", "PY_CODING_AGENT_CONFIRM_EDITS", "true")),
            confirm_bash=confirm_bash
            if confirm_bash is not None
            else _bool(_setting(env_file, settings, "confirmBash", "PY_CODING_AGENT_CONFIRM_BASH", "true")),
            read_only=read_only
            if read_only is not None
            else _bool(_setting(env_file, settings, "readOnly", "PY_CODING_AGENT_READ_ONLY", "false")),
            stream_output=stream_output
            if stream_output is not None
            else _bool(_setting(env_file, settings, "streamOutput", "PY_CODING_AGENT_STREAM_OUTPUT", "true")),
            auto_compact=auto_compact
            if auto_compact is not None
            else _bool(_setting(env_file, settings, "autoCompact", "PY_CODING_AGENT_AUTO_COMPACT", "false")),
            auto_compact_threshold=auto_compact_threshold
            if auto_compact_threshold is not None
            else int(_setting(env_file, settings, "autoCompactThreshold", "PY_CODING_AGENT_AUTO_COMPACT_THRESHOLD", "40")),
            auto_compact_keep=auto_compact_keep
            if auto_compact_keep is not None
            else int(_setting(env_file, settings, "autoCompactKeep", "PY_CODING_AGENT_AUTO_COMPACT_KEEP", "8")),
            context_window_tokens=context_window_tokens
            if context_window_tokens is not None
            else int(
                _setting(
                    env_file,
                    settings,
                    "contextWindowTokens",
                    "PY_CODING_AGENT_CONTEXT_WINDOW_TOKENS",
                    str(default_context_window),
                )
            ),
            max_output_tokens=max_output_tokens
            if max_output_tokens is not None
            else int(
                _setting(
                    env_file,
                    settings,
                    "maxOutputTokens",
                    "PY_CODING_AGENT_MAX_OUTPUT_TOKENS",
                    str(default_max_output),
                )
            ),
            context_trigger_ratio=context_trigger_ratio
            if context_trigger_ratio is not None
            else float(
                _setting(
                    env_file,
                    settings,
                    "contextTriggerRatio",
                    "PY_CODING_AGENT_CONTEXT_TRIGGER_RATIO",
                    "0.85",
                )
            ),
            context_safety_tokens=context_safety_tokens
            if context_safety_tokens is not None
            else int(
                _setting(
                    env_file,
                    settings,
                    "contextSafetyTokens",
                    "PY_CODING_AGENT_CONTEXT_SAFETY_TOKENS",
                    "4096",
                )
            ),
            compact_max_messages=compact_max_messages
            if compact_max_messages is not None
            else int(_setting(env_file, settings, "compactMaxMessages", "PY_CODING_AGENT_COMPACT_MAX_MESSAGES", "50")),
            keep_recent_tool_results=keep_recent_tool_results
            if keep_recent_tool_results is not None
            else int(_setting(env_file, settings, "keepRecentToolResults", "PY_CODING_AGENT_KEEP_RECENT_TOOL_RESULTS", "3")),
            tool_result_token_limit=tool_result_token_limit
            if tool_result_token_limit is not None
            else int(
                _setting(
                    env_file,
                    settings,
                    "toolResultTokenLimit",
                    "PY_CODING_AGENT_TOOL_RESULT_TOKEN_LIMIT",
                    "12000",
                )
            ),
            enabled_tools=tools_setting,
        )


def _setting(env_file: dict[str, str], settings: dict[str, Any], json_name: str, env_name: str, default: str) -> str:
    value = os.environ.get(env_name)
    if value is not None:
        return value
    value = env_file.get(env_name)
    if value is not None:
        return value
    value = settings.get(json_name)
    if value is None:
        return default
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _secret(value: str) -> str:
    return os.environ.get(value, value)


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
