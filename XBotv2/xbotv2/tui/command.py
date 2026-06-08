"""Slash command registry for the TUI composer.

Unified command system: client commands, server commands, skill invocations,
and tool/MPC tool invocations all share the same CommandSpec format.

Type tags shown in completion:
  [client cmd] [server cmd] [skill] [tool] [mcp]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CommandKind = Literal["client", "server", "skill", "tool", "mcp"]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    kind: CommandKind
    description: str
    args: str = ""
    raw: str = ""
    display_label: str = ""
    short_label: str = ""
    parameters: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.short_label:
            tag = _KIND_TAGS.get(self.kind, self.kind)
            object.__setattr__(self, "short_label", f"{self.name} [{tag}] {self.description}")
        if not self.display_label:
            object.__setattr__(self, "display_label", self.short_label)


_KIND_TAGS: dict[CommandKind, str] = {
    "client": "client cmd",
    "server": "server cmd",
    "skill": "skill",
    "tool": "tool",
    "mcp": "mcp",
}

_ALIASES: dict[str, str] = {
    "/exit": "exit", "/quit": "exit", "/q": "exit",
    "/clear": "clear", "/help": "help",
}

_COMMANDS: dict[str, CommandSpec] = {
    "exit": CommandSpec(
        name="exit", kind="client",
        description="退出 TUI，不保存",
        raw="/exit",
        parameters={"--force": "不保存直接退出"},
    ),
    "clear": CommandSpec(
        name="clear", kind="client",
        description="清除事件流（session/thread 保留）",
        raw="/clear",
    ),
    "help": CommandSpec(
        name="help", kind="client",
        description="显示帮助信息。用法: /help [command-name]",
        raw="/help",
        parameters={"[command-name]": "要查看详情的命令名（可选）"},
    ),
}

_SEARCH_ORDER: list[str] = ["help", "clear", "exit"]


def register_server_commands(commands: list[dict]) -> None:
    for item in commands:
        name = str(item.get("name") or "").strip().removeprefix("/")
        if not name:
            continue
        kind = item.get("kind", "server")
        slash = item.get("slash", f"/{name}")
        _ALIASES[slash.lower()] = name
        _COMMANDS[name] = CommandSpec(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            description=str(item.get("description") or f"server command: {name}"),
            raw=slash,
            parameters=item.get("parameters") or {},
        )
        if name not in _SEARCH_ORDER:
            _SEARCH_ORDER.insert(max(0, len(_SEARCH_ORDER) - 1), name)


def register_dynamic_commands(items: list[dict], kind: CommandKind) -> None:
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        slash = f"/{name}"
        _ALIASES[slash.lower()] = name
        _COMMANDS[name] = CommandSpec(
            name=name,
            kind=kind,
            description=str(item.get("description") or name),
            raw=slash,
            parameters=item.get("parameters") or {},
        )
        if name not in _SEARCH_ORDER:
            _SEARCH_ORDER.insert(max(0, len(_SEARCH_ORDER) - 1), name)


def parse_slash_command(text: str) -> CommandSpec | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, tail = stripped.partition(" ")
    canonical = _ALIASES.get(head.lower())
    if canonical is None:
        return CommandSpec(
            name="unknown", kind="client", description="",
            args=tail.strip(), raw=stripped,
            display_label=f"{stripped} — not implemented",
            short_label=f"unknown: {stripped}",
        )
    base = _COMMANDS[canonical]
    return CommandSpec(
        name=base.name, kind=base.kind, description=base.description,
        args=tail.strip(), raw=stripped,
        display_label=base.display_label, short_label=base.short_label,
        parameters=base.parameters,
    )


def known_command_labels() -> tuple[str, ...]:
    return tuple(
        f"{_COMMANDS[name].display_label or _COMMANDS[name].short_label}"
        for name in _SEARCH_ORDER
    )


def is_slash_command(text: str) -> bool:
    return text.strip().startswith("/")


def get_command(name: str) -> CommandSpec | None:
    return _COMMANDS.get(name)


def all_commands() -> list[CommandSpec]:
    return [_COMMANDS[name] for name in _SEARCH_ORDER]


def search_commands(query: str) -> list[CommandSpec]:
    normalised = query.strip().lower()
    if not normalised:
        return [_COMMANDS[name] for name in _SEARCH_ORDER]
    if normalised.startswith("/"):
        prefix = normalised[1:]
        scored: list[tuple[int, CommandSpec]] = []
        for name in _SEARCH_ORDER:
            spec = _COMMANDS[name]
            short = spec.name
            if short.startswith(prefix) or name.startswith(prefix):
                score = 0 if short.startswith(prefix) else 1
                scored.append((score, spec))
                continue
            if prefix and prefix in spec.short_label.lower():
                scored.append((2, spec))
        scored.sort(key=lambda item: (item[0], _SEARCH_ORDER.index(item[1].name)))
        return [spec for _, spec in scored]

    words = [w for w in normalised.split() if w]
    scored: list[tuple[int, CommandSpec]] = []
    for name in _SEARCH_ORDER:
        spec = _COMMANDS[name]
        haystack = spec.short_label.lower()
        if all(w in haystack for w in words):
            longest = max(len(w) for w in words)
            scored.append((len(haystack) - longest, spec))
    scored.sort(key=lambda item: (item[0], _SEARCH_ORDER.index(item[1].name)))
    return [spec for _, spec in scored]


def complete_command(prefix: str) -> CommandSpec | None:
    if not prefix.startswith("/"):
        return None
    # Resolve alias before searching
    canonical = _ALIASES.get(prefix.lower())
    if canonical:
        return _COMMANDS.get(canonical)
    matches = search_commands(prefix)
    return matches[0] if matches else None
