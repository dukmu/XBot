# XBot Hermes 当前设计报告

## 一、总体架构

XBot Hermes 是一个**以状态为中心的本地 Agent 运行时**。文件系统是唯一的可信状态源，LLM 只是 planner / executor / verifier 之一。

```
┌─────────────────────────────────────────────────────────┐
│                   HermesInteraction                      │
│     (编排层：配置加载 → 图构建 → 事件发射)                 │
├─────────────────────────────────────────────────────────┤
│  RuntimeContext       LoopHooks         ToolRegistry     │
│  (会话/线程/任务标识)   (6阶段钩子)       (34个工具)       │
├─────────────────────────────────────────────────────────┤
│                   LangGraph 循环                         │
│   START → prepare_context → agent → tools → (循环)      │
├─────────────────────────────────────────────────────────┤
│                   TaskStateStore                         │
│   events.jsonl  graph.jsonl  context_tree.jsonl          │
│   mailbox.jsonl  plan.yaml  state.yaml  context.md       │
│   claims.yaml  goal.md  summaries/  artifacts/           │
└─────────────────────────────────────────────────────────┘
```

### 核心原则

1. **文件即状态**：每个 session 拥有一个 DAG 状态目录，JSONL 是 append-only 真相源，YAML 是物化视图。
2. **Hook 驱动**：所有 guard 逻辑（sandbox、permission、cache、compact）都在 hook 函数中，不在图节点内联。
3. **可插拔工具**：ToolRegistry 替代硬编码的 `get_all_tools()`，支持动态注册和过滤。
4. **缓存友好上下文**：system prompt 按参数 memoized，DAG 投影附加在消息历史末尾，最小化 KV-cache 失效。

### 数据流

```
用户输入 → HermesInteraction.send_user_message()
  → _start_turn() → record_turn_started()
  → graph.ainvoke({messages: [HumanMessage]})
    → prepare_context (必要时压缩上下文)
    → agent (build_context_messages → LLM 调用)
    → tools (guard hooks → 中断处理 → 执行 → cache/compact hooks)
    → (循环或结束)
  → _finish_turn() → record_turn_finished()
  → InteractionResult(events)
```

---

## 二、模块清单（24 个模块）

| 模块 | 行数 | 职责 | 状态 |
|------|------|------|------|
| `state.py` | 1232 | 文件化 DAG 状态：事件记录、计划管理、上下文树、邮箱、声明、摘要、物化 | 核心，体量最大的模块 |
| `tools.py` | 1236 | 全部 34 个内置工具 + 子代理运行时 + 调试工具 + 辅助函数 | 体量最大，职责混合 |
| `sandbox.py` | 838 | Bubblewrap 沙箱策略 + 执行后端 + 文件操作 | 策略与后端耦合 |
| `interaction.py` | 649 | 运行时编排器：配置加载、图构建、事件发射、流处理、中断处理 | 职责较多 |
| `mock_llm.py` | 428 | 确定性测试 LLM，支持流式、工具调用、错误注入 | 测试用具 |
| `tool_runtime.py` | 340 | Hook 驱动的 guard pipeline + ToolNode 工厂 | 清晰 |
| `terminal.py` | 294 | CLI 终端适配器，事件渲染 | 清晰 |
| `config.py` | 294 | 配置加载、路径解析、环境变量展开 | 清晰 |
| `planning.py` | 273 | 纯函数 DAG 库：验证、物化、调度、版本快照 | 优秀 |
| `context.py` | 241 | 缓存友好的上下文组装：system prompt 记忆化 + DAG suffix | 清晰 |
| `graph.py` | 204 | LangGraph 拓扑定义，三层节点 + hook 注入 | 清晰 |
| `verification.py` | 192 | 状态一致性验证工具（供测试使用） | 测试辅助 |
| `models.py` | 161 | 全部 Pydantic 数据模型 | 清晰 |
| `registry.py` | 159 | 可插拔工具注册表，支持通配符过滤 | 基本完整 |
| `compaction.py` | 149 | 上下文压缩，保持工具调用组完整 | 清晰 |
| `cache.py` | 134 | 工具结果缓存（内存 + 文件） | 清晰 |
| `hooks.py` | 84 | Loop 钩子系统（6 阶段） | 完整 |
| `checkpoint.py` | 93 | 文件化 LangGraph checkpoint（pickle） | 功能层 |
| `skills.py` | 87 | 技能发现（仅 markdown） | 功能层 |
| `permissions.py` | 83 | 工具权限系统（allow/deny/ask + 正则匹配） | 清晰 |
| `smoke_llm.py` | 139 | 冒烟测试 LLM（确定性重构） | 测试用具 |
| `llm.py` | 39 | LLM 工厂（openai/anthropic/smoke） | 清晰 |
| `runtime.py` | 37 | RuntimeContext 数据类 | 清晰 |

