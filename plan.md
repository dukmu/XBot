# XBot Hermes 重构计划

## 定位

本计划面向当前 `claude-refactor` 分支，依据三类信息制定：

- Codex 上一轮会话 `019e8321-eee1-7033-88b2-c4ec0c529d68` 在 `master` 上形成的结论和提交结果；
- 当前 `claude-refactor` 分支的代码审查；
- `status.md`、`AGENTS.md`、`current_design.md` 与实际代码之间的语义漂移。

方向不再摇摆：

> State 是系统中心。Runtime 是小型协调器。Tools 和 Hooks 是可加载能力。Context 构建必须显式。Compaction 是可审计流程，不是隐藏的 prompt 变形。

不要保留双语义。不要保留旧架构兼容分支。先把模型和流程定清楚，再一次性把主路径切过去。简单即最优。

## Codex 历史结论

上一轮 Codex 在 `master` 上完成的是一个有价值的 MVP，不是最终架构。

已经完成并验证：

- `data/sessions/<session_id>/state/` 下的文件化 agent state。
- append-only 的 `events.jsonl`、`graph.jsonl`、`context_tree.jsonl`、`mailbox.jsonl`。
- `state.yaml`、`context.md`、`claims.yaml`、summaries、artifacts、plan versions。
- Task mode：`goal.md`、可执行 `plan.yaml`、`plan_next`、`plan_update`、任务完成 guard、DAG attribution。
- Context tree、mailbox、attach-mode subagent、file-backed LangGraph checkpoint、tool-result cache、memory/debug/read locator tools。
- 严格 DeepSeek smoke：两个连续 DAG task、动态 `plan_add_nodes`、compact 事件、关键工具 DAG 归因、默认不持久化 token delta、文件级 claims。

上一轮明确没有宣称完成：

- claims 只是存储，验证语义仍弱。
- compaction summary 可能事实漂移，缺少独立校验。
- detach/background subagent runner 没有完成。
- agent trajectory 的顺序和节点一致性验证仍不够强。
- runtime 仍有隐式 context/global 和 LangGraph 绑定假设。

关键结论：上一轮打下的 state/DAG 基础要保留；当前分支不应推倒重来，而应把 runtime、tools、hooks、context 统一到一条清晰主路径。

## 当前分支审查结论

`claude-refactor` 的方向正确，但目前处在半迁移状态。

主要问题：

- `xbot/builtin_tools/` 已存在，但实际 runtime 仍通过旧 `xbot.tools` 启动工具。
- `builtin_tools.get_all_tools()` 当前不完整：`task_begin`、`task_status`、`task_exit`、`plan_add_nodes` 缺失或不是正确的 `BaseTool`。
- `xbot/hooks/guard.py` 仍从 `xbot.tools.get_tool_sandbox_mode` 读取 sandbox mode，说明 `ToolRegistry` 不是真实元数据源。
- `HermesInteraction._user_input_state()` 没有传入配置里的 `agent_role`，导致 `build_context_messages()` 可能回退到 `"A helpful assistant"`。
- `context.py` 仍直接读 runtime contextvar、config、state 文件，context 构建不是纯输入到输出。
- 文档声称的 API 和实际代码不一致，例如 hook 文件路径、函数名、工具数量、registry 主路径。

结论：当前分支不是稳定终点，而是架构切换中间态。下一轮目标是把它收口成唯一主路径。

## 目标模型

### 身份模型

四个 ID 必须职责清晰，不能混用。

```text
session_id      命名 workspace/cache/state/saver/subagents
personality_id  选择 instructions/memory/permissions/sandbox/skills
thread_id       LangGraph checkpoint conversation key
task_id         DAG state subject；主 agent 是 "agent"，子 agent 是 subagent_id
```

主 agent state：

```text
data/sessions/<session_id>/state/
```

子 agent state：

```text
data/sessions/<session_id>/subagents/<subagent_id>/state/
```

不允许再出现 `tasks/default/` 语义，也不允许 subagent 创建 sibling session。

### RuntimeFrame

每次 graph 调用都应由显式 `RuntimeFrame` 驱动。

```text
RuntimeFrame
  runtime: RuntimeContext
  user: UserContext
  personality: PersonalityProjection
  sandbox: SandboxProjection
  tools: ToolRegistrySnapshot
  task: TaskProjection
  messages: sanitized history
```

`RuntimeFrame` 由 `HermesInteraction` 构建，然后传给 graph/context/tool stages。context 构建代码不再自行发现 config 或 state。

### ToolRegistry

只保留一个工具事实源。

```text
ToolRegistry
  entries:
    name:
      tool: BaseTool
      sandbox_mode: sandboxed | host
      group: filesystem | task | plan | summary | ...
      source: builtin | plugin
      enabled: bool
```

规范来源：

```text
xbot/builtin_tools/
```

临时桥接：

```text
xbot/tools.py
```

`xbot/tools.py` 可以暂时 re-export `builtin_tools`，但不能继续定义第二套工具实现或第二套 `TOOL_SANDBOX_MODE`。

### LoopHooks

