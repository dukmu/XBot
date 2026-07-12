# Engineering Behavior

This document defines how XBotv2 changes should be made. It is a working
discipline for continuous iteration.

## Non-negotiables

- Keep every touched file simple, consistent, and readable.
- Prefer one clear contract over parallel partial contracts.
- Preserve the current `HookStage` values while improving their contracts,
  payloads, and tests.
- Do not mark existing public stages as experimental just to avoid specifying
  behavior.
- Do not remove public behavior as cleanup unless the replacement path and
  compatibility impact are explicit.
- Keep plugins on `xbotv2.api`; built-in plugins must remain templates for
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

The client/server protocol needs one event model and one request correlation
model. HTTP and SSE are the current main transport. Any JSONL frame or alternate
transport must either share the same event contract or stay out of the main
runtime path until it does.

The accepted message request id must remain identical across Engine turn
context, turn-scoped Hooks, and every SSE envelope. Blocking interactions use
their own nested ids; they must not replace or overload the outer turn id.

Agent-initiated interaction is part of the protocol, not a TUI-only feature.
Permission requests and user questions must be registered by the server before
their SSE event becomes visible, resolved through a request-id endpoint, and
acknowledged on the original stream before the turn continues. A client that
cannot support an interaction must fail or cancel it explicitly rather than
leave the engine waiting indefinitely.

The TUI keeps each pending interaction payload as its state source instead of
duplicating active flags and request ids. A cancelled or failed turn, and the
start of a new turn, clears unresolved client interaction state so later input
cannot be routed to an expired request.

An accepted turn has a closed lifecycle. Once `turn_started` is visible, the
client eventually receives one `turn_finished` or `turn_cancelled`. Engine
failures remain visible as `error` and are followed by `turn_finished`, allowing
the UI to clear running state without hiding the diagnostic. The SSE `end`
sentinel belongs to transport framing and is consumed before TUI state
reduction.

## Hook Direction

Hook optimization means making the existing stages easier to reason about. The
current enum remains intact while the implementation gains clearer categories,
documented stage payloads, stage-specific return rules, and tests for execution
order, short-circuiting, and failure handling. Existing public fields and types
are preferred over parallel read views.

Persistence Hooks run once per changed message checkpoint. Repeated safety
calls from normal completion, exception cleanup, or session close must be
no-ops when the normalized history is unchanged. This keeps Hook observations
meaningful while preserving immediate tool-result durability.

## Plugin Direction

The plugin lifecycle should become the reference implementation for extension
authors:

- load config and external resources in `on_load`;
- register all declared resources through setup capabilities;
- record runtime registrations so unload and rollback are complete;
- release resources in `on_unload`;
- expose diagnostics without reaching into core internals.

## Built-in Extension Direction

Skills, MCP, and token management should be maintained as built-in plugin
templates. They should demonstrate the public API, not special access to runtime
internals.
