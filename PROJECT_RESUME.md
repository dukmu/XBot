# XBotv2 项目简历

## 一句话总结

XBotv2 是一个从零重写的通用 C/S 架构 AI Agent 运行时：以 XCore 插件运行时为底座，`agentloop` 只保留「调用模型 → 运行工具 → 重复」的 ReAct 核心，`core` 只放数据契约；权限、沙箱、持久化、用量、压缩、技能、MCP、工作区扩展全部是监听事件、注入服务的插件。配类型化 HTTP/SSE v3 协议、四合一文件系统工具、声明式插件树（`xcore.yaml`），并由 HarnessBench 104 任务评估闭环持续驱动迭代。

## 30 秒总结

前身是基于 LangGraph 的 Hermes 原型，控制流复杂、依赖过重。我主导从零重写，目标"通用、可读、稳定"，并在此后完成了一轮彻底的插件化重构。系统按边界组织：Clients（Textual TUI / Web / ACP / terminal）→ Protocol（HTTP/SSE/UDS，API v3 session/thread 资源模型）→ Application（启动组装、agents 服务）→ agentloop（Engine + ToolsService）→ core（纯数据契约）；其余能力全部是插件，插件只依赖 `XBotv2.core` 稳定面与注入服务，互不 import（架构检查脚本强制）。两条主线贯穿全程：一是把输入语义讲清楚——用户消息忙时折入当前 turn、后台完成只注入上下文、Goal 延续是唯一主动唤醒（为此移除了整个 mailbox 体系）；二是以评估驱动开发——HarnessBench 104 任务横向对比，将系统从基线 0.787 推到 0.845（DeepSeek 269aa3 平均分 0.8445、37 个满分任务），并验证了每任务成本降到 M3 的六成。最终 XBotv2 739 项 + XCore 105 项测试全绿、minimax 真实冒烟可用。

## 技术栈与关键数据

| 维度 | 内容 |
|---|---|
| 语言/运行 | Python 3.12，`uv` workspace，UTF-8 全链路 |
| 插件运行时 | XCore：`xcore.yaml` 声明式插件树 + 全局/工作区 overlay，事件 + 服务注入 |
| 核心 | `agentloop` Engine（调用模型→运行工具→重复）+ ToolsService；`core` 纯数据契约 |
| 协议 | HTTP + SSE（typed DTO）、Unix Domain Socket、OpenAPI v3、wire protocol v3 |
| 客户端 | Textual TUI、TypeScript Web（UDS 代理）、terminal、ACP v1 |
| LLM | MiniMax（默认）/ OpenAI / Anthropic / LM Studio 兼容适配器 + Mock |
| 工具 | `read`/`edit`/`path`/`search` 四合一 + shell（前台/后台）+ 子代理 + 技能 + MCP |
| 评估 | Inspect 桥接 HarnessBench，104 任务对比，结果按 commit 归档 |
| 测试 | XBotv2 739 项收集（最近全量 734 通过）+ XCore 105 项 |

## 架构与设计

```
Clients(TUI/Web/ACP/terminal) → Protocol(HTTP/SSE/UDS · API v3)
  → Application(boot · agents 服务 · 只组装不构造)
  → agentloop(Engine: 调用模型→运行工具→重复 · ToolsService+guards)
      core(纯数据契约: messages/tools/events/agents/loop/runtime)
  → Plugins(permissions/sandbox/usage/persistence/compact/skills/mcp/
            agents/goal/todolist/browser/workspace_instructions …)
      插件间只通过事件与注入服务通信，互不 import
```

核心设计原则：

