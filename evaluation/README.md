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
  --log-dir /tmp/xbot-inspect-logs
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

## Adapter Boundary

`xbot_eval.xbot_agent` is an Inspect Solver. For each sample it:

1. obtains the local Inspect sandbox workspace;
2. starts a fresh XBot ACP process and session;
3. sends the sample input as an ACP prompt;
4. collects ACP assistant message chunks;
5. returns them as the Inspect `ModelOutput`.

This first adapter supports Inspect's local sandbox only. Container and remote
sandbox path mapping are intentionally outside the smoke scope.