---

## 三、上下文构造

### 三层结构（缓存友好）

```
[SystemMessage: system_prompt]     ← 按参数 memoized，跨 turn 稳定
[HumanMessage: 用户消息]             ←
[AIMessage: 助手回复]               ← append-only 历史
[ToolMessage: 工具结果]              ←
...                                 ←
[SystemMessage: DAG suffix]         ← 每轮变化，放在末尾
```

**KV-cache 影响**：system prompt 和所有旧消息的 KV 缓存跨 turn 复用；仅末尾 DAG suffix 需要每轮重新计算。

### System Prompt 组成

1. **模板** (`data/config/system_template.md`) — 用户信息、Agent 角色、操作规则
2. **运行时角色通知** — "Primary agent runtime." 或自定义
3. **Agent 指令** (`personalities/<id>/instructions.md`)
4. **长期记忆** (`personalities/<id>/memory.md`)
5. **技能摘要** (从 `SKILL.md` 文件加载)
6. **运行时规则** (硬编码：task mode 规则、缓存使用、DAG 驱动)
7. **沙箱摘要** (来自 `SandboxPolicy.describe()`)

### DAG Suffix（每轮变化）

- ISO 时间戳（每轮变化，保证 suffix 唯一）
- 用户/会话元数据
- 运行时计数器（active_subagents、pending_mailbox_items）
- **任务状态投影** = `context.md` 的内容（从磁盘每轮读取）

### context.md 的生成（project_context）

由 `TaskStateStore.project_context()` 从以下来源生成：

| 节 | 来源 |
|----|------|
| Mode | task.yaml (chat / task) |
| Goal | goal.md |
| Active DAG Node | plan.yaml (running 节点) |
| Running Nodes | plan.yaml |
| Ready Nodes（首个含详情） | plan.yaml（去重，限制数量） |
| Pending Nodes（前10） | plan.yaml |
| Completed Nodes（最近10） | plan.yaml |
| Blocked/Failed Nodes | plan.yaml |
| Plan Errors | plan.yaml 的验证错误 |
| Pending Mailbox（前5） | mailbox.jsonl（agent + parent 收件人） |
| Recent Summaries（最近3） | summaries/ 目录 |

### 当前未投影的内容

- **上下文树** (`context_tree.jsonl`) — 数据存在，可通过 `context_head`/`context_rewind` 工具查询，但树的拓扑结构不会自动注入 prompt。这是设计选择：上下文树用于分支管理和回溯导航，当前模型通过工具主动查询。
- **声明** (`claims.yaml`) — 数据存在，可通过 `claim_list` 查询，但不会自动投影到 context.md。后续可添加"Relevant Claims"节。

### 历史净化

`sanitize_message_chain()` 移除孤立的 `ToolMessage`（其 `tool_call_id` 不匹配任何已知 `AIMessage.tool_calls[].id`），防止向 provider 发送过期工具结果。

### 上下文压缩

触发条件：消息数 > 24 OR 估算字符数 > 32000 OR `compression_requested` 标志。

1. `split_for_compaction()` 将消息分组为工具调用组（保持 AI+Tool 配对）
2. 较旧的组由 LLM 总结
3. 总结写入 `summaries/summary_N.md`
4. 所有旧消息被替换为 `[Compacted History]` SystemMessage + 保留的最近消息
5. 压缩会使整个历史 KV-cache 失效（全量替换）

---

## 四、DAG 系统

### Plan DAG 结构

