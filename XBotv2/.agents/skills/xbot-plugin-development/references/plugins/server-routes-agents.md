# `server-routes-agents`

Agent selection HTTP routes — list and switch the active Agent.
Registered via `contribute_router()` as `xbot.http.agents`.

- **Import/profile:** `server-routes-agents`, server profile.
- **Source:** `XBotv2/agents/protocol.py`,
  `XBotv2/agents/http/plugin.py`.
- **Injects/provides:** none (uses `contribute_router`).
- **Subscribes to events:** `http/route` (`REGISTER_ROUTE`).

## Routes (`build_router`)

```python
def build_router(*, sessions: SessionsPort) -> APIRouter:
```

### `GET /sessions/{session_id}/threads/{thread_id}/agents` → `AgentListResponse`

```python
@router.get(
    "/sessions/{session_id}/threads/{thread_id}/agents",
    operation_id="list_agents",
)
async def list_agents(session_id: str, thread_id: str) -> AgentListResponse:
    catalog = await sessions.dispatch(
        session_id, thread_id, LIST_AGENTS, EmptyRequest()
    )
    return AgentListResponse(
        active=catalog.active,
        agents=[
            AgentInfo(
                name=definition.name,
                description=definition.description,
                mode=definition.mode,
                provider=definition.provider or "",
                model=definition.model or "",
                context_window=definition.context_window or 0,
            )
            for definition in catalog.agents
        ],
    )
```

### `PUT /sessions/{session_id}/threads/{thread_id}/agent` → `AgentSelectionResponse`

```python
@router.put(
    "/sessions/{session_id}/threads/{thread_id}/agent",
    operation_id="select_agent",
)
async def select_agent(
    session_id: str,
    thread_id: str,
    payload: AgentSelectionRequest,
) -> AgentSelectionResponse:
    selected = await sessions.dispatch(
        session_id, thread_id, SELECT_AGENT, SelectAgent(payload.name)
    )
    return AgentSelectionResponse(
        session_id=session_id,
        thread_id=thread_id,
        agent=selected.active,
        provider=selected.provider,
        model=selected.model,
        model_mode=selected.model_mode,
        context_window=selected.context_window,
    )
```

## Wire models

```python
class AgentInfo(WireModel):
    name: str = Field(min_length=1)
    description: str
    mode: Literal["primary", "subagent", "all"]
    provider: str = ""
    model: str = ""
    context_window: int = Field(default=0, ge=0)

class AgentListResponse(WireModel):
    active: str = ""
    agents: list[AgentInfo] = Field(default_factory=list)

class AgentSelectionRequest(WireModel):
    name: str = Field(min_length=1)

class AgentSelectionResponse(WireModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str
    model_mode: str = ""
    context_window: int = Field(ge=0)
```

## Cross-references

- Depends on: `server` (`contribute_router`), `sessions` (`SessionsPort`).
- Depended on by: HTTP agent clients, TUI agent views.
- Pairs with: `agent-catalog` (`LIST_AGENTS`), `agent-runtime` (`SELECT_AGENT`).

## Common pitfalls

- **`AgentSelectionRequest.name` must match a registered agent**:
  the agent name is validated against `AgentCatalog` definitions
  during `SELECT_AGENT` dispatch. Unknown agents raise `OperationError`.
- **`mode="primary"` agents are selectable**: `LIST_AGENTS` filters
  out hidden agents, but primary agents are included. Use `hidden=True`
  to register system-only agents.
- **`context_window` defaults to 0**: if `AgentDefinition.context_window`
  is None, it becomes 0 in the response. Consumers should check for
  this as "unspecified".
