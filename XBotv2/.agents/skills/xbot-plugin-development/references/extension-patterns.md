# XBot Plugin Extension Patterns

Use the smallest pattern that matches the user boundary. These examples are
complete enough to adapt, but names and domain types should remain owned by the
plugin package.

## Tool: Work Requested by the Agent

A Tool is model-visible work. Dependencies belong on a named handler, while
the only core-provided invocation metadata is an optional keyword-only
`ToolCall`.

```python
from typing import Protocol

from XBotv2.core import Tool, ToolCall, ToolResult


class WeatherClient(Protocol):
    async def current(self, city: str) -> dict[str, object]: ...


class WeatherTools:
    def __init__(self, client: WeatherClient) -> None:
        self._client = client

    async def weather(
        self,
        city: str,
        *,
        tool_call: ToolCall,
    ) -> ToolResult:
        """Return current weather for one city."""
        report = await self._client.current(city)
        return ToolResult.success(
            f"Weather loaded for {city}",
            data={"call_id": tool_call.id, "report": report},
        )


class WeatherPlugin:
    name = "weather"
    inject = ["tools", "weather_client"]

    def apply(self, ctx, config=None) -> None:
        handler = WeatherTools(ctx.weather_client)
        ctx.tools.register(
            Tool.from_function(handler.weather),
            namespace="weather",
            timeout_seconds=30,
        )


plugin = WeatherPlugin()
```

Return `ToolResult.failure(code, message, retryable=...)` for an expected domain
failure. Let programming errors propagate; do not convert every exception into
success text. Keep `data` and `client_events` JSON-compatible. Never call the
handler directly from production Agent flow—the standard registry path owns
schema validation, guards, permissions, dispatch, and after-call events.

## Command: Direct Human Control

A slash command receives the raw text after the command name. Parse it at the
command boundary and return `CommandResult`; do not manufacture a ToolCall.

```python
from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    split_command_args,
)


class GreetingCommands:
    async def greet(self, raw_args: str) -> CommandResult:
        args = split_command_args(raw_args)
        if len(args) != 1:
            return command_usage("/greet <name>")
        return CommandResult(f"Hello, {args[0]}!")


class GreetingPlugin:
    name = "greeting-command"
    inject = ["commands"]

    def apply(self, ctx, config=None) -> None:
        handler = GreetingCommands()
        ctx.commands.register(Command(
            name="greet",
            description="Greet one person",
            usage="/greet <name>",
            examples=("/greet Ada",),
            parameters={"name": "Person to greet"},
            handler=handler.greet,
        ))
```

Test quoted input, empty input, invalid syntax, the success result, and unload.
Use a `kind="prompt"` command only for client-side prompt expansion; it has no
server handler.

## Typed Event: A Fact With Zero or More Observers

The package that owns the fact owns its payload and event name. Put them in a
small public `contracts.py`, then let producers emit and observers listen.

```python
# contracts.py
from dataclasses import dataclass

TASK_COMPLETED = "example/task-completed"


@dataclass(frozen=True, slots=True)
class TaskCompleted:
    task_id: str
    output: str
```

```python
# observer.py
from .contracts import TASK_COMPLETED, TaskCompleted


class CompletionObserver:
    def __init__(self, audit) -> None:
        self._audit = audit

    async def on_completed(self, event: TaskCompleted) -> None:
        await self._audit.record(event.task_id, event.output)


class ObserverPlugin:
    name = "completion-observer"
    inject = ["audit"]

    def apply(self, ctx, config=None) -> None:
        observer = CompletionObserver(ctx.audit)
        ctx.on(TASK_COMPLETED, observer.on_completed)
```

Emit with `await events.emit(TASK_COMPLETED, TaskCompleted(...))`. Observers
normally return `None`. Use `serial` only when the event contract explicitly
defines a short-circuit result; use `chain` for a documented transformation
pipeline and `waterfall` for around-middleware. Do not reuse `EventContext` as
an arbitrary cross-plugin payload.

## Service: A Capability Consumed by Another Plugin

Define the narrow consumer contract as a `Protocol` in the owning public
package. The provider publishes one service; the consumer declares it in
`inject` and resolves it once in `apply`.

