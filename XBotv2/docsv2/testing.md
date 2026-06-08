# XBotv2 Testing

## Test Suite

**378 passed, 2 warnings** (June 2026).

```bash
.venv/bin/python -m pytest XBotv2/tests -q
```

## Coverage

Overall coverage: **~79%** (5371 statements).

Key module coverage:
- `hooks/manager.py`: 100%
- `hooks/types.py`: 100%
- `llm/messages.py`: 100%
- `core/context.py`: 96%
- `core/bootstrap.py`: 95%
- `core/engine.py`: 82%
- `tools/runtime.py`: 72%

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
│   ├── test_protocol.py     # Protocol frames, provider config
│   ├── test_tool_registry.py # Namespace register, restrict, filter
│   ├── test_tool_runtime_cache.py # Tool execution, result cache
│   ├── test_tool_dispatch_timeout.py # Timeout handling
│   ├── test_tui_client.py   # TUI state, permission/reasoning rendering
│   └── test_plugin_loader.py # Plugin discovery, loading, rollback
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

- `temp_data_dir`: isolated data/config directory
- `temp_workspace`: isolated workspace directory
- `state_store`: CoreStateStore with a session
- `sandbox_policy`: SandboxPolicy(enabled=False)

## Running Specific Tests

```bash
# Core engine tests
pytest XBotv2/tests/core/test_engine.py -v

# Skill and MCP tests
pytest XBotv2/tests/core/test_skills.py XBotv2/tests/core/test_mcp.py -v

# Integration tests
pytest XBotv2/tests/integration/ -v

# With coverage
pytest XBotv2/tests --cov=xbotv2 --cov-report=term-missing -q
```
