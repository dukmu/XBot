# XBot Hermes 重构与 TUI C/S 计划

## 定位

本计划面向当前 `claude-refactor` 分支。上一轮 runtime 重构已经把核心方向基本收口：可加载 built-in tools、显式 hooks、明确的 RuntimeFrame、可审计 compaction、文件化 state、真实 provider smoke 都已经通过。当前不继续扩张 multi-agent；重点是把交互入口固定为 client/server 边界和稳定事件协议，并在此基础上重构 TUI。

方向保持不变：

> State 是系统中心。Runtime 是唯一执行协调器。UI 只是协议客户端。Tools/Hooks 可加载。Context 构建和压缩必须显式、可审计、可测试。

## Codex 历史结论

会话 `019e8321-eee1-7033-88b2-c4ec0c529d68` 在 `main/master` 上完成的是 state-centered MVP，而不是最终架构。关键结论需要继续保留：

- `data/sessions/<session_id>/state/` 是主 agent 的文件化 DAG state。
- `events.jsonl`、`graph.jsonl`、`context_tree.jsonl`、`mailbox.jsonl` 是 append-only source of truth。
- `state.yaml`、`context.md`、`claims.yaml`、summaries、artifacts、plan versions 是 materialized/projection 层。
- Task mode 通过 `goal.md`、可执行 `plan.yaml`、`plan_next`、`plan_update` 和 DAG attribution 驱动复杂任务。
- LangGraph checkpoint 只是 executor recovery，不是人类可审计 state。
- Context compaction、claims、summaries 必须留下证据链，不能只在 prompt 中隐式发生。

上一轮没有解决的问题也要继续承认：

- UI/terminal 必须只作为协议客户端。
- normalized `InteractionEvent` 只是 Python 内部对象，不是跨进程 wire contract。
- tool call、interrupt、streaming delta 必须有完整 schema。
- terminal/TUI 不允许解析 LangChain message/chunk/tool object。

## 当前分支审查结论

`claude-refactor` 已完成主 runtime 的大部分重构，当前状态比旧计划更靠前：

- `xbot.builtin_tools` 已是 canonical built-in tool source。
- `ToolRegistry` 从 canonical tools 启动，sandbox metadata fail-closed。
- `LoopHooks` 可配置加载，guard hooks 通过 registry 查工具 metadata。
- `RuntimeFrame` / `ContextProjection` 已成为 context 构建输入，`context.py` 不再静默读取全局 runtime state。
- Compaction 已写 summary source refs、graph event、context-tree node。
- Restart consistency、claims projection、mailbox dispatcher、detached subagent MVP 都已有测试覆盖。
- 最新验证已通过：`uv run pytest -q`、compile check、`scripts/provider_smoke_refactor.py` real provider smoke。

terminal/TUI 侧已经完成 C/S MVP，仍需继续完善：

- `main.py` terminal 模式启动 protocol client；`main.py server` 才创建 `HermesInteraction`。
- `xbot/protocol.py` 定义 JSONL frame 和 internal event encoder。
- `xbot/server.py` 实现 stdio runtime server。
- `xbot/terminal.py` 只渲染 protocol frames。
- shell/exec 已有 `tool.call.started -> tool.execution.started -> tool.result.*` renderer tests。
- 后续仍需补齐 interrupt/resume、failure kinds、cache refs、replay golden tests 和更完整 TUI 布局。

结论：runtime 主路径已经接近一致；下一步不是继续堆功能，而是把 UI 边界切干净。TUI 必须只消费稳定 wire events，不能解析 LangChain 对象。

## 语义不一致清单

### 1. InteractionEvent 不是 Wire Event

当前 `InteractionEvent(kind, source, payload)` 是内部归一化事件，不是协议。它缺少：

- `protocol_version`
- `event_id`
- `seq`
- `request_id`
- `turn_id`
- `run_id`
- `thread_id`
- `session_id`
- `tool_call_id`
- stable `payload` schema

因此它不能直接作为 TUI/remote UI 的长期契约。

### 2. message 与 tool_call 边界不稳定

provider 可能把 tool call 暴露在 `content_blocks`、`tool_calls`、stream chunk 或 update message 中。当前 interaction 层已做了初步归一化，但 terminal 仍保留直接解析 message block 的逻辑，导致同一 tool call 可能被多条路径显示。

目标语义：

```text
provider/langgraph internals -> runtime normalization -> protocol event -> UI render
```

UI 不再解析 provider/langchain message。

### 3. shell/exec 工具事件生命周期不完整

当前 `shell` 是工具名，底层通过 sandbox `run_shell()` 执行，但 UI 看到的通常只是：

```text
Tool Call> shell({'command': 'pwd'})
Tool shell> exit_code=0
```

缺失的语义：

