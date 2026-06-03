# Hermes 架构与设计

Hermes 是 XBot 的目标形态：一个轻量级、单用户、本地优先、状态中心的 agent runtime。

核心原则：

> State 是系统中心。LLM 是 planner/executor/verifier/summarizer 之一。UI 只是协议客户端。

当前 `claude-refactor` 分支已经完成 runtime 主路径重构：可加载 built-in tools、`ToolRegistry`、`LoopHooks`、显式 `RuntimeFrame`、cache-friendly context、可审计 compaction、文件化 DAG state、claims/summaries 投影、restart recovery、真实 provider smoke、JSONL client/server MVP，以及 transcript-first live curses protocol TUI。下一阶段重点是增强 replay/golden tests、interrupt/resume 覆盖和更强的取消/审批控制。

## 当前能力状态

| 能力 | 状态 | 说明 |
|------|------|------|
| LangGraph ReAct executor | 已实现 | `prepare_context -> agent -> tools -> prepare_context/END` |
| Interaction runtime | 已实现 | `HermesInteraction` 统一发起 turn/resume/stream，并输出内部 normalized events |
| Terminal adapter | 已实现 MVP | terminal 是 protocol client，不直接创建 `HermesInteraction` |
| RuntimeFrame | 已实现 | 每次 user turn/resume 都显式构建 frame/projection |
| Context construction | 已实现 | `context.py` 从 `RuntimeFrame`/`ContextProjection` 构造消息，不再读取全局 state |
| Context compaction | 已实现 | 保留 tool-call groups，写 summary source refs、graph event、context-tree node |
| Tool registry | 已实现 | `xbot.builtin_tools` 是 canonical source，`ToolRegistry` 持有 sandbox metadata |
| Hooked loop | 已实现 | before/after context/agent/tools hooks，可从 personality config 加载 |
| Permission | 已实现 | deny -> allow -> ask -> default |
| Sandbox | 已实现 MVP | bubblewrap fail-closed；未知 sandboxed 工具拒绝执行 |
| Tool result cache | 已实现 | session-scoped file-backed cache，大结果用 `cache://` ref、summary、preview、metadata |
| File-backed task state | 已实现 | append-only JSONL + materialized YAML |
| Task mode / Plan DAG | 已实现 | `task_begin`、`plan_next`、`plan_update`、versioned `plan.yaml` |
| Claims / summaries | 已实现 | claim evidence/confidence/supersede metadata 投影到 runtime state |
| Context tree / rewind | 已实现 MVP | append-only tree，rewind 只移动 head，不回滚副作用 |
| Mailbox | 已实现 MVP | append-only send/read/ack，runtime 可处理 pending mailbox |
| Subagent | 暂停扩张 | attach/detach MVP 存在；当前计划不推进 multi-agent async scheduler |
| Protocol server | 已实现 MVP | `main.py server` 在 stdio 上提供 JSONL runtime server |
| Curses TUI | 已实现 live MVP | `main.py tui` 基于后台 protocol reader 维护 transcript-first UI state；usage/cache/tool/interrupt 都来自 protocol events |

## 总体架构

当前已实现 C/S runtime：

```text
main.py
  -> terminal/tui protocol client
      -> JSONL stdio
          -> main.py server
              -> HermesInteraction
                  -> RuntimeFrame
                  -> LangGraph executor
                  -> InteractionEventNormalizer
                  -> TaskStateStore / checkpoint / cache
```

目标 UI 分层保持：

```text
Client / TUI
  - 读取用户输入
  - 渲染 protocol events
  - 发出 protocol commands
  - 不 import LangChain/LangGraph/runtime/tools

Transport
  - Current: JSONL over stdio
  - Next: Unix domain socket
  - Later: WebSocket

Runtime Server
  - 独占 HermesInteraction
  - 管理 session/thread/personality lifecycle
  - 把内部 InteractionEvent 编码为 protocol events
  - 校验 interrupt/resume/cancel request

HermesInteraction
  - 构建 RuntimeFrame
  - 调用 LangGraph executor
  - 处理 stream/resume/restart
  - 记录 turn events

InteractionEventNormalizer
  - 把 LangGraph/provider messages、updates、custom payloads 归一化为内部 InteractionEvent
  - 去重 checkpoint/stream message
  - 组装 streamed tool-call chunks
  - 提取 usage metadata

State / Tools / Hooks / Sandbox
  - append-only state 是事实源
  - ToolRegistry 是工具事实源
  - LoopHooks 是 runtime 扩展点
  - Sandbox 是宿主资源隔离层
```

