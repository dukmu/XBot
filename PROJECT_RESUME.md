# XBotv2 项目简历

## 一句话总结

XBotv2 是一个从零重写的通用 C/S 架构 AI Agent 运行时：约 155 行的极简 ReAct 核心循环，配类型化 HTTP/SSE 协议、41 阶段 Hook 契约、统一文件系统工具与权限/沙箱、事务式插件体系（Goal/TodoList/Skills/MCP/Compact），并由 HarnessBench 104 任务评估闭环持续驱动迭代。

## 30 秒总结

前身是基于 LangGraph 的 Hermes 原型，控制流复杂、依赖过重。我主导从零重写，目标"通用、可读、稳定"。系统按五层设计边界组织：Clients（Textual TUI / Web）→ Protocol（HTTP/SSE/UDS，API v3 session/thread 资源模型）→ Core（SessionRuntime/Engine/ContextBuilder/AgentInbox/InteractionWaiter/Hooks）→ Tools（ToolRegistry/权限/沙箱/filesystem_ops）→ Providers（OpenAI/Anthropic/Mock），插件只依赖 `xbotv2.api` 稳定面。两条主线贯穿全程：一是把输入语义讲清楚——用户消息忙时折入当前 turn、后台完成只注入上下文、Goal 延续是唯一主动唤醒（为此移除了整个 mailbox 体系）；二是以评估驱动开发——HarnessBench 104 任务横向对比，将系统从基线 0.787 推到 0.845（中位 0.905、37 个满分任务），并验证了每任务成本降到 M3 的六成。最终 798 项测试全绿、多 Provider 实测可用。

## 技术栈与关键数据

| 维度 | 内容 |
|---|---|
| 语言/运行 | Python 3.12，`uv`，UTF-8 全链路 |
| 依赖立场 | 零 LangChain（已系统性移除），核心无框架依赖 |
| 协议 | HTTP + SSE（`ServerEvent` 类型化 DTO）、Unix Domain Socket、OpenAPI v3、API `xbotv2.v3` |
| 核心 | `_run_turn_impl` 约 155 行编排器 + 显式阶段方法 |
| Hook | 41 阶段，observer/transform/guard 三类契约 + 类型化视图 |
| 客户端 | Textual TUI、TypeScript Web（UDS 代理） |
| LLM | OpenAI / Anthropic / LM Studio 兼容适配器 + Mock |
| 评估 | Inspect 桥接 HarnessBench，104 任务对比 |
| 测试 | Core 693 + Integration 100 = 798 通过（近期全量） |

## 架构与设计

```
Plugins(compact/todolist/goal/skills/mcp/agents) ──import xbotv2.api──▶ Core
Clients(TUI/Web) → Protocol(HTTP/SSE/UDS · SessionManager)
                 → Core(SessionRuntime · Engine · ContextBuilder · AgentInbox · InteractionWaiter · Hooks)
                 → Tools(ToolRegistry · PermissionSystem · Sandbox/Bubblewrap · filesystem_ops)
                 → Providers(OpenAI/Anthropic/Mock)
                 → Persistence(append-only messages.jsonl · 指纹增量)
```

核心设计原则：

- **稳定公共 API 是契约不是实现**：`xbotv2.api` 符号清单由 `test_public_api.py` 从 `api_inventory.md` 校验；插件永不触达 HookManager/Engine 内部。
- **显式设计边界**：core/protocol/providers/tools/plugins/clients 各司其职；slash 命令（人机）、Agent 工具、prompt 展开是三条独立执行边界，互不借用。
- **输入三通道不混用**：用户消息 → `pending-fold`（忙时折入）；后台完成 → `AgentInbox`（只注入下个 turn 上下文，不唤醒）；Goal 延续 → `request_continuation`（唯一主动唤醒）。
- **provider 中立**：所有 Provider 差异（blocks/thinking/tool 调用/usage/重试）封装在适配器内，核心只面对归一化 `Message`/`ToolCall`。
- **可读性优先**：拒绝通用 DSL、数值优先级框架、包装执行器。

