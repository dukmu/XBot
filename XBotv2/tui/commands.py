"""Textual-owned command registration and dynamic command discovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from XBotv2.commands import (
    Command,
    CommandDescription,
    CommandResult,
    CommandsPort,
    describe_command,
)


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """One submitted command resolved against the current public catalog."""

    raw: str
    name: str
    args: str = ""
    description: CommandDescription | None = None


ClientHandler = Callable[[str], Awaitable[None]]


def _client_command(
    name: str,
    description: str,
    usage: str,
    handler: ClientHandler,
) -> Command:
    async def execute(raw_args: str) -> CommandResult:
        await handler(raw_args)
        return CommandResult("")

    return Command(
        name=name,
        description=description,
        kind="client",
        handler=execute,
        usage=usage,
        exclusive=False,
    )


def register_client_commands(commands: CommandsPort, app) -> None:
    """Register only local presentation commands in the shared command port."""
    definitions = (
        _client_command(
            "help",
            "Show all commands or detailed help for one command",
            "/help [command]",
            app._cmd_help,
        ),
        _client_command(
            "status", "Show what the session, runtime and model are doing", "/status", app._cmd_status
        ),
        _client_command(
            "settings", "Open this TUI's settings", "/settings", app._cmd_settings
        ),
        _client_command(
            "session", "Switch to another session; with no argument, choose from a list", "/session [session-id]", app._cmd_session
        ),
        _client_command(
            "thread", "View a session thread; subagent threads are read-only", "/thread [thread-id]", app._cmd_thread
        ),
        _client_command("jobs", "List the background tasks this client is tracking", "/jobs [stop <id>|stopall]", app._cmd_jobs),
        _client_command(
            "provider", "Switch provider; with no argument, choose from the catalogue", "/provider [status|list|use <name>]", app._cmd_provider
        ),
        _client_command("model", "Switch model; with no argument, choose from the current provider", "/model [status|list|use [<provider>] <model>]", app._cmd_model),
        _client_command(
            "effort", "Switch reasoning effort; with no argument, choose from the tiers", "/effort [<level>]", app._cmd_effort
        ),
        _client_command("agent", "Switch agent; with no argument, choose from the list", "/agent [status|list|use <name>|<name>]", app._cmd_agent),
        _client_command(
            "thinking", "Show or hide the model's reasoning blocks", "/thinking [on|off|toggle]", app._cmd_thinking
        ),
        _client_command(
            "details", "Show or hide tool call payloads", "/details [on|off|toggle]", app._cmd_details
        ),
        _client_command(
            "attach", "Attach a local image to the next message", "/attach <path> | /attach clear", app._cmd_attach
        ),
        _client_command(
            "approve", "Approve a pending permission request", "/approve <interaction-id> [once|session]", app._cmd_approve
        ),
        _client_command(
            "deny", "Deny a pending permission request", "/deny <interaction-id>", app._cmd_deny
        ),
        _client_command(
            "answer", "Answer a pending user-input request", "/answer <interaction-id> <text>", app._cmd_answer
        ),
        _client_command(
            "clear-screen", "Clear the visible transcript", "/clear-screen", app._cmd_clear_screen
        ),
        _client_command("copy", "Copy the latest reply", "/copy", app._cmd_copy),
        _client_command("exit", "Quit the client", "/exit", app._cmd_exit),
    )
    for command in definitions:
        commands.register(command)


class CommandRegistry:
    """Merge fiber-owned local commands with the current server catalog."""

    def __init__(
        self,
        commands: CommandsPort,
        *,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        self._commands = commands
        self._client_aliases = {
            alias.lower(): name
            for alias, name in (aliases or {"/exit": "exit", "/quit": "exit", "/q": "exit", "/cls": "clear-screen"}).items()
        }
        self._server_commands: dict[str, CommandDescription] = {}
        self._server_order: tuple[str, ...] = ()

    def _client_descriptions(self) -> tuple[CommandDescription, ...]:
        return tuple(
            describe_command(command)
            for command in self._commands.all()
            if command.kind == "client"
        )

    def merge(self, catalog: Sequence[CommandDescription]) -> None:
        """Replace the server catalog; the client port owns local precedence."""
        local_names = {item.name for item in self._client_descriptions()}
        commands: dict[str, CommandDescription] = {}
        order: list[str] = []
        for spec in catalog:
            if spec.name in local_names or spec.name in commands:
                continue
            commands[spec.name] = spec
            order.append(spec.name)
        self._server_commands = commands
        self._server_order = tuple(order)

    def _build_aliases(self) -> dict[str, str]:
        aliases = dict(self._client_aliases)
        for name in self._server_order:
            spec = self._server_commands[name]
            alias = spec.slash.split(maxsplit=1)[0].lower() if spec.slash else ""
            if alias and alias not in aliases:
                aliases[alias] = name
        return aliases

    def names(self) -> tuple[str, ...]:
        local = tuple(item.name for item in self._client_descriptions())
        return local + self._server_order

    def get(self, name: str) -> CommandDescription | None:
        local = self._commands.get(name)
        if local is not None and local.kind == "client":
            return describe_command(local)
        return self._server_commands.get(name)

    @staticmethod
    def is_command(text: str) -> bool:
        return text.strip().startswith("/")

    def client_command(self, name: str) -> Command | None:
        command = self._commands.get(name)
        return command if command is not None and command.kind == "client" else None

    def resolve(self, command: str) -> CommandDescription | None:
        """Resolve one bare name, slash, or alias against the current catalog."""
        value = command.strip()
        if not value or any(character.isspace() for character in value):
            return None
        slash = value if value.startswith("/") else f"/{value}"
        name = self._build_aliases().get(slash.lower(), "")
        if name:
            return self.get(name)
        for candidate in self.names():
            description = self.get(candidate)
            if description is not None and (
                candidate.lower() == value.lstrip("/").lower()
                or description.slash.lower() == slash.lower()
            ):
                return description
        return None

    def parse(self, text: str) -> ParsedCommand | None:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        head, _, tail = stripped.partition(" ")
        description = self.resolve(head)
        return ParsedCommand(
            raw=stripped,
            name=description.name if description is not None else head.lstrip("/"),
            args=tail.strip(),
            description=description,
        )

    def search(self, query: str) -> tuple[CommandDescription, ...]:
        terms = query.strip().lower().lstrip("/").split()
        ordered = [(self.get(name), index) for index, name in enumerate(self.names())]
        if not terms:
            return tuple(spec for spec, _ in ordered if spec is not None)
        ranked: list[tuple[int, int, CommandDescription]] = []
        for spec, index in ordered:
            if spec is None:
                continue
            haystack = f"{spec.name} {spec.description} {spec.usage}".lower()
            if not all(term in haystack for term in terms):
                continue
            rank = 0 if spec.name.lower().startswith(terms[0]) else 1
            ranked.append((rank, index, spec))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(spec for _, _, spec in ranked)

    def complete(self, query: str) -> tuple[CommandDescription, ...]:
        term = query.strip().lower().lstrip("/")
        ranked: list[tuple[int, int, CommandDescription]] = []
        for index, name in enumerate(self.names()):
            spec = self.get(name)
            if spec is None:
                continue
            lowered = spec.name.lower()
            if not term or lowered.startswith(term):
                ranked.append((0, index, spec))
            elif term in lowered:
                ranked.append((1, index, spec))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(spec for _, _, spec in ranked)

    def labels(self) -> tuple[str, ...]:
        return tuple(
            f"{spec.name}  {spec.description}"
            for spec in (self.get(name) for name in self.names())
            if spec is not None
        )


def format_command_help(spec: CommandDescription) -> str:
    """Render the public catalog entry without restating command metadata."""
    lines = [f"{spec.slash} — {spec.description}", f"Usage: {spec.usage}"]
    if spec.parameters:
        lines.extend(("", "Parameters:"))
        lines.extend(
            f"  {name}  {description}"
            for name, description in spec.parameters.items()
        )
    if spec.examples:
        lines.extend(("", "Examples:"))
        lines.extend(f"  {example}" for example in spec.examples)
    return "\n".join(lines)


__all__ = [
    "CommandRegistry",
    "ParsedCommand",
    "format_command_help",
    "register_client_commands",
]