- **稳定公共 API 是契约不是实现**：插件从 `XBotv2.core` 导入契约，经 `ctx.*` 服务（`ctx.tools`/`ctx.agents`/`ctx.permissions`/`ctx.approval`/`ctx.state_store`…）使用运行时能力；符号清单由 `test_public_api.py` 从 `api_inventory.md` 校验，插件永不触达 agentloop/application 内部。
- **loop 只做三件事**：调用模型、运行工具、重复。权限、沙箱、持久化、用量、压缩、交互、inbox 等一切超出该范围的能力都监听事件（`SESSION_INIT`/`AGENT_CONFIGURED`/`BEFORE_TOOL_CALL`/`AFTER_MODEL_RESPONSE`/`TURN_END`/`AFTER_CONTEXT_COMPONENTS_BUILD`…）完成。
- **插件间零混杂**：不通过 `ctx.get` 绕过、不在插件内导入其他插件实现；`scripts/check_architecture.py` 在每次重构时强制这些边界。
- **工作区归 `workspace_instructions`**：AGENTS.md 每请求注入、`.agents/*.md` 发现（以 overlay 覆盖 builtin/data-root）、`.xbot/plugins.yaml` overlay 都是这一个插件的事。
- **权限与沙箱解耦**：permission 是唯一批准通道（allow/ask/deny + 参数/路径正则）；sandbox 是强制兜底（Bubblewrap，无 ask、fail-closed），两者不合并。
- **可读性优先**：拒绝通用 DSL、数值优先级框架、包装执行器。

## 插件体系详细设计

插件化重构的验收标准（原话）：*"超出「调用模型、运行工具、重复」的所有内容，都属于监听事件分类体系的插件；永远不应该通过 ctx.get 绕过这一限制，重新引入新耦合"*。

**事件契约**：`core/events.py` 的 `Events` 枚举 + `ctx.on` 注册，分 observer（全部运行）、transform（返回阶段专属载荷）、guard（短路/否决，first-non-None）三类语义；事件载荷只携带契约字段，不暴露应用服务容器。

**服务注入**：每个能力由插件注册为 ctx 服务（`ctx.agents` registry + create seam、`ctx.tools` register + guard、`ctx.permissions`、`ctx.approval`、`ctx.state_store`、`ctx.usage`、`ctx.settings`、`ctx.llm`/`ctx.model`、`ctx.interactions`、`ctx.storage`），消费方在 `inject` 声明依赖，插件卸载时一切 effect 自动回滚。

**工具管线**：`agentloop/tool_service.py` 只做 register/guard/validate/dispatch；guard 自持依赖（权限 guard、沙箱 guard 各自注册），执行器不 lookup 权限/沙箱/批准/job 服务，工具 owner 在注册时捕获自己的注入依赖。

**启动链**：`application.start → boot_application → ctx.agents.create(AgentCreateOptions) → agents 服务（registry + set_factory + resolve config/provider/definition）→ agentloop.factory（只构造 Engine）`；子代理走同一个 create seam 的 child application，不另起一套构造。

**职责解耦**：`LoopState` 由 session 插件创建，persistence 只 hydrate/observe；usage 自持 `usage.yaml`，不依赖 state_store；工作区一切扩展归 `workspace_instructions`；agent 定义里的 `permission` 值原始透传，由 permissions 插件在应用时归一化/校验。

## Goal 与 TodoList 插件实现

两者是"目标 vs 步骤"的分层：Goal 持有持久目标，TodoList 跟踪具体工作项，互不代庖。

**GoalPlugin（`XBotv2/goal`）——外置状态机 + 自动延续**

- 数据模型：session 状态命名空间（`state.namespace("goal")`）中至多一条 Goal 记录 `{objective, status(active/paused/complete/blocked), summary, token_budget}`，转移即时持久化，resume 把 terminal/paused 转回 active。
- 模型面三个工具：`create_goal` / `get_goal` / `update_goal`（`Literal + Tool.from_function`，无手写 schema）。
- 人机面 `/goal` 命令：复用同一私有状态转移，不构造 ToolCall、不进权限、不追加模型历史。
- **自动延续机制（核心）**：每次成功转移（set/resume）后置 `_continuation_pending` 标记并预约一次新 turn；`TURN_START` 发现 `ctx.continuation` 时清标记并为该 turn 构建一份非持久化 Goal 快照；`TURN_END` 在 Goal 仍 active 时最多再预约一次。运行时通知（后台任务/子代理完成）不驱动该状态机。
- 权限：工具注册时由权限 guard 统一把关，不写硬编码绕过。
- 完成/阻塞保留 Goal 与 summary 供人工审查，`clear` 才删除；不允许模型仅凭"过程复杂/用了 Todo"就创建 Goal，`complete` 要求逐项结果与验证。