## Hook 系统详细设计

Hook 是插件在 `PluginSetupContext.register_hook` 注册的同步有序异步回调，共 **41 个 `HookStage`**，每个阶段有明确的类别、返回契约、短路策略、严格性与载荷，文档矩阵 `hook_stage_matrix.md` 由测试反向校验（`test_hooks.py` 逐行核对矩阵覆盖全部阶段）。

**三类契约（阶段的核心抽象）**

- **Observer**：必须返回 `None`，每个回调都运行，失败默认记录日志。
- **Transform**：返回阶段专属文档化字典（如 `{context_messages}`、`{user_input}`、`{tool_results}`）。字典按阶段声明键校验——未知键、空字典、任意值、observer 返回控制流都是契约错误。
- **Guard**：返回 `HookDecision`。`CONTINUE` 继续下一个 guard；`DENY`/`STOP` 终止并给出显式原因；`ALLOW` 只在 `BEFORE_TOOL_CALL` 合法，它只记录预授权、后续 guard 与核心权限策略仍可拒绝——**Hook 能收窄/拒绝/改写调用，但不能授予权限**，最终以核心权限对变换后调用的检查为准。

**失败语义与短路**

- 生命周期/持久化 strict 阶段（`on_session_init`、`on_session_close`、`on_stop`、`before_state_persist` 等）：跑完所有回调后把失败聚合成 `ExceptionGroup` 抛出。
- guard/transform 失败立即传播——带着未授权或部分变换的操作继续是危险的；任务取消同样立即传播，不跑后续回调。
- 默认短路集（`SHORT_CIRCUIT_STAGES`）与调用方显式短路（`short_circuit=True`，如 `AFTER_AGENT`）两套机制，由矩阵记录。

**持久化 Hook 的"检查点"语义**（避免重复观察）

`Engine.save_messages()` 先比较归一化消息快照指纹，未变化则直接返回，不触发 `BEFORE_STATE_PERSIST`/`AFTER_STATE_PERSIST`。before 阶段允许改写消息列表，该变更与同一 checkpoint 一起落盘；工具消息保留立即 checkpoint，保证 assistant 的 tool_calls 一旦提交就有配对 tool result。

**类型化视图（防止插件触碰实现）**

- `ModelRequestView`（frozen/slotted）：messages/tools/llm 只读视图；变换阶段用返回字典而非原地改 `model_request`。
- `ContextComponent`（frozen/slotted）：记录 role/content/source/prompt stage；`AFTER_CONTEXT_COMPONENTS_BUILD` 可整体替换 `ctx.context_components`，非法元素在转 provider 消息前即失败。
- `invoke_model(messages)`：一次无绑定辅助调用，供 Compact 等做摘要；不递归跑 Hook、不发 assistant 流事件，失败在替换历史前向调用方传播。
- `request_user_input()`：与内置 `ask_user` 同一交互通道，插件（如 MCP elicitation）可复用而不产生第二个 waiter。

**Engine 编排**：`_run_turn_impl` 只解释自己负责的阶段载荷（accept/context/model/tool/finish 各阶段方法独立解释），无统一 Hook 结果解释器；内部完成记录不越界为协议事件。

## Goal 与 TodoList 插件实现

两者是"目标 vs 步骤"的分层：Goal 持有持久目标，TodoList 跟踪具体工作项，互不代庖。

**GoalPlugin（`builtin_plugins/goal`）——外置状态机 + 自动延续**