```yaml
# plan.yaml
version: 3
status: active
root: n_goal
nodes:
  - id: n_goal
    type: goal
    title: "构建最小二乘法工程"
    depends_on: []
    status: verified
  - id: n_inspect
    type: inspection
    title: "检查工作区状态"
    depends_on: [n_goal]
    status: completed
    success_criteria:
      - "已确认工作区文件和Python环境"
  - id: n_implement
    type: implementation
    title: "实现最小二乘法"
    depends_on: [n_inspect]
    status: running
```

### DAG 验证（planning.py）

1. 无重复节点 ID
2. `root` 节点必须存在（如设置了的话）
3. 每个 `depends_on` 引用必须存在
4. 无环（DFS 环检测）

### DAG 调度器（select_ready_node）

优先级排序键：`(type_priority, explicit_priority, dep_count, node_id)`

- `type_priority`：验证节点为 `0`，其他为 `1`（验证节点优先）
- `explicit_priority`：来自节点的 `priority` 字段，默认 100
- `dep_count`：依赖数越少越优先
- `node_id`：字母序固定决胜

节点"就绪"条件：
- 状态为 `pending` 或 `ready`
- **所有**依赖节点状态在 `TERMINAL_SUCCESS = {"completed", "succeeded", "verified"}` 中

### 计划版本管理

每次计划变更都会将前后快照保存到 `versions/plans/`：
- `index.yaml` 追踪 `version`、`hash`、`path`、`timestamp`
- 旧计划永不删除，仅标记 superseded

---

## 五、任务模式：复杂任务执行

### 生命周期

```
Chat Mode → task_begin(goal, steps) → Task Mode → task_exit(status) → Chat Mode
```

### 执行流程

**1. 进入任务模式** (`task_begin`)
- 将目标写入 `goal.md`
- 根据步骤通过 `replace_plan()` 构建 DAG → 创建 `n_goal` + 用户节点
- 写入 `plan.yaml`，mode 改为 task
- 通过 `project_context()` 生成 `context.md`

**2. 扩展计划** (`plan_autofill`)
- 添加标准骨架：`n_inspect → n_implement → n_verify → n_report`
- 每个节点含 `depends_on`、`success_criteria`、`priority`
- 已有同类型节点时不重复创建

**3. 驱动执行** (`plan_next` / `plan_update`)
- `plan_next()`：调度器选择最高优先级的就绪节点，标记为 running
- Agent 执行实际工作（filesystem_read、filesystem_write、shell 等）
- `plan_update(node_id, status)`：推进节点到 completed / verified / failed / blocked
- 完成节点自动解锁其下游依赖

**4. 监控** (`task_status`)
- 返回当前 DAG 状态、活跃节点、就绪节点、`next_action` 建议

**5. 退出** (`task_exit`)
- 验证：无未完成/running/blocked/failed 节点残留
- 切换 mode 回 chat
- 保留所有状态供审计

### 实际例子（notes.md 的最小平方法任务）

DAG: `{1, 2} → 3 → {4 → 5, 6} → 7`

实际执行轨迹（来自 DeepSeek 运行）：
- 11 轮工具调用，4 次上下文压缩
- 7 个 DAG 节点全部完成
- 24 个计划版本快照
- 5 个摘要文件
- 产物：`generate_data.py` + `least_squares.py`，R² = 0.98

---

## 六、工具系统

### 34 个内置工具（按功能分组）

| 分组 | 工具 | 沙箱模式 |
|------|------|---------|
| **文件系统** | `shell`、`filesystem_read`、`filesystem_write`、`filesystem_list` | sandboxed |
| **通信** | `ask`、`message_send` | host |
| **任务模式** | `task_begin`、`task_status`、`task_exit` | host |
| **计划 DAG** | `plan_add_nodes`、`plan_autofill`、`plan_next`、`plan_update`、`plan_node_history` | host |
| **摘要** | `summary_add`、`summary_list`、`summary_read` | host |
| **声明** | `claim_add`、`claim_list` | host |
| **子代理** | `subagent_create`、`subagent_wait`、`subagent_list`、`subagent_stop` | sandboxed |
| **记忆** | `memory_update`、`memory_list`、`memory_search` | sandboxed/host |
| **上下文** | `context_head`、`context_rewind` | host |
| **邮箱** | `mailbox_send`、`mailbox_read` | host |
| **调试** | `debug_analyze` | host |
| **缓存** | `cache_read` | host |
| **压缩** | `compact` | host |
| **技能** | `skill_load` | sandboxed |

