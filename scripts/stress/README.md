# XBot stress runner

With no server URL, the runner automatically starts an isolated real HTTP
server backed by MockLLM:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --layer server --scenario all --mock-tools
```

Run the deterministic application probes against an isolated MockLLM with
explicit in-process modes:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --in-process --layer server --scenario all \
  --mock-tools --concurrency 4 --turns 10 --report /tmp/xbot-stress.json
```

`ASGITransport` is useful for application throughput, but it buffers an
unbounded GET `/events` stream. Therefore the replay scenario is explicitly
skipped in `--in-process` mode. Exercise cursor replay and reconnect against a
real HTTP server, or let the runner bind a disposable local socket:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --in-process-http --layer server --scenario all \
  --servers 2
```

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --base-url http://127.0.0.1:4096 \
  --layer server --scenario replay,streaming
```

For already running workers, pass all addresses to exercise one shared
session across them:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --server-urls http://127.0.0.1:4096,http://127.0.0.1:4097 \
  --layer server --scenario multi_server
```

The WebUI probe uses Playwright. By default it starts a temporary Vite
development server, points its `/api` proxy at the disposable MockLLM HTTP
server, and shuts both processes down after the probe:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --layer web --report /tmp/xbot-web.json
```

The WebUI dependencies must already be installed under `XBotv2/web` (the
runner never mutates the Node environment). To reuse an existing frontend,
pass `--web-url URL`; to reuse an existing API as the Vite proxy target, pass
`--external --base-url API_URL` and omit `--web-url`.

The TUI probe uses a real PTY. It defaults to `.venv/bin/xbot tui --server
<base-url>` and can be overridden with `--tui-command`.

Use `--external --base-url URL` (or `--server-urls URL1,URL2`) when the
service is already running. Without `--external` and without a URL, server
and TUI layers keep the disposable server alive for the duration of the
probe.

Server scenarios are `lifecycle`, `streaming`, `replay`, `persistence`,
`payload`, `delivery`, `same_session`, `multi_server`, `tools`, `errors`,
`stream_failure`, `incomplete`, `long_history`, and `llama_cpp`;
`--scenario all` runs each one. `same_session` verifies that concurrent
requests entering one process share one turn driver and use the declared
steer/queue policy. It verifies that each POST message submission returns 202
and that the sole GET `/events` stream retains the complete lifecycle, cursor
order, and terminal event.
`--servers 2` starts independent application instances
against one data root and enables `multi_server`, which detects split-brain
ownership when the same session is routed to different workers. It is a probe,
not a distributed lock implementation: a failing invariant is evidence that
the deployment needs a cross-worker ownership mechanism. With the current
implementation this is expected to expose the missing mechanism: duplicate
turn ownership can corrupt trajectory positions, which is a product defect,
not a reason to mark the probe successful.
`--mock-tools` makes the isolated provider produce a deterministic shell tool
call; without it, the tool scenario is reported as skipped rather than
pretending that a tool was exercised. The
WebUI and TUI probes are separate layers because they require a running
HTTP/Web server and a usable terminal respectively.

The failure probes are one-shot and self-recovering. `stream_failure` injects
a provider exception after a partial MockLLM delta; `incomplete` truncates the
first real TCP GET `/events` event stream after one non-empty body frame. Both
send a recovery turn and query public thread status, so a timeout or a
`running` turn is a failure.
Use `--failure-mode provider` or `--failure-mode http_truncate` for
`stream_failure`; `incomplete` always uses the latter and requires live HTTP.
Web/TUI layers receive the same inputs when these scenarios are selected.

`long_history` appends `--history-turns` turns, records per-append latency with
history size, captures the authoritative API event stream, and drives the same
trajectory through browser/PTY probes. `llama_cpp` is separate from the local
MockLLM: it skips with an explicit reason unless `--llama-cpp-url` (or
`XBOT_LLAMA_CPP_URL`) is configured. A remote direct completion validates
context transport only; it does not claim to exercise XBot compaction without
an actual remote-provider integration.

Reports contain latency distributions, event counters, correctness
invariants, and failures. A scenario is not considered successful when a
transport or protocol error is swallowed; the runner records it and exits
non-zero.

The checked-in `reports/` directory is reserved for explicit verification
artifacts. A full run report includes the exact commands, pass/skip/failure
interpretation, and the unabridged JSON emitted by `--report`.
