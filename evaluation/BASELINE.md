# XBot Inspect Baseline

Date: 2026-07-28

Raw Inspect logs (local, ignored by Git):

- `results/2026-07-28-minimax-m3/smoke.eval`
- `results/2026-07-28-minimax-m3/harnessbench-subset.eval`

Configuration:

- XBot branch: `eval-harness`
- Provider/model: `minimax/Minimax-M3`
- Inspect AI: `0.3.249`
- ACP: `0.11.1`
- Samples: 5, executed serially
- Inspect model: `none` (XBot owns its provider calls)

## Results

| Task | Score | Input tokens | Output tokens | Failed checks |
| --- | ---: | ---: | ---: | --- |
| `016-code-repair-pytest` | 0.900 | 10,849 | 4,907 | implementation constraints |
| `057-interruption-resume` | 1.000 | 12,599 | 4,482 | none |
| `060-task-cancellation-cleanup` | 1.000 | 8,695 | 2,844 | none |
| `105-partial-batch-resume-ledger` | 0.700 | 16,240 | 10,440 | final and partial results |
| `xbot-background-shell` | 1.000 | 13,454 | 604 | none |

Aggregate:

- Mean outcome score: `0.920`
- Standard error: `0.058`
- Input tokens: `61,837`
- Output tokens: `23,277`

## Findings

The first full run exposed a missing ACP client-side permission callback when
the model produced an external path by mistake. The adapter now implements the
standard permission request and denies it. XBot receives that denial as a Tool
result and can recover instead of terminating the evaluation.

`016-code-repair-pytest` demonstrates why a passing test command is not enough:
the code passed pytest but the official oracle detected an implementation
constraint violation and applied its penalty.

`105-partial-batch-resume-ledger` completed both prompts in one ACP session but
did not fully satisfy the artifact contract. Its token usage is also the
highest in the subset. This is the first task to inspect when improving
multi-turn state preservation and recovery efficiency.

The XBot-specific background task completed its artifact and process checks:
the Agent started a session-owned background shell, received the completion
notification, inspected task state, and produced the requested report.