**TodolistPlugin（`XBotv2/todolist`）——单工具原子替换**

- 模型面只有 `update_todos`：整体替换完整清单 `[{content, status}]`，**不做 per-item 的增删改查工具**（避免状态碎片化）。
- 校验：每项恰好 `content`+一个合法状态；非空未完成清单必须恰好一个 `in_progress`；`todos: []` 清空。整单先验证再一次持久化，非法输入不可能部分生效。
- 幂等：重复当前清单是 no-op，结果明确提示"先干活再调用"；全部完成后结果带 `todos`+`cleared` 结构化数据，并清空 active 清单。
- 结果留在正常对话路径（下个模型调用可见），不注入 system 消息、不改写 provider 上下文；变更时一次状态命名空间写，resume 看到同一清单；unload 移除工具但保留会话数据。

**验证**：Goal/Todo 语义重写为可观察行为断言（`test_goal.py`、`test_todolist.py`），配合 `/goal` 命令实测（create→auto continuation→pause→resume→complete 全链路）；Goal 延续在评估任务中验证不会因后台通知反复唤醒。

## 关键技术决策与演进

1. **废弃 LangGraph/Hermes 原型 → 从零重写**：控制流与依赖过重，重写换取可读性与所有权清晰。
2. **移除整个 mailbox 体系 → pending-fold + AgentInbox + request_continuation**：mailbox 把用户消息、后台事件、goal 事件混在一个队列，语义混乱。改为三通道分离（见架构节），同时删掉对应的 mailbox 事件与能力。
3. **API v2 → v3 session/thread 资源模型**：全局命令路径改为版本化资源；协议持有 typed DTO，TUI/Web/SDK 共用同一契约。
4. **统一文件系统语义**：host 与 bwrap 两套重复实现收敛为 `filesystem/`（ThreadStorage + 统一工具语义），12 个细粒度工具合并为 `read`/`edit`/`path`/`search` 四个，mode/operation 显式映射到具体文件系统操作与权限路径；`content_read` 并入 `read` 的 media 模式（目前支持图片）。
5. **Prompt 瘦身与结构化**：合并重复默认指令、结构化内部 runtime 消息、删除死配置 `system_template`、fenced `var` block 做变量展开。
6. **非交互 fail-closed**：once 模式过滤 `{ask_user, request_permission}`，权限 ask 直接拒绝，杜绝无限等待。
7. **输入状态机与协议解耦**：SSE 断连不再等于 session 销毁；resume 从持久历史重建运行时，交互请求显式声明不可恢复。
8. **彻底插件化重构（本阶段主线）**：把 loop 里的 compact/持久化/交互/inbox/ask/usage 全部移出为插件监听事件；tools/ 移入 agentloop 作为工具服务；持久化与 usage 解耦；工作区 agents 发现归 `workspace_instructions`；移除 agents 内的 permission 策略解析（原始透传，权限插件归一化）。每一步都以"loop 只做三件事"为验收标准，并配 `scripts/check_architecture.py` 强制边界。

## 遇到的问题：分析 · 应对 · 结果

### 1. 运行时输入、通知与 turn 的边界

**问题**：忙时用户输入、排队消息与后台完成通知，与运行中/未来的 turn 之间边界不清——消息容易重复投递或丢失 turn 边界；后台完成一旦"隐式唤起 turn"就会反复产生新 turn，或需要一套复杂状态机来区分来源。
**分析**：单队列同时承载用户输入、后台通知与 goal 事件，"应该注入内容"和"应该启动 turn"被混为一谈；折入的消息没有明确的 turn 归属，客户端渲染与事件流不同步。
**应对**：把输入边界收敛为三条简单通道，不做来源判断状态机——`pending-fold` 在下一个工具批边界整体折入（按序发布 `message` 事件、末位流独占合并回复、无工具边界的残留以 `input_rejected` 拒绝并让客户端重试）；`AgentInbox` 只入队，在下个 turn 的上下文组装时一次性以运行时事件注入，**从不唤醒 turn**；goal 延续走显式 continuation，成为唯一主动唤醒。
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
**应对**：统一到 `XBotv2/jobs` 的 JobRegistry 单一实体，两者共用完整生命周期，工具层只按 kind 适配；wait/read/cancel 各自有明确边界与输出上限。
**结果**：JobRegistry 重构所在提交在评估中相对稳定点配对 +0.0173/+0.0236，工具失败率降到 2.37%。