- 数据模型：`PluginStore` 中至多一条 Goal 记录 `{objective, status(active/paused/complete/blocked), summary, token_budget}`，转移即时持久化，resume 把 terminal/paused 转回 active。
- 模型面三个工具：`create_goal` / `get_goal` / `update_goal`（`Literal + Tool.from_function`，无手写 schema）。
- 人机面 `/goal` 命令：复用同一私有状态转移，不构造 ToolCall、不进权限、不追加模型历史。
- **自动延续机制（核心）**：每次成功转移（set/resume）后置 `_continuation_pending` 标记并调 `request_continuation()` 预约一次新 turn；`ON_TURN_START` 发现 `ctx.continuation` 时清标记并为该 turn 构建一份非持久化 Goal 快照；`ON_TURN_END` 在 Goal 仍 active 时最多再预约一次。运行时通知（后台任务/子代理完成）不驱动该状态机。
- 权限：`BEFORE_TOOL_CALL` 对三个基础工具返回 `ALLOW` 预授权，核心权限仍可否决。
- 完成/阻塞保留 Goal 与 summary 供人工审查，`clear` 才删除；不允许模型仅凭"过程复杂/用了 Todo"就创建 Goal，`complete` 要求逐项结果与验证。

**TodolistPlugin（`builtin_plugins/todolist`）——单工具原子替换**

- 模型面只有 `update_todos`：整体替换完整清单 `[{content, status}]`，**不做 per-item 的增删改查工具**（避免状态碎片化）。
- 校验：每项恰好 `content`+一个合法状态；非空未完成清单必须恰好一个 `in_progress`；`todos: []` 清空。整单先验证再一次持久化，非法输入不可能部分生效。
- 幂等：重复当前清单是 no-op，结果明确提示"先干活再调用"；全部完成后结果带 `todos`+`cleared` 结构化数据，并清空 active 清单。
- 结果留在正常对话路径（下个模型调用可见），不注入 system 消息、不改写 provider 上下文；变更时一次 `PluginStore` 写，resume 看到同一清单；unload 移除工具但保留会话数据。

**验证**：Goal/Todo 语义重写为可观察行为断言（`test_goal.py` 130 行、`test_todolist.py`），配合 `/goal` 命令实测（create→auto continuation→pause→resume→complete 全链路）；Goal 延续在评估任务中验证不会因后台通知反复唤醒。

## 关键技术决策与演进

1. **废弃 LangGraph/Hermes 原型 → 从零重写**：控制流与依赖过重，重写换取可读性与所有权清晰。
2. **移除整个 mailbox 体系 → pending-fold + AgentInbox + request_continuation**：mailbox 把用户消息、后台事件、goal 事件混在一个队列，语义混乱。改为三通道分离（见架构节），同时删掉 `BEFORE/AFTER_MAILBOX_DELIVERY` 两个 Hook 与 `enqueue_mailbox` 能力。
3. **API v2 → v3 session/thread 资源模型**：全局命令路径改为版本化资源；协议持有 typed DTO，TUI/Web/SDK 共用同一契约。
4. **统一文件系统语义**：host 与 bwrap 两套重复实现收敛为一套 `filesystem_ops`；patch 限定单文件以保证权限/审计精确。
5. **Prompt 瘦身与结构化**：合并重复默认指令、结构化内部 runtime 消息、删除死配置 `system_template`、fenced `var` block 做变量展开。
6. **非交互 fail-closed**：once 模式过滤 `{ask_user, request_permission}`（`bootstrap.py:211`），杜绝无限等待。
7. **输入状态机与协议解耦**：SSE 断连不再等于 session 销毁；resume 从持久历史重建运行时，交互请求显式声明不可恢复。

## 遇到的问题：分析 · 应对 · 结果

### 1. 运行时输入、通知与 turn 的边界