Hooks 是可加载 runtime 扩展，不是兼容旧路径的隐藏分支。

```text
LoopHooks
  before_context
  after_context
  before_agent
  after_agent
  before_tools
  after_tools
```

标准 hooks：

```text
before_tools:
  sandbox_permission_guard
  active_ask

after_tools:
  cache_large_results
  compact_requested
  persist_tool_trace
```

guard hook 必须通过 `ToolRegistry` 查询工具元数据，不能 import `xbot.tools` 决定 sandbox 行为。

### Context 构建

context 是确定性 pipeline。

```text
RuntimeFrame
  -> ContextProjection
  -> ContextMessages
  -> Provider call
```

消息布局保持 cache-friendly：

```text
[SystemMessage: stable prefix]
[history messages]
[SystemMessage: dynamic task suffix]
```

stable prefix 只包含：

- resolved system template；
- personality instructions；
- memory；
- skills summary；
- stable runtime rules；
- sandbox summary。

dynamic suffix 只包含：

- 当前时间；
- `context.md` 的 task projection；
- active DAG node 和 ready nodes；
- pending mailbox count；
- active subagent summary；
- 后续加入的 relevant claims 和 summaries。

`context.py` 不应直接调用 `load_agent_prompt()`、`load_memory()`、`try_get_runtime_task_state()`。这些都是 frame/projection 的输入，不是 context 构建内部依赖。

### Compaction 流程

compaction 是显式 state transition。

```text
messages
  -> split into tool-safe groups
  -> select old groups
  -> summarize with source markers
  -> write summary artifact
  -> append graph/context_tree event
  -> replace old groups with compacted summary message
```

规则：

- 不能拆开 AI tool-call message 和对应 ToolMessage。
- 不能压掉未解决 interrupt。
- summary artifact 必须记录 source message ids 或 ranges。
- compaction 必须写入 `graph.jsonl`、`context_tree.jsonl`、`summaries/`。
- compacted message 必须能和普通 system prompt 区分。

## Runtime 流程图

```text
HermesInteraction
  load RuntimeContext
  load PersonalityProjection
  load ToolRegistry
  load LoopHooks
  load TaskStateStore
  build RuntimeFrame
        |
        v
LangGraph executor
  prepare_context
    before_context hooks
    compaction stage
    after_context hooks
        |
        v
  agent
    before_agent hooks
    build ContextMessages from RuntimeFrame
    provider call
    after_agent hooks
        |
        v
  tools
    before_tools hooks
    permission/sandbox/ask interrupts
    execute approved tools
    after_tools hooks
        |
        v
  prepare_context or END
```

LangGraph 只是 executor，不是架构本身。runtime model 必须能脱离 LangGraph 内部细节做单元测试。

## 开发计划

### Phase 0：冻结模型

目标：先消除歧义，再改行为。

工作：

- 定义 `RuntimeFrame`、`PersonalityProjection`、`TaskProjection`、`ToolEntry`。
- 为 session/personality/thread/task/primary state/subagent state 写路径一致性测试。
- 更新文档，明确 `builtin_tools` 是未来唯一工具源。

验收：

- frame construction 可以不启动 LangGraph 单测。
- 测试不再依赖隐式默认 `agent_role` 或隐式 state。

### Phase 1：工具可加载

目标：一个 canonical tool implementation，一个 registry。

工作：

- 修复所有 `xbot/builtin_tools/*`，确保每个工具都有 `@tool`。
- 每个模块显式导出 `*_TOOLS`。
- `xbot.builtin_tools.get_all_tools()` 返回完整工具集。
- sandbox metadata 进入 `ToolRegistry` entry。
- `bootstrap_registry()` 改为加载 `xbot.builtin_tools`。
- `xbot/tools.py` 改成薄 re-export。
- 增加 registry integrity tests：
  - 每个导出对象都是 `BaseTool`；
  - 每个 enabled tool 都有 sandbox metadata；
  - `filesystem` wildcard 正确展开；
  - 无重复工具名；
  - 迁移期旧 `xbot.tools.get_all_tools()` 和新 registry 工具名一致。

验收：

- runtime 从 `builtin_tools` 启动。
- guard hooks 不再 import `xbot.tools`。
- `uv run pytest -q` 通过。

### Phase 2：Hooks 可加载

目标：hooks 是显式 runtime 组件。

工作：

- 保持 `LoopHooks` 简单。
- 增加 loader：组装 standard hooks 和 config 指定的可选 hooks。
- hook 顺序必须确定。
- hook context 必须包含 `ToolRegistry`。
- 所有 tool guard metadata lookup 改为 registry。
- 增加测试：
  - hook registration order；
  - short-circuit；
  - permission deny/ask；
  - sandbox deny/ask；
  - cache/compact after-hooks。

验收：

- standard hooks 可通过单一入口加载。
- 测试里可注册 custom hook，不需要改 graph。
- registry 切换前后 guard 行为一致。

### Phase 3：Context 显式化

目标：context construction 是纯转换。

工作：

