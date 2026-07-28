# HarnessBench Task Sources

The task directories in this folder are copied from:

- Repository: <https://github.com/Qihoo360/harness-bench>
- Revision: `1025086a446653702b80cfb48babbeec35db6b2c`
- Intended use: research evaluation of the XBot harness

Included tasks:

- `016-code-repair-pytest`
- `057-interruption-resume`
- `060-task-cancellation-cleanup`
- `105-partial-batch-resume-ledger`

Their prompts, fixtures, hooks, ground truth, rubrics, and deterministic oracle
files are retained together. Inspect uses the prompt/fixtures/oracle directly;
the copied HarnessBench runner and LLM process grader are not used.

`xbot-background-shell` is an XBot-specific task stored alongside the imported
subset. It evaluates the session-owned background shell and notification flow
and is not part of HarnessBench.
