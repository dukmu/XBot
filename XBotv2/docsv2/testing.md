# Testing

## Structure

```
tests/
  conftest.py               # Shared fixtures: temp_data_dir, temp_workspace
  core/                     # Core tests (NO plugins loaded)
    conftest.py
    test_hooks.py           # HookManager, all 17 stages
    test_state.py           # CoreStateStore, events, plugin state
    test_context.py         # ContextBuilder, fragments, cache
    test_tool_registry.py   # ToolRegistry, filtering
    test_sandbox.py         # SandboxPolicy
    test_permissions.py     # PermissionSystem
    test_engine.py          # Engine ReAct loop
    test_bootstrap.py       # Bootstrap sequence
    test_plugin_loader.py   # Plugin discovery, deps
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
```

## Running Tests

```bash
# All core tests
python -m pytest tests/core/ -q

# Specific test file
python -m pytest tests/core/test_hooks.py -q

# With verbose output
python -m pytest tests/core/ -v

# Run a single test
python -m pytest tests/core/test_engine.py::TestEngineBasics::test_simple_text_response -v
```