- 在 `interaction.py` 或 `runtime.py` 增加 `build_runtime_frame()`。
- 增加 `build_context_projection(frame)`。
- `build_context_messages()` 改为接收 projection，不再读全局 config/state。
- 保证 `agent_role`、`system_notice`、sandbox summary、memory、skills、task projection 都来自 frame。
- 保持 stable prefix、history、dynamic suffix 的布局。
- 增加 context 精确断言测试：
  - personality role；
  - memory；
  - skills；
  - task projection；
  - mailbox count；
  - active node。

验收：

- `context.py` 不直接依赖 runtime state contextvar。
- fake frame 可以独立构建 context。
- provider prompt 一定包含配置里的 `agent_role`。

### Phase 4：Compaction 可审计

目标：压缩是可回放状态变化，不是不可见 prompt 修改。

工作：

- 保留 `split_for_compaction()`，但显式定义 message group 模型。
- summary artifact 写入 source ids/ranges。
- 每次 compaction 写入 graph event 和 context tree event。
- 增加最小 summary verifier，检查 summary 至少引用 source ranges。
- 增加测试：
  - AI/tool group 不被拆开；
  - unresolved interrupt 不被压掉；
  - summary artifact 被写入；
  - context tree 记录 compaction；
  - task projection 仍包含 recent summaries。

验收：

- compaction 可以从 state files 审计。
- 模型能看出某段是 compacted history，不是 system instruction。

### Phase 5：Runtime 一致化

目标：一个 runtime 模型，一个 state root，一个 checkpoint 策略。

工作：

- `TaskStateStore` 继续作为 source of truth。
- `FileBackedSaver` 继续负责 LangGraph checkpoint。
- 清理或显式标注 `InMemoryStore` 只是 executor-local cache，不是状态事实源。
- `HermesInteraction.create()` 固定构建：
  - paths；
  - state store；
  - frame builder；
  - registry；
  - hooks；
  - graph executor。
- 增加 restart tests：
  - 同 session/thread 恢复 checkpoint；
  - 新 `HermesInteraction` 可读取旧 state projection；
  - 可行时验证 interrupt resume after restart。

验收：

- restart tests 证明 state/checkpoint 不依赖单个进程实例。
- `state.yaml` 仍能从 append-only logs 和 `plan.yaml` 重建一致视图。

### Phase 6：语义状态增强

目标：claims、summaries、task status 有实际行为价值。

工作：

- claims 增加 confidence、evidence refs、invalidates_if、superseded_by。
- relevant claims 投影到 `context.md`。
- `verify_task_state()` 增加 claims/summaries 语义检查。
- `task_status` 和 `debug_analyze` 报告 claim/summary health。

验收：

- verified claim 必须有 evidence。
- superseded claim 不再作为当前事实投影。
- claims 可链接 graph events、summary 或 artifact。

### Phase 7：Subagent 和 Mailbox Runtime

目标：先保留 attach-mode 稳定，再谨慎加入 detached runtime。

工作：

- 保留 attach subagent 在 parent session 下运行。
- runtime/frame/registry 一致后再实现 detach runner。
- 增加 budgets、timeout、cancellation、child workspace policy、diff handoff。
- 增加 runtime mailbox dispatcher，用于 background events。

验收：

- detached subagent 不创建 sibling session。
- parent 和 child DAG 可独立审计。
- child outputs 通过 mailbox 和 graph events 回传。

## 推进顺序

一次性推进主路径，但按稳定节点提交：

1. Frame and projections。
2. Canonical `builtin_tools` registry。
3. Hook loader and registry-backed guard hooks。
4. Pure context construction。
5. Auditable compaction。
6. Runtime restart consistency。
7. Semantic claims/summaries。
8. Detached subagent runner。

不要在 Phase 1-5 完成前推进 Phase 7。runtime 不一致时做 multi-agent，只会放大混乱。

## 验证计划

基础验证：

```bash
uv run pytest -q
python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py xbot/builtin_tools/*.py xbot/hooks/*.py tests/*.py
```

新增 targeted tests：

```bash
uv run pytest -q -k "tool_registry or builtin_tools"
uv run pytest -q -k "hooks or permission or sandbox"
uv run pytest -q -k "context_projection or compaction"
uv run pytest -q -k "restart or checkpoint or task_state"
```

最终行为 smoke：

```bash
uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke
```

smoke 必须验证：

- 两个连续 DAG tasks；
- `task_begin`、`plan_next`、`plan_update`、`task_exit`；
- 动态 `plan_add_nodes`；
- compact tool 或 runtime compaction；
- 关键 tool calls 有 DAG attribution；
- claims 显式提到目标文件且有 evidence；
- 默认不持久化 token deltas；
- 运行后 state files 可审计。

## 不可妥协约束

- 一个 canonical tool source。
- 一个 canonical hook loader。
- 一个 explicit runtime frame。
- 主 agent 一个 state root。
- 不保留旧 tool/guard semantics 的隐藏 fallback。
- prompt construction 不允许静默读取全局 runtime state。
- compaction 必须可从文件审计。
- subagent state 必须在 parent session 下。
- 测试不能只覆盖旧路径而让新路径损坏。

最优解是少概念、显式数据流、确定性 loader、可读 state 文件。