### 5. 沙箱工具的保护边界与实现冗余

**问题**：文件系统操作若在宿主进程内直接执行，会绕过沙箱保护；而与 shell 各维护一套执行代码，则保护边界与语义都会漂移。
**分析**：保护边界应该由统一的沙箱运行时给出，而不是每个工具自己实现"安全"。
**应对**：文件系统工具经 Bubblewrap 沙箱后端执行，与 shell 共用同一套挂载策略；权限与沙箱彻底解耦——permission 负责批准（allow/ask/deny），sandbox 只做强制（fail-closed），不发起批准、不加临时规则。
**结果**：单一保护边界、代码冗余消除，host/bwrap 契约一致性由测试覆盖。

### 6. 交互能力的运行时边界

**问题**：once 等非交互模式若仍安装交互 sink 而客户端从不回应，会无限等待。
**分析**：交互性不是工具属性，而是运行时模式；在工具执行里堆协议条件会导致每个工具都要考虑交互。
**应对**：`interactive` 标记贯穿运行时组装，非交互下过滤 `{ask_user, request_permission}`，权限 ask 直接 fail-closed。
**结果**：once 模式不挂起；交互能力的边界由运行时声明，而不是散落在工具逻辑里。

### 7. 多 Provider 的语义归一化

**问题**：不同 LLM Provider 的消息块结构、thinking、工具调用分段与用量统计口径各异，直接映射会破坏互操作与成本统计。
**分析**：核心不能为每个 Provider 写一套分支；差异必须收敛在单一归一化契约之后。
**应对**：统一 provider-neutral `Message`/`ModelResponse` 契约，适配器内完成块列表转换、thinking 元数据保留、用量口径对齐，重试只在 Provider 实现内发生。
**结果**：OpenAI / Anthropic / LM Studio / MiniMax 多 Provider 实测可用；评估桥修正后双 Provider 全量运行（269aa3 的 M3 与 DeepSeek 各 106 任务）。

### 8. 插件化重构中的边界反复

**问题**：重构早期反复出现"表面插件化"——包归了类但逻辑仍硬编码在 loop/工具里，或插件之间互相 import/通过 ctx.get 绕过注入，例如 loop 依赖 llm/persistence、持久化与 usage 耦合、工作区 agents 发现塞在 agents 插件里、agents 内部解析 permission 策略。
**分析**：职责归属不清时，事件/服务只是装饰；真正的边界要由"谁拥有什么"的定义 + 机器检查共同保证。
**应对**：以 DSH 为参照定死职责——loop 只做「调用模型、运行工具、重复」，工作区一切归 `workspace_instructions`，权限策略归 permissions 插件；写 `scripts/check_architecture.py` 强制 import/服务注入边界，每次重构跑它并及时纠正自己。
**结果**：XBotv2 739 项测试全绿，真实 minimax 冒烟跑通（可见工具逐一实际调用，6 个被权限拦下均为预期策略），架构检查零违规。

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

