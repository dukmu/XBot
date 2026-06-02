# XBot Refactor Status

## Objective

Follow `plan.md` under the constraints from `task.md`: move XBot toward a state-centered Hermes runtime with file-backed agent DAG state, append-only events, explicit runtime contracts, and verification coverage.

Current continuation objective: make the personality configuration system consistent and intuitive, clean old config logic, replace brittle coverage with behavior-oriented smoke tests, and prove an isolated code refactor run is auditable with the current DeepSeek provider.

## Current Scope

- Phase 1: Add file-backed agent DAG state without replacing LangGraph.
- Phase 2: Add explicit runtime contract records at the interaction boundary.
- Phase 3: Move context-frame construction out of the LangGraph graph module.
- Phase 4: Persist large tool-result cache entries to files.
- Phase 5: Make `plan.yaml` an executable DAG with validation, ready-node selection, and versioning.
- Preserve current terminal/runtime behavior while adding observability and recovery foundations.

## Progress

- [x] Read `plan.md` and `task.md`.
- [x] Confirmed current gap: runtime state is in-memory and not task-file-backed.
- [x] Implement file-backed agent state directory and append-only logs.
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
- [x] Verification phase progress: `xbot/verification.py` verifies agent state files, plan DAG validity, append-only log counts, and `state.yaml` materialized consistency.
- [x] Runtime path isolation progress: `xbot.config` uses context-local runtime paths instead of a single process-global `_RUNTIME_PATHS`.
- [x] Personality layout progress: canonical config is now `data/personalities/<id>/personality.yaml`, `instructions.md`, `memory.md`, `permissions.json`, `sandbox.json`, and `skills/`.
- [x] Old config logic cleanup: `xbot.config` no longer reads `data/personality`, `AGENT.md`, `MEMORY.md`, `person.yaml`, or global `data/config/agent|permissions|sandbox`.
- [x] Behavior smoke coverage: `tests/test_personality_runtime.py` creates an isolated data dir, runs a smoke provider through `HermesInteraction`, refactors `calculator.py`, and verifies task audit state.
- [x] Real provider smoke script: `scripts/provider_smoke_refactor.py` creates an isolated data dir and runs an actual provider, defaulting to DeepSeek OpenAI-compatible config.
- [x] Real DeepSeek provider smoke completed successfully.
- [x] Context tree MVP: `context_tree.jsonl` records append-only context nodes, `state.yaml` materializes head/node counts, and `context_rewind` moves the head without deleting history.
- [x] Mailbox MVP: `mailbox.jsonl` records send/read acknowledgements, `state.yaml` materializes pending counts, and agent-facing tools can send/read messages.
- [x] Subagent MVP: `subagent_create(mode="attach")` runs a child thread inside the parent session, accesses the main workspace, writes a result, and reports back through parent mailbox.
- [x] Checkpoint persistence MVP: `FileBackedSaver` persists LangGraph checkpoints to `data/sessions/<id>/saver/langgraph.pkl` and reloads them across saver instances.
- [x] Agent state layout MVP: primary agent DAG state now lives at `data/sessions/<id>/state/`; attach subagents use `subagents/<id>/state/` and their own `saver/`.
- [x] Trace persistence guard MVP: detailed normalized `InteractionEvent` traces are no longer persisted by default; enable with `XBOT_TRACE_EVENTS=1` or `trace_events=True`.
- [x] Debug tools MVP: `debug_analyze` summarizes task DAG, plan, state, context tree, mailbox, and subagent manifests.
- [x] Event-write performance MVP: append-only logs remain immediate, while materialized `state.yaml` rewrites are batched during turn event projection.
- [x] Task mode MVP: `task_begin` records global goal, replaces executable DAG, writes `context.md`, and `plan_next`/`plan_update` actively drive nodes.
- [x] Read locator MVP: `filesystem_read` supports pattern search, line ranges, context lines, and max char truncation.
- [x] Summary/context MVP: compaction and `summary_add` write durable summary artifacts, and latest summaries plus pending mailbox project into `context.md`.
- [x] DAG attribution MVP: task-mode graph events and summaries carry `plan_node_id`; `state.yaml` exposes per-node DAG activity and `plan_node_history` inspects a node's event trail.
- [x] Memory tools MVP: `memory_update` writes structured entries, while `memory_list` and `memory_search` make long-term memory queryable.
- [x] Debug DAG view MVP: `debug_analyze(scope="dag")` exposes plan nodes, per-node activity, recent graph events, and event counts grouped by node/type.
- [x] Task mode guard MVP: plan mutation/scheduling tools require task mode, completed exit refuses unfinished/blocked/failed DAG nodes, and `context.md` includes completed plus blocked/failed node projections.
- [x] Single-active DAG scheduler MVP: `plan_next` returns the existing running node instead of opening a second running node.
- [x] Task-mode prompt contract MVP: system prompt tells the model to use `task_begin` for complex multi-step work and drive the DAG through `plan_next`/`plan_update`.
- [x] Plan autofill MVP: `plan_autofill` grows a standard inspect/implement/verify/report DAG skeleton and skips duplicate stage types.
- [x] Task guidance MVP: `task_status` and `debug_analyze` expose `next_action` recommendations for chat/ready/running/blocked/completed DAG states.
- [x] Claims/summary structure MVP: `claim_add`/`claim_list` write structured evidence claims, summaries use markdown front matter, and `verify_task_state` checks both.
- [x] Subagent DAG/debug MVP: attach-mode subagents start with their own delegation DAG, parent graph records delegated/finished events, and `debug_analyze` expands child DAG summaries.
- [x] Live stream trace attribution MVP: when trace is enabled, stream events are persisted at event time, token deltas are not persisted, and tool calls keep the active DAG node attribution.
- [x] Plan success-state MVP: `completed` and `verified` both satisfy DAG dependencies, avoiding provider-specific wording from deadlocking the scheduler.