**问题**：忙时用户输入、排队消息与后台完成通知，与运行中/未来的 turn 之间边界不清——消息容易重复投递或丢失 turn 边界；后台完成一旦"隐式唤起 turn"就会反复产生新 turn，或需要一套复杂状态机来区分来源。
**分析**：单队列同时承载用户输入、后台通知与 goal 事件，"应该注入内容"和"应该启动 turn"被混为一谈；折入的消息没有明确的 turn 归属，客户端渲染与事件流不同步。
**应对**：把输入边界收敛为三条简单通道，不做来源判断状态机——`pending-fold` 在下一个工具批边界整体折入（按序发布 `message` 事件、末位流独占合并回复、无工具边界的残留以 `input_rejected` 拒绝并让客户端重试）；`AgentInbox` 只入队，在下个 turn 的上下文组装时一次性以 `<runtime_event>` 注入，**从不唤醒 turn**；goal 延续走显式 `request_continuation`，成为唯一主动唤醒。
**结果**：排队输入稳定进入 transcript、合并回复单次投递；通知不产生新 turn，goal 延续与通知互不竞争；排队→折入→合并链路有集成测试覆盖、TUI 实测通过，Long-running Autonomy 评估任务表现稳定。

### 2. Agent 注意力管理：细粒度工具返回导致模型涣散

**问题**：细粒度的工具返回——例如让模型逐项增删改查 todo——会诱导模型沉浸在条目维护中，反复调整状态而偏离真实任务。
**分析**：模型会把"工具调用成功"误当成"任务推进"，工具面越碎，注意力越分散。
**应对**：工具面提供整单原子的操作（`update_todos` 一次整体替换并校验），工具描述明确"何时不该用"，重复当前清单为 no-op 并提示先干活再调用。
**结果**：todo 状态始终一致、不再分散注意力，模型把精力留在实际工作上；这一约束也成为通用 Agent 设计准则（见收获）。

### 3. 上下文利用率低

**问题**：模型上下文被低价值内容挤占，真实任务可用空间不足。
**分析**：两个来源——核心提示词过度鼓励"规划-验证-反馈"循环，把简单任务拉长成多轮往返；工具返回过重，重复内容与易变信息（如文件指纹）直接混入上下文。
**应对**：提示词瘦身（合并重复默认指令、结构化内部 runtime 消息）；工具返回轻量化（截断、模型可见的相对路径引用、指纹等易变细节按需读取而不注入）；确定性前缀稳定 provider cache。
**结果**：评估中每任务 token 从约 306k 降至约 146k，完整输入 cache-read 比例稳定在 91–92%，Software Engineering 类别分数回升。

### 4. 后台任务两套机制

**问题**：后台 shell 任务与后台 subagent 若各建一套任务管理（ID、状态转移、等待、取消、输出存储、清理），会形成两套漂移的机制。
**分析**：shell 工具与 subagent 工具自然长得像，但生命周期语义不同，容易各自实现一遍。
**应对**：统一到 `api/jobs/JobRegistry` 单一实体，两者共用完整生命周期，工具层只按 kind 适配；wait/read/cancel 各自有明确边界与输出上限。
**结果**：JobRegistry 重构所在提交在评估中相对稳定点配对 +0.0173/+0.0236，工具失败率降到 2.37%。

### 5. 沙箱工具的保护边界与实现冗余

**问题**：文件系统操作若在宿主进程内直接执行，会绕过沙箱保护；而与 shell 各维护一套执行代码，则保护边界与语义都会漂移。
**分析**：保护边界应该由统一的沙箱运行时给出，而不是每个工具自己实现"安全"。
**应对**：文件系统操作以子进程方式在 Bubblewrap 沙箱内启动，与 shell 共用同一套后端与挂载策略；host 与沙箱共用同一 `filesystem_ops` 语义，删除沙箱内重复脚本。
**结果**：单一保护边界、代码冗余消除，host/bwrap 契约一致性由测试覆盖。

### 6. 交互能力的运行时边界

**问题**：once 等非交互模式若仍安装交互 sink 而客户端从不回应，会无限等待。
**分析**：交互性不是工具属性，而是运行时模式；在工具执行里堆协议条件会导致每个工具都要考虑交互。
**应对**：`interactive` 标记贯穿 runtime 组装，非交互下过滤 `{ask_user, request_permission}`，权限 ask 直接 fail-closed。
**结果**：once 模式不挂起；交互能力的边界由运行时声明，而不是散落在工具逻辑里。

