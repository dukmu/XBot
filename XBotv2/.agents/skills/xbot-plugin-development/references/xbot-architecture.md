# XBot ownership and runtime

XBot has one process XCore root. The loader imports only implementation modules
and mounts the scope functions they declare. There is no application factory,
Host or alternate runtime container.

Owners use `api/`, `impl/`, `doc/`, `tests/`. Cross-owner imports use `api/`.
The implementation entry is `impl/plugin.py`, with ordinary `mount_process`,
`mount_session`, `mount_thread` functions. Every function declares `inject` and
`provide`; `Config` declares its validation model. No facet means no instance.
A module with no facets is a configuration error. External module names are
exact and plugin-directory discovery uses `<package>/impl/plugin.py`.

The root owns shared provider definitions/connections, configuration resources,
process jobs registry, carrier listeners and SessionManager. A session owner
holds writer ownership and subagent coordination. Its thread scopes own
mutable model selection, loop/working set, inbox, tools, commands and
interactions. Equal resolved provider configuration shares a directory;
workspace/session overlays remain independent. Agent definitions cache source
parsing, while each thread binds variables and selection separately.

`SESSIONS.open` / `open_thread` return real Contexts. Transports locate a thread
through `SESSIONS.locate`, then resolve the responsible business API there.
SessionManager manages identities and owner lifetimes, not turns or provider,
permission, tool or interaction forwarding. Main thread identity is `main`;
child identity is independent of its Agent reference.

`AGENT_LOOP` is the one execution service: submit, interrupt, status and history
control. Its Engine, asyncio locks and tasks remain implementation details.
Agent definitions and selection belong to agents; Engine construction belongs
to agentloop. On resume, agents reread the saved Agent reference against current
definitions and reapply explicit provider/model/effort facts.

Scopes isolate declared services, including disabled facet declarations. A
missing private service never resolves a parent's same-named service. XCore
checks publication against owner and scope declarations. Required failures and
cycles prevent opening; optional failures dispose only their own facet.
`no_plugins` uses entry `optional` flags, with no named capability list.

Concurrent opens share an initialization task and shield it from individual
caller cancellation. Independent roots cannot share a session writer lock,
even in one PID. A failed opening rolls back its new owner while preserving
existing data and accepted input. Close retains failed owners for retry and
prevents a replacement writer until cleanup succeeds.

Closing stops admissions, cancels and awaits owned turns/provider streams,
tools, commands, interactions, children and jobs, then revokes effects and
releases storage/locks. Started synchronous tool workers are awaited through
cancellation. HTTP/ACP own listening and connection lifetimes; signals close
the root through generic lifecycle callbacks. Interactions own user input and
TUI owns `TerminalClient` / `TERMINAL_CLIENT`.

Persistence still uses JSONL/JSON. SQLite and bounded working sets are a later
storage change. Provider caches are not currently bounded. Three imports of
general workspace operations from `core.filesystem` remain audited migration
work; persistence is not a universal workspace executor.
