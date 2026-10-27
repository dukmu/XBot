# XBot stress runner

With no server URL, the runner automatically starts an isolated real HTTP
XBot server and an independently listening loopback OpenAI-compatible MockLLM.
Model requests use the configured production OpenAI adapter, including its
HTTP/SSE parser, tool schema serialization, reasoning and usage conversion:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --layer server --scenario all --mock-tools
```

Run the deterministic application probes against an isolated MockLLM with
an explicit in-process XBot transport:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --in-process --layer server --scenario all \
  --mock-tools --concurrency 4 --turns 10 --report /tmp/xbot-stress.json
```

`--in-process` uses `ASGITransport` only between the client and XBot; the
provider still listens on a real TCP socket. `ASGITransport` buffers an
unbounded GET `/events` stream, so every scenario requiring that stream is
explicitly skipped in this mode. It does not verify SSE, replay or reconnect.
Exercise these paths against a real HTTP server, or let the runner bind a
disposable local socket for XBot as well:

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
`--scenario all` runs every registered scenario, including probes that need
external setup or a particular fault harness. Failures remain failures; use
explicit selections for a focused run. `same_session` verifies that concurrent
requests entering one process share one turn driver and use the declared
steer/queue policy. It verifies that each POST message submission returns 202
and that the sole GET `/events` stream retains the complete lifecycle, cursor
order, and terminal event.
`--servers 2` starts independent process roots against one data directory.
`multi_server` verifies that a second writer receives the explicit
`409 session_in_use` conflict while the first owner keeps its conversation,
and that the second server can complete work in a different session. A refused
second writer is the expected successful invariant. Threads in one session
share their owner's lock; the lock is released after all owned work drains.
`--mock-tools` makes the isolated provider produce a deterministic shell tool
call; without it, the tool scenario is reported as skipped rather than
pretending that a tool was exercised. The
WebUI and TUI probes are separate layers because they require a running
HTTP/Web server and a usable terminal respectively.

The failure probes are one-shot and self-recovering. `stream_failure` injects
a real provider HTTP stream disconnect after a partial reasoning delta;
`incomplete` truncates the
first real TCP GET `/events` event stream after one non-empty body frame. Both
send a recovery turn and query public thread status, so a timeout or a
`running` turn is a failure.
Use `--failure-mode provider` or `--failure-mode http_truncate` for
`stream_failure`; `incomplete` always uses the latter and requires live HTTP.
Web/TUI layers receive the same inputs when these scenarios are selected.
Run each fault mode in its own invocation so its one-shot injection is not
consumed by another probe:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --scenario streaming,replay,tools --mock-tools --turns 2 --concurrency 1
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress \
  --scenario stream_failure --failure-mode provider
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress --scenario incomplete
```

The network mock generates responses on demand. Tool responses are selected
from the current request's last message, so concurrent conversations do not
share a preallocated response sequence. It sends separate text, reasoning,
tool argument and usage frames, with configurable delay and deterministic
jitter. Its infrastructure regression tests also exercise one-shot HTTP 503,
malformed JSON/UTF-8 and request timeout faults through the production adapter:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m pytest \
  XBotv2/tests/integration/test_stress_harness.py -q
```

This focused network verification does not establish that the whole product
stress matrix passes. The existing `lifecycle` probe currently exposes fresh
empty-thread trajectory reads returning 404 and empty-session deletion raising
`FileNotFoundError`; the runner retains those failures for the owning production
boundaries to fix.

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

## Product workload

`product` is a separate bounded-observer entry point. It starts a real XBot
HTTP listener and the independently listening OpenAI-compatible mock, with the
production provider adapter between them. It creates 512 user turns, each with
seven completed tool round trips and a final model request (at least 4,096 agent
model requests), at least 20 committed `/compact` operations, and 64 child thread
open/close lifecycles in batches of four. The mock issues ordinary shell, todo,
and team tool calls through the production registry: each session creates five
tasks, keeps one pending and completes four gradually, and publishes a board
message each turn. Every eighth turn also requests a bounded record-range
artifact. The harness enables the optional team plugin and declares explicit
permissions for the exercised team tools in its isolated configuration. `--concurrency` is the
number of independent sessions (1, 4, or 16), not a product limit. The compact
configuration retains one recent turn for this workload; the mock's recent
request-body capture is disabled. Child threads are opened and closed through
the public session API without submitting child turns.

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress.product \
  --short --output /tmp/xbot-product-short.json
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress.product \
  --concurrency 4 --slow-sse --output /tmp/xbot-product-4.json
```

Short mode uses eight user turns, two compactions and four child lifecycles;
it supports concurrency 1. `--slow-sse` keeps a real socket open after
reading its initial event and separately attempts a new connection using the
initial cursor. A 409 on that reconnect establishes cursor expiry, not that the
held connection itself was blocked by TCP buffering. The full workload requires
the 409 when this option is selected; short mode may not fill the event window.
The 200 MiB output, PTY and browser paths are separate from this matrix.

The report records bounded latency samples, per-stage process RSS and
tracemalloc, GC-tracked object counts, active Context and session-subscriber counts, file
descriptors, child-process counts,
current JSONL/SQLite/WAL file lengths and Linux process I/O counters. Stage
samples also record each active main thread's public working-message count,
completed turns and committed compactions, so comparisons can identify matching
working-set and active-thread conditions. XBot,
the network mock and the observer share one process, so RSS and process I/O
cannot be attributed to XBot alone; shell subprocess I/O is not included in
`/proc/self/io`. File lengths are not physical write volume. Tracemalloc and
GC scans add measurement overhead. The live file-size budget defaults to 256 MiB and is checked during running
turns and child batches. Linux process physical write bytes are separately
bounded at 2 GiB after server startup. The running-turn monitor checks both
budgets every 250 ms; override them with `--max-disk-mib` or `--max-write-mib`
when a larger workload is intentional. Results and runtime data
remain local; reports must not be committed.

The separate large-output case generates 200 MiB through the normal shell tool,
then streams its artifact over HTTP and checks each chunk without retaining the
body. Run it once for a matrix, with a separate report and data directory:

```sh
PYTHONPATH=XBotv2 .venv/bin/python -m scripts.stress.large_output \
  --data-dir /tmp/xbot-large-data --output /tmp/xbot-large.json
```

The case records its exact target and verified byte count. `--bytes` can set a
smaller development probe; such a result is not evidence of the 200 MiB case.
Its default live file-size and process-write budgets are 512 MiB and 2 GiB;
shell subprocess writes are not attributed by `/proc/self/io`.
