# Session manager

Public identity, open/close/locate and summary contracts live in
`XBotv2/session/api/contracts.py`. Implementation is in `session/impl/manager.py`
and facets in `session/impl/plugin.py`.

`SESSIONS` is process-level. `open(OpenSession(...))` and
`open_thread(OpenThread(...))` return the real thread Context. `locate` returns
the active owner; transports then require the business owner's public service.
There are no session forwarding methods for input, history, provider selection,
permissions, tools, jobs or interaction resolution.

The session owner publishes `SESSION_INFO`; each child thread publishes
`THREAD_INFO`. Main identity is `main`. Definitions are references, not thread
IDs. Writer locks live for the session owner and are shared only by its
children. Independent runtimes, including same-PID roots, conflict clearly;
different sessions run independently.

Concurrent openings share a task, and caller cancellation is shielded. Closing
blocks new openings and retains mappings until cleanup succeeds. Failed
pre_stop keeps the owner locatable and locked for retry. Empty startup does not
create session records; unused opens stay absent from catalogs and leave no
session directory after successful close.

Thread history working sets and mutations belong to `AGENT_LOOP`. Archive
reads, physical fork/delete and artifacts belong to `PERSISTENCE`. Record
models and unique stateless projection constructors are actually defined in
`session/api/records.py`; TUI imports that API directly.
