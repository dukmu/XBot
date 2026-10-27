# Agent loop and tools

The loop owns the live thread working set, inbox, turn execution, model/tool
sequencing and history mutations. Public `AGENT_LOOP` is the actual execution
service: submit, interrupt, status, pending-input and history control, and typed
operation invocation. Its Engine, task and lock stay in `impl/`; there is no
factory, second runner protocol or application container.

`mount_thread` creates tools, restores its state/inbox from thread persistence,
then constructs Engine once model selection, guards and owner services are
available. Agents owns definition parsing/binding and selection. The loader
only discovers and mounts facets. Business HTTP routes locate an actual thread
Context through the session API and invoke this owner service.

`TOOLS`, `INBOX`, `AGENT_LOOP` and `LOOP_STATE` are the canonical service keys.
Hook declarations/payloads live in `api/events.py`, public execution/input/tool
contracts in `api/contracts.py`, execution values in `api/models.py`, outputs
in `api/outputs.py` and tool wire values in `api/protocol.py`.

Accepted input and tool results commit before completion events. Turn count is
a lifetime counter through edits and compaction. The default iteration limit
is `None`; only an explicitly finite value imposes a cap. Configuring a model
does not reset an explicit cap or turn an unlimited loop into a finite one.

History has one mutable working-set owner. Persistence owns the durable human
transcript and model surface; compaction changes the latter while retaining
original human transcript text. HTTP reads the same canonical transcript for
active, closed and resumed threads. Clear/undo/regenerate still change its
visible projection according to their operations. No alternate active-only
content source is maintained in session management.

Tools follow registry resolution, original-argument validation, standard guards
and execution. Plugin registrations belong to actual fibers; late callbacks
pass an owner. No slash command is transformed into a synthetic ToolCall.
Synchronous tool workers run outside the event loop and remain awaited through
cancellation until their actual I/O finishes, including timeout paths.

pre_stop rejects new work, cancels and awaits active turns/provider streams and
owned tools before effects disappear. Other owners drain commands, interactions,
children and jobs in that same phase. Persistence/locks are released afterward.
JSONL/JSON formats and currently unbounded working sets remain unchanged here;
SQLite and bounded storage are subsequent work.