### 工具执行路径

1. LLM 发出 `AIMessage` 附带 `tool_calls: [{name, args, id}]`
2. `route_after_agent` 检测到工具调用 → 路由到 `tools` 节点
3. `build_hook_tools_node` 编排：
   - 从最后一条 AI 消息提取待处理工具调用
   - `sandbox_permission_guard_hook` → 计算 denials + asks
   - `active_ask_hook` → 处理 ask 元工具
   - 处理中断（user_ask、tool_confirm）
   - 构建 denials map → `make_simple_tool_node` 按调用使用
   - 执行批准的工具（沙箱上下文内）
   - `cache_result_hook` → 缓存大结果
   - `compact_result_hook` → 触发压缩标志

---

## 七、Hook 系统

### 六阶段循环钩子

```
before_context  → [prepare_context 核心]  → after_context
before_agent    → [agent 核心]            → after_agent
before_tools    → [tools 核心]            → after_tools
```

### Hook API

```python
class LoopHooks:
    before_context: list[HookFn]
    after_context: list[HookFn]
    before_agent: list[HookFn]
    after_agent: list[HookFn]
    before_tools: list[HookFn]
    after_tools: list[HookFn]

    def register(stage, fn)        # 注册钩子
    async def run(stage, ctx)      # 按序运行；首个真值返回短路
```

### 默认 Guard Hooks（4 个已注册）

1. **sandbox_permission_guard_hook** (`before_tools`) — 沙箱门控 + 权限检查
2. **active_ask_hook** (`before_tools`) — ask 元工具中断处理
3. **cache_result_hook** (`after_tools`) — 缓存大工具结果
4. **compact_result_hook** (`after_tools`) — 触发压缩

注册方式：
```python
hooks = build_default_hooks()
register_default_guard_hooks(hooks)
```

---

## 八、工具注册表

### API

```python
class ToolRegistry:
    def register(tool, sandbox_mode)       # 注册工具
    def get(name) → BaseTool | None        # 按名称查找
    def get_all() → list[BaseTool]         # 全部已注册工具
    def filter(names) → list[BaseTool]     # 过滤 + 通配符展开 ("filesystem" → read/write/list)
    def sandbox_mode(name) → str           # 获取沙箱模式
    def registered(name) → bool            # 检查是否已注册
    def unregister(name)                   # 移除工具
```

### 引导

`bootstrap_registry()` 导入 `tools.py`，调用 `get_all_tools()`，使用 `TOOL_SANDBOX_MODE` 注册每个工具。这是从旧硬编码列表到注册表架构的桥接。

---

## 九、状态系统

### 目录布局

```
data/sessions/<session_id>/state/
  task.yaml           # mode (chat/task), status, timestamps
  goal.md             # 声明的目标
  plan.yaml           # DAG 节点、状态、依赖
  events.jsonl        # append-only 交互事件
  graph.jsonl         # append-only 图事件（DAG 归因）
  context_tree.jsonl  # append-only 上下文树节点
  mailbox.jsonl       # append-only 邮箱消息
  state.yaml          # 物化视图（从所有 JSONL 计算得出）
  context.md          # 给模型看的上下文投影
  claims.yaml         # 结构化声明 + 证据
  artifacts/          # 产物文件（diffs/、logs/、reports/ 等）
  versions/plans/     # 计划版本快照（index.yaml + 文件）
  summaries/          # 压缩和手动摘要（markdown）
  checkpoints/        # 遗留目录（versions/ 是活跃的）
  locks/              # 锁文件目录（已创建但未使用）
```

### 关键操作

- `ensure_initialized()` — 创建所有目录，写入初始文件
- `materialize_state()` — 读取所有 JSONL → 计算当前状态字典 → 写入 `state.yaml`
- `project_context()` — 从计划 + 邮箱 + 摘要构建 `context.md`
- `defer_materialization()` — 批量写入 state.yaml（避免重复 I/O）
- `begin_task_mode()` / `exit_task_mode()` — 进入/退出任务模式
- `start_next_plan_node()` — 调度器选择并标记 running
- `update_plan_node_status()` — 推进节点，版本快照计划