## Notes

- Multi-agent remains MVP-only: mailbox, attach-mode subagents, and child runtime layout exist, but there is not yet a full async runner/scheduler.
- The first implementation should keep existing behavior stable and add state as an outer runtime layer.
- Targeted verification passed: `uv run pytest -q tests/test_runtime_boundaries.py -k "task_state_store or interaction_records_file_state or tool_result_cache"` (`4 passed`).
- Compile verification passed: `python -m py_compile main.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py`.
- Full verification passed: `uv run pytest -q` (`58 passed`).
- Full compile verification passed: `python -m py_compile main.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py`.
- Latest local verification passed: `uv run pytest -q` (`60 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Latest full verification passed: `uv run pytest -q` (`70 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after summary/context projection changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`72 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after DAG attribution and memory tools: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`73 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after task-mode guard changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`79 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Latest DeepSeek smoke passed: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke` (`SMOKE PASSED`, two consecutive DAG tasks, trajectory validation enabled).
- Latest full verification passed: `uv run pytest -q` (`80 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Latest full verification passed: `uv run pytest -q` (`81 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Latest full verification passed: `uv run pytest -q` (`82 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Latest strict DeepSeek smoke passed: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke` (`SMOKE PASSED`, two consecutive DAG tasks, compact accepted via tool/runtime event, key tools DAG-attributed, no persisted token deltas, calculator.py and stats.py claims both present).
- Latest full verification passed: `uv run pytest -q` (`74 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after single-active DAG scheduler changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`75 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after task-mode prompt contract changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`76 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after plan autofill changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`77 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after task guidance changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`77 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after agent state layout changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`78 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after trace persistence guard changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The successful run is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Latest full verification passed: `uv run pytest -q` (`79 passed`).
- Latest compile verification passed: `python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py`.
- Real DeepSeek smoke passed after dual-task trajectory changes: `uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke`. The run completed two refactor tasks, verified required task/DAG/compact/claim/summary/file tool traces, and is auditable at `/tmp/xbot-deepseek-smoke/sessions/deepseek-smoke/state/`.
- Context tree targeted verification passed: `uv run pytest -q tests/test_runtime_boundaries.py -k "context_tree or task_state_store_materializes_events or verify_task_state or tool_sandbox"` (`4 passed`).
- Checkpoint persistence targeted verification passed: `uv run pytest -q tests/test_agent.py::test_persistence_checkpoint_restore` (`1 passed`).

## Acceptance Audit

- Phase 1 file-backed agent state: complete. `xbot/state.py` creates `task.yaml`, `goal.md`, `plan.yaml`, `events.jsonl`, `graph.jsonl`, `state.yaml`, `context.md`, `claims.yaml`, `artifacts/`, `checkpoints/`, `versions/`, `summaries/`, and `locks/`.
- Append-only runtime and graph logs: complete. `TaskStateStore` appends JSONL events and materializes `state.yaml`.
- Interaction integration: complete. `HermesInteraction` records user/resume turns and normalized interaction events when a `TaskStateStore` is present; `create()` initializes the primary DAG state under `data/sessions/<session_id>/state/`.
- Runtime contracts: complete for this pass. `RunRecord` and `TurnRecord` are explicit Pydantic models and are used at the interaction boundary.
- Verification coverage: complete for this pass. Tests cover agent state initialization, materialization from event logs, and interaction event persistence.
- Out of scope by plan: multi-agent execution, mailbox, rewind/context tree, and replacing LangGraph checkpoint persistence.
- Loop decoupling: complete for the MVP. Context construction lives in `xbot/context.py`, context compaction lives in `xbot/compaction.py`, and tool guard/interrupt/cache hooks live in `xbot/tool_runtime.py`.
- Tool-result cache persistence: complete for MVP. `HermesInteraction.create()` configures `GLOBAL_TOOL_RESULT_CACHE` to write under the session cache directory.
- Plan/DAG state: complete for MVP. `plan.yaml` is validated as a DAG, `state.yaml` includes a scheduler view, and plan change snapshots are indexed under `versions/plans`.
- Explicit runtime context: complete for MVP. Session, personality, thread, task, run, trace, and path identifiers are represented by `RuntimeContext`; legacy global path helpers remain for tools/config compatibility.
- Verification phase: complete for MVP. `verify_task_state()` checks required agent state files, plan validity, event counts, graph-event counts, and plan projection errors.
- Runtime path isolation: complete for MVP. Existing helper APIs remain, but the underlying path state is context-local and covered by tests.
- Personality config system: complete locally. Directory layout is canonical and lower-case under `data/personalities`.
- Isolated smoke behavior: complete with smoke model and real DeepSeek provider. The DeepSeek run changed `calculator.py` in an isolated workspace and produced auditable agent state files.
- Context tree/rewind: MVP complete. Remaining scope is context projection from tree branches into model prompts and richer branch inspection commands.
- Mailbox: MVP complete. Remaining scope is wiring runtime background events onto the mailbox queue.
- Subagent: attach-mode MVP complete. Remaining scope is true async detach runner, budgets/timeouts, cancellation, and child workspace diff handoff.
- Persistence: checkpoint MVP complete via file-backed saver. Remaining scope is replacing pickle-backed checkpoint/store with official SQLite/Postgres packages when available.
- Current subagent layout note: new child runs stay under the parent session. Existing `data/sessions/*__subagent__*` directories are historical manual-run artifacts from the earlier implementation and were not deleted.
