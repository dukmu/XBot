# XBot Refactor Status

## Objective

Follow `plan.md` under the constraints from `task.md`: move XBot toward a state-centered Hermes runtime with file-backed task state, append-only events, explicit runtime contracts, and verification coverage.

Current continuation objective: make the personality configuration system consistent and intuitive, clean old config logic, replace brittle coverage with behavior-oriented smoke tests, and prove an isolated code refactor run is auditable with the current DeepSeek provider.

## Current Scope

- Phase 1: Add file-backed task state without replacing LangGraph.
- Phase 2: Add explicit runtime contract records at the interaction boundary.
- Phase 3: Move context-frame construction out of the LangGraph graph module.
- Phase 4: Persist large tool-result cache entries to files.
- Phase 5: Make `plan.yaml` an executable DAG with validation, ready-node selection, and versioning.
- Preserve current terminal/runtime behavior while adding observability and recovery foundations.

## Progress

- [x] Read `plan.md` and `task.md`.
- [x] Confirmed current gap: runtime state is in-memory and not task-file-backed.
- [x] Implement file-backed task directory and append-only logs.
- [x] Connect `HermesInteraction` to the file-backed state store.
- [x] Add tests for event replay/materialized state and interaction logging.
- [x] Run verification commands.
- [x] Update README and architecture/testing docs.
- [x] Complete acceptance audit against `plan.md` scope.
- [x] Phase 3 progress: context-frame construction moved into `xbot/context.py`.
- [x] Phase 4 progress: tool-result cache can persist large results to files and reload them.
- [x] Phase 5 progress: `xbot/planning.py` validates plan DAGs, computes `ready_nodes`/`active_node`, and versions plan updates through `TaskStateStore`.
- [x] Loop decoupling progress: tool guardrails, permission/sandbox interrupt handling, and tool-result cache hooks moved into `xbot/tool_runtime.py`.
- [x] Loop decoupling progress: context compaction moved into `xbot/compaction.py`.
- [x] Runtime context progress: `xbot/runtime.py` adds explicit `RuntimeContext`, and `HermesInteraction` uses it for session/personality/thread/task/run identity at the boundary.
- [x] Verification phase progress: `xbot/verification.py` verifies task files, plan DAG validity, append-only log counts, and `state.yaml` materialized consistency.
- [x] Runtime path isolation progress: `xbot.config` uses context-local runtime paths instead of a single process-global `_RUNTIME_PATHS`.
- [x] Personality layout progress: canonical config is now `data/personalities/<id>/personality.yaml`, `instructions.md`, `memory.md`, `permissions.json`, `sandbox.json`, and `skills/`.
- [x] Old config logic cleanup: `xbot.config` no longer reads `data/personality`, `AGENT.md`, `MEMORY.md`, `person.yaml`, or global `data/config/agent|permissions|sandbox`.
- [x] Behavior smoke coverage: `tests/test_personality_runtime.py` creates an isolated data dir, runs a smoke provider through `HermesInteraction`, refactors `calculator.py`, and verifies task audit state.
- [x] Real provider smoke script: `scripts/provider_smoke_refactor.py` creates an isolated data dir and runs an actual provider, defaulting to DeepSeek OpenAI-compatible config.
- [x] Real DeepSeek provider smoke completed successfully.
- [x] Context tree MVP: `context_tree.jsonl` records append-only context nodes, `state.yaml` materializes head/node counts, and `context_rewind` moves the head without deleting history.
- [x] Mailbox MVP: `mailbox.jsonl` records send/read acknowledgements, `state.yaml` materializes pending counts, and agent-facing tools can send/read messages.
- [x] Subagent MVP: `subagent_create(mode="attach")` runs a child thread inside the parent session, accesses the main workspace, writes a result, and reports back through parent mailbox.
- [x] Checkpoint persistence MVP: `FileBackedSaver` persists LangGraph checkpoints to `data/sessions/<id>/checkpoints/langgraph.pkl` and reloads them across saver instances.
- [x] Debug tools MVP: `debug_analyze` summarizes task DAG, plan, state, context tree, mailbox, and subagent manifests.
- [x] Event-write performance MVP: append-only logs remain immediate, while materialized `state.yaml` rewrites are batched during turn event projection.

