# Engineering Behavior

This document defines how XBotv2 changes should be made. It is a working
discipline for continuous iteration.

## Non-negotiables

- Keep every touched file simple, consistent, and readable.
- Prefer one clear contract over parallel partial contracts.
- Dispatch runtime events through the XCore context (`ctx.serial`/`ctx.emit`) while improving their contracts,
  payloads, and tests.
- Do not mark existing public stages as experimental just to avoid specifying
  behavior.
- Do not remove public behavior as cleanup unless the replacement path and
  compatibility impact are explicit.
- Keep plugins on `api`; built-in plugins must remain templates for
  third-party plugins.
- Treat docs, tests, and typed models as part of the implementation.

## Change Discipline

Each architecture iteration should answer these questions before code grows:

1. Which contract is being clarified or simplified?
2. Which current behavior must remain true?
3. Which API, protocol, hook, plugin, or tool list needs to be updated?
4. Which test proves the behavior, not just the implementation detail?
5. Which old complexity becomes unnecessary after the change?

## C/S Direction

HTTP and SSE expose typed request, response, and event contracts. Keep transport
DTOs at the owning protocol boundary and preserve event sequence, cursor, and
turn-scope semantics across clients. A new transport must adapt those contracts
explicitly rather than create a parallel runtime event model.

Input request IDs, turn IDs, and interaction IDs have separate owners and
meanings. Preserve correlation where the producer contract defines it; never
substitute an interaction ID for a turn or message request ID.

Agent-initiated interaction is part of the protocol, not a TUI-only feature.
Permission requests and user questions must be registered by the server before
their SSE event becomes visible, resolved through a request-id endpoint, and
acknowledged on the original stream before the turn continues. A client that
cannot support an interaction must fail or cancel it explicitly rather than
leave the engine waiting indefinitely. Waiting for a local answer must not stop
the client from consuming terminal events on the SSE stream.

Clients keep typed pending interaction payloads as the source for response
dialogs. Terminal turn events and new turns must not leave stale request state
that can route an answer to an expired interaction.

An accepted turn has an explicit terminal boundary in its event contract.
Diagnostics remain observable alongside terminal state so clients can clear
running indicators without hiding failures. SSE framing is consumed by the
transport adapter and is not a runtime business event.

Persisted message history must also remain closed. If a client interrupt,
disconnect, or process restart leaves a trailing assistant tool call without a
tool response, the engine appends an error tool result before persisting or
resuming. Session recovery never replays a permission decision or user answer.

## Hook Direction

Hook optimization means making the existing stages easier to reason about.
Loop stages and their payloads are declared in the owning event contracts.
Short-circuit stages use serial dispatch and documented result types; observer
stages use ordinary event delivery. Keep stage-specific return rules explicit
and covered by behavior tests.

Persistence Hooks run once per changed message checkpoint. Repeated safety
calls from normal completion, exception cleanup, or session close must be
no-ops when the normalized history is unchanged. This keeps Hook observations
meaningful while preserving immediate tool-result durability.

## Plugin Direction

The plugin lifecycle should become the reference implementation for extension
authors:

- declare required services through `inject`;
- consume validated config and register resources in `apply`;
- return or register disposers for external resources;
- let the owning XCore fiber remove registrations and run cleanup;
- expose diagnostics without reaching into core internals.

## Built-in Extension Direction

Skills, MCP, and token management should be maintained as built-in plugin
templates. They should demonstrate the public API, not special access to runtime
internals.