- 请求何时创建；
- 是否需要 permission/sandbox approval；
- approval 是谁给的；
- 何时真正开始执行；
- stdout/stderr 是 inline 还是 cache ref；
- exit code 与 tool call id 的绑定；
- 失败是 deny、sandbox unavailable、process failed、timeout，还是 runtime error。

这就是 terminal 在 tool call handle exec 上容易出 bug 的根因：UI 层没有完整执行事件，只能猜。

### 4. Interrupt/Resume 没有跨进程协议

protocol client/server 必须区分：

- server 发出 `interrupt.requested`
- client 渲染确认或问题
- client 发回 `interrupt.resume`
- server 校验 `interrupt_id`、`request_id`、idempotency key
- server 继续同一个 checkpoint/turn

### 5. Runtime 与 UI 仍在同一控制流

旧的 direct-runtime terminal 路径已经移除。后续真实 TUI、headless client、测试 replay、远程控制都必须复用 JSONL protocol。

### 6. Multi-agent 暂停

当前分支已有 attach/mailbox/detached MVP，但用户明确要求暂时不推进 multi-agent。后续计划不扩张这些能力，不新增 async scheduler、worker pool、跨 agent UI 面板或 mailbox 自动化。

## 目标模型

### 分层

```text
Client / TUI
  - render protocol events
  - collect user input
  - send protocol commands
  - never import LangChain, LangGraph, tools, sandbox, state store

Protocol Transport
  - JSONL over stdio for MVP
  - Unix domain socket as next step
  - WebSocket only after protocol稳定

Runtime Server
  - owns HermesInteraction
  - owns session/thread/personality lifecycle
  - translates internal InteractionEvent into protocol events
  - serializes interrupts/resume/cancel

HermesInteraction
  - owns graph invoke/stream/resume
  - owns RuntimeFrame and state
  - emits internal events only

State / Tools / Hooks / Sandbox
  - unchanged source of truth
```

### Runtime 流程图

```text
client user input
  -> client.command user.message
  -> server request router
  -> HermesInteraction.stream_user_message()
  -> internal InteractionEvent stream
  -> protocol encoder
  -> server.event stream
  -> client renderer
```

Interrupt:

```text
tool/ask/sandbox interrupt
  -> internal interrupt event
  -> server.event interrupt.requested
  -> client prompt
  -> client.command interrupt.resume
  -> HermesInteraction.stream_resume()
  -> protocol event stream continues
```

Tool call:

```text
provider tool call chunk/update
  -> interaction assembles complete call
  -> tool.call.started
  -> permission/sandbox decision events
  -> tool.execution.started
  -> tool.result.completed | tool.result.failed | interrupt.requested
```

## 通信协议

MVP 使用 JSONL，一行一个 frame。stdio 最简单，便于测试、录制和 diff；协议不绑定 Python UI，后续 Node.js TUI 可以直接复用。

### Frame Envelope

所有 client/server frame 都必须有 envelope：

```json
{
  "protocol_version": "xbot.hermes.v1",
  "frame_id": "frame_01H...",
  "seq": 17,
  "ts": "2026-06-03T12:00:00Z",
  "direction": "client_to_server",
  "type": "user.message",
  "session_id": "default",
  "thread_id": "default",
  "request_id": "req_01H...",
  "payload": {}
}
```

字段规则：

- `protocol_version`：必须固定，server/client 不匹配时 fail closed。
- `frame_id`：每条 frame 唯一。
- `seq`：连接内单调递增，由发送方维护。
- `ts`：UTC ISO timestamp。
- `direction`：`client_to_server` 或 `server_to_client`。
- `type`：命令或事件类型。
- `session_id`：state/cache/workspace 命名空间。
- `thread_id`：checkpoint conversation key。
- `request_id`：一次 user message/resume/cancel 的 correlation id。
- `payload`：按 `type` 使用强 schema。

### Client Commands

```text
hello
session.open
user.message
interrupt.resume
run.cancel
ping
shutdown
```

`hello` payload：

```json
{
  "client_name": "xbot-tui",
  "client_version": "0.1.0",
  "supported_protocols": ["xbot.hermes.v1"],
  "capabilities": {
    "streaming": true,
    "rich_tool_view": true
  }
}
```

`session.open` payload：

```json
{
  "personality_id": "default",
  "streaming": true,
  "print_thoughts": false,
  "trace_events": false
}
```

`user.message` payload：

```json
{
  "content": "hello",
  "input_id": "input_01H...",
  "mode": "chat"
}
```

`interrupt.resume` payload：

```json
{
  "interrupt_id": "intr_01H...",
  "approved": true,
  "answer": null,
  "idempotency_key": "resume_01H..."
}
```

`run.cancel` payload：

```json
{
  "target_request_id": "req_01H...",
  "reason": "user_cancelled"
}
```

### Server Events