## Notes

- No multi-agent, mailbox, rewind, or full LangGraph rewrite in this pass.
- The first implementation should keep existing behavior stable and add state as an outer runtime layer.
- Targeted verification passed: `uv run pytest -q tests/test_runtime_boundaries.py -k "task_state_store or interaction_records_file_state or tool_result_cache"` (`4 passed`).
- Compile verification passed: `python -m py_compile main.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py`.
- Full verification passed: `uv run pytest -q` (`58 passed`).
- Full compile verification passed: `python -m py_compile main.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py`.
- Latest local verification passed: `uv run pytest -q` (`60 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Latest full verification passed: `uv run pytest -q` (`66 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after subagent/runtime fixes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/tasks/calculator-refactor/`.
- Context tree targeted verification passed: `uv run pytest -q tests/test_runtime_boundaries.py -k "context_tree or task_state_store_materializes_events or verify_task_state or tool_sandbox"` (`4 passed`).
- Checkpoint persistence targeted verification passed: `uv run pytest -q tests/test_agent.py::test_persistence_checkpoint_restore` (`1 passed`).

## Acceptance Audit

- Phase 1 file-backed task state: complete. `xbot/state.py` creates `task.yaml`, `goal.md`, `plan.yaml`, `events.jsonl`, `graph.jsonl`, `state.yaml`, `context.md`, `claims.yaml`, `artifacts/`, `checkpoints/`, `summaries/`, and `locks/`.
- Append-only runtime and graph logs: complete. `TaskStateStore` appends JSONL events and materializes `state.yaml`.
- Interaction integration: complete. `HermesInteraction` records user/resume turns and normalized interaction events when a `TaskStateStore` is present; `create()` initializes one under `data/sessions/<session_id>/tasks/<thread_id>/`.
- Runtime contracts: complete for this pass. `RunRecord` and `TurnRecord` are explicit Pydantic models and are used at the interaction boundary.
- Verification coverage: complete for this pass. Tests cover task directory initialization, materialization from event logs, and interaction event persistence.
- Out of scope by plan: multi-agent execution, mailbox, rewind/context tree, and replacing LangGraph checkpoint persistence.
- Loop decoupling: complete for the MVP. Context construction lives in `xbot/context.py`, context compaction lives in `xbot/compaction.py`, and tool guard/interrupt/cache hooks live in `xbot/tool_runtime.py`.
- Tool-result cache persistence: complete for MVP. `HermesInteraction.create()` configures `GLOBAL_TOOL_RESULT_CACHE` to write under the session cache directory.
- Plan/DAG state: complete for MVP. `plan.yaml` is validated as a DAG, `state.yaml` includes a scheduler view, and prior plan versions are stored under `checkpoints/plans`.
- Explicit runtime context: complete for MVP. Session, personality, thread, task, run, trace, and path identifiers are represented by `RuntimeContext`; legacy global path helpers remain for tools/config compatibility.
- Verification phase: complete for MVP. `verify_task_state()` checks required task files, plan validity, event counts, graph-event counts, and plan projection errors.
- Runtime path isolation: complete for MVP. Existing helper APIs remain, but the underlying path state is context-local and covered by tests.
- Personality config system: complete locally. Directory layout is canonical and lower-case under `data/personalities`.
- Isolated smoke behavior: complete with smoke model and real DeepSeek provider. The DeepSeek run changed `calculator.py` in an isolated workspace and produced auditable task files.
- Context tree/rewind: MVP complete. Remaining scope is context projection from tree branches into model prompts and richer branch inspection commands.
- Mailbox: MVP complete. Remaining scope is wiring runtime background events onto the mailbox queue.
- Subagent: attach-mode MVP complete. Remaining scope is true async detach runner, budgets/timeouts, cancellation, and child workspace diff handoff.
- Persistence: checkpoint MVP complete via file-backed saver. Remaining scope is replacing pickle-backed checkpoint/store with official SQLite/Postgres packages when available.
- Current subagent layout note: new child runs stay under the parent session. Existing `data/sessions/*__subagent__*` directories are historical manual-run artifacts from the earlier implementation and were not deleted.
