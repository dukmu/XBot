"""The slash-command registry.

The catalogue entry is the repository's own :class:`CommandDescription` -- the
same model the server publishes for its own commands -- so there is one shape for
"a command the user can type", and no field is restated here.

Two rules:

* parsing never guesses. An unknown slash returns the text with no match, and the
  caller decides whether to say "not implemented";
* the server catalogue *replaces* the previous one, because the server is the
  source of truth for what it offers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from XBotv2.commands import CommandDescription


@dataclass(frozen=True)
class ParsedCommand:
    """One submitted line, resolved against the catalogue.

    ``description`` is ``None`` when the slash names nothing the client knows;
    a caller must handle that case rather than silently sending it to the server.
    """

    raw: str
    name: str
    args: str = ""
    description: CommandDescription | None = None


def _builtin(
    name: str,
    description: str,
    *,
    usage: str = "",
    parameters: Mapping[str, str] | None = None,
    exclusive: bool = False,
) -> CommandDescription:
    return CommandDescription(
        name=name,
        slash=f"/{name}",
        kind="client",
        description=description,
        usage=usage or f"/{name}",
        parameters=dict(parameters or {}),
        exclusive=exclusive,
    )


# The client's own commands, in the order they are offered when the query is
# empty. Everything else the user can type comes from the server catalogue.
BUILTIN_COMMANDS: tuple[CommandDescription, ...] = (
    _builtin("help", "Show the commands this client can run", usage="/help"),
    _builtin(
        "status",
        "Show what the session, runtime and model are doing",
        usage="/status",
    ),
    _builtin(
        "session",
        "Switch to another session; with no argument, choose from a list",
        usage="/session [session-id]",
    ),
    _builtin(
        "thread",
        "View a session thread; subagent threads are read-only",
        usage="/thread [thread-id]",
    ),
    _builtin(
        "jobs",
        "List the background tasks this client is tracking",
        usage="/jobs [stop <id>|stopall]",
    ),
    _builtin(
        "provider",
        "Switch provider; with no argument, choose from the catalogue",
        usage="/provider [status|list|use <name>]",
    ),
    _builtin(
        "model",
        "Switch model; with no argument, choose from the current provider",
        usage="/model [status|list|use [<provider>] <model>]",
    ),
    _builtin(
        "effort",
        "Switch reasoning effort; with no argument, choose from the tiers",
        usage="/effort [<level>]",
    ),
    _builtin(
        "agent",
        "Switch agent; with no argument, choose from the list",
        usage="/agent [status|list|use <name>|<name>]",
    ),
    _builtin(
        "thinking",
        "Show or hide the model's reasoning blocks",
        usage="/thinking [on|off|toggle]",
    ),
    _builtin(
        "details",
        "Show or hide tool call payloads",
        usage="/details [on|off|toggle]",
    ),
    _builtin(
        "attach",
        "Attach a local image to the next message",
        usage="/attach <path> | /attach clear",
    ),
    _builtin(
        "approve",
        "Approve a pending permission request",
        usage="/approve <interaction-id> [once|session]",
    ),
    _builtin(
        "deny",
        "Deny a pending permission request",
        usage="/deny <interaction-id>",
    ),
    _builtin(
        "answer",
        "Answer a pending user-input request",
        usage="/answer <interaction-id> <text>",
    ),
    _builtin("clear-screen", "Clear the visible transcript", usage="/clear-screen"),
    _builtin("copy", "Copy the latest reply", usage="/copy"),
    _builtin("exit", "Quit the client", usage="/exit"),
)

# Only commands this client can actually carry out are advertised. A catalogue
# entry that answers "not implemented" is worse than not offering it, so the rest
# arrive with their implementations (see the rewrite spec's remaining steps).

# Shorthands the client owns. A server alias can never take one of these.
BUILTIN_ALIASES: Mapping[str, str] = {
    "/exit": "exit",
    "/quit": "exit",
    "/q": "exit",
    "/cls": "clear-screen",
}


class CommandRegistry:
    """The commands a client can run, local ones first."""

    def __init__(
        self,
        *,
        builtins: Iterable[CommandDescription] = BUILTIN_COMMANDS,
        aliases: Mapping[str, str] = BUILTIN_ALIASES,
    ) -> None:
        self._client_commands: dict[str, CommandDescription] = {
            spec.name: spec for spec in builtins
        }
        self._client_order: tuple[str, ...] = tuple(
            spec.name for spec in builtins
        )
        self._client_aliases: dict[str, str] = {
            alias.lower(): name for alias, name in aliases.items()
        }
        self._server_commands: dict[str, CommandDescription] = {}
        self._server_order: tuple[str, ...] = ()
        self._build_aliases()

    @classmethod
    def with_builtins(cls) -> "CommandRegistry":
        return cls()

    # --- catalogue ----------------------------------------------------

    def merge(self, catalog: Sequence[CommandDescription]) -> None:
        """Replace the server catalogue.

        Names and aliases the client already owns are skipped: the client's own
        commands are not the server's to redefine.
        """
        commands: dict[str, CommandDescription] = {}
        order: list[str] = []
        for spec in catalog:
            if spec.name in self._client_commands or spec.name in commands:
                continue
            commands[spec.name] = spec
            order.append(spec.name)
        self._server_commands = commands
        self._server_order = tuple(order)
        self._build_aliases()

    def _build_aliases(self) -> None:
        self._aliases: dict[str, str] = dict(self._client_aliases)
        for name in self._server_order:
            spec = self._server_commands[name]
            alias = spec.slash.split(maxsplit=1)[0].lower() if spec.slash else ""
            if not alias or alias in self._aliases:
                continue
            self._aliases[alias] = name

    # --- lookup -------------------------------------------------------

    def names(self) -> tuple[str, ...]:
        return self._client_order + self._server_order

    def get(self, name: str) -> CommandDescription | None:
        return self._client_commands.get(name) or self._server_commands.get(name)

    def is_command(self, text: str) -> bool:
        return text.strip().startswith("/")

    def parse(self, text: str) -> ParsedCommand | None:
        """Resolve one submitted line, or ``None`` when it is not a command."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        head, _, tail = stripped.partition(" ")
        name = self._aliases.get(head.lower(), "")
        if not name:
            # The registry also accepts a command written by its canonical name
            # when the catalogue's slash differs from ``/name``.
            for candidate in self.names():
                if self.get(candidate).slash.lower() == head.lower():
                    name = candidate
                    break
        return ParsedCommand(
            raw=stripped,
            name=name or head.lstrip("/"),
            args=tail.strip(),
            description=self.get(name) if name else None,
        )

    def search(self, query: str) -> tuple[CommandDescription, ...]:
        """Commands matching ``query``, best first.

        A name prefix outranks a mere mention, and within a rank the declared
        order is kept, so the list does not shuffle as the user types.
        """
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
        """Commands whose *name* matches what is being typed.

        Deliberately narrower than :meth:`search`: a completion popup that also
        matched descriptions would offer "copy" for the prefix ``/st`` because its
        description contains "latest".
        """
        term = query.strip().lower().lstrip("/")
        ranked: list[tuple[int, int, CommandDescription]] = []
        for index, name in enumerate(self.names()):
            spec = self.get(name)
            if spec is None:
                continue
            lowered = spec.name.lower()
            if not term:
                ranked.append((0, index, spec))
            elif lowered.startswith(term):
                ranked.append((0, index, spec))
            elif term in lowered:
                ranked.append((1, index, spec))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(spec for _, _, spec in ranked)

    def labels(self) -> tuple[str, ...]:
        """One line per command, for the palette and the completion popup."""
        return tuple(
            f"{spec.name}  {spec.description}"
            for spec in (self.get(name) for name in self.names())
            if spec is not None
        )


__all__ = [
    "BUILTIN_ALIASES",
    "BUILTIN_COMMANDS",
    "CommandRegistry",
    "ParsedCommand",
]