关键边界：

- UI 只知道 JSON protocol frame。
- server 才能调用 `HermesInteraction`。
- `InteractionEvent` 是 Python 内部事件，不是 wire contract。
- `HermesInteraction` 不解析 UI 协议，也不直接承担 provider message 去重；event normalization 在 `xbot/interaction_events.py`。
- LangChain message/chunk/ToolMessage 不能越过 server 边界。
- tool lifecycle 必须通过 `tool_call_id` 串联。

## 身份与路径模型

四个 ID 不能混用：

```text
session_id      命名 workspace/cache/state/saver/subagents
personality_id  选择 instructions/memory/permissions/sandbox/skills
thread_id       LangGraph checkpoint conversation key
task_id         DAG state subject；主 agent 是 agent，child 使用 subagent_id
```

主 agent state：

```text
data/sessions/<session_id>/state/
```

LangGraph checkpoint：

```text
data/sessions/<session_id>/saver/langgraph.pkl
```

Tool cache：

```text
data/sessions/<session_id>/cache/tool-results/
```

Subagent state：

```text
data/sessions/<session_id>/subagents/<subagent_id>/state/
data/sessions/<session_id>/subagents/<subagent_id>/saver/
```

不允许恢复旧语义：

- 主 agent 不创建 `tasks/default/`。
- `thread_id` 不决定 state root。
- subagent 不创建 sibling session。

## 文件化 State

每个 session 主 agent state 目录：

```text
data/sessions/<session_id>/state/
  task.yaml
  goal.md
  plan.yaml
  events.jsonl
  graph.jsonl
  context_tree.jsonl
  mailbox.jsonl
  state.yaml
  context.md
  claims.yaml
  artifacts/
  checkpoints/
  versions/plans/
  summaries/
  locks/
```

语义：

- `events.jsonl`：runtime turn events，append-only。
- `graph.jsonl`：tool call、artifact、summary、interrupt 等执行轨迹投影，append-only。
- `context_tree.jsonl`：turn/message/tool/summary/rewind/context_compacted 节点，append-only。
- `mailbox.jsonl`：send/read/ack，append-only。
- `state.yaml`：从 append-only logs materialize 出来的视图，不是事实源。
- `context.md`：给模型看的任务投影。
- `claims.yaml`：结构化 claims，包含 evidence、confidence、invalidates/superseded metadata。
- `plan.yaml`：可执行 DAG，不是 markdown todo。
- `versions/plans/`：每次 plan mutation 的 before/after snapshot。
- `summaries/`：compaction/manual summary artifacts，带 source refs/ranges。

实现边界：

- `xbot/state.py` 的 `TaskStateStore` 是运行时状态门面：记录 turn 生命周期、触发 `state.yaml` 与 `context.md` materialization，并协调各个文件化 state 子模块。
- `xbot/state_event_logs.py` 的 `StateEventLogs` 管理 append-only JSONL：runtime events、graph projection、context tree、mailbox、event id/timestamp enrich。
- `xbot/task_plan_store.py` 的 `TaskPlanStore` 管理 `task.yaml`、`goal.md`、`plan.yaml` 和 `versions/plans/`，负责 task mode、调度选择、plan mutation 和版本快照。
- `xbot/state_records.py` 的 `StateRecords` 管理非 append-only 的结构化记录：summary markdown artifacts 与 `claims.yaml`。`TaskStateStore` 在记录创建后追加对应 runtime/graph event。
- `xbot/state_context.py` 渲染 prompt-visible `context.md`，只接收已准备好的 DAG/mailbox/summary/claim 输入。
- `xbot/state_materialization.py` 构造 `state.yaml` dict，集中处理 turn counts、DAG activity、mailbox/context-tree projection 和 claims/summaries counters。
- `xbot/state_projection.py` 只做纯 projection：从 JSONL rows materialize context tree、mailbox、DAG activity，不写文件。

不可变约束：

- 不修改旧 JSONL 行；修正通过新事件表达。
- `state.yaml` 必须能从 logs 重建。
- 大 payload 不直接写入 prompt；用 cache ref。
- errors 被记录，不被删除。

## RuntimeFrame 与 Context

每次 `user.message` 边界会重新加载 runtime 可见配置，并重建本轮 graph 依赖：