---

## 十、当前结构性问题

### 设计层面（体量过大的模块）

| 模块 | 行数 | 问题 |
|------|------|------|
| `tools.py` | 1236 | 工具定义 + 子代理运行时 + 调试逻辑 + 辅助函数混合 |
| `state.py` | 1232 | 事件 + 计划 + 邮箱 + 声明 + 摘要 + 物化全在一个类中 |
| `sandbox.py` | 838 | 策略层和 bubblewrap 后端耦合在同一类中 |
| `interaction.py` | 649 | 配置 + 图构建 + 事件处理 + 去重 + 流组装混合 |

### 功能层面（已实现但未接入）

| 组件 | 状态 |
|------|------|
| Context tree 在 LLM prompt 中 | 数据存在，可通过工具查询；未自动投影到提示中（设计选择） |
| Claims 在 LLM prompt 中 | 数据存在，可通过 `claim_list` 查询；未投影到 context.md |
| `pending_mailbox_items` | 硬编码为 0，后续计划实现 |

### 性能关注点

| 位置 | 问题 |
|------|------|
| `state.py:882-889` | 每次写事件时通过遍历所有现有行计算 `next_index`——每次写 O(n) |
| `state.py:763-835` | `materialize_state()` 将所有事件读入内存 |
| `state.py` 多处 | 无文件锁——并发写入可能损坏 JSONL/YAML |
| `skills.py:76-86` | `get_skills_summary()` 每个 LLM 调用都读取磁盘，无缓存 |

### 工具级别

| 工具 | 状态 |
|------|------|
| `search_workspace` | 未实现（task.md H1 指定） |
| `subagent_create(mode="detach")` | 创建记录但不生成后台进程（仅 attach 模式工作） |
| `message_send` | 返回内容但无实际投递机制 |
| `validate_sandbox_modes()` | `registry.py` 中为桩函数 |

### 其他

| 问题 | 位置 |
|------|------|
| 无进程看门狗——如果进程被 SIGKILL，JSONL 最后一行可能不完整，重启时无恢复 | `state.py` |
| `checkpoint.py` 使用 pickle 格式，跨 Python 版本迁移脆弱 | `checkpoint.py` |
| `sandbox.py:71` 临时目录创建但未清理 | `sandbox.py` |
| 子代理无超时——如果子代理挂起，父代理也会挂起 | `tools.py:982-1117` |
| `skills.py` 使用原始 `open()` 绕过沙箱 | `skills.py:28,57` |

---

## 十一、task.md 覆盖率

| task.md 章节 | 描述 | 覆盖 |
|-------------|------|------|
| H0: 文件状态机 | 任务目录、goal/plan/graph/events/artifacts | ✅ |
| H0: 最小循环 | prepare_context → agent → tools | ✅ |
| H0: 中断+恢复 | GraphInterrupt + FileBackedSaver | ✅ |
| H1: 工具注册表 | ToolRegistry（register/filter/sandbox） | ✅ |
| H1: 工具 schema | LangChain tool binding | ✅ |
| H1: 权限策略 | PermissionSystem allow/deny/ask | ✅ |
| H1: read_file / write_file / shell | 全部实现 | ✅ |
| H1: search_workspace | 未实现 | ❌ |
| H2: 带 skill.yaml 的技能 | 仅 SKILL.md | ❌ |
| H2: 规划器选择技能 | 未实现 | ❌ |
| H3: Memory / Claims / Summaries | 基础实现，缺少部分字段 | ⚠️ |
| H3: 上下文压缩 | compaction.py | ✅ |
| H4: Subagent（attach） | 已实现 | ✅ |
| H4: Subagent（detach + 后台运行器） | 未实现 | ❌ |
| 4.3: Plan patches | 仅 add_nodes，无选择性 supersede | ⚠️ |
| 4.4: Unblock-chain 优先级 | 调度器缺失 | ⚠️ |
| 7: Middleware hooks | LoopHooks 已实现 | ✅ |
| 10: Budget tracking | 无 max_iterations/used_iterations | ❌ |

---

*报告基于 `claude-refactor` 分支生成。97 个测试通过。DeepSeek smoke 通过。DAG 任务运行通过。*