```text
hello.ok
session.opened
run.started
turn.started
message.delta
message.completed
tool.call.started
tool.approval.requested
tool.execution.started
tool.result.completed
tool.result.failed
interrupt.requested
status
error
turn.finished
run.finished
pong
shutdown.ok
```

`message.delta` payload：

```json
{
  "message_id": "msg_01H...",
  "role": "assistant",
  "content_delta": "text",
  "channel": "final",
  "is_reasoning": false
}
```

`message.completed` payload：

```json
{
  "message_id": "msg_01H...",
  "role": "assistant",
  "content": "done",
  "finish_reason": "stop"
}
```

`status` payload：

```json
{
  "code": "context_compacted",
  "message": "Context compacted: summarized 2 messages, kept 1 recent messages.",
  "severity": "info"
}
```

`error` payload：

```json
{
  "code": "runtime_error",
  "message": "human readable summary",
  "retryable": false,
  "details_ref": null
}
```

## Tool Event Protocol

Tool event 必须以 `tool_call_id` 串联完整生命周期。

### tool.call.started

```json
{
  "tool_call_id": "call_01H...",
  "name": "shell",
  "args_json": {"command": "pwd"},
  "args_preview": "pwd",
  "source": "agent",
  "plan_node_id": "n_inspect",
  "requires_approval": false,
  "sandbox_mode": "sandboxed",
  "status": "pending"
}
```

### tool.approval.requested

```json
{
  "interrupt_id": "intr_01H...",
  "tool_call_id": "call_01H...",
  "name": "shell",
  "question": "Tool 'shell' needs approval before execution.",
  "permission_decision": "ask",
  "sandbox_decision": "allow",
  "args_preview": "pwd"
}
```

### tool.execution.started

```json
{
  "tool_call_id": "call_01H...",
  "name": "shell",
  "execution_id": "exec_01H...",
  "cwd": "/home/shefrin/repo/XBot/data/sessions/default/workspace",
  "sandbox_mode": "sandboxed"
}
```

### tool.result.completed

```json
{
  "tool_call_id": "call_01H...",
  "name": "shell",
  "execution_id": "exec_01H...",
  "status": "completed",
  "exit_code": 0,
  "stdout": "inline small output",
  "stderr": "",
  "result_ref": null,
  "mime_type": "application/json",
  "size_bytes": 128,
  "truncated": false
}
```

### tool.result.failed

```json
{
  "tool_call_id": "call_01H...",
  "name": "shell",
  "execution_id": "exec_01H...",
  "status": "failed",
  "failure_kind": "process_error",
  "exit_code": 1,
  "message": "command failed",
  "stdout_ref": null,
  "stderr_ref": "cache://tool-results/..."
}
```

设计约束：

- UI 只渲染 tool events，不读取 `ToolMessage`。
- shell/exec 输出大于阈值时只传 `cache://` ref 和 summary。
- permission deny、sandbox deny、bubblewrap unavailable、timeout、process exit nonzero 必须是不同 `failure_kind`。
- 一个 `tool_call_id` 不允许重复 `tool.call.started`；重放时通过 `event_id`/`tool_call_id` 幂等。

## Context 构建与压缩流程

当前 RuntimeFrame/ContextProjection 方向正确，后续只做收紧：

```text
RuntimeFrame
  -> ContextProjection
  -> ContextMessages
  -> provider call
```

保持规则：

- `context.py` 不读取全局 config/state。
- stable system prefix 与 dynamic task suffix 分离。
- claims/summaries/task projection 都来自 frame/projection。
- compaction 必须保留 tool-call group，不压掉 unresolved interrupt。
- summary artifact 必须写 source refs/ranges。
- `context_compacted` 是 runtime status/protocol event，不作为 assistant message 输出。

## TUI 重构计划

### Phase A：协议模型

目标：先把 wire contract 定下来，不写 TUI 特效。

工作：

- 已新增 `xbot/protocol.py`，使用 Pydantic 定义 envelope、commands、events、tool payloads。
- 已新增 internal `InteractionEvent -> ProtocolEvent` encoder。
- 已定义 protocol version mismatch 的 fail-closed 行为。
- 待补充更完整 golden JSONL fixtures。

验收：

- 不启动 TUI 也能单测 protocol encode/decode。
- 所有 payload 都能 JSON serialize。
- `tool.call.started` 不含 LangChain 对象。

### Phase B：Runtime Server

目标：主循环和 UI 分进程/分模块。

工作：

- 已新增 `xbot/server.py`，封装 `HermesInteraction.create()`。
- 已实现 stdio JSONL server：读 client commands，写 server events。
- `main.py` 是 thin launcher：terminal client 或 server。
- server 负责 request serialization；同一 session/thread 默认一次只跑一个 active request。
- interrupt 状态由 server 保存，并校验 resume 的 `interrupt_id`。

验收：