- `personality.yaml`
- `permissions.json`
- `sandbox.json`
- provider/model config
- tools registry snapshot
- hooks
- memory、instructions、system template、skills summary

刷新保留同一个 `session_id`、`thread_id`、`state_dir` 和 LangGraph checkpoint path。历史消息、DAG、claims、summaries 和 cache 继续从文件化 state/checkpoint 恢复。`interrupt.resume` 不触发刷新，避免审批对象和实际执行边界不一致。

每次 provider call 都由当前 `RuntimeFrame` 驱动：

```text
RuntimeFrame
  runtime: RuntimeContext
  user: UserContext
  personality: PersonalityProjection
  sandbox: SandboxProjection
  tools: ToolRegistrySnapshot
  task: TaskProjection
  system_notice
  active_subagents
```

转换链：

```text
RuntimeFrame
  -> ContextProjection
  -> ContextMessages
  -> provider call
```

消息布局：

```text
[SystemMessage: stable prefix]
[history messages]
[SystemMessage: dynamic task suffix]
```

stable prefix 包含：

- system template
- personality instructions
- memory
- skills summary
- stable runtime rules
- sandbox summary

dynamic suffix 包含：

- 当前 runtime state
- freshly projected `context.md` task projection
- active/ready/running DAG nodes
- pending mailbox count
- active subagent count
- relevant claims/summaries

规则：

- `context.py` 不直接读取 config/state contextvars。
- graph checkpoint 只保存 serializable projection dict，不保存自定义 `RuntimeFrame` 对象。
- compaction summary 作为 compacted history/context artifact，不伪装成普通 assistant message。

## LangGraph Executor

当前 executor 是三阶段 loop：

```text
START
  -> prepare_context
       before_context hooks
       compaction
       after_context hooks
  -> agent
       before_agent hooks
       build ContextMessages
       provider call
       after_agent hooks
  -> tools
       before_tools hooks
       sandbox + permission + ask/confirm
       execute tools
       after_tools hooks
  -> prepare_context or END
```

LangGraph 是 executor，不是架构事实源。可审计 state 在 `TaskStateStore`，checkpoint 在 `FileBackedSaver`。

## Tools 与 Hooks

工具事实源：

```text
xbot/builtin_tools/
  filesystem.py
  task_mode.py
  plan.py
  summary.py
  claims.py
  memory.py
  mailbox.py
  subagent.py
  debug.py
  cache_tool.py
  skill.py
```

不保留 `xbot.tools` 兼容模块；新代码必须从 `xbot.builtin_tools` 或 `ToolRegistry` 获取工具。

`ToolRegistry` entry：

```text
name
tool: BaseTool
sandbox_mode: sandboxed | host
```

Hook stages：

```text
before_context
after_context
before_agent
after_agent
before_tools
after_tools
```

标准 hooks 负责：

- permission/sandbox guard
- active `ask`
- large result cache
- compact requested handling
- runtime/tool trace persistence

guard hook 必须通过 `ToolRegistry` 查询 sandbox metadata，不能导入兼容工具模块判断行为。

## 权限与 Sandbox

权限顺序：

```text
deny -> allow -> ask -> default
```

sandbox 是第二层控制面：

- permission 决定工具能不能调用。
- sandbox 决定工具在宿主机上能看到什么。
- sandbox 开启后未知工具 fail closed。
- bubblewrap 不可用时 sandboxed 工具失败，不回退 host execution。
- deny/ask resource 必须在挂载层遮蔽，不只做 Python 路径检查。

实现边界：

- `xbot/sandbox.py` 的 `SandboxPolicy` 是策略门面：解析 runtime config、做 resource decision、处理 one-call approval，并组合执行后端。
- `xbot/sandbox_types.py` 定义 sandbox 共享数据结构和 literal aliases，避免策略层和后端互相导入。
- `xbot/sandbox_shell.py` 只做保守 shell path preflight，用于提前发现明显的 path/redirect ask-deny；真实隔离仍由 bubblewrap enforcement 完成。
- `xbot/sandbox_bwrap.py` 是 bubblewrap 后端：构建 mount argv、启动子进程、处理 timeout/output truncation，不参与权限决策。

工具执行路径：

```text
agent tool call
  -> ToolRegistry metadata
  -> SandboxPolicy.guard_tool_call
  -> PermissionSystem.check
  -> optional tool_confirm interrupt
  -> execute approved tool
  -> ToolMessage or error ToolMessage
  -> output cache hook
```