- **全栈系统设计能力**：从协议（SSE/类型化 DTO/API 版本化）、核心运行时（agentloop/事件/工具服务）、权限与沙箱体系、插件框架（XCore + 声明式插件树），到 TUI/Web/ACP 客户端与评估基建，独立完成一个完整 C/S Agent 系统的架构与实现。
- **架构判断力**：能说清"什么时候该拒绝一个抽象、一个能力该归谁"——mailbox 演进、统一 filesystem、移除 LangChain、收敛双任务机制、把工作区归一个插件、把权限策略归权限插件，每一次都是先做语义分析再动结构，而不是堆框架；输入通道、通知注入、turn 唤醒这类边界问题优先用简单线性模型而非状态机解决。
- **Agent 设计准则**：项目反复验证了一条原则——**避免让模型注意力涣散的设计**。细粒度的工具返回（逐项 todo 操作）会诱导模型沉浸在状态维护里；在上下文中混入易变信息（文件指纹、动态状态）会让模型把噪声当特征。工具面应提供整单原子、语义清晰的操作，上下文只保留稳定、语义化的信息，字节级细节通过按需读取（相对路径引用）触达，而不是注入。
- **数据驱动的工程习惯**：建立评估闭环（HarnessBench + Inspect），每次重构用配对对比与 95% 区间验证收益方向，避免"测试全绿就宣布完成"；区分"分数提升"与"真正修复"（逐任务审计回归与修复）。
- **大规模重构执行能力**：跨 100+ 文件的插件化重构（tools→agentloop 服务、loop 瘦身、persistence/usage 解耦、工作区归属、权限解耦）配合 800+ 项测试（XBotv2 739 + XCore 105）保障，能控制回归并逐任务修复评估暴露的缺陷。
- **工程纪律与自省**：沉淀了"每次提交前自问四问"（是否不必要抽象/是否加文档/是否过测试/测试是否有效）的习惯，并把文档、测试、类型、架构检查脚本视为实现的一部分。
- **AI 辅助工程协作方法论**：熟练用长会话 + 上下文压缩维持长周期项目记忆，用第三方评审（Minimax）作线索而非基准，保持独立判断。

## 未来计划

### 1. 记忆模块

把已预留的 `data/memory/MEMORY.md`（`RuntimePaths.memory_dir/memory_file`）实现为分层记忆：会话级摘要由现有 Compaction 提供基础，长期事实/偏好写入持久记忆，并保持"记忆内容 = 数据而非指令"的既有注入边界。
**进展与来源**：Codex CLI 的 `~/.codex/memories/` + `AGENTS.md` 指令链（本项目开发会话已验证其长周期记忆效果）；MemGPT/Letta 的分层记忆与逐出；Anthropic memory tool 模式。现有模型请求摘要能力可直接复用于记忆写入与召回。

### 2. 多 Agent 协作

现有 subagent 已提供基础（`spawn_subagent`/`wait_subagent`/`read_subagent`/`cancel_subagent`，父子线程生命周期）；规划升级为团队协作：任务委派、Agent 间消息传递、上下文 fork、控制权交接。
**进展与来源**：Anthropic 多 Agent 研究系统（orchestrator-worker 架构）；OpenAI Agents SDK / Swarm 的 handoff 语义；Codex 多 Agent 协作模式——本项目自己的 Codex 开发会话即以主 Agent + 子 Agent 任务实际跑通了该工作流。

### 3. RAG 与上下文工程

复用现有 `read`/`search`（含 media 模式）、上下文外部化（模型可见相对路径引用）作为检索基础；规划引入 embedding + 向量检索做仓库/文档级知识召回，同时保持"检索结果 = 数据而非指令"的边界，并沿用确定性前缀以稳定 provider cache。
**进展与来源**：Agentic RAG；Anthropic contextual retrieval；上下文工程与 prompt caching（本项目已在评估中把完整输入 cache-read 比例做到 91–92%）。

### 4. 自进化

以现有 HarnessBench 评估闭环为引擎（104 任务、配对对比与 95% 区间、evaluated commit 追踪），规划基于评估反馈的自动改进：失败任务归因 → 自动生成回归用例 → 提示词/工具描述自动调优；长线探索自我奖励/自我改进式训练。
**进展与来源**：Self-Rewarding LMs 等自我奖励方向；agent self-improvement 研究；本项目已落地的"评估 → 修复 → 复评"循环（如 269aa3 相对稳定点 +0.0173/+0.0236 的迭代证据）是它的工程前提。

### 5. ICQ gateway

把会话接入 ICQ 等即时消息平台：网关桥接消息通道 ↔ 现有 HTTP/SSE 协议与交互端点（`permission_request`/`user_input_required`），使远端消息用户能驱动 Agent 并获得授权/提问回调。当前仓库尚无相关代码，属全新规划。
**进展与来源**：Telegram/WhatsApp 类消息机器人网关模式（消息事件 → 会话 → 结构化回复），以及 MCP 对消息平台桥接的通用做法；网关只做协议翻译、不进入核心执行路径。
