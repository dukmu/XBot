# Built-in plugins

This page is only the navigation map. The complete contracts, dependencies,
effects, state ownership, client/server facets, and examples are maintained in
the skill's [plugin index](../.agents/skills/xbot-plugin-development/references/plugins_list.md)
and [per-plugin references](../.agents/skills/xbot-plugin-development/references/plugins/README.md).

The bundled tree is `XBotv2/xcore.yaml`. `id` is the overlay identity and
`name` is the import name. Profiles determine which carrier can mount an
entry; activation itself is dependency-driven.

No plugin is mandatory merely because it is bundled. Boot starts the configured
tree, including an empty tree, and reports failed or dependency-blocked plugins
without rejecting unrelated capabilities. An operation checks the services it
actually needs: serving HTTP needs `server`, and running ACP needs `acp_agent`.
A server without the session plugin can still expose other routes; it does not
acquire session routes or a session manager implicitly. The Agent construction
entry still requires an initialized Agent runtime; its remaining composition
dependencies are under review, not a universal boot rule.

XCore optional dependencies do not gate activation. When a declared dependency
appears, disappears or changes instance, XCore disposes the consumer's old
effects and activates it against the current services. Startup settles these
bindings before returning; a consumer must not retain undeclared optional
service references and expect them to update.

The persistence plugin constructs its own thread store and file-backed state
from `SessionLaunch`. The launch carries the deferred-materialization choice;
the application entry neither creates the store nor tests a plugin ID to choose
storage. Session publishes the store's state service, or memory state when no
store is available. It owns history/metadata restoration and inbox construction;
persistence subscribes to subsequent metadata changes and materialization
boundaries. Consumers use the same registered XCore state service.

`SessionLaunch` is also the single launch-time owner of session/thread identity,
workspace, provider route, parent thread, interaction mode, and subagent status.
Agent construction receives those facts from that service. `AgentCreateOptions`
contains only the requested Agent definition/selection and an optional model
override; it does not duplicate launch identity or context.

Input hooks apply at both turn-start and next-step boundaries. `RejectInput`
reports rejection and stops the current turn; `CompleteTurn` publishes its
result and stops the current turn. In a claimed batch, only the processed
prefix (including the short-circuited input) is consumed. Unprocessed inputs
remain pending. Neither result may be silently ignored after a tool call or a
text-only model response.

`AcceptInput` may replace the input payload, but must retain the claimed inbox
identity, target and payload kind. `INPUT_ACCEPTED` canonicalization may change
only message parts and artifact references; identity, message kind and runtime
notice provenance remain unchanged. Invalid hook results fail before history
append, leaving the original input available for retry.

Busy-session input defaults to `steer` (the next step), not a separate queued
turn. Explicit `queue` still targets the next turn; neither submission cancels
the running tool or model invocation. Accepted human messages retain a
`steering` fact, and the context compiler adds a brief temporary-supplement
instruction: continue the current task unless the user explicitly asks to
interrupt or abandon it. The instruction is not inserted into user-authored
parts or the TUI transcript, and it is reconstructed on resume from that fact.

Inbox change payloads carry only the changed facts: `Inserted` carries the full
input and wake flag; `Edited` carries `id/content`; `Retargeted` carries
`id/target`; `Removed` carries `id`; `Claimed`, `Consumed` and `Discarded` carry
`ids`. Consumers resolve accepted content from canonical history rather than
expecting additional input copies in consumption events. Claims remain runtime
ownership, not proof of durable consumption.

