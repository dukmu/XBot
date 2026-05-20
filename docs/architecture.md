# Hermes 架构与设计

Hermes 是 XBot 的目标形态：一个轻量级、高质量、单用户、本地优先的 agent。当前阶段的设计原则是：先把主循环、上下文构造、工具安全和恢复语义做稳，再逐步引入上下文树、subagent 和 mailbox。

## 设计原则

1. **简单优先**：默认采用线性会话链；树、分支、异步任务只在主循环稳定后引入。
2. **运行时契约优先**：先定义 Run、Turn、Interrupt、ContextFrame、ToolResultRef 等硬契约。
3. **工具安全优先**：工具执行前有 input guardrail，执行后有 output guardrail。
4. **上下文质量优先**：模型看到的是精心构造的 ContextFrame，不是原始数据库 dump。
5. **副作用显式化**：rewind 只改变上下文 head，不回滚文件、shell、memory 等外部副作用。
6. **系统沙箱优先**：所有会接触宿主资源的工具都必须通过系统级 sandbox 后端，不能只做 Python 级路径检查。deny/ask 必须在挂载层遮蔽，不能只在工具入口拦截。

## 当前状态

| 能力 | 当前状态 | 说明 |
|------|----------|------|
| LangGraph ReAct 主循环 | 已实现 | `agent -> tools -> agent` |
| Terminal runtime | 已实现 | 支持 interrupt/resume 的基本处理 |
| Interaction runtime | 已实现 | `xbot.interaction` 统一发起 turn/resume 并输出标准事件，CLI 只是 adapter |
| Provider 配置 | 已实现 | OpenAI/Anthropic 兼容 |
| 权限 allow/deny/ask | 已实现 | 本轮改为 deny 优先 |
| 系统级 sandbox | 已实现 MVP | `bwrap` 后端，工具默认拒绝，未知工具不放行，deny/ask 通过挂载遮蔽 |
| 主动 ask | 本轮实现 | `ask` 工具触发 `user_ask` interrupt |
| 工具过滤 | 本轮实现 | `agent.yaml` 的 `tools` 控制暴露工具 |
| System state frame | 本轮实现 | 每次模型调用注入当前运行状态，不沉淀为历史 |
| ToolResultRef/cache | 本轮实现 MVP | 大工具结果返回 ref，支持 `cache_read` |
| 上下文压缩 | 基础实现 | 进入 agent 前将旧线性消息压缩为摘要节点 |
| 上下文树/rewind | 规划中 | 不进入当前 MVP |
| subagent/mailbox | 规划中 | 后续在 EventQueue 基础上实现 |
| 持久化 | 开发阶段内存 | 默认 `InMemorySaver` / `InMemoryStore` |

## P0 硬契约

Hermes 先围绕 5 个契约演进。

### Run

一次 agent 运行实例。建议字段：

```yaml
run_id: run_...
thread_id: default
started_at: ...
current_turn_id: turn_...
trace_id: trace_...
```

当前代码主要使用 LangGraph `thread_id`，`run_id/trace_id` 尚未持久化。

### Turn

一次用户输入或 runtime 事件驱动的模型循环。建议字段：

```yaml
turn_id: turn_...
run_id: run_...
input_kind: user_message | interrupt_resume | background_event
created_at: ...
```

### InterruptEvent

所有用户打断都走统一结构，通过 `type` 区分语义。

```yaml
interrupt_id: int_...
type: user_ask | tool_confirm
question: ...
tool_name: optional
args: optional
reasons: optional
sandbox: optional
permission: optional
resume_schema: ...
```

当前实现使用 LangGraph interrupt payload，最小字段为 `type`、`question`、`tool_name`、`args`。

### ContextFrame

每次模型调用前构造一次 ContextFrame。

```text
ContextFrame =
  SystemPrompt
  + RuntimeState
  + MessageChain
```

重要约束：

- Tool schema 由模型 API 绑定，不写进普通 prompt。
- RuntimeState 是瞬时系统消息，不作为长期历史节点保存。
- 原始 think/reasoning 不默认进入下一轮上下文。
- 需要长期保留的思考结论应写成 assistant 消息、plan artifact 或 memory。

### ToolResultRef

大工具结果不直接塞回模型上下文，而是返回引用。

```yaml
ref: cache://tool-result/<id>
mime_type: text/plain
summary: ...
size: 12345
read_hint: use cache_read(ref, query or max_chars)
```

小结果直接内联；大结果写入运行时 cache，通过 `cache_read` 按需读取。

### SandboxConfig

Hermes 现在把沙箱当作独立于权限系统的第二层控制面：

```yaml
enabled: true
backend: bubblewrap
default: deny
network: false
timeout_seconds: 30
max_output_chars: 20000
resources:
  - path: sessions/<session_id>/workspace
    access: readwrite
    recursive: true
  - path: personality/<personality_id>
    access: readonly
```

语义：

- `permissions` 决定一个 tool 能不能被调用。
- `sandbox` 决定这个 tool 在宿主机上能看到什么。
- `sandbox` 开启后，未注册工具默认拒绝。
- `sandbox` 不是 Python 级路径过滤，而是 bubblewrap 级隔离。deny/ask 资源会被遮蔽，获批后只临时挂载精确路径。
- `shell`、`filesystem_*`、`skill_load`、`subagent_create`、`memory_update` 等会碰宿主资源的工具，都必须走 sandbox 后端。
- 如果 `bwrap` 不可用，sandboxed 工具失败闭合，不回退到宿主直接执行。

## 当前主循环

```text
START
  -> agent
  -> tools
       -> tool_confirm interrupt when approval is needed
       -> user_ask interrupt when ask() is called
       -> output guardrail / cache hook
  -> agent
  -> END
```