### 7. 多 Provider 的语义归一化

**问题**：不同 LLM Provider 的消息块结构、thinking、工具调用分段与用量统计口径各异，直接映射会破坏互操作与成本统计。
**分析**：核心不能为每个 Provider 写一套分支；差异必须收敛在单一归一化契约之后。
**应对**：统一 provider-neutral `Message`/`ModelResponse` 契约，适配器内完成块列表转换、thinking 元数据保留、用量口径对齐，重试只在 Provider 实现内发生。
**结果**：OpenAI / Anthropic / LM Studio / MiniMax 多 Provider 实测可用；评估桥修正后双 Provider 全量运行（269aa3 的 M3 与 DeepSeek 各 106 任务）。

## 评估结果与迭代

评估基础设施：`evaluation/` 下 Inspect 桥接 HarnessBench，任务结果按 commit 归档（`results/harnessbench-*`），报告统一口径重算（移除多模态任务后 104 任务）。

**104 任务总览（2026-08-12 报告口径）**

| 指标 | 原始基线 | XBot old | 稳定点 | OpenCode 对照 | M3 269aa3 | DeepSeek 269aa3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 平均分 | 0.7868 | **0.8591** | 0.8208 | 0.8374 | 0.8382 | 0.8445 |
| 中位数 | 0.8268 | **0.9101** | 0.8462 | 0.8687 | 0.8834 | 0.9052 |
| 满分任务 | 23 | **35** | 33 | 27 | 30 | 37 |
| ≥0.8 | 56 | **73** | 58 | 65 | 67 | 68 |
| ACP 工具失败率 | 14.78% | 5.79% | 3.02% | **0.65%** | 2.48% | 2.37% |
| 每任务 token | ~210k | ~306k | ~308k | ~183k | ~238k | **~146k** |
| 每任务模型调用 | 14.02 | 14.97 | 14.57 | **13.29** | 16.14 | **9.75** |

**迭代轨迹**：基线 0.787 → XBot old 0.859（历史最高）→ 稳定点回落 0.821 → 269aa3 双 Provider 回升。269aa3 相对稳定点配对均值差 **M3 +0.0173**、**DeepSeek +0.0236**（后者 95% 区间几乎不覆盖负值，方向最强的一次）；相对 OpenCode 对照，DeepSeek 平均分 0.8445 > 0.8374，且每任务成本与模型调用数更优。评估驱动修复的任务包括 `078`(+0.52)、`084`(+0.43)、`073`(+0.48)、`091`(+0.36)、`020`(+0.29) 等（每任务变化以表格归档在报告中）。

**局限（报告如实声明）**：两个新 run 为单次完整运行，非严格受控基准；多模态任务已从任务集移除；`008`/`013` 在新 run 中归档轨迹缺失、评分仅基于工作区产物。这些都作为已知证据边界记录，不掩盖。

## 收获（求职者立场）

- **全栈系统设计能力**：从协议（SSE/类型化 DTO/API 版本化）、核心运行时（引擎/上下文/Hook）、工具与权限体系、插件框架，到 TUI/Web 客户端与评估基建，独立完成一个完整 C/S Agent 系统的架构与实现。
- **架构判断力**：能说清"什么时候该拒绝一个抽象"——mailbox 演进、统一 filesystem、移除 LangChain、收敛双任务机制，每一次都是先做语义分析再动结构，而不是堆框架；输入通道、通知注入、turn 唤醒这类边界问题优先用简单线性模型而非状态机解决。
- **Agent 设计准则**：项目反复验证了一条原则——**避免让模型注意力涣散的设计**。细粒度的工具返回（逐项 todo 操作）会诱导模型沉浸在状态维护里；在上下文中混入易变信息（文件指纹、动态状态）会让模型把噪声当特征。工具面应提供整单原子、语义清晰的操作，上下文只保留稳定、语义化的信息，字节级细节通过按需读取（相对路径引用）触达，而不是注入。
- **数据驱动的工程习惯**：建立评估闭环（HarnessBench + Inspect），每次重构用配对对比与 95% 区间验证收益方向，避免"测试全绿就宣布完成"；区分"分数提升"与"真正修复"（逐任务审计回归与修复）。
- **大规模重构执行能力**：跨 100+ 文件的重构（JobRegistry 统一、mailbox 移除、prompt 瘦身）配合 798 项测试保障，能控制回归并逐任务修复评估暴露的缺陷。
- **工程纪律与自省**：沉淀了"每次提交前自问四问"（是否不必要抽象/是否加文档/是否过测试/测试是否有效）的习惯，并把文档、测试、类型视为实现的一部分。
- **AI 辅助工程协作方法论**：熟练用长会话 + 上下文压缩维持长周期项目记忆，用第三方评审（Minimax）作线索而非基准，保持独立判断。