| id/name | profiles | primary responsibility | important injected services |
|---|---|---|---|
| config | agent, server | session settings/policy; independent configuration routes | session facet: runtime paths, launch, plugin overrides/dirs, runtime log, no_plugins; HTTP facet: runtime paths, server/sessions |
| client_transport | client | HTTP API client using its own plugin configuration | none |
| tui | client | Textual terminal client and local command presentation using its own plugin configuration | client API, commands |
| persistence | agent, server, acp | thread store, durable subscribers and reader factory | session launch; subscriber: loop state, thread persistence, runtime log |
| usage | agent | normalized usage snapshot and events | state, loop state, runtime log |
| agents | agent, server | Agent catalog, selection, engine creation | catalog, loop factory, LLM, tools, agent inbox, session launch |
| session | agent, server, acp | SessionManager, thread runtime, hydration, inbox, artifacts, history routes | launch, paths, commands, runtime log, application factory; optional thread persistence |
| jobs | agent, server | shell/subagent job registry | commands, engine, sessions |
| commands | agent, server, client | human command catalog and dispatch | sessions/server where mounted |
| llm | agent, server | provider/model directory and selection | runtime log, agent runtime, sessions |
| agentloop | agent, server | Tool registry and AgentLoop factory | runtime log, session launch, sessions |
| context_builder | agent | provider-facing context compiler and prompt components | runtime log, artifacts |
| prompts | agent | prompt component registry | context builder |
| sandbox | agent | filesystem/process/network ceiling | paths, session, tools, settings |
| permissions | agent | regex Tool policy and approval flow | tools, interactions, state, settings |
| coretools | agent | filesystem and shell Tools; workspace extension hooks | tools, session, sandbox, artifacts, jobs, workspace root |
| subagents | agent | stable child-thread collaboration and execution jobs | session manager, current session, catalog, jobs, tools |
| goal | agent | durable objective, explicit goal tools, same-session continuation and `/goal` | tools, commands, engine, state, usage; optional jobs |
| todolist | agent, server | atomic checklist snapshot | tools/state or sessions |
| skills | agent | SKILL.md discovery and prompt/tool activation | tools, commands, sandbox |
| mcp_plugin | agent | MCP server tool/resource/prompt bridges | tools, model, interactions, session, usage, loop state |
| content_cache | agent | lossless externalization of oversized current user input and Tool output text | artifacts |
| compact | agent | append-only semantic history replacement | tools, commands, model, loop state, artifacts |
| browser | agent | web research and isolated browser Tools | tools, sandbox, artifacts |
| token_manager | agent | request/context observation | session |
| workspace_instructions | agent | AGENTS.md context contribution | variables, workspace root |
| interactions | agent | live client event routing, ask-user and input waiters | tools, session launch |
| caption | agent | session title Tool and automatic first-message caption | tools, model, loop state, session, agent options, usage |
| workspaces | server, acp | workspace/session catalog and directory API | sessions, state, workspace root |
| acp_plugin | acp | ACP carrier | sessions, ACP launch, runtime log |
| server | server | FastAPI carrier and health/hello | runtime log |

## Built-in tool timeouts

```yaml
- id: coretools
  config:
    shell_max_timeout_seconds: 120
    tool_timeout_seconds: 60
    tool_timeouts:
      search: 120
      wait_shell: 30
```

All configuration values above are finite positive seconds. `tool_timeout_seconds`
is the registered dispatch deadline for this plugin's non-shell built-ins;
`tool_timeouts` overrides it by tool name (`read`, `edit`, `path`, `search`,
`list_shells`, `wait_shell`, `read_shell`, `cancel_shell`). Unknown names and
`shell` are configuration errors. Workspace extensions and other plugins retain
their own registration policies.

The Agent-facing `shell.timeout_seconds` controls process runtime:

- Foreground: omitted/null uses `shell_max_timeout_seconds`; an explicit value
  must be positive and no greater than that maximum. Invalid values return
  `invalid_arguments` without launching the command.
- Background (`background: true`): omitted/null or `0` means unlimited runtime;
  positive finite values are independent of the foreground maximum. Negative
  values are invalid. Jobs do not survive session shutdown.
- A runtime deadline kills and reaps the process group and returns `tool_timeout`.
  A background deadline becomes a failed job, not user cancellation.

Process deadlines start on execution, excluding approval and job queue waits.
`wait_shell.timeout_ms` is a separate **wait** budget: `0` polls immediately,
omission waits until completion, and expiry never kills the job. Its enclosing
tool call is still bounded by the configured registration deadline.

Sandboxing owns isolation and cancellation cleanup, not a competing timeout.
Provider/network transport timeouts are separate. Dispatch cancellation cannot
forcibly stop synchronous Python work already running in a worker thread;
process-backed sandbox I/O and shell commands do support process cleanup.

## Process session defaults

The `session` plugin owns process-level defaults through its declared Config:

```yaml
- id: session
  config:
    workspace_root: /path/to/workspace
    provider_name: null
    no_plugins: false
```

`workspace_root` defaults to the current directory and is resolved to an
absolute path when mounted. `provider_name: null` leaves provider selection to
the Agent's layered LLM configuration; a non-null value explicitly selects a
provider for sessions opened through this server. It does not define another
provider catalog. `no_plugins` controls optional plugins in those Agent trees,
not the server's HTTP capabilities.

The server CLI converts launch arguments to an in-memory overlay of this entry;
`start_server_application(paths=..., overrides=...)` only loads that overlay and
starts the carrier. Embedded callers use the same `PluginOverlay` API, with no
parallel provider/workspace/no-plugins parameters. Unspecified
CLI options preserve YAML values; supplied `--workspace`, `--provider`, and
`--no-plugins` override them without writing the YAML file. These process
defaults are loaded at server startup, not live-reconfigured by catalog edits.
The session manager provides the process workspace to dependent plugins; the
HTTP carrier does not own a separate `ServerOptions` object. ACP supplies its
data directory through the same session configuration overlay while continuing
to take each Agent workspace from the ACP session request.