## Task Mode 与 Plan DAG

任务模式是当前 state 的执行模式，不是另一个目录。

核心工具：

- `task_begin(goal, steps_json)`：进入 task mode，写 `goal.md`，替换 `plan.yaml`。
- `plan_autofill(scope)`：补 inspect/implement/verify/report 骨架。
- `plan_add_nodes(nodes_json)`：追加 DAG 节点并 version plan。
- `plan_next()`：选择 ready node；已有 running node 时返回它。
- `plan_update(node_id, status)`：推进 completed/verified/failed/blocked。
- `plan_node_history(node_id)`：查看归因到节点的 graph events。
- `task_status()`：读取 goal/plan/context/next_action。
- `task_exit(status)`：退出 task mode；completed 时拒绝未完成 DAG。

调度语义：

- 单 active/running node。
- verification nodes 优先。
- `completed` 和 `verified` 都能解锁依赖。
- blocked/failed 需要显式处理，不能假装完成。

## Compaction

compaction 是可审计 state transition：

```text
message history
  -> split tool-safe groups
  -> select old groups
  -> summarize with source refs
  -> write summaries/summary_N.md
  -> append graph.jsonl context_compacted
  -> append context_tree.jsonl compacted node
  -> replace old groups with compacted summary message
```

约束：

- AI tool-call message 与对应 ToolMessage 不拆开。
- unresolved tool call/interrupt 不压缩。
- summary artifact 记录 source message ids/ranges。
- 用户只看到 runtime status，不看到 summary 作为 assistant answer。

不压缩：

- unresolved tool call。
- unresolved interrupt。
- 当前 turn 和最近窗口内的原始消息。
- active/running DAG node、ready nodes、blocked/failed nodes。
- claims、summaries、goal、plan；这些由文件化 state 和 dynamic suffix 投影。
- cache refs、evidence refs、plan node refs。

可压缩：

- 旧的自然语言对话。
- 已有 `cache://` ref 的旧大工具输出。
- 已完成 DAG node 的过程性细节。
- 重复 runtime/status 文本。

保留形式：

- compacted summary message 进入 provider history。
- `summaries/*.md` 写 source refs/ranges。
- `graph.jsonl` 和 `context_tree.jsonl` 写 compact event/node。
- `state.yaml` / `context.md` 投影 latest summaries 和 relevant claims。

## 内部事件与 Wire Protocol

内部 `InteractionEvent`：

```yaml
kind: message | message_delta | tool_call | interrupt | status | error
source: agent | tool | runtime | permission | sandbox | user
payload: Any
```

它只用于 Python runtime 内部。跨进程必须使用 protocol frame：

```json
{
  "protocol_version": "xbot.hermes.v1",
  "frame_id": "frame_01H...",
  "seq": 17,
  "ts": "2026-06-03T12:00:00Z",
  "direction": "server_to_client",
  "type": "tool.call.started",
  "session_id": "default",
  "thread_id": "default",
  "request_id": "req_01H...",
  "payload": {
    "tool_call_id": "call_01H...",
    "name": "shell",
    "args_json": {"command": "pwd"},
    "args_preview": "pwd",
    "sandbox_mode": "sandboxed",
    "status": "pending"
  }
}
```

Client commands：

```text
hello
session.open
user.message
interrupt.resume
run.cancel
ping
shutdown
```

Server events：

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
```

Protocol invariants：

- frame 必须 JSON serializable。
- `seq` 在连接内单调递增。
- `request_id` 串联一次 user message/resume/cancel。
- `tool_call_id` 串联完整 tool lifecycle。
- `interrupt_id` 串联 prompt/resume。
- 大输出用 `cache://` ref。
- UI 不解析 `AIMessage`、`AIMessageChunk`、`ToolMessage`。

## 完整运行时例子

场景：用户在 TUI 输入“检查当前目录并告诉我文件列表”。模型决定调用 `shell(command="pwd")` 和 `filesystem_list(path=".")`。

### 1. Client 启动并握手

Client 发：

```json
{"protocol_version":"xbot.hermes.v1","seq":1,"direction":"client_to_server","type":"hello","session_id":"demo","thread_id":"demo","request_id":"req_hello","payload":{"client_name":"xbot-tui","supported_protocols":["xbot.hermes.v1"]}}
```

Server 做：

