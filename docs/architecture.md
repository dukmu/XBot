# Hermes 架构与设计

Hermes 是 XBot 的目标形态：一个轻量级、高质量、单用户、本地优先的 agent。核心循环已支持 Hook 注入、插件化工具注册和缓存友好的 DAG 上下文投影。上下文树、subagent (attach 模式) 和 mailbox 均已实现 MVP。

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
| ToolResultRef/cache | 已实现 MVP | 大工具结果返回 ref，支持 `cache_read`，runtime 默认写入文件 cache |
| 文件化任务 state | 已实现 MVP | `events.jsonl` / `graph.jsonl` append-only，`state.yaml` materialized view |
| Plan/DAG state | 已实现 MVP | `plan.yaml` 校验依赖，materialize `ready_nodes`/`active_node`，版本写入 `versions/plans/index.yaml` |
| Task mode | 已实现 MVP | `task_begin` 进入显式任务模式，`goal.md`/`plan.yaml`/`context.md` 共同驱动 DAG 执行 |
| Summaries | 已实现 MVP | 压缩摘要和手动摘要以带 front matter 的 markdown 写入 `summaries/`，并投影进 `context.md` |
| 上下文压缩 | 基础实现 | 进入 agent 前将旧线性消息压缩为摘要节点 |
| 上下文树/rewind | 已实现 MVP | `context_tree.jsonl` append-only，`state.yaml` materialize head，`context_rewind` 只移动 head 不回滚副作用 |
| mailbox | 已实现 MVP | `mailbox.jsonl` append-only，支持发送、读取、ack，`state.yaml` 投影 pending count |
| subagent | 已实现 MVP | `attach` 模式在 parent session 内同步运行 child thread，共享 main workspace；`detach` 模式保留 pending record |
| debug tools | 已实现 MVP | `debug_analyze` 汇总 task/DAG/plan/state/context/mailbox/subagent |
| 持久化 | 已实现 MVP | LangGraph checkpoint 写入 `data/sessions/<id>/saver/langgraph.pkl`，store 仍为内存 |

## 文件化任务 State

当前实现把文件化 state 放在 interaction runtime 外层，不替换 LangGraph 主循环：

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
  versions/
  summaries/
  locks/
