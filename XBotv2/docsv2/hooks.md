# Hooks

Hooks are synchronous-in-order asynchronous callbacks registered through
`PluginSetupContext.register_hook`.

Hook stages have three contracts:

- **Observer** stages ignore return values and run every callback.
- **Transform** stages return a documented dictionary such as
  `{"context_messages": messages}`.
- **Guard** stages return `HookDecision`. `CONTINUE` runs the next guard;
  `DENY` or `STOP` ends the stage with an explicit reason.

Lifecycle and persistence stages marked strict run every callback and then raise
an `ExceptionGroup` containing failures. Other observer failures are logged.
Guard and transform failures propagate immediately because continuing with a
partially authorized or partially transformed operation is unsafe. Task
cancellation always propagates immediately and does not run later callbacks.

Persistence Hooks describe changed-message checkpoints, not calls to a save
method. `Engine.save_messages()` compares the normalized message payload with
the last successful write. If unchanged, it returns without running
`BEFORE_STATE_PERSIST` or `AFTER_STATE_PERSIST`. A before Hook may change the
message list; that updated payload is written in the same checkpoint. Tool
messages retain an immediate checkpoint so persisted assistant tool calls gain
their matching tool results as soon as the batch commits.

`HookContext` exposes session metadata and stage-specific data. Turn-scoped
contexts also expose the message `request_id` used by the C/S event envelope;
session lifecycle contexts use an empty value. Plugins register
callbacks through `PluginSetupContext`; they do not receive the hook manager,
tool registry, context builder, or engine implementation.

Model-request stages expose `ctx.model_request` for inspection. Use the
documented stage return dictionary when replacing messages, tools, or the LLM.

`AFTER_CONTEXT_COMPONENTS_BUILD` exposes
`ctx.context_components: list[ContextComponent]`. Each component is immutable
and records its role, content, source, and prompt stage. Observer return values
remain ignored, but the Hook may replace `ctx.context_components` with a new
list. Every entry must be a public `ContextComponent`; invalid replacements
fail before conversion to provider messages.

The complete current stage enum is exported as `xbotv2.api.HookStage`. Hook
cleanup must preserve the existing enum values while making their behavior
clearer. Optimization should reuse existing fields and types, document return
rules, narrow runtime access, and test ordering, failure handling, and
short-circuiting. A new public payload type requires a repeated contract gap
shared by independent consumers.

Do not mark an existing stage as experimental merely to avoid specifying its
contract. Do not remove a stage as cleanup before the plugin lifecycle and
documentation describe the replacement behavior.

The full stage-by-stage contract is maintained in
[Hook stage matrix](hook_stage_matrix.md). That matrix must cover every
`HookStage` value.
