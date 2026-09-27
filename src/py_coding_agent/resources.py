from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NamedText:
    name: str
    path: Path
    text: str
    description: str = ""
    body: str = ""


def load_skills(workspace: Path) -> dict[str, NamedText]:
    return _load_named_markdown(workspace / "skills", "SKILL.md")


def load_prompts(workspace: Path) -> dict[str, NamedText]:
    prompts: dict[str, NamedText] = {}
    prompt_dir = workspace / "prompts"
    if not prompt_dir.is_dir():
        return prompts
    for path in sorted(prompt_dir.glob("*.md")):
        prompts[path.stem] = NamedText(path.stem, path, path.read_text(encoding="utf-8", errors="replace"))
    return prompts


def render_prompt(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{{" + key + "}}", value)
    return result


def parse_key_values(parts: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    positional: list[str] = []
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
        else:
            positional.append(part)
    if positional:
        values["input"] = " ".join(positional)
    return values


def _load_named_markdown(root: Path, filename: str) -> dict[str, NamedText]:
    items: dict[str, NamedText] = {}
    if not root.is_dir():
        return items
    for path in sorted(root.glob(f"*/{filename}")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        metadata, body = _parse_frontmatter(raw)
        name = metadata.get("name", path.parent.name)
        description = metadata.get("description", "")
        items[name] = NamedText(name, path, raw, description=description, body=body)
    return items


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    metadata: dict[str, str] = {}
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"').strip("'")
    if end_index is None:
        return {}, text
    return metadata, "\n".join(lines[end_index + 1 :]).lstrip()