```

语义：

- `state/` 是当前 session 主 agent 的唯一 DAG state；默认 `mode=chat`，用于记录普通对话和运行轨迹。
- `saver/` 存放 LangGraph checkpoint，不和人类可审计的 DAG state 混在一起。
- attach-mode subagent 拥有自己的 `subagents/<subagent_id>/state/` 和 `subagents/<subagent_id>/saver/`；它不是 parent 的第二个主任务目录。
- `session_id` 是一次隔离运行的外层命名空间，决定 workspace/cache/state/saver/subagents 的根目录。
- `personality_id` 选择 agent 的指令、memory、权限和 sandbox 配置。
- `thread_id` 只用于 LangGraph 对话 checkpoint 的线程键，不再决定主 DAG state 目录。
- `task_id` 是当前 DAG state 的主体标识；主 agent 固定为 `agent`，subagent 使用自己的 `subagent_id`。
- 当复杂任务需要受控执行时，agent 或用户通过 `task_begin` 将该 state 切换到 `mode=task`。任务模式下，`goal.md` 是全局目标，`plan.yaml` 是可执行 DAG，`context.md` 是给模型看的当前任务投影。
- system prompt 明确要求复杂多步工作先进入 task mode，并在 task mode 内通过 `plan_next`/`plan_update` 推进 DAG，避免只有工具存在但模型不知道该使用。
- `events.jsonl` 是 runtime 事件流，包括 turn start/finish 和 normalized `InteractionEvent`。
- 详细 normalized `InteractionEvent` trace 默认不写入本地事件流，避免长会话性能瓶颈；设置 `XBOT_TRACE_EVENTS=1` 或构造 runtime 时开启 `trace_events=True` 才会落盘。
- `graph.jsonl` 是执行轨迹投影，包括 turn node、tool call、interrupt、message artifact 等事件。
- `context_tree.jsonl` 是上下文树事件流；turn/message/tool/error 会生成节点，rewind 只写入 head movement。
- `mailbox.jsonl` 是 runtime/parent/subagent 通信队列；send/read/ack 都是 append-only 事件。
- `state.yaml` 是从 append-only 日志 materialize 出来的当前视图，不是 source of truth。
- `plan.yaml` 是可校验 DAG，`xbot.planning` 检查缺失依赖/环并计算 `ready_nodes` 与 `active_node`。
- 计划变更会在 `versions/plans/` 记录变更前后快照，`index.yaml` 保存 version/hash/path，`latest.yaml` 保存最新快照内容。
- task mode 下的 turn、tool、artifact、summary 图事件会归因到当前 running/active plan node；`state.yaml.dag` 汇总每个节点的活动计数和最新事件。
- `goal.md`、`context.md`、`claims.yaml` 是稳定文件边界；`claim_add` 写入带 evidence/status 的 claim，verification 会校验结构。
- agent 可通过 `context_head` 读取当前上下文树投影，通过 `context_rewind` 将 head 移到既有节点。该操作不删除历史，也不回滚文件、shell、memory 等外部副作用。
- agent 可通过 `mailbox_send` 和 `mailbox_read` 交换可审计消息。读取时可 ack；ack 不删除消息，只影响 pending 投影。
- 压缩和 `summary_add` 产生的摘要写入 `summaries/summary_N.md`，文件包含 YAML front matter；最近摘要会投影到 `context.md`，verification 会校验摘要结构。
- `memory_update` 追加结构化长期记忆；`memory_list` 和 `memory_search` 允许按条目读取和检索，而不是把 `memory.md` 当作不可查询的大文本。

### Task Mode And Plan Tools

任务模式不是“一个额外目录”，而是当前 thread state 的执行模式：

- `task_begin(goal, steps_json)`：记录全局目标，替换当前 DAG，写入 `goal.md` 和 `context.md`。
- `plan_add_nodes(nodes_json)`：向 DAG 追加节点，保持计划版本化。
- `plan_autofill(scope, constraints_json)`：为当前任务补齐标准 inspect/implement/verify/report DAG 骨架，已有同类型节点时不重复创建。
- `plan_next()`：由调度器选择 ready node，并将其标记为 running；如果已有 running node，则返回当前 running node，不启动第二个节点。
- `plan_update(node_id, status)`：推进节点到 `completed`/`verified`、`failed`、`blocked` 等状态；`completed` 和 `verified` 都会解锁后续依赖。
- `plan_node_history(node_id)`：读取归因到某个 DAG 节点的 graph events。
- `task_status()`：读取当前 goal/plan/context 投影，并返回 `next_action` 建议（例如 `plan_next`、`plan_update`、`task_exit`）。
- `task_exit()`：退出任务模式，保留 DAG 和事件历史。

任务模式下，agent 应先推进当前 active/running DAG 节点；不能把复杂任务退化为普通聊天列表。如果任务缺少可执行结构，agent 可以先用 `plan_autofill` 生成标准骨架，再用 `plan_add_nodes` 做任务特定扩展。调度器保持单 running node，不允许通过连续 `plan_next` 并行打开多个 DAG 节点。`plan_add_nodes`、`plan_autofill`、`plan_next`、`plan_update` 只能在 task mode 中执行；`task_exit(status="completed")` 会检查 DAG，存在 ready/pending/running/blocked/failed 节点时拒绝完成退出。需要中止时必须显式用 `cancelled` 或 `failed` 状态退出。

`context.md` 会投影 active/running/ready/pending 节点，也会保留最近 completed 节点和 blocked/failed 节点，使模型能看到 DAG 执行结果，而不是只看到下一步。

`state.yaml` 的计划投影示例：

```yaml
plan:
  version: 2
  status: active
  active_node: n_verify_state
  ready_nodes: [n_verify_state]
  pending_nodes: []
  errors: []