- 校验 protocol version。
- 返回 server capability。
- 不创建 runtime turn。

Server 回：

```json
{"protocol_version":"xbot.hermes.v1","seq":1,"direction":"server_to_client","type":"hello.ok","session_id":"demo","thread_id":"demo","request_id":"req_hello","payload":{"selected_protocol":"xbot.hermes.v1"}}
```

### 2. Client 打开 session

Client 发 `session.open`：

```json
{"protocol_version":"xbot.hermes.v1","seq":2,"direction":"client_to_server","type":"session.open","session_id":"demo","thread_id":"demo","request_id":"req_open","payload":{"personality_id":"default","streaming":true}}
```

Server 做：

1. `configure_runtime_paths(session_id="demo", personality_id="default")`
2. `ensure_session_dirs()`
3. load user/provider/personality/permissions/sandbox/skills
4. `bootstrap_registry()` 从 `xbot.builtin_tools` 加载工具和 sandbox metadata
5. `load_standard_hooks(agent_config.hooks)`
6. 创建 `TaskStateStore` at `data/sessions/demo/state/`
7. 创建 `FileBackedSaver` at `data/sessions/demo/saver/langgraph.pkl`
8. 创建 `HermesInteraction`

产生文件：

```text
data/sessions/demo/
  workspace/
  cache/tool-results/
  saver/langgraph.pkl
  state/task.yaml
  state/goal.md
  state/plan.yaml
  state/events.jsonl
  state/graph.jsonl
  state/context_tree.jsonl
  state/mailbox.jsonl
  state/state.yaml
  state/context.md
  state/claims.yaml
```

Server 回 `session.opened`，payload 包含 session paths summary、enabled tools、sandbox summary。

### 3. Client 发送用户消息

Client 发：

```json
{"protocol_version":"xbot.hermes.v1","seq":3,"direction":"client_to_server","type":"user.message","session_id":"demo","thread_id":"demo","request_id":"req_001","payload":{"content":"检查当前目录并告诉我文件列表","input_id":"input_001","mode":"chat"}}
```

Server 做：

1. 为 `req_001` 建立 active request。
2. 调用 `HermesInteraction.stream_user_message(content)`。
3. `TaskStateStore.record_turn_started()` 追加到 `events.jsonl`。

`events.jsonl` 新增：

```json
{"type":"turn_started","turn_id":"turn_000001","input_kind":"user_message","content":"检查当前目录并告诉我文件列表", "...":"..."}
```

Server 发：

```json
{"type":"run.started","request_id":"req_001","payload":{"run_id":"run_...","trace_id":"trace_..."}}
{"type":"turn.started","request_id":"req_001","payload":{"turn_id":"turn_000001","input_kind":"user_message"}}
```

### 4. Runtime 构建 RuntimeFrame

`HermesInteraction._user_input_state()` 调用 `_build_runtime_frame()`：

```text
RuntimeContext:
  session_id=demo
  personality_id=default
  thread_id=demo
  task_id=agent
  run_id=run_...
  trace_id=trace_...
  state_dir=data/sessions/demo/state
  checkpoint_path=data/sessions/demo/saver/langgraph.pkl

PersonalityProjection:
  agent_role
  system_template
  instructions
  memory
  skills_summary

TaskProjection:
  context_text = state/context.md
  pending_mailbox_items = 0

ToolRegistrySnapshot:
  names = enabled tool names
  sandbox_modes = registry.sandbox_modes()
```

Frame 被转换为 graph state：

```text
context_projection: {...serializable dict...}
messages: [HumanMessage("检查当前目录并告诉我文件列表")]
```

### 5. prepare_context 构建模型上下文

LangGraph 进入 `prepare_context`：

1. 运行 `before_context` hooks。
2. 判断是否需要 compaction。
3. 如触发 compaction，写 summary artifact 和 `context_compacted` events。
4. 运行 `after_context` hooks。

本例第一轮不压缩。

`agent` 节点调用 `build_context_messages()`：

```text
[SystemMessage stable prefix]
[HumanMessage 用户输入]
[SystemMessage dynamic task suffix]
```

模型看到的是显式 context，不读取 UI 状态。

### 6. Agent 产生 tool calls

Provider 可能在 stream 中先发半成品：

```text
tool_call chunk: shell({})
tool_call chunk: shell({"command":"pwd"})
```

`HermesInteraction._complete_tool_calls_from_chunk()` 只在参数完整时产生内部事件：

