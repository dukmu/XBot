# XBot Inspect Evaluation

This is an independent Inspect AI project. It evaluates XBot through the public
ACP command and does not import XBot runtime internals.

## Setup

From the repository root:

```bash
uv sync --project evaluation
```

The runner reads the selected provider from `XBotv2/data/config/providers.yaml`.
Provide the API key through the environment variable named by that provider.

## Full HarnessBench

Run all 106 tasks with four concurrent samples:

```bash
uv run --project evaluation python evaluation/run_harnessbench.py \
  --name harnessbench-$(date +%Y%m%d-%H%M%S) \
  -j 4
```

The runner uses Inspect's `eval-set` workflow. Provider requests are retried up
to eight times, sample errors twice, and failed task attempts up to ten times
with exponential waiting. Re-run the same command with the same `--name` to
resume the eval set in place; Inspect reuses completed samples.

Use `--limit` for a bounded validation:

```bash
uv run --project evaluation python evaluation/run_harnessbench.py \
  --name harnessbench-smoke -j 4 --limit 4
```

For an older log that was already recorded as successful despite timed-out
samples, explicitly re-run only those sample IDs in the same result directory:

```bash
uv run --project evaluation python evaluation/run_harnessbench.py \
  --name harnessbench-existing \
  --sample-id task-id-1,task-id-2 \
  -j 2
```

Inspect writes an additional `.eval` log in that directory and does not
overwrite the original evidence. New runs treat a time-limited sample as an
error, so normal `eval-set` retries and `inspect eval-retry` can recover it.

Each sample receives an isolated Inspect workspace and a fresh XBot ACP
process. Inspect's Agent Bridge supplies the model endpoint, so model messages,
tool calls, usage, events, tags, and scores are stored in standard Inspect
fields. Multi-round tasks retain one ACP session within the sample. After each
sample, the original XBot session directory is collected into:

```text
evaluation/results/<name>/data/sessions/<session-id>/
```

Before execution, the runner snapshots the selected XBot `config/`, `.agents/`,
and `memory/` inputs into:

```text
evaluation/results/<name>/data/
```

The evaluation runs from that snapshot. Because HarnessBench mock services are
trusted loopback endpoints, the runner explicitly enables the Browser plugin's
existing `network.allow_private` option in the snapshot. The product default
remains disabled. Inspect `.eval` files and input snapshots are local evidence
and are ignored by Git.

## Results

The current baseline is documented in:

- `REPORT.md`: human-readable analysis and limitations;
- `HARNESSBENCH_RESULTS.json`: aggregate and per-task machine-readable results;
- `results/harnessbench-final-20260730/`: canonical local Inspect log;
- `.inspect/harnessbench-final-20260730/`: canonical local offline viewer.

Start the Inspect viewer directly:

```bash
uv run --project evaluation inspect view \
  --log-dir evaluation/results/harnessbench-final-20260730
```

## Adapter Boundary

`xbot_eval.adapter.xbot_bridge_agent` is the full-benchmark solver. It:

1. obtains the Inspect sample workspace;
2. starts Inspect's standard Agent Bridge;
3. creates isolated XBot runtime data pointing at the bridge;
4. starts XBot through ACP;
5. executes all sample rounds in one XBot session;
6. returns standard Inspect messages, output, usage, and events.

HarnessBench fixtures, lifecycle hooks, local mock services, and deterministic
oracles remain owned by the task layer. Public mock URLs default to their local
loopback equivalents; set `HARNESSBENCH_PUBLIC_URL_TEMPLATE` when a different
mapping is required.
