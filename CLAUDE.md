# CLAUDE.md

This file provides context for Claude Code when working in this repository.

## Project

XBot Hermes — single-user local agent runtime. State-centric architecture: file-backed agent DAG state, append-only event logs, LangGraph ReAct loop, permission/sandbox tool guards, task mode with executable plan DAG.

## Start Here

Read [`AGENTS.md`](./AGENTS.md) for the full agent guide covering architecture, conventions, current subsystem status, known issues, and development workflow.

## Quick Reference

### Run tests

```bash
uv run pytest -q
```

### Compile check

```bash
python -m py_compile main.py xbot/*.py tests/*.py
```

### Smoke test (needs provider)

```bash
uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke
```

### Run the agent

```bash
python main.py
```

## Key files

| File                    | Role                                                            |
| ----------------------- | --------------------------------------------------------------- |
| `xbot/state.py`       | File-backed agent DAG state (1300+ lines, most critical module) |
| `xbot/graph.py`       | LangGraph 3-node ReAct loop                                     |
| `xbot/interaction.py` | Main runtime entry point                                        |
| `xbot/tools.py`       | All 34 built-in tools                                           |
| `xbot/planning.py`    | DAG validation and deterministic scheduler                      |
| `xbot/context.py`     | System prompt assembly                                          |
| `status.md`           | Implementation progress log                                     |
| `task.md`             | Long-term design vision                                         |
| `plan.md`             | Refactoring plan                                                |

## Hard rules

1. Always update Dev Status to status.md
2. Keep The Agent system pluginable and modulized
3. Test-Driven Dev, always make sure write and use correct and reliable tests case.
4. Tests use `temp_data_dir`, never real `data/sessions/default`.