```python
from typing import Protocol


class NotesPort(Protocol):
    async def add(self, text: str) -> str: ...


class NotesService:
    def __init__(self, state) -> None:
        self._state = state

    async def add(self, text: str) -> str:
        notes = list(await self._state.get("items", []))
        notes.append(text)
        await self._state.set("items", notes)
        return str(len(notes))


class NotesProvider:
    name = "notes-provider"

    def apply(self, ctx, config=None) -> None:
        ctx.set("notes", NotesService(ctx.state.namespace("notes")))


class NotesConsumer:
    name = "notes-consumer"
    inject = ["notes"]

    def apply(self, ctx, config=None) -> None:
        handler = ConsumerHandler(ctx.notes)
        ctx.on("example/save-note", handler.save)
```

When `notes-provider` unloads, XCore removes the service and rolls the consumer
back to pending. When it returns, the consumer is composed again. Do not build
a reload event, callback hole, or polling loop for this dependency lifecycle.

## State: One Logical Namespace, One Typed Snapshot

For related fields, store a versioned snapshot under one key instead of
writing several keys that can describe different moments.

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Checklist:
    version: int
    items: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {"version": self.version, "items": list(self.items)}

    @classmethod
    def from_json(cls, raw: Any) -> "Checklist":
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("unsupported checklist state")
        items = raw.get("items")
        if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
            raise ValueError("checklist items must be strings")
        return cls(version=1, items=tuple(items))
```

```python
class ChecklistStore:
    def __init__(self, state) -> None:
        self._state = state

    async def load(self) -> Checklist:
        raw = await self._state.get("snapshot")
        return Checklist(1, ()) if raw is None else Checklist.from_json(raw)

    async def save(self, checklist: Checklist) -> None:
        await self._state.set("snapshot", checklist.to_json())
```

`None` is valid here because an absent persisted key is an explicit initial
state, not because a required runtime dependency might be missing. Reject
malformed or unsupported versions at this ownership boundary. Do not write
`plugin_state/foo.json`, mix configuration into the snapshot, duplicate
conversation history, or manually serialize XBot `Message` objects.

## External Resource: Explicit Owner and Cleanup

When the plugin owns a client, process, page, or connection, give it a named
owner and register one cleanup effect. Construct the owner only after validated
configuration and required services are available.

```python
class SearchRuntime:
    def __init__(self, client) -> None:
        self.client = client

    async def close(self) -> None:
        await self.client.aclose()


class SearchPlugin:
    name = "search-runtime"

    async def apply(self, ctx, config) -> None:
        runtime = SearchRuntime(make_client(config["endpoint"]))
        ctx.dispose(runtime.close)
        ctx.set("search_runtime", runtime)
```

Effects are unwound in reverse order. Register cleanup immediately after the
resource is acquired, before later registrations that may fail.

## Runtime Discovery: Transactional Registration

Static registrations made during `apply` are already fiber-owned. If the
plugin discovers items later, it must track exactly what it added and roll back
the partial batch on failure.

```python
class DynamicTools:
    def __init__(self, tools) -> None:
        self._tools = tools
        self._names: set[str] = set()

    def replace(self, definitions: list[Tool]) -> None:
        added: list[str] = []
        try:
            for definition in definitions:
                added.append(self._tools.register(definition, namespace="dynamic"))
        except Exception:
            for name in reversed(added):
                self._tools.unregister(name)
            raise
        previous, self._names = self._names, set(added)
        for name in previous:
            self._tools.unregister(name)

    def close(self) -> None:
        for name in tuple(self._names):
            self._tools.unregister(name)
        self._names.clear()
```

Production replacement may need a collision-safe two-phase strategy depending
on names. The invariant is that failed discovery leaves the previous complete
set or no set—never an undocumented mixture. Test the failure in the middle of
a batch and a repeated identical discovery.

## Boundary Review

Before adding code, answer:

1. Who requests the work: model, human, client, another plugin, or lifecycle?
2. Which package owns the contract and its typed payload?
3. Which services are truly required for activation?
4. Which object owns runtime resources and cleanup?
5. Which data is durable, and what is its single canonical snapshot?
6. What public observation proves behavior and teardown?

If those answers are unclear, do not solve the uncertainty with `Any`, a
whole-Context field, nested closures, `getattr`, or `if dependency is None`.
