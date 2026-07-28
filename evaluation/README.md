# XBot Inspect Adapter

This directory is an independent [Inspect AI](https://inspect.aisi.org.uk/)
evaluation project. It launches XBot through its public ACP command and does not
import or modify XBot internals.

## Smoke Run

From the repository root:

```bash
uv sync --project evaluation
MINIMAX_API_TOKEN=... \
uv run --project evaluation inspect eval evaluation/tasks/smoke.py \
  --model none \
  --ctl-server false \
  --log-dir evaluation/results/runs
```

The task creates a local Inspect sandbox, starts:

```text
xbot acp --data-dir XBotv2/data --provider minimax --no-plugins
```

and sends one prompt over ACP. The expected score is `1`.

The following environment variables select another installed XBot or provider:

| Variable | Default |
| --- | --- |
| `XBOT_EVAL_COMMAND` | repository `.venv/bin/xbot` |
| `XBOT_EVAL_DATA_DIR` | repository `XBotv2/data` |
| `XBOT_EVAL_PROVIDER` | `minimax` |

Provider credentials are deliberately passed explicitly by each task. The smoke
task currently forwards only `MINIMAX_API_TOKEN`.

## HarnessBench Subset

The research subset contains four tasks copied from
[Qihoo360/harness-bench](https://github.com/Qihoo360/harness-bench) and one
XBot-specific background-shell task:

- code repair with pytest closure;
- interrupted work and stateful resume;
- cancellation and temporary-artifact cleanup;
- partial-batch failure and idempotent resume;
- background shell completion, notification, and terminal-state inspection.

Run the five samples serially so that provider limits and session traces remain
easy to compare:

```bash
MINIMAX_API_TOKEN=... \
uv run --project evaluation inspect eval \
  evaluation/tasks/harnessbench_subset.py \
  --model none \
  --max-samples 1 \
  --ctl-server false \
  --log-dir evaluation/results/runs
```

Each Inspect sample gets a fresh local temporary workspace. The task's original
fixture files are copied into that workspace, multi-round prompts use the same
XBot ACP session, and the original deterministic `oracle_grade.py` scores the
final artifacts. XBot usage and a bounded ACP event trace are retained in the
Inspect sample metadata.

Inspect `.eval` files are evaluation evidence and must remain under
`evaluation/results/`. Only per-sample sandbox workspaces and disposable XBot
runtime state belong in the system temporary directory.

## Adapter Boundary

`xbot_eval.xbot_agent` is an Inspect Solver. For each sample it:

1. obtains the local Inspect sandbox workspace;
2. starts a fresh XBot ACP process and session;
3. sends the sample prompts through one ACP session;
4. collects ACP assistant messages, usage, and bounded event metadata;
5. returns the messages as the Inspect `ModelOutput`.

This first adapter supports Inspect's local sandbox only. Container and remote
sandbox path mapping are intentionally outside the smoke scope.