```

当前 LangGraph checkpoint 使用 `FileBackedSaver` 写入 session checkpoint 文件，`InMemoryStore` 仍只作为运行期 store。长期可审计状态以任务目录为准。

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

当前 interaction runtime 已生成 `RunRecord` 并关联 `task_id`，文件化 state 记录 turn/event；LangGraph checkpoint 通过 `FileBackedSaver` 持久化到 session checkpoint 文件。

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
  - path: personalities/<personality_id>
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

当前 `prepare_context` 节点调用 `xbot.compaction` 执行压缩。

## 交互运行时

P0 将“交互程序”和“终端 UI”拆开：

- `xbot.interaction.HermesInteraction` 负责加载配置、构建图、发起用户 turn、resume interrupt、输出标准事件。
- `xbot.runtime.RuntimeContext` 显式携带 `session_id`、`personality_id`、`thread_id`、`task_id`、`run_id`、`trace_id` 和路径集合。
- `xbot.config` 使用 context-local runtime paths，工具/配置 helper 保持现有 API，但不再共享单个进程级 `_RUNTIME_PATHS`。
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

上下文构造代码集中在 `xbot.context`，上下文压缩代码集中在 `xbot.compaction`。LangGraph 节点只负责调用这些阶段并把结果提交给模型。这让后续 context projector/context tree 可以替换构造逻辑，而不需要重写工具节点。

验证阶段集中在 `xbot.verification`，用于检查任务目录必需文件、`plan.yaml` DAG、append-only 日志计数和 `state.yaml` materialized view 是否一致。

### SystemPrompt

来源：

- `data/config/system_template.md`
- `data/personalities/<personality_id>/instructions.md`
- `data/personalities/<personality_id>/memory.md`
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

工具调用运行时集中在 `xbot.tool_runtime`，LangGraph 图只负责把 tool node 接入 loop。工具调用经过三层处理：

1. **Sandbox gate**：系统沙箱先判定工具是否注册、是否可执行、是否需要 ask。
2. **Permission guardrail**：权限系统检查工具名和参数。
3. **Execution**：由系统沙箱后端或 host-safe 路径执行工具。
4. **Output guardrail**：大结果写 cache，返回 ToolResultRef。

工具结果 cache 是开发阶段的内存实现。它不是长期记忆，也不保证跨进程恢复。

工具失败必须保持显式：工具函数不把异常伪装成普通成功文本；图边界将真实执行异常转换为 `ToolMessage(status="error")`。`GraphInterrupt` 不属于工具失败，必须继续冒泡给 interaction runtime 处理。

## 已实现的复杂能力

### 上下文树 (已实现 MVP)

`context_tree.jsonl` 记录 append-only 上下文节点，`state.yaml` materialize head 和 node 计数。`context_rewind` 移动 head 但不删除历史：

- `node_id`
- `parent_id`
- `current_head`
- `compacted` 节点
- `tree_path`
- `rewind`

明确约束：`rewind` 不回滚外部副作用，只改变后续上下文从哪个节点继续生长。

### Subagent

当前 `subagent_*` 已有 MVP worker：

- `subagent_create(mode="attach")` 在真实 `HermesInteraction` 中同步启动 child thread，仍位于 parent session 下。
- child runtime 访问 parent workspace，启动时进入自己的 task-mode DAG，拥有独立 agent DAG state、context tree、mailbox 和 audit log。
- parent graph 记录 `subagent_delegated` / `subagent_finished` 事件，并按 parent active plan node 归因。
- child 完成后写入 parent `subagents/<id>/result.txt`，并向 parent mailbox 发送 `subagent_completed` 或 `subagent_failed`。
- `subagent_create(mode="detach")` 只创建 pending record，后续由后台 runner 接管。
- child runtime 使用专门的 system notice，明确 subagent 身份、workspace 边界、父子协作方式和不要直接面向用户输出。

后续路线：

1. 真正异步 subagent：带 timeout、取消、max tool calls、权限继承策略。
2. background runner：扫描 pending record，消费 mailbox 任务并写回结果。
3. subagent workspace diff：把 child workspace 的变更以 patch/artifact 形式交给 parent 审核。

### Debug Tools

`debug_analyze` 是只读 host 工具，用来检查当前 task 的运行时结构：

- task/session/thread 路径
- `state.yaml` 关键计数和状态
- `plan.yaml` active/ready/errors
- 最近 DAG/runtime events
- 每个 plan node 的 DAG 活动计数，配合 `plan_node_history` 做局部追踪
- context tree 和 mailbox 投影
- subagent manifest 摘要，以及 child `state.yaml` / `plan.yaml` / DAG activity 摘要

`debug_analyze(scope="dag")` 会收窄到 DAG/plan/subagent 视图，包含 plan node 表、`state.yaml.dag` 活动投影，以及最近事件按 `plan_node_id` 和事件类型聚合后的计数。
默认 `debug_analyze` 也会给出 task `next_action`，用于定位当前 DAG 卡在哪一步。

### Mailbox (已实现 MVP)

`mailbox.jsonl` 记录 append-only 消息（send/read/ack）。`state.yaml` 投影 pending count。RuntimeState 显示未读高优先级摘要。后续路线：统一 `EventQueue` 抽象。

## 持久化策略

当前阶段：

- `FileBackedSaver`，写入 `data/sessions/<session_id>/saver/langgraph.pkl`
- `InMemoryStore`
- file-backed tool cache

目标阶段：

| 数据 | 目标 |
|------|------|
| checkpoint | 官方 SQLite/Postgres saver |
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
