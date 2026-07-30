# XBot HarnessBench Evaluation

## Run

The canonical run completed all 106 HarnessBench tasks through XBot's ACP
adapter and Inspect AI's standard Agent Bridge.

| Field | Value |
| --- | --- |
| Date | 2026-07-30 |
| Model | `anthropic/Minimax-M3` |
| Concurrency | 4 |
| Inspect AI | 0.3.249 |
| XBot revision recorded by Inspect | `b249dca` (dirty) |
| Samples | 106 |
| Mean score | 0.7814 |
| Standard error | 0.0209 |
| Median score | 0.8268 |
| Perfect scores | 24 |
| Scores >= 0.8 | 57 |
| Scores >= 0.5 | 95 |
| Zero scores | 1 |

The canonical Inspect log is:

```text
results/harnessbench-final-20260730/
  2026-07-30T10-40-36-00-00_xbot-harnessbench-full_bJeKdobCHrY6YtKYXmqfWG.eval
```

Its SHA-256 is
`75d1ca37b55d17f642dd39efc7ba7165107c773e87a106a4869f15c9d330f11f`.
`HARNESSBENCH_RESULTS.json` contains the complete per-task summary.

## Results

| Task class | Samples | Mean |
| --- | ---: | ---: |
| Long-running Autonomy & State Adaptation | 11 | 0.8654 |
| SRE, DevOps & Release Ops | 7 | 0.8403 |
| Office & Business Communication | 12 | 0.8094 |
| Workspace, Tool Use & Multimodal Operations | 15 | 0.7997 |
| Vertical Professional Workflows | 12 | 0.7709 |
| Software Engineering & Codebase Maintenance | 22 | 0.7547 |
| Data, BI & Finance Analytics | 14 | 0.7397 |
| Knowledge, Evidence & Retrieval | 13 | 0.7314 |

Hard tasks averaged 0.7245 across 42 samples. Medium tasks averaged 0.8199
across 30 samples. The strongest measured area is persistent autonomous work;
retrieval/evidence and structured data analysis are the weakest class-level
areas.

The lowest-scoring samples were:

| Task | Score | Main failed checks |
| --- | ---: | --- |
| `013-image-edit` | 0.0000 | required edited images and description |
| `087-cli-parser-bug-tests` | 0.2250 | pytest, hidden behavior, regression tests |
| `009-git-pr-merge` | 0.2500 | merge/push and repository state |
| `077-archive-manifest-defense` | 0.2759 | manifest and rejection contracts |
| `092-schema-drift-audit` | 0.3269 | drift identity, details, severity, summary |

These checks establish what outputs were missing; they do not by themselves
prove whether the cause was model reasoning, tool capability, prompt behavior,
or an adapter defect. That distinction requires trace-level review of each
sample.

## Usage

Inspect recorded:

- 2,547,751 input tokens;
- 603,005 output tokens;
- 20,516,260 cache-read tokens;
- 23,667,016 tokens in total;
- 3,782 messages, 1,551 model events, and 2,014 tool calls.

The large cache-read count is expected to dominate total token accounting for
multi-turn tool use. A few samples consumed disproportionate context: five
tasks exceeded 800,000 total tokens, including one failed image task and two
low-scoring repair/validation tasks. Those traces are the highest-value targets
for studying stalled iteration, ineffective repeated reads, and compact
behavior. Token volume alone is not evidence of a core runtime defect.

## Evaluation Integrity

The first standard run completed 104 tasks and had two adapter infrastructure
errors caused by bridge-port reuse. The adapter was fixed to allocate a fresh
port per attempt. Inspect's standard `eval-retry` then reused the 104 completed
samples and reran the two failures. The canonical log contains 106 unique,
completed samples; both retried samples scored 1.0.

The final log uses Inspect's standard model, message, event, usage, tag, and
score fields. It can therefore be inspected with the normal Inspect viewer
rather than relying on XBot-specific metadata.

There are two reproducibility limitations:

1. Inspect recorded revision `b249dca` with a dirty worktree, so the commit alone
   does not identify the exact adapter source.
2. The temporary XBot runtime data used by this completed run was removed. It is
   not recoverable and has not been replaced with current data, because that
   would create false evidence.

Future runs snapshot the actual `config/`, `.agents/`, and `memory/` inputs into
their result directory before evaluation and run from that snapshot. Transient
sessions and logs remain runtime artifacts unless explicitly retained.

## Priorities

1. Review the five lowest-scoring traces before changing prompts or tools.
2. Inspect high-token, low-score tasks for repeated ineffective operations and
   compact/context behavior.
3. Separate capability gaps such as image editing from general Agent-quality
   conclusions.
4. Re-run the full benchmark only after a defined XBot change, from a clean
   revision with the input data snapshot retained.