```python
InteractionEvent(
  kind="tool_call",
  source="agent",
  payload={"id":"call_pwd","name":"shell","args":{"command":"pwd"}}
)
```

Server encoder 转成 protocol event：

```json
{"type":"tool.call.started","request_id":"req_001","payload":{"tool_call_id":"call_pwd","name":"shell","args_json":{"command":"pwd"},"args_preview":"pwd","sandbox_mode":"sandboxed","status":"pending"}}
```

UI 渲染 tool panel，但不执行工具、不解析 LangChain message。

### 7. tools 节点执行 guard

`tools` 节点读取 AIMessage 的 tool calls：

```text
pending_tool_calls:
  - shell(command="pwd")
  - filesystem_list(path=".")
```

对每个 call：

1. `ToolRegistry.sandbox_mode(name)`
2. `SandboxPolicy.guard_tool_call(name, args, mode)`
3. `PermissionSystem.check(name, args)`
4. 如任一结果是 ask，构造合并 `tool_confirm` interrupt。

如果 personality 允许 `filesystem_list` 但 shell 需要确认，server 发：

```json
{"type":"tool.approval.requested","request_id":"req_001","payload":{"interrupt_id":"intr_shell_001","tool_call_id":"call_pwd","name":"shell","question":"Tool 'shell' needs approval before execution.","permission_decision":"ask","sandbox_decision":"allow","args_preview":"pwd"}}
{"type":"interrupt.requested","request_id":"req_001","payload":{"interrupt_id":"intr_shell_001","type":"tool_confirm","question":"Tool 'shell' needs approval before execution."}}
```

`events.jsonl` 记录 turn interrupted；checkpoint 保留当前 graph state。

### 8. Client resume interrupt

用户在 TUI 里批准。Client 发：

```json
{"protocol_version":"xbot.hermes.v1","seq":4,"direction":"client_to_server","type":"interrupt.resume","session_id":"demo","thread_id":"demo","request_id":"req_002","payload":{"interrupt_id":"intr_shell_001","approved":true,"idempotency_key":"resume_001"}}
```

Server 做：

1. 校验当前 pending interrupt。
2. 调用 `HermesInteraction.stream_resume({"approved": true})`。
3. 继续同一个 LangGraph checkpoint。
4. 追加新的 `interrupt_resume` turn event。

### 9. Tool 真正执行

Server 发 tool execution start：

```json
{"type":"tool.execution.started","request_id":"req_002","payload":{"tool_call_id":"call_pwd","name":"shell","execution_id":"exec_pwd_001","cwd":"data/sessions/demo/workspace","sandbox_mode":"sandboxed"}}
```

`shell` 工具调用 `SandboxPolicy.run_shell(command)`：

- bubblewrap 创建隔离进程。
- workspace 以 readwrite mount 暴露。
- personality/config/state 按 policy readonly 或遮蔽。
- network disabled。
- stdout/stderr/max output/timeout 受 sandbox config 控制。

工具返回 JSON 字符串：

```json
{"stdout":"/home/.../data/sessions/demo/workspace\n","stderr":"","exit_code":0}
```

Tool result cache hook 判断大小；小结果 inline，大结果写：

```text
data/sessions/demo/cache/tool-results/<digest>.json
```

Server 发：

```json
{"type":"tool.result.completed","request_id":"req_002","payload":{"tool_call_id":"call_pwd","name":"shell","execution_id":"exec_pwd_001","status":"completed","exit_code":0,"stdout":"/home/.../workspace\n","stderr":"","result_ref":null,"truncated":false}}
```

`graph.jsonl` 记录 tool call/result，并在 task mode 下带 `plan_node_id`。

### 10. Agent 观察工具结果并回答

LangGraph 回到 `prepare_context -> agent`：

- 上一轮 AI tool call 和对应 ToolMessage 保持成组。
- 如果消息过长，compaction 只压缩完整可安全 group。
- dynamic suffix 更新 task/context projection。

模型生成最终文本：

```text
当前 workspace 是 ...，文件包括 ...
```

Streaming 时 server 发：

```json
{"type":"message.delta","request_id":"req_002","payload":{"message_id":"msg_001","role":"assistant","content_delta":"当前 workspace 是","channel":"final","is_reasoning":false}}
{"type":"message.delta","request_id":"req_002","payload":{"message_id":"msg_001","role":"assistant","content_delta":" ...","channel":"final","is_reasoning":false}}
{"type":"message.completed","request_id":"req_002","payload":{"message_id":"msg_001","role":"assistant","content":"当前 workspace 是 ...，文件包括 ...","finish_reason":"stop"}}
```