## Composition pattern

The session plugin provides the thread ArtifactStore independently of optional
conversation persistence. The application host does not construct or select an
artifact service, and ThreadPersistence does not own one. Session artifact
downloads first validate the logical reference against conversation history,
then use the live store or an offline filesystem reader for that thread.

The Agent Context owns the runtime StateService. Runtime persistence is bound
with `ThreadPersistence.create(..., state=ctx.state)`; it does not create a
second cache or supply the root Context's state. `ThreadPersistence.open(...)`
is the separate offline entry point and opens its own state reader. Creating
or reading either state view does not itself write a state file.

Disabling persistence selects `StateService.memory()` for that Agent Context.
Usage and other plugins keep their namespace API and values during the live
session but do not write plugin-state files. Active-session attachment and
message paging use live history; closing such a session leaves no resumable
conversation. Tool artifacts are still available when tools need them; this
setting is not a filesystem sandbox or a prohibition on tool-created files.

New managed threads defer metadata until they have durable content. Turn
completion flushes it; persistence-plugin teardown also flushes it when plugin
state or inbox/history was written before the first turn. This allows a normal
close/resume of a state-only thread without materializing an unused session.
This teardown guarantee does not make metadata and state writes atomic across
a process crash.

Live history stores retain their shared per-path trajectory state independently
of the bounded cache for recent offline readers. Switching among active threads
therefore does not force an append to reread its own prefix. Appends extend the
in-memory trace and lifetime turn count incrementally. Compaction validates its
replacement against the current projections before writing, then folds only
that new record after the append succeeds. Nested summaries retain ancestry
edges rather than repeatedly copying every original message ID. Clear resolves
those edges when it actually replaces the transcript span. Cold replay publishes
each projection only after validation succeeds; failed reads do not expose a
partially reconstructed view. Cold-history paging remains separate persistence
work.

The interactions plugin owns `client_events`; the application host only passes
an optional parent client interface as a launch fact. Permission and ask-user
services register their request types on that shared routing capability;
transports install a live sink through the public interface. A child without
its own sink forwards requests to its parent. Unloading the child removes only
its plugin-owned services, not the parent's routing capability.

The subagents plugin uses the process `SessionManager` as the sole owner of
child runtimes. `spawn_subagent` creates a stable direct-child thread and a
one-shot execution job; `send_message` writes a `RuntimeInput` to that thread's
canonical inbox without waking an idle Agent; `followup_task` creates a new job
on the same thread. Job IDs identify executions, while thread IDs identify
conversation targets. Child transcripts, pending inputs, resume, permissions,
and shutdown therefore use the same session path as every other Agent thread;
there is no plugin-local child executor or second mailbox.

Plugin import/construction and activation failures are isolated by default.
The loader warns and skips entries it cannot mount; XCore rolls back registered
effects when Config validation or `apply` fails, including nested `inject`
mounts. Its error log is diagnostic, not a request to terminate the whole tree.
Dependents whose required services are unavailable remain pending, while
unrelated plugins can run.

Hosts gate startup on capabilities they actually consume, using XCore
`Context.require`: Agent runtime/engine, HTTP server/sessions, ACP agent, and
the terminal client. There is no loader-maintained list of critical plugin
names. Capability owners must declare real dependencies: for example, the
Agent runtime requires the permission service so failed policy initialization
cannot silently turn into unguarded Tool execution. Missing required services
abort that host and clean it up; CLI/HTTP diagnostics retain activation errors.

Malformed tree declarations still fail parsing. Explicit configuration edits
still validate the selected plugin schema before writing; generic tree reads
do not import and prevalidate every plugin. Cancellation and process-exit
signals propagate. This policy concerns plugin activation, not arbitrary
runtime event handlers: the existing event dispatch error semantics remain.

The server carrier has no synthetic Agent session or session-bound `settings`.
The config plugin mounts those settings only when an actual session launch is
available. Its HTTP facet uses runtime paths for plugin configuration catalogs
and writes; session policy requests dispatch to the addressed Agent thread.

The root `plugin.py` is the only registration object. It can define named
dependency-gated mount functions for catalog, runtime, commands, and HTTP.
Routers are contributed through `server.contribute_router`; a route facet is
not a second plugin. A plugin's `apply` should be short and declarative:

```python
class ExamplePlugin:
    name = "example"

    async def apply(self, ctx: Context, config=None) -> None:
        await ctx.inject(["tools", "settings"], mount_runtime)
        await ctx.inject(["server"], mount_http)

plugin = ExamplePlugin()
```
