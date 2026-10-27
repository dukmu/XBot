# agentloop

Agent turns, inbox, working state and standard tool execution.

- [Responsibilities, API, dependencies, state and examples](doc/design.md)
- [Public declarations](api/)
- [Focused tests](tests/)


The conversation archive exposes `latest_execution(session_id, thread_id)`
and `message(session_id, thread_id, message_id)`. An execution records its turn
identity, claimed input identities, status and, on successful completion, the
final assistant message identity if there is one. `running` in a cold archive
means that execution started but no terminal fact was recorded. It does not
imply the task is still alive or that an earlier assistant message is its reply.
The thread event stream publishes `ExecutionEnded` with the same record after
the owning task releases its turn lock, including pre-turn-start failures.

`latest_input` can filter by runtime input source and event and reports the
accepted input's current target and whether it is pending, removed, discarded,
or consumed. `execution_for_input` finds the latest execution that claimed an
input, even if other executions have followed it. Removal alone describes an
inbox operation; its meaning is determined by the caller.

The live loop exposes `execution_id` while it owns an execution; the archive
can retrieve its exact recorded state using `execution_for_turn`.
