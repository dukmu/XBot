# XBotv2 Testing

## Test Suite

```bash
uv run pytest XBotv2/tests -q
```

## Coverage

Generate current coverage instead of recording a count that drifts with every
iteration:

```bash
uv run pytest XBotv2/tests --cov=xbotv2 --cov-report=term-missing -q
```

## Test Structure

```
tests/
├── conftest.py              # fixtures: temp_data_dir, temp_workspace, state_store
├── core/
│   ├── test_engine.py       # ReAct loop, hooks, streaming, reasoning, compaction
│   ├── test_context.py      # ContextBuilder, sanitize, cache
│   ├── test_bootstrap.py    # Bootstrap flow, tool filter
│   ├── test_state.py        # CoreStateStore
│   ├── test_persistence.py  # Message persistence, artifacts
│   ├── test_command.py      # CommandSpec, kind field, search/completion
│   ├── test_sandbox.py      # SandboxPolicy, BubblewrapBackend capabilities
│   ├── test_skills.py       # SkillRegistry, shell injection, permission scope
│   ├── test_mcp.py          # MCPClient stdio/HTTP, MCPTool, result normalization
│   ├── test_hooks.py        # HookManager registration, execution
│   ├── test_permissions.py  # PermissionSystem, rule matching
│   ├── test_protocol.py     # Provider and protocol-adjacent config
│   ├── test_sse.py          # Shared SSE encoder/decoder
│   ├── test_tool_registry.py # Namespace register, restrict, filter
│   ├── test_tool_runtime_cache.py # Tool execution, result cache
│   ├── test_tool_dispatch_timeout.py # Timeout handling
│   ├── test_tui_client.py   # TUI state, permission/reasoning rendering
│   ├── test_plugin_loader.py # Plugin discovery, loading, rollback
│   └── test_plugin_store.py  # Plugin state isolation and atomic persistence
├── integration/
│   ├── test_http_transport.py # HTTP/SSE endpoints, session lifecycle, commands
│   ├── test_tui_interaction.py # TUI slash commands, help, skills, completion
│   └── test_tui_interrupt_and_usage.py # Interrupt handling, usage events
└── bench/
    └── test_http_latency.py  # Transport latency benchmark
```

## MockLLM

Deterministic provider for tests. Supports:
- `responses`: list of `{content, tool_calls, chunks, usage_metadata}`
- `chunks`: simulate streaming deltas with `additional_kwargs` (reasoning)
- `call_history`: verify LLM calls

```python
llm = MockLLM(responses=[{
    "content": "Hello",
    "chunks": [{"content": "Hel"}, {"content": "lo"}],
}])
```

## Fixtures

- `temp_data_dir`: isolated data directory (contains `config/` subdir)
- `temp_workspace`: isolated workspace directory
- `state_store`: CoreStateStore with a session
- `sandbox_policy`: SandboxPolicy(enabled=False)
- `tests/fixtures/sse/`: golden HTTP/SSE stream envelopes used by integration
  tests.

The SSE fixtures cover every `KNOWN_SERVER_EVENT_TYPES` value. The shared codec
suite covers comments, multi-line data, text event ids, unterminated final
messages, Unicode, and invalid control-field line breaks.

HTTP integration tests also run real local uvicorn sockets for agent-initiated
permission and `ask_user` requests. They verify that interaction requests are
registered before publication, remain answerable while SSE waits, and resume
the original tool/agent turn after the response endpoint records an answer.
They also interrupt live permission and `ask_user` waits, require the original
SSE stream to emit `turn_cancelled`, and verify that late responses return 410.
Protocol tests reject incomplete interaction, error, tool-result, and usage
payloads. HTTP integration tests assert the complete `ErrorResponse` shape so
handlers cannot drift into endpoint-specific error dictionaries.
The SSE suite also requires the typed payload inventory to cover every known
server event type. Direct turn-stream tests verify that early consumer closure
cancels and closes the background Engine stream, releases the session lock,
and does not leave an interaction waiter running.

## Running Specific Tests

```bash
# Core tests, compilation, and diff checks
python XBotv2/scripts/check_iteration.py

# Core engine tests
pytest XBotv2/tests/core/test_engine.py -v

# Skill and MCP tests
pytest XBotv2/tests/core/test_skills.py XBotv2/tests/core/test_mcp.py -v

# Integration tests
pytest XBotv2/tests/integration/ -v

# With coverage
pytest XBotv2/tests --cov=xbotv2 --cov-report=term-missing -q
```
