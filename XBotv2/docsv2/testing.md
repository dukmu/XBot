# Testing

## Structure

```text
tests/
  conftest.py
  core/
    test_bootstrap.py
    test_builtin_filesystem.py
    test_command.py
    test_context.py
    test_engine.py
    test_hooks.py
    test_permissions.py
    test_persistence.py
    test_plugin_loader.py
    test_protocol.py
    test_sandbox.py
    test_state.py
    test_tool_dispatch_timeout.py
    test_tool_registry.py
    test_tool_runtime_cache.py
    test_tui_client.py
  integration/
    test_http_transport.py
    test_tui_interaction.py
    test_tui_interrupt_and_usage.py
  bench/
    test_http_latency.py
```

## Principles

- Tests use temporary data/workspace directories unless explicitly validating the
  checked-in Stage 2 data layout.
- Core tests pass `plugin_dirs=[]` for pure-core cases.
- `MockLLM` provides deterministic provider responses and records provider input
  messages.
- Runtime session output is ignored; config files are trackable.
- TUI tests exercise transport/session boundaries and keep runtime imports out of
  `xbotv2.tui` modules.

## Fixtures

| Fixture | Provides |
| --- | --- |
| `temp_data_dir` | Temporary Stage 2 data root with `config/` and `sessions/` |
| `temp_workspace` | Temporary external workspace root |
| `hook_manager` | Empty HookManager |
| `tool_registry` | Empty ToolRegistry |
| `permission_system` | PermissionSystem with default ask |
| `sandbox_policy` | SandboxPolicy for temp workspace |
| `context_builder` | Fresh ContextBuilder |
| `mock_llm` | MockLLM with no responses |
| `state_store` | CoreStateStore with workspace/provider metadata |
| `session_info` | SessionInfo with session/thread/workspace/provider |

## Coverage Targets

Stage 2 tests cover:

- default generated session ids
- explicit resume missing session returns 404
- one HTTP server hosting multiple workspace roots
- `AGENTS.md` inclusion in provider-facing context
- shell cwd set to workspace root
- sandbox defaults for workspace/external read/write
- symlink escape denial
- HTTP command discovery and execution
- provider listing from `providers.yaml`
- provider/permission/sandbox session override materialization
- permission response scope forwarding for session/global approvals
- policy command validation and live-policy reset behavior
- command results not entering message history
- TUI dynamic command completion and server command dispatch
- live permission and user-input interaction flow
- ESC interrupt and realtime usage rendering

## Running Tests

Run from repository root.

```bash
# Full XBotv2 test tree
.venv/bin/python -m pytest XBotv2/tests

# Core only
.venv/bin/python -m pytest XBotv2/tests/core

# HTTP protocol integration
.venv/bin/python -m pytest XBotv2/tests/integration/test_http_transport.py

# TUI integration
.venv/bin/python -m pytest XBotv2/tests/integration/test_tui_interaction.py \
  XBotv2/tests/integration/test_tui_interrupt_and_usage.py

# Diff and whitespace check before commit
git diff --check
```

Current Stage 2 verification baseline:

```text
370 passed, 2 warnings
```

The warnings are third-party `websockets` deprecations emitted through uvicorn.

## MockLLM

```python
from xbotv2.llm.mock import MockLLM

llm = MockLLM(responses=[
    {"content": "checking", "tool_calls": [
        {"name": "shell", "args": {"command": "pwd"}, "id": "call_pwd"},
    ]},
    {"content": "done"},
])
```

Use `llm.get_call_messages(index)` to inspect provider-facing context. This is
how tests verify that workspace `AGENTS.md` enters the system prompt and command
results stay out of message history.