- headless test 已覆盖 `hello -> session.open`。
- 待补 `user.message`、interrupt/resume roundtrip。
- cancel 命令仍待实现；不能破坏 append-only state。

### Phase C：Terminal Client 替换

目标：修复现有 terminal tool call/exec 语义问题。

工作：

- `xbot/terminal.py` 已改为协议客户端 renderer。
- terminal 已删除对 `AIMessage`、`AIMessageChunk`、`ToolMessage` 的直接解析。
- tool call 显示只基于 `tool.*` events。
- shell/exec 输出根据 `tool.result.*` 渲染，支持 inline/ref/truncated。
- approval prompt 只基于 `interrupt.requested` / `tool.approval.requested`。

验收：

- `shell({'command': ...})` 半成品不会显示。
- 同一 tool call 不重复显示。
- exec/shell 的 start/result/failure 显示顺序稳定。
- 现有 terminal tests 已迁移到 protocol renderer tests。

### Phase D：新 TUI

目标：在稳定协议上做真正 TUI，而不是改旧静态 UI。

当前先落地 stdlib curses MVP，原因是依赖面最小、能立即验证 C/S 边界和 replayable state。Textual/Rich 或 Node.js TUI 只能作为后续 protocol adapter 加入，不能改变 server/runtime 契约。

工作：

- 已新增 `xbot/tui.py`，包含 `TuiState`、message/tool state models 和 `CursesTuiClient`。
- 已新增 `main.py tui` 入口，启动协议 client 并连接 `main.py server`。
- 已实现左侧工具/interrupt/error 区、主消息区、状态栏和输入行。
- 已支持 event replay：`TuiState.apply(frame)` 只消费 protocol frames，测试覆盖 message stream、tool lifecycle、cache ref 和 interrupt。
- 后续增强：nonblocking server event pump、可滚动 panes、更明确的 approval controls、golden JSONL fixtures。

验收：

- TUI 不 import `HermesInteraction`。
- TUI 可连接 stdio server。
- 同一 JSONL 事件日志可在测试中 replay。
- TUI 不解析 `AIMessage`、`AIMessageChunk` 或 `ToolMessage`。

### Phase E：保持无回退

目标：只保留一条运行时语义，不保留 legacy direct terminal。

验收：

- 用户入口仍简单：`python main.py` 或 `python main.py server`。
- 不存在 UI 直接解析 LangChain message 的主路径。
- 不存在 `TerminalSession(runtime)` direct-runtime 入口。

## 测试计划

必须保留：

```bash
uv run pytest -q
python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py xbot/builtin_tools/*.py xbot/hooks/*.py tests/*.py
uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke
```

新增 targeted tests：

```bash
uv run pytest -q -k "protocol"
uv run pytest -q -k "server or jsonl"
uv run pytest -q -k "tool_event or shell_event or exec"
uv run pytest -q -k "interrupt_resume_protocol"
uv run pytest -q -k "terminal_protocol_renderer"
```

Golden tests：

- `hello/session.open` handshake。
- 普通 assistant streaming。
- shell tool call 完整 lifecycle。
- shell permission ask + resume。
- sandbox deny。
- large stdout/stderr cache ref。
- runtime error。
- context compaction status。

## 当前不推进

- 不推进 multi-agent async scheduler。
- 不新增 worker pool。
- 不设计跨 agent TUI 面板。
- 不把 mailbox 自动调度作为 TUI 重构前置条件。
- 不替换 LangGraph。
- 不引入 WebSocket 作为 MVP 必需项。

## 推进顺序

1. 已完成协议模型、stdio runtime server、protocol encoder、terminal renderer、shell/exec lifecycle 展示和 curses TUI MVP。
2. 下一步补 golden JSONL fixtures，覆盖 handshake、message stream、tool lifecycle、interrupt/resume、runtime error。
3. 再补 server request serialization、interrupt idempotency、cancel/failure-kind/cache-ref 测试。
4. 增强 curses TUI 的 nonblocking event pump、scroll panes、approval controls。
5. 需要更丰富 UI 时，再增加 Textual/Rich 或 Node.js adapter；adapter 只能复用现有 JSONL protocol。
6. 保持无 legacy direct UI path。

不要先做 UI 皮肤。先把事件模型做对。

## 不可妥协约束

- UI 不 import LangChain/LangGraph。
- UI 不解析 provider chunks。
- UI 不读取 `ToolMessage`。
- server 是 runtime 的唯一调用方。
- 所有跨进程数据必须是 JSON serializable protocol frame。
- tool lifecycle 必须有 `tool_call_id`。
- interrupt/resume 必须有 `interrupt_id` 和 `request_id`。
- 大输出必须走 cache ref。
- append-only state 仍是事实源。
- multi-agent 暂停，直到 C/S 和 TUI 稳定。

简单即最优：一条 runtime 主路径，一个协议，一个 renderer，一个可审计 state。
