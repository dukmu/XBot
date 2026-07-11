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
partially authorized or partially transformed operation is unsafe.

`HookContext` exposes session metadata and scoped capabilities, including the
tool registry and sandbox policy. Plugins must not import Engine internals.

The complete stage enum is exported as `xbotv2.api.HookStage`. New stages are
not added for internal implementation details; a stage is justified only when a
plugin needs a stable interception point.

