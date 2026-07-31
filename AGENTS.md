# XBot Development Guidelines

## Project Direction

`XBotv2` is the mainline implementation. It is a general, readable C/S Agent
system with stable public APIs. Prefer simple control flow, explicit ownership,
and consistent behavior over framework-like abstraction.

Every change must preserve these priorities:

1. correctness and stable external behavior;
2. concise, consistent, readable code;
3. clear boundaries between core, protocol, providers, tools, plugins, and
   clients;
4. enough documentation and meaningful verification;
5. minimal new concepts and dependencies.

Do not revive legacy implementations or retain compatibility paths without an
explicit requirement.

## Environment

- Use the repository virtual environment at `.venv`.
- Install and update dependencies with `uv`; do not modify the global Python
  environment.
- Run XBot commands through `.venv/bin/xbot` or `uv run`.
- Run tests with the repository environment and `PYTHONPATH=XBotv2` when
  required.
- The evaluation project uses its own environment under `evaluation/.venv`.
- Use UTF-8 exclusively for source, configuration, documentation, and persisted
  text.
- Do not commit virtual environments, caches, generated web bundles, runtime
  sessions, logs, or evaluation result directories.

## Git Workflow

`main` is the stable branch.

- Do not develop directly on `main`.
- Create `dev-*` branches for features and planned improvements.
- Create `fix-*` branches for focused bug fixes.
- Branch from the latest tested `main`.
- Keep implementation, tests, evaluation, and evaluation-driven fixes on that
  same development branch until the work is accepted.
- If evaluation exposes a defect, fix it on the current development branch and
  rerun the affected verification there. Do not move the work through an
  intermediate integration branch.
- Merge the fully reviewed and evaluated development branch into `main` once,
  with a meaningful merge commit.
- Keep commits small and organized by responsibility. Do not mix core changes,
  generated artifacts, evaluation changes, and unrelated cleanup.
- Never rewrite, remove, or include user changes that are outside the task.
- Record the exact evaluated development commit. Any code change after that
  point invalidates the recorded result until the affected evaluation is run
  again.
- Keep raw evaluation logs, result directories, runtime sessions, and generated
  viewers local and ignored. Commit evaluation framework or task changes only
  when they are durable project inputs.

## Design Boundaries

- Protocol types describe transport contracts; they do not own plugin or
  command business logic.
- Human-facing slash commands, Agent-facing tools, prompt expansion, and client
  commands have different execution boundaries. Do not route one through
  another for convenience.
- Reuse the standard tool registry and execution path. Do not create wrapper
  executors, synthetic ToolCalls, or hard-coded permission bypasses.
- Keep provider-specific message, usage, tool-call, and retry behavior inside
  provider implementations behind the common provider contract.
- Keep runtime-only state separate from persisted conversation state.
- Core capabilities must not depend on a particular plugin through hard-coded
  names or injected callbacks.
- Prefer existing repository patterns and standard schemas. Add an abstraction
  only when it removes demonstrated duplication or clarifies ownership.

## Implementation Discipline

- Read the relevant ownership boundary before editing.
- Keep changes scoped; do not combine a bug fix with broad refactoring.
- Remove obsolete logic instead of adding another fallback around it.
- Do not silently recover from malformed internal state or unsupported
  behavior. Return a clear error at the responsible boundary.
- Make configuration semantics consistent across global, workspace, session,
  and Agent layers.
- Update user or developer documentation when behavior, configuration, API, or
  workflow changes.
- Comments should explain non-obvious constraints, not narrate ordinary code.

## Verification

Testing is evidence, not the definition of completion.

- Run focused tests for the behavior being changed.
- Run broader core or integration suites when the blast radius crosses module
  boundaries.
- Prefer observable behavior over private fields, literal prompt text, or
  duplicated implementation assertions.
- Do not add tests solely to raise coverage or lock in incidental structure.
- A green suite does not prove provider interoperability, interactive behavior,
  recovery, or test quality. Inspect the exercised path and failure modes.
- Use real provider or interactive smoke tests when the defect depends on
  streaming, tool-call formatting, permissions, reconnect behavior, or TUI
  interaction.
- Record tests that were not run and why.

Before every commit, ask:

1. Did this introduce an unnecessary abstraction, state, branch, or fallback?
2. Is the code simpler and more consistent than before?
3. Does the public behavior or configuration need documentation?
4. Were the relevant tests run?
5. Do those tests actually prove the requested behavior?

## Lessons To Preserve

- Do not change protocol models to solve plugin-local or command-local
  problems.
- Do not treat internal messages as user messages.
- Do not make reconnect, resume, mailbox delivery, and persisted history share
  semantics merely because they all involve messages.
- Do not let background completion notices repeatedly create new turns.
- Do not cache or truncate Agent-authored tool arguments before execution.
- Long user input and Tool results may be externalized, but the stored artifact
  must retain the original content and use model-facing relative paths.
- Do not report a task as complete while a requested tool call or required
  verification step is still pending.
- Do not infer success from a summary message, a passing command, or an all-green
  test suite without checking the actual artifact and contract.
- When evidence is gone, document the limitation; never reconstruct current
  data and present it as historical runtime evidence.
