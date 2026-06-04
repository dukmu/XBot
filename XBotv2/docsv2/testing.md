# Testing

## Structure

```
tests/
  conftest.py               # Shared fixtures: temp_data_dir, temp_workspace
  core/                     # Core tests (NO plugins loaded)
    conftest.py
    test_hooks.py           # HookManager, all 42 stages
    test_state.py           # CoreStateStore, materializer, events, plugin state
    test_context.py         # ContextBuilder, fragments, cache
    test_builtin_filesystem.py  # Built-in filesystem tool metadata/write modes
    test_tool_registry.py   # ToolRegistry, filtering
    test_tool_runtime_cache.py  # Sandbox path resolution, permission events, tool-result cache hook
    test_sandbox.py         # SandboxPolicy
    test_permissions.py     # PermissionSystem
    test_engine.py          # Engine ReAct loop
    test_bootstrap.py       # Bootstrap sequence
    test_plugin_loader.py   # PluginLoader discovery, deps, manifest fragments, unload cleanup
    test_protocol.py        # Protocol frames, provider config, subprocess server, interaction events/responses, and terminal wrapper roundtrips
    test_tui_client.py      # Curses TUI state, interaction rendering, queue drain, and runtime import boundary
  plugins/                  # Per-plugin tests (loads only that plugin)
    planning/
    compact/
    skills/
    ...
  integration/              # Full integration with all plugins
```

## Principles

1. **No module-level state**: All caches are constructor-injected objects
2. **No ContextVar leakage**: Fixtures ensure cleanup after each test
3. **`temp_data_dir` only**: Never write to real `data/sessions/`
4. **MockLLM**: Deterministic, configurable response sequences
5. **Each test creates its own engine**: No shared state between tests
6. **Core tests load zero plugins**: Test the engine in its purest form

## Fixtures

| Fixture | Scope | Provides |
|---------|-------|----------|
| `temp_data_dir` | function | Temp data directory with config/sessions/personalities |
| `temp_workspace` | function | Temp workspace directory |
| `hook_manager` | function | Empty HookManager |
| `tool_registry` | function | Empty ToolRegistry |
| `permission_system` | function | PermissionSystem (default: ask) |
| `sandbox_policy` | function | SandboxPolicy (disabled) |
| `context_builder` | function | Fresh ContextBuilder |
| `mock_llm` | function | MockLLM with no responses |
| `state_store` | function | CoreStateStore in temp directory |
| `session_info` | function | Minimal SessionInfo |
| `hook_context` | function | Basic HookContext for loop hooks |

## MockLLM

```python
from langchain_core.messages import HumanMessage
from xbotv2.llm.mock import MockLLM

# Simple text responses
llm = MockLLM(responses=[
    {"content": "Hello!"},
])

# With tool calls
llm = MockLLM(responses=[
    {
        "content": "I'll check that.",
        "tool_calls": [
            {"name": "shell", "args": {"command": "ls"}, "id": "call_1"},
        ],
    },
    {"content": "Done. Found 3 files."},
])

# Verify tool calls
assert llm.verify_tool_call_made("shell", min_count=1)

# Verify request context passed to the provider
llm.invoke([HumanMessage(content="check context")])
assert [m.content for m in llm.get_call_messages(0)] == ["check context"]
```

## Running Tests

Run commands from the repository root. The root `pyproject.toml` sets
`pythonpath = ["XBotv2", "."]`, so XBotv2 tests import the package without a
manual `PYTHONPATH`.

```bash
# All core tests
uv run pytest XBotv2/tests/core/ -q

# Specific test file
uv run pytest XBotv2/tests/core/test_hooks.py -q

# Plugin loader discovery and materialized state coverage
uv run pytest XBotv2/tests/core/test_plugin_loader.py XBotv2/tests/core/test_state.py -q

# JSONL protocol, stdio server subprocess, and terminal wrapper roundtrips
uv run pytest XBotv2/tests/core/test_protocol.py -q

# Curses TUI state and import boundary coverage
uv run pytest XBotv2/tests/core/test_tui_client.py -q

# Protocol interaction events and terminal wrapper roundtrips
uv run pytest XBotv2/tests/core/test_protocol.py -q

# With verbose output
uv run pytest XBotv2/tests/core/ -v

# Run a single test
uv run pytest XBotv2/tests/core/test_engine.py::TestEngineBasics::test_simple_text_response -v
```

## Planned Token Budget Tests

`docsv2/token_budget_hooks.md` defines the evidence required before token
estimation and budget control are frozen. The important future checks are:

- context component metadata preserves source, plugin owner, and render order
- tool-schema filtering happens before provider binding
- provider request failures trigger a provider-specific hook plus `ON_ERROR`
- user intake, tool-call lifecycle, client-event, and persistence hooks receive
  the intended context fields
- message persistence round-trips provider/tool metadata while filtering
  internal `xbotv2_` side-channel kwargs from restored history
- stop, compaction, permission, failure, and post-tool-batch hooks receive the
  intended context fields
- observe-only token stats persist source breakdowns without changing behavior
- hard budget failures short-circuit before any provider call
