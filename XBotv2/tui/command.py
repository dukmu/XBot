"""Client command registry and server-provided command completions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CommandKind = Literal["client", "server", "prompt"]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    kind: CommandKind
    description: str
    usage: str = ""
    args: str = ""
    raw: str = ""
    display_label: str = ""
    short_label: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.short_label:
            tag = _KIND_TAGS.get(self.kind, self.kind)
            object.__setattr__(self, "short_label", f"{self.name} [{tag}] {self.description}")
        if not self.display_label:
            object.__setattr__(self, "display_label", self.short_label)


_KIND_TAGS: dict[CommandKind, str] = {
    "client": "client cmd",
    "server": "server cmd",
    "prompt": "prompt",
}

_CLIENT_ALIASES: dict[str, str] = {
    "/exit": "exit", "/quit": "exit", "/q": "exit",
    "/clear-screen": "clear-screen", "/cls": "clear-screen", "/help": "help",
    "/thinking": "thinking", "/details": "details",
    "/attach": "attach",
}

_CLIENT_COMMANDS: dict[str, CommandSpec] = {
    "exit": CommandSpec(
        name="exit", kind="client",
        description="Quit the TUI",
        raw="/exit",
    ),
    "clear-screen": CommandSpec(
        name="clear-screen", kind="client",
        description="Clear the visible transcript without changing the session",
        raw="/clear-screen",
    ),
    "help": CommandSpec(
        name="help", kind="client",
        description="Show commands or detailed help for one command",
        usage="/help [command-name]",
        raw="/help",
        parameters={"[command-name]": "Optional command name"},
    ),
    "thinking": CommandSpec(
        name="thinking",
        kind="client",
        description="Expand or collapse model reasoning",
        usage="/thinking [on|off|toggle]",
        raw="/thinking",
    ),
    "details": CommandSpec(
        name="details",
        kind="client",
        description="Expand or collapse tool execution details",
        usage="/details [on|off|toggle]",
        raw="/details",
    ),
    "attach": CommandSpec(
        name="attach", kind="client",
        description="Attach a local image to the next message",
        usage="/attach <path> | /attach clear",
        raw="/attach",
    ),
}
_CLIENT_ALIASES.update({f"/{name}": name for name in _CLIENT_COMMANDS})

_CLIENT_SEARCH_ORDER = (
    "help", "clear-screen", "thinking", "details", "attach", "exit",
)


class CommandRegistry:
    """Instance-held command directory: UI-local commands + server catalog.

    One registry per client session: the server catalog is merged into the
    instance at connect time, so discovery, completion, and parsing never
    mutate module-level state.
    """

    def __init__(
        self,
        *,
        client_commands: dict[str, CommandSpec] | None = None,
        client_aliases: dict[str, str] | None = None,
        client_search_order: tuple[str, ...] | None = None,
    ) -> None:
        self._client_commands = dict(client_commands or _CLIENT_COMMANDS)
        self._client_aliases = dict(client_aliases or _CLIENT_ALIASES)
        self._client_search_order = list(client_search_order or _CLIENT_SEARCH_ORDER)
        self.reset()

    @classmethod
    def default(cls) -> "CommandRegistry":
        return cls()

    def reset(self) -> None:
        """Restore the directory to the local client commands only."""
        self._aliases = dict(self._client_aliases)
        self._commands = dict(self._client_commands)
        self._search_order = list(self._client_search_order)

    def merge_server(self, commands: list[dict]) -> None:
        """Replace the server command catalog (client commands win)."""
        self.reset()
        for item in commands:
            name = str(item.get("name") or "").strip().removeprefix("/")
            if not name or name in self._client_commands:
                continue
            kind = item.get("kind", "server")
            slash = item.get("slash", f"/{name}")
            alias = str(slash).split(maxsplit=1)[0]
            if alias.lower() in self._client_aliases:
                continue
            self._aliases[alias.lower()] = name
            self._commands[name] = CommandSpec(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                description=str(item.get("description") or f"server command: {name}"),
                usage=str(item.get("usage") or slash),
                raw=slash,
                parameters=item.get("parameters") or {},
            )
            if name not in self._search_order:
                self._search_order.insert(max(0, len(self._search_order) - 1), name)

    def parse(self, text: str) -> CommandSpec | None:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        head, _, tail = stripped.partition(" ")
        canonical = self._aliases.get(head.lower())
        if canonical is None:
            return CommandSpec(
                name="unknown", kind="client", description="",
                args=tail.strip(), raw=stripped,
                display_label=f"{stripped} — not implemented",
                short_label=f"unknown: {stripped}",
            )
        base = self._commands[canonical]
        return CommandSpec(
            name=base.name, kind=base.kind, description=base.description,
            usage=base.usage,
            args=tail.strip(), raw=stripped,
            display_label=base.display_label, short_label=base.short_label,
            parameters=base.parameters,
        )

    def labels(self) -> tuple[str, ...]:
        return tuple(
            f"{self._commands[name].display_label or self._commands[name].short_label}"
            for name in self._search_order
        )

    def is_slash(self, text: str) -> bool:
        return text.strip().startswith("/")

    def get(self, name: str) -> CommandSpec | None:
        return self._commands.get(name)

    def search(self, query: str) -> list[CommandSpec]:
        normalised = query.strip().lower()
        if not normalised:
            return [self._commands[name] for name in self._search_order]
        if normalised.startswith("/"):
            prefix = normalised[1:]
            scored: list[tuple[int, CommandSpec]] = []
            for name in self._search_order:
                spec = self._commands[name]
                short = spec.name
                if short.startswith(prefix) or name.startswith(prefix):
                    score = 0 if short.startswith(prefix) else 1
                    scored.append((score, spec))
                    continue
                if prefix and prefix in spec.short_label.lower():
                    scored.append((2, spec))
            scored.sort(
                key=lambda item: (item[0], self._search_order.index(item[1].name))
            )
            return [spec for _, spec in scored]

        words = [w for w in normalised.split() if w]
        scored: list[tuple[int, CommandSpec]] = []
        for name in self._search_order:
            spec = self._commands[name]
            haystack = spec.short_label.lower()
            if all(w in haystack for w in words):
                longest = max(len(w) for w in words)
                scored.append((len(haystack) - longest, spec))
        scored.sort(
            key=lambda item: (item[0], self._search_order.index(item[1].name))
        )
        return [spec for _, spec in scored]


__all__ = ["CommandKind", "CommandRegistry", "CommandSpec"]