## 未来计划

### 1. 记忆模块

把已预留的 `data/memory/MEMORY.md`（`RuntimePaths.memory_dir/memory_file`）实现为分层记忆：会话级摘要由现有 Compaction 提供基础，长期事实/偏好写入持久记忆，并保持"记忆内容 = 数据而非指令"的既有注入边界。
**进展与来源**：Codex CLI 的 `~/.codex/memories/` + `AGENTS.md` 指令链（本项目开发会话已验证其长周期记忆效果）；MemGPT/Letta 的分层记忆与逐出；Anthropic memory tool 模式。现有 `invoke_model` 摘要能力可直接复用于记忆写入与召回。

### 2. 多 Agent 协作

现有 subagent 已提供基础（`spawn_subagent`/`wait_subagent`/`read_subagent`/`cancel_subagent`，`threads.jsonl` 父子生命周期）；规划升级为团队协作：任务委派、Agent 间消息传递、上下文 fork、控制权交接。
**进展与来源**：Anthropic 多 Agent 研究系统（orchestrator-worker 架构）；OpenAI Agents SDK / Swarm 的 handoff 语义；Codex 多 Agent 协作模式——本项目自己的 Codex 开发会话即以 `/root` 主 Agent + `spawn_agent`/`send_message`/`followup_task` 实际跑通了该工作流。

### 3. RAG 与上下文工程

复用现有 `filesystem_*`、`content_read` 与上下文外部化（模型可见相对路径引用）作为检索基础；规划引入 embedding + 向量检索做仓库/文档级知识召回，同时保持"检索结果 = 数据而非指令"的边界，并沿用确定性前缀以稳定 provider cache。
**进展与来源**：Agentic RAG；Anthropic contextual retrieval；上下文工程与 prompt caching（本项目已在评估中把完整输入 cache-read 比例做到 91–92%）。

### 4. 自进化

以现有 HarnessBench 评估闭环为引擎（104 任务、配对对比与 95% 区间、evaluated commit 追踪），规划基于评估反馈的自动改进：失败任务归因 → 自动生成回归用例 → 提示词/工具描述自动调优；长线探索自我奖励/自我改进式训练。
**进展与来源**：Self-Rewarding LMs 等自我奖励方向；agent self-improvement 研究；本项目已落地的"评估 → 修复 → 复评"循环（如 269aa3 相对稳定点 +0.0173/+0.0236 的迭代证据）是它的工程前提。

### 5. ICQ gateway

把会话接入 ICQ 等即时消息平台：网关桥接消息通道 ↔ 现有 HTTP/SSE 协议与交互端点（`permission_request`/`user_input_required`），使远端消息用户能驱动 Agent 并获得授权/提问回调。当前仓库尚无相关代码，属全新规划。
**进展与来源**：Telegram/WhatsApp 类消息机器人网关模式（消息事件 → 会话 → 结构化回复），以及 MCP 对消息平台桥接的通用做法；网关只做协议翻译、不进入核心执行路径。