当前 `compress` 节点保留，但压缩触发仍是简单机制。

## 交互运行时

P0 将“交互程序”和“终端 UI”拆开：

- `xbot.interaction.HermesInteraction` 负责加载配置、构建图、发起用户 turn、resume interrupt、输出标准事件。
- `xbot.terminal.TerminalSession` 只负责 CLI 输入、UTF-8 终端配置、事件渲染和确认提示。
- 后续 agent 侧调用、TUI、mailbox runner 都应复用 `HermesInteraction`，不直接依赖 `main.py`。
- `main.py` 只保留参数解析和启动终端 session。

当前事件最小结构：

```yaml
kind: message | interrupt | status | error
source: agent | tool | runtime | permission | sandbox | user
payload: ...
```

## 上下文构造

### SystemPrompt

来源：

- `data/config/personality_template.md`
- `data/personality/<personality_id>/AGENT.md`
- `data/personality/<personality_id>/MEMORY.md`
- skills 摘要

SystemPrompt 负责角色、规则、工具使用约束和长期记忆。

### RuntimeState

RuntimeState 每次模型调用动态生成：

```text
# Runtime State
time: 2026-05-20T...
user: Alice (local_user)
platform: local
session_type: private
active_subagents: 0
pending_mailbox_items: 0
```

RuntimeState 不进入 `state["messages"]`，避免时间、状态计数等瞬时信息污染历史。

### MessageChain

当前阶段仍使用 LangGraph `messages` 作为线性链。后续引入 context tree 后，MessageChain 从当前 head 选择一条 path 构造。

## ask 与权限确认

Hermes 区分两种 interrupt。

### user_ask

由 agent 主动调用 `ask(question)` 触发。用户回答后，回答作为 `ask` 工具结果返回给模型。

```text
agent -> ask(question)
runtime -> interrupt(type="user_ask")
user -> answer
tools -> ToolMessage("User answered: ...")
agent -> continue
```

### tool_confirm

由权限系统或 sandbox ask 触发。一个工具调用如果同时需要权限确认和 sandbox 资源确认，只向用户发出一次合并确认；拒绝后返回标准拒绝工具结果。

```text
agent -> tool_call
permission -> deny | allow | ask
sandbox -> deny | allow | ask
ask -> interrupt(type="tool_confirm")
user -> approved true/false
tools -> execute or return denial
```

权限判断采用 deny 优先，避免危险规则被宽泛 allow 覆盖。

## 工具运行时

工具调用经过三层处理：

1. **Sandbox gate**：系统沙箱先判定工具是否注册、是否可执行、是否需要 ask。
2. **Permission guardrail**：权限系统检查工具名和参数。
3. **Execution**：由系统沙箱后端或 host-safe 路径执行工具。
4. **Output guardrail**：大结果写 cache，返回 ToolResultRef。

工具结果 cache 是开发阶段的内存实现。它不是长期记忆，也不保证跨进程恢复。

工具失败必须保持显式：工具函数不把异常伪装成普通成功文本；图边界将真实执行异常转换为 `ToolMessage(status="error")`。`GraphInterrupt` 不属于工具失败，必须继续冒泡给 interaction runtime 处理。

## 暂缓的复杂能力

### 上下文树

仍保留为目标，但不进入当前 MVP。未来最小实现只包括：

- `node_id`
- `parent_id`
- `current_head`
- `compacted` 节点
- `tree_path`
- `rewind`

明确约束：`rewind` 不回滚外部副作用，只改变后续上下文从哪个节点继续生长。

### Subagent

当前 `subagent_*` 是 P0 task record 工具，只创建/读取/停止任务记录，不启动 worker。先不实现 autonomous async subagent。推荐路线：

1. 同步 worker：一次任务，一次返回。
2. background task：固定 workflow 或固定工具链。
3. 真正异步 subagent：带 timeout、取消、max tool calls、权限继承策略。

### Mailbox

先不实现双 mailbox。推荐先实现统一 `EventQueue`：

```yaml
event_id: evt_...
audience: user | agent | both
source: runtime | subagent:<id> | tool:<name>
summary: ...
payload_ref: optional
status: unread | read | archived
```

RuntimeState 只放未读高优先级摘要和数量。

## 持久化策略

当前阶段：

- `InMemorySaver`
- `InMemoryStore`
- 内存 tool cache

目标阶段：

| 数据 | 目标 |
|------|------|
| checkpoint | SQLite |
| tool cache metadata | SQLite |
| large payload | SQLite/blob/filesystem hybrid |
| context tree | SQLite |
| EventQueue | SQLite |

## 实现路线

### P0：稳定主循环

- 工具过滤
- deny 优先权限
- 主动 ask interrupt
- RuntimeState 注入
- ToolResultRef/cache MVP
- interaction runtime 与 terminal adapter 解耦
- 默认系统 sandbox 与工具失败显式化

### P1：压缩

- 线性 MessageChain 压缩
- compacted summary
- facts/open threads/tool refs

### P2：最小上下文树

- node 表
- current head
- rewind
- 明确 side-effect ledger

### P3：后台能力

- 同步 worker
- EventQueue
- background task
- 异步 subagent

## 当前代码对应关系

| 模块 | 职责 |
|------|------|
| `main.py` | Terminal runtime loop、interrupt resume、图执行 |
| `xbot/graph.py` | LangGraph 节点、ContextFrame、工具 runtime |
| `xbot/tools.py` | 内置工具、`cache_read` |
| `xbot/cache.py` | 开发阶段内存 ToolResultRef cache |
| `xbot/permissions.py` | deny/allow/ask 权限规则 |
| `xbot/config.py` | YAML/JSON 配置加载和 runtime path 派生 |
| `xbot/models.py` | Pydantic 配置模型和运行时契约模型 |