### 11. Turn 完成并落盘

`HermesInteraction._finish_turn()`：

- 如果无 error/interrupt，turn status = `completed`。
- `events.jsonl` 追加 `turn_finished`。
- `state.yaml` materialize turn count、event counts、DAG projection。
- `FileBackedSaver` 写 checkpoint。

Server 发：

```json
{"type":"turn.finished","request_id":"req_002","payload":{"turn_id":"turn_000002","status":"completed"}}
{"type":"run.finished","request_id":"req_002","payload":{"status":"completed"}}
```

最终数据流总结：

```text
User input
  -> client command JSONL
  -> server request router
  -> HermesInteraction
  -> RuntimeFrame
  -> ContextProjection
  -> LangGraph prepare_context/agent/tools
  -> ToolRegistry + hooks + permission + sandbox
  -> TaskStateStore append-only logs
  -> internal InteractionEvent
  -> protocol encoder
  -> server event JSONL
  -> TUI renderer
```

## 当前未完成与改进方向

已完成 MVP：

1. `xbot/protocol.py`：Pydantic protocol envelope、commands、events、tool payloads。
2. `InteractionEvent -> ProtocolEvent` encoder。
3. `xbot/server.py`：stdio JSONL runtime server。
4. terminal protocol renderer，删除 LangChain message parsing。
5. shell/exec lifecycle renderer tests。

下一步：

1. interrupt/resume、deny/failure kind、cache ref、replay golden tests。
2. scrollback panes、快捷审批、取消命令和更强的 interrupt/cancel controls。
3. Unix socket/WebSocket transport only after stdio protocol stabilizes。

暂不推进：

- multi-agent async scheduler。
- worker pool。
- 跨 agent TUI 面板。
- WebSocket transport。
- 替换 LangGraph。
- SQLite/Postgres 持久化替代当前 file-backed checkpoint。

## 当前代码对应关系

| 模块 | 职责 |
|------|------|
| `main.py` | Terminal protocol client launcher and `server` subcommand |
| `xbot/interaction.py` | `HermesInteraction`，runtime turn/resume/stream 边界 |
| `xbot/interaction_events.py` | internal event data and LangGraph/provider payload normalization |
| `xbot/runtime.py` | RuntimeContext/RuntimeFrame/projections |
| `xbot/graph.py` | LangGraph executor wiring |
| `xbot/context.py` | ContextProjection -> provider messages |
| `xbot/compaction.py` | Tool-safe message compaction and audit records |
| `xbot/tool_runtime.py` | tools node guard/execution/cache hooks |
| `xbot/registry.py` | ToolRegistry and sandbox metadata |
| `xbot/builtin_tools/` | canonical built-in tools |
| `xbot/hooks/` | LoopHooks and standard guard/cache/compact hooks |
| `xbot/state.py` | TaskStateStore append-only runtime state facade |
| `xbot/state_event_logs.py` | append-only runtime/graph/context/mailbox JSONL operations |
| `xbot/state_context.py` | prompt-visible `context.md` rendering |
| `xbot/state_materialization.py` | materialized `state.yaml` projection builder |
| `xbot/task_plan_store.py` | task metadata, executable plan, and plan versions |
| `xbot/state_records.py` | summary artifacts and structured claims |
| `xbot/state_projection.py` | pure JSONL projection helpers |
| `xbot/checkpoint.py` | FileBackedSaver for LangGraph checkpoint |
| `xbot/cache.py` | file-backed tool-result cache |
| `xbot/sandbox.py` | sandbox resource-policy facade |
| `xbot/sandbox_types.py` | shared sandbox types and decisions |
| `xbot/sandbox_shell.py` | shell path preflight parser |
| `xbot/sandbox_bwrap.py` | bubblewrap execution backend |
| `xbot/permissions.py` | permission rules |
| `xbot/verification.py` | state verification |
| `xbot/terminal.py` | Protocol terminal client/renderer |
| `xbot/tui.py` | Protocol curses TUI state/client |
| `xbot/protocol.py` | JSONL protocol frame schema and runtime event encoder |
| `xbot/server.py` | JSONL runtime server owning `HermesInteraction` |
