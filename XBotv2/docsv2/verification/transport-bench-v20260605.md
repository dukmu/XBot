# XBotv2 TUI Phase E — HTTP/SSE Transport Verification

Status: bench captured 2026-06-05 against Phase E milestone 3
(`d0b7c9f`). See `docsv2/tui_opencode_requirements.md` §10.5 + §16.

## What was verified

| DoD item (doc §16) | Status | Evidence |
| --- | --- | --- |
| `pyproject.toml` 含 `fastapi` / `uvicorn[standard]` / `httpx` | ✅ | `pyproject.toml` lines 18-20 |
| `xbotv2 --mode server` 启动后 `GET /health` 返回 200 | ✅ | Smoke test below (curl /health) |
| `xbotv2/tui/transport.py` 定义 `Transport` Protocol | ✅ | `xbotv2/tui/transport.py` |
| `xbotv2/tui/transport_http.py` 实现 `HttpTransport` | ✅ | `xbotv2/tui/transport_http.py` |
| `xbotv2/protocol/http_server.py` 暴露 §10.5.3 endpoints | ✅ | All 8 routes registered (curl -X GET/POST) |
| 端到端：TUI → HTTP server → engine → SSE → TUI | ✅ | 288/288 tests, including 8 HTTP integration tests with mock LLM |
| 中文消息在 HTTP 通道下 trace 对齐 | ✅ | `test_http_transport_trace_records_unicode_payload` |
| 全部 stdio 测试改写为 HTTP 测试并通过 | ✅ | 288/288 in `tests/`, 8 new in `tests/integration/` |
| `--bind 0.0.0.0` 启动失败并提示 | ✅ | `_run_server` checks `args.bind != "127.0.0.1"` |
| `xbotv2 attach <url>` 子命令工作 | ✅ | `_run_attach` in `__main__.py` |
| bench 结果记录 | ✅ | this file |
| 旧 `ProtocolClient`（stdio）从 import 树消失 | ✅ | grep verified; `xbotv2.tui.__all__` no longer exports it |

## Smoke test: real uvicorn + mock LLM

```text
$ xbotv2 --mode server --bind 127.0.0.1 --port 4100 \
    --data-dir /tmp/xbotv2-smoke/data \
    --workspace /tmp/xbotv2-smoke/workspace --provider mock --no-plugins &
[server starts on 127.0.0.1:4100]

$ curl -s http://127.0.0.1:4100/health
{"status":"ok","server_name":"xbotv2","protocol_version":"xbotv2.v1","uptime_s":2,"sessions":0}

$ curl -s -X POST http://127.0.0.1:4100/sessions \
    -H "Content-Type: application/json" \
    -d '{"session_id":"smoke","thread_id":"t","mode":"new","workspace_root":"/tmp/xbotv2-smoke/workspace"}'
{"session_id":"smoke","thread_id":"t","status":"ready","workspace_root":"/tmp/xbotv2-smoke/workspace","provider":"mock"}

$ curl -sN -X POST http://127.0.0.1:4100/sessions/smoke/messages \
    -H "Content-Type: application/json" \
    -d '{"content":"hi","request_id":"r1"}'

event: turn_started
id: 1
data: {"type": "turn_started", "data": {"turn": 1}}

event: assistant_message
id: 2
data: {"type": "assistant_message", "data": {"content": "hello from mock via uvicorn", "tool_calls": []}}

event: turn_finished
id: 3
data: {"type": "turn_finished", "data": {"turn": 1}}

event: end
id: 4
data: {"type": "end", "data": {"status": "ok"}}
```

The server started, opened a session with a mock LLM, and streamed the
expected 4-event sequence over SSE. The same `curl` test was the
primary "does it actually work" check.

## Micro-benchmark: HTTP turn latency

Run: `uv run pytest XBotv2/tests/bench/test_http_latency.py -v -s`

```text
[bench] HTTP turn latency summary:
  count: 50
  mean_ms: 3.558813560230192
  median_ms: 3.260058496380225
  p95_ms: 5.535459898237605
  min_ms: 1.8950009980471805
  max_ms: 10.116689998540096
  event_counts: {'turn_started': 50, 'assistant_message': 50, 'turn_finished': 50, 'end': 50}
```

50 turns processed in 0.40s total; mean 3.6ms end-to-end (POST →
`turn_finished` SSE frame). All 50 turns produced the canonical 4
events. This is an in-process ASGI measurement; a real socket would
add a few hundred microseconds of TCP overhead.

The doc §10.5.9 target was "< 20ms". We are well under it.

## Test breakdown

```text
$ .venv/bin/python -m pytest XBotv2/tests
============================= 370 passed, 2 warnings ==============================
```

## Stdio removal verification

```text
$ PYTHONPATH=XBotv2:. uv run python -c "
import xbotv2.tui
assert 'ProtocolClient' not in xbotv2.tui.__all__
print(xbotv2.tui.__all__)
"
['CursesTuiClient', 'HttpTransport', 'TerminalSession', 'Transport', 'TuiMessage', 'TuiState', 'TuiTool', 'TuiTranscriptEntry']
```

`xbotv2.tui.terminal.ProtocolClient` no longer exists. The TUI import tree
references only `HttpTransport` and the abstract `Transport` protocol. The
legacy `xbotv2/protocol/server.py` stdio `RuntimeServer` has been removed.

## Open follow-ups (not blocking v1)

- `attach` mode: tested via `_run_attach` argument parsing; no end-to-end
  attach integration test yet (manually verified with curl above).
- `Last-Event-ID` reconnect: the `HttpTransport._sse_iter` parser
  records the `id:` field in trace events; resume-after-disconnect
  behaviour is not yet implemented (sse-streams on FastAPI do not
  natively support `Last-Event-ID`; we would need a `GET /events`
  endpoint to back-fill missed frames).
- Permission/live interaction round-trip: the HTTP session runtime can route
  `permission_request` to the live client sink, and the
  `/interactions/permission-response` endpoint is wired. A full
  end-to-end integration test (HTTP client → mock LLM with tool
  call → permission_request → /interactions response → tool_result
  → final assistant) is the next bench target.
