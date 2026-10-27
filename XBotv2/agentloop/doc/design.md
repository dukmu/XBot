# Agent loop and tools

The loop owns the authoritative thread working set, inbox, accepted turns,
model/tool sequencing and execution facts. It uses the provider-neutral model
contract and the standard tool registry, guards and dispatcher. It does not own
Agent definitions, permission decisions, compaction policy, storage or clients.

Public imports use `api/contracts.py` (input/inbox/tool contracts and tool-list
operation), `api/state.py` (`LoopState` and `ThreadExecution` consumer
protocols), `api/events.py` (hook declarations and payload/result types),
`api/outputs.py` (turn output values), and `api/protocol.py` (tool-catalog wire
values). Package roots do not re-export these declarations.

`impl/plugin.py` is the loader entry. It mounts tools and the existing loop
factory after runtime-log/launch services appear; the HTTP catalog additionally
waits for server and sessions. State mounting waits for launch and immutable
runtime variables, with optional thread persistence. It constructs the working
history, metadata lifecycle and inbox, restores their durable values when
present, and publishes `loop_state`, `thread_metadata` and `agent_inbox`.
Session prepares paths, artifacts and the common state service, then waits for
the loop-owned state before publishing its business service and commands.
This dependency relationship works independently of plugin-tree order.

`LoopRuntimeState`, `ConversationHistory`, `ThreadMetadataState`, `AgentInbox`,
Engine, ToolRegistry, ToolsService and tool execution are private implementation
objects. Cross-plugin callers consume the actual protocols they need; they do
not construct these objects. The existing single-purpose factory remains for
the current Agent composition and will be removed with that runtime relationship.

The working history and metadata are single mutable owners. Accepted input and
tool results commit through the supplied history sink before completion facts
are published. Turn count is a lifetime counter, preserved through edits and
compaction. Claims and active tasks are runtime facts; durable input
consumption follows committed history. This layout change does not bound or
redesign the current transcript cache or change persistence formats.

Tools registered by a plugin are owned by that plugin's XCore fiber. Later
callbacks pass the live owner explicitly. Registry identity protects later
registrations from stale cleanup. Execution resolves the enabled definition,
validates the original arguments, applies guards, calls the tool function with
its optional ToolCall parameter, and produces canonical ToolExecution values.
Synchronous functions run in a worker thread. The owning tool supplies any
dispatch deadline; permissions remain guard-owned. No slash command is turned
into a synthetic ToolCall.

`TOOLS`, `INBOX`, `AGENT_LOOP_FACTORY` (in `api/contracts.py`) and `LOOP_STATE`
(in `api/state.py`) are canonical ServiceKey declarations used by publication,
injection and consumers. Loop hook declarations are EventKey values parameterized
by their actual payload. Their `.name` is diagnostic text; subscribe with the
shared declaration object rather than constructing a new key with the same name.

Observer hooks use XCore emission; only `SHORT_CIRCUIT_EVENTS` use serial
interception and their declared result union. `EventDispatch`/`EventEmitter`
come from `xcore.api.events`, not a duplicate loop event port. Input receipt,
context/model requests and tool execution each retain their existing stages.
Loop outputs are separate from internal hook payloads and session HTTP/SSE
frames. Prompt-changing plugins affect context construction at its public hook;
the loop does not maintain a second prompt cache.

There is no separate loop plugin configuration model. Agent selection resolves
memory, identity, provider and execution settings; the default iteration limit
is `DEFAULT_MAX_ITERATIONS = 200`. Tool selection is a name tuple or `AllTools`.
The per-thread HTTP catalog is `GET /sessions/{session_id}/threads/{thread_id}/tools`.

```python
from XBotv2.agentloop.api.events import Events, TurnEnded
from XBotv2.agentloop.api.contracts import TOOLS
from XBotv2.core.api.tools import Tool, succeeded_text

async def lookup(query: str):
    """Look up a value."""
    return succeeded_text(query)

def mount(ctx):
    ctx.require(TOOLS).register(Tool.from_function(lookup))
    ctx.on(Events.TURN_END, on_turn_end)

def on_turn_end(event: TurnEnded):
    print(event.stop_reason)
```

Owner tests cover inbox identity/mutation, working-history lineage, registry,
argument preservation, guards, dispatch timeouts and worker responsiveness.
Composed startup, input routing, durable completion, provider streams and HTTP
remain central integration tests. Closing/reconnect/wait and storage semantics
are unchanged by this boundary migration.
