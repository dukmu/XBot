# XBot Inspect Evaluation

This is an independent Inspect AI project. It evaluates Agent frameworks through
their public ACP commands and does not import XBot runtime internals.

## Setup

From the repository root:

```bash
uv sync --project evaluation
```

The runner reads the selected provider from `XBotv2/data/config/providers.yaml`.
Provide the API key through the environment variable named by that provider.

OpenCode comparisons require an `opencode` executable on `PATH`, or an explicit
`--agent-command`. The adapter does not install or configure OpenCode globally.

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
Each run also writes `run-manifest.json` into the result directory with the
selected provider, retry settings, Agent command, data snapshot, Git commit,
tracked-dirty state, and Inspect arguments, so the result directory can be
inspected without reconstructing the original command.

Each sample receives an isolated Inspect workspace and a fresh Agent ACP
process. Inspect's Agent Bridge supplies the model endpoint, so model messages,
tool calls, usage, events, tags, and scores are stored in standard Inspect
fields. Multi-round tasks retain one ACP session within the sample. XBot writes
its runtime state directly into:

```text
evaluation/results/<name>/data/xbot/samples/<attempt>/sessions/<session-id>/
```

Before execution, the runner snapshots the selected XBot `config/`, `.agents/`,
and `memory/` inputs into:

```text
evaluation/results/<name>/data/xbot/
```

The evaluation runs from that snapshot. Because HarnessBench mock services are
trusted loopback endpoints, the runner explicitly enables the Browser plugin's
existing `network.allow_private` option in the snapshot. The product default
remains disabled. Inspect `.eval` files and input snapshots are local evidence
and are ignored by Git.

The evaluation depends on the official HarnessBench package, pinned from
`Qihoo360/harness-bench` at commit `1025086a446653702b80cfb48babbeec35db6b2c`.
The task oracles import `harnessbench.grading` directly; there is no XBot-side
compatibility shim. The official package honors `RUBRIC_API_KEY`,
`RUBRIC_BASE_URL`, and `RUBRIC_MODEL` for quality grading, and
`HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1` / `HARNESSBENCH_SKIP_PROCESS_GRADE=1`
to skip LLM scoring cleanly. Without those credentials, quality metadata is
recorded as skipped and the deterministic workspace oracle remains
authoritative.

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

`xbot_eval.adapters` is the only adapter registry. The runner selects one
adapter by name, calls its `prepare()` method, and passes only the selected
adapter name to the Inspect task. The task obtains its solver from the same
registry; it contains no framework-specific branches.

Each adapter owns its executable discovery, data directory, generated
configuration, subprocess environment, ACP launch, and Inspect bridge solver:

- `adapters/xbot.py` snapshots XBot inputs under `data/xbot/`, creates the
  per-sample bridge provider, and writes XBot sessions there;
- `adapters/opencode.py` stores its adapter configuration and isolated
  per-sample OpenCode homes under `data/opencode/`.

`adapters/acp.py` and `adapters/common.py` contain only the ACP session and
HarnessBench sample lifecycle shared by both implementations. Inspect owns the
model endpoint and generation settings for both adapters. XBot writes each
attempt directly below `data/xbot/samples/`; an interrupted run therefore keeps
the configuration, conversation, Tool results, and other session artifacts
already produced instead of depending on an end-of-run copy.

The ACP adapter selects the standard `allow_once` option offered by the Agent.
For XBot, workspace scope is enforced by XBot's own bwrap sandbox and permission
policy rather than by heuristics inside the Inspect adapter.

HarnessBench fixtures, lifecycle hooks, local mock services, and deterministic
oracles remain owned by the task layer. Public mock URLs default to their local
loopback equivalents; set `HARNESSBENCH_PUBLIC_URL_TEMPLATE` when a different
mapping is required.

## XBot And OpenCode Comparison

Run both frameworks sequentially at the same committed revision. Each command
uses four concurrent samples, but the frameworks do not compete for provider
quota at the same time:

```bash
uv run --project evaluation python evaluation/run_harnessbench.py \
  --name m3-comparison-xbot \
  --adapter xbot \
  --provider minimax \
  -j 4

uv run --project evaluation python evaluation/run_harnessbench.py \
  --name m3-comparison-opencode \
  --adapter opencode \
  --provider minimax \
  -j 4
```

Generate the paired report after both runs finish:

```bash
uv run --project evaluation python evaluation/compare_harnessbench.py \
  --xbot evaluation/results/m3-comparison-xbot \
  --opencode evaluation/results/m3-comparison-opencode \
  --output evaluation/results/m3-comparison
```

OpenCode runs as an ACP subprocess with `--pure`. Every sample receives isolated
`HOME` and XDG directories under its result snapshot:

```text
evaluation/results/<name>/data/opencode/samples/<sample-and-attempt>/
```

Inspect's own XDG directories are likewise rooted at
`evaluation/results/<name>/data/inspect/`. Short-lived socket files use the
ignored `evaluation/results/.tmp/` directory to stay within the Unix socket
path limit.

The generated configuration disables automatic updates and sharing, denies
paths outside the sample workspace, and points OpenCode at the same local
Inspect Agent Bridge used by XBot. The real provider credential stays in the
Inspect process; OpenCode receives only the local bridge address and the dummy
key `inspect`. Existing files under the user's home directory are not used as
OpenCode configuration or modified.

The deterministic workspace oracle is the primary quality measure. The report
also includes paired deltas, retries, time, provider usage, and standard ACP Tool
terminal states. A completed Tool call is not proof that the task was completed
correctly. LLM process grading can be applied offline to both result sets later,
but is not mixed into the primary score.
