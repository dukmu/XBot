# XBotv2 所有权/生命周期架构审计报告

审计日期：2026-09-15（审计只读）；修复日期：2026-09-15（下方"修复状态"为修复后补记）
审计对象：`/home/shefrin/repo/XBot/XBotv2/`
方法：三个并行 subagent 按插件分组做数据流追溯（谁构造 → 谁持有 → 谁写 → 谁读 → 何时），主代理做跨切面扫描。判据为 cordis/XCore 机制（typed service 构造即注册、fiber 生命周期、总线事件、依赖门保证时序）。**主代理已对全部"最高严重度"项与各组的头条发现逐条复核代码路径；凡未亲自复核的项均标注。**

---

## 修复状态（全部发现已逐项处理）

修复原则：不加检查脚本护栏；逐项改正源码；修复使用 cordis/XCore 惯用法（typed service、总线事件、fiber 生命周期、单一属主、响亮失败）。每项均补了针对性回归测试。

### 核心运行时（分区 2）
- **F1 ✓** 删除 `LoopState.inbox_items`；`/status` 改为向 engine 取权威 `pending_input_count`（`build_session_commands(..., pending_input_count=...)`、`Session.status(*, pending_input_count)`）。回归：`test_status_command_asks_the_engine_for_pending_count`。
- **F2 ✓** `turn_count` 单一属主：`LoopState.turn_count` 变属性（同步 session 镜像），history 变更不再重算；hydrate 从 `conversation_stats(...).turns`（压缩已折叠 SessionStats）显式恢复。回归：`test_turn_count_survives_compaction`。
- **F3 ✓** `Engine.inbox` 改必填构造参数，删除 `inbox or AgentInbox(...)` 静默回退与 `LoopState.inbox_sink`；持久/瞬态 inbox 只由 `agent_inbox` 服务提供，直接构造点显式传入。
- **F4 ✓** loader 对嵌套 inject 句柄输出 `plugin.activation.deferred`（state + missing_dependencies）遥测，使"被阻塞"与"不需要"可区分（顶层仍响亮失败）。
- **F5 ✓** 短路事件的独立观察者改 `prepend=True`（caption、content_cache），并在 `Engine._dispatch` 文档化显式优先级契约。回归：`test_observer_listener_runs_even_when_a_short_circuit_listener_answers`。
- **F6 ✓** `ToolsService.register/guard` 在 apply 外未声明 `cleanup="caller"` 时回滚并抛错；新增组合级 `ownership` 供测试 harness/嵌入方声明；skills/MCP 等 listener 注册方显式声明 caller 清理。
- **F7 ◐** 删除冗余 `inject` 读取与 `session_launch` 声明；`session` 的 `artifacts` 声明经实验证实承载挂载作用域（移除会导致同作用域重复注册 `workspace_root`），已恢复并注明原因。

### 能力插件（分区 3）
- **F1 ✓** `cancel()` 仅在无 live task 时短路 PENDING；排队任务走 task 取消，`_execute` 在信号量后重查 terminal，`_finish` 幂等；`stop_all` 批量同步取消。回归：`test_cancel_queued_job_never_starts_and_completes_once`、`test_shutdown_cancels_queued_job_before_it_starts`。
- **F2 ✓** `attach_session` 不再校验目录存在（目录校验只属显式 `create`/`ensure`）。回归：`test_attach_session_never_requires_the_directory_to_exist`。
- **F3 ✓** `allowed-tools` 成为真实限缩：清单生效期间清单外工具被拒，skills 自身注册的工具豁免。回归：`test_active_skill_allowlist_restricts_other_tools`。
- **F4 ✓** caption 仅在成功应用标题后置位；去掉 `getattr(ctx.session, "turn_count")` 探测。回归：`test_transient_provider_failure_retries_caption_on_next_turn`。
- **F5 ✓** 压缩先提交权威 surface、后写元数据 marker（不再有幽灵 marker）；实况 `compaction_completed.summary` 与回放共用 `compaction_summary_text`。
- **F6 ✓** JobRegistry 改 `TaskEventPort` 发布 `task/updated`、`task/completed`；插件用 `ctx.on`（fiber 持有）订阅，shutdown 期间抑制发布。
- **F7 ✓** 删除只增不读的 `SubagentLauncher._active`。
- **F8 ✓** `ContentCacheService` 改单槽缓存并在 TURN_END 清空。
- **SMELL ✓** `goal.token_budget` 接入 `UsageService`：达预算即停止调度续跑并记 goal 为 blocked。回归：`test_goal_token_budget_stops_continuation_scheduling`。

### 传输与集成（分区 4）
- **F1 ✓（安全）** 提权改声明式：`Tool.escapes_sandbox` 由属主声明；shell 工具在无审批层时**拒绝**（fail closed），管线在无 approval 守卫时于 dispatch 前拒绝；sandbox 守卫不再按工具名字符串放行；帮助文本更正。回归：`test_pipeline_denies_escalation_without_approval_guard` 等。
- **F2 ✓** MCP `_dispose` 调 `_rollback_all()`（卸载即回滚注册）。
- **F3 ✓** `PromptsService.add` 无属主 fiber 时响亮失败；子代理目录改为每次构建经 `CONTEXT_COMPONENTS_BUILT` 贡献。回归：`test_prompt_fragment_registration_outside_apply_fails_loudly`、`test_subagent_catalog_is_contributed_per_build`。
- **F4 ✓** `SandboxPort` 补齐 `resolve_read_path`/`check_filesystem_access`，browser 不再越协议。
- **F5 ✓** `Tool.kind` 由属主声明，引擎在 `tool_calls_started` 携带（新增 `ToolCallStartedItem` 线格式），ACP 直接消费、回放不再自造分类，删除虚构条目。
- **F6 ✓** MCP sampling 改用 `llm.invoke_llm`，删除重复合并循环与函数内导入。
- **F7 ✓** 授权范围改由属主 `Tool.grant_selectors` 声明；permissions 从已注册工具读取，未声明时保守约束全部标量参数。回归：`test_grant_scope_follows_owner_declaration_not_a_private_copy`。
- **F8 ✓** provider 畸形 tool-call 参数改抛 `ToolArgumentsError`，不再伪造 `{}`。回归：`test_malformed_tool_call_arguments_fail_loudly`。
- **F9 ✓** `/provider` 保留 no-arg 交互选择器（用户要求）；`list`/`status` 一律转发服务端（删除本地自绘列表与 `ls` 别名）。回归：`test_provider_command_keeps_local_picker_and_remote_view`。
- **F10 ✓** `on_unmount` 取消 stream timer 与线程视图泵；渲染路径改容忍式查询。
- **F11 ✓** `ConfigService.user_context()` 实时解析树（删除构造期捕获参数）；session scope 的 `applies_to` 假承诺去掉。回归：`test_user_context_follows_session_overlay_writes`。
- **F12 ✓** 删除零生产者的 `hello_ok`/`session_ready`/`status`/`shutdown_ok` 分支；`tool_started` 对齐真实 `tool_calls_started` 并渲染工具起始行。
- **S13 ✓** ACP 转发器 finally 释放活动 prompt。回归：`test_prompt_is_released_when_the_session_stream_ends`。
- **S14 ✓** `SandboxPolicy.replace_config` 走单一 `_load_config` + 后端重建。
- **L15 ✓** 浏览器网络闸门在无 sandbox policy 时 fail closed。回归：`test_browser_network_fails_closed_without_sandbox_policy`。
- **L16/L17/L18/L19/L22/L23/L24/L25/L26/L27/L28/L29/L30 ✓**：关闭失败改为记录；删除无订阅者的 `client/event`；LLM 选择错误类型化 `code`；跨包私有 helper 转公开；policy 条目缺失/禁用可诊断；复用 `split_command_args`；重复答案告警；policy 路由定位 main thread；runtime_input 形状归 `core.messages` 共用；ACP 窗口随 `agent_configured` 刷新；无属主 fragment 渲染报错、fragment API 对称。
- **L20/L21 — 审计已判可接受**（TUI 渲染缓存有界提升性；`trace_event` 属诊断通道），本次不改。

### 跨切面（分区 1）
- **CC1 ◐**：`config/service.py`、`interactions/plugin.py` 的绕路函数内导入上移到顶层（无环）；`mcp_plugin/callbacks.py` 的能力错位随 F6 消除；`application/child.py` 保留（模块边界纠缠，改动风险大于收益）。

### 第二轮：sink / 动态改写专项（按 "sink" 关键字全量枚举后逐条判定）

判据：**构造期注入的写穿端口合规**；**构造后被外部改写的字段/回调 = 违规**。

合规保留（构造期绑定，仅此一处写入）：
- `core/history.py` 的 `HistorySink`（`ConversationHistory(sink=persistence.history)`）与 `AgentInbox(sink=persistence.inbox)`：写穿端口在构造时注入，生命周期内不再重绑。

已修复：
- **`InboxSink` 协议重复定义**（`agentloop/contracts.py` 与 `agentloop/inbox.py` 各一份）→ 由 contracts 单一拥有，inbox 改为导入。
- **`ClientEventRouter.set_sink(sink) -> previous`（可变 sink 交换 API）** → 改为 `install(sink) -> disposer`：安装者持有幂等恢复函数，共享路由不会再被"手工 previous 保存/恢复"改写；`ClientEventsPort` 契约同步，`session/runtime.py` 改用 disposer。
- **`ACPEventMapper.set_context_size`（本轮之前我引入的外部改写）** → 删除 setter，改为 `updates(event, fallback_context_size=...)` 参数；转发器只在本地变量跟踪窗口。
- **`BrowserSession._sandbox` 每次 `open()` 被写**（守卫路由在调用外读取该字段）→ sandbox 改为构造期绑定，`open()` 不再接收/存储 sandbox。
- **`LoopState` 被外部改写字段**（`resumed`、`turn_count`、`session.provider`）→ 收敛为类型化转移 `restore_resumed()`、`restore_turn_count()`、`set_provider()`；persistence/agents 改走这些转移。现在对 `SessionInfo` 的写入**只存在于 LoopState 内部**。
- **`CommandsService.register` 与 `AgentCatalog.register/register_markdown` 忽略 `bound_effect` 的 False**（与核心 F6 同族：apply 外注册即无主）→ 采用同一契约：回滚 + 响亮报错，除非调用方声明 `cleanup="caller"` 或组合期 `ownership="caller"`；skills 的 session-init 命令注册显式声明 caller 清理。
- **唤醒投递从回调 sink 改为总线事件**：删除 `AgentInbox/Engine.set_wake_driver` 与 `runtime.__post_init__` 的回调注入；`InboxSplice` 新增类型化 `wake: bool` 字段，由 inbox 在 splice 上声明唤醒意图，session runtime 用自己已有的 `INBOX_SPLICE` 监听决定是否唤醒（`inject` 仍为非唤醒）。回归：`test_aliases_share_two_fifo_targets_and_wakeup_semantics` 断言 `[False, True, True]`。

验证：Python 全量 **1012 passed**；受影响路径（skills/agents/permissions/http transport/session/inbox/acp/browser/tui）逐一复跑通过。

### 第三轮：真实运行暴露的回归（用户实测发现，已修复并复现验证）

这三项都是**测试全绿但真实运行会炸**的路径，来自本轮修复本身的失误，已按真实 HTTP server + 真实客户端复现并修复：

- **TUI 命令发现全部失效（`/status`、`/provider`、`/sandbox`、`/compact` 报 "not implemented"）**：L19 把 `TerminalSession` 的私有属性/属性方法 `_thread_path` 一并改名为 `thread_path`，但两处调用仍写 `self._thread_path` → `list_commands()` 抛 `AttributeError` → TUI 捕获后命令目录为空 → 所有服务端命令解析为 "unknown"。修复：调用点统一为 `self.thread_path`。**真实验证**：对真实 server 调用 TUI 客户端 `list_commands()` 返回 15 条命令（此前 0 条）。
- **caption 写标题会把整轮 turn 打挂**：我为标题实时刷新新增了 `session_updated` 线帧，但**未注册类型化线模型** → `session_event()` 里 `KeyError` → 从 `THREAD_METADATA_CHANGED` 的 emit 冒泡回 caption 的 `state.update(title=...)` → turn failed，标题也永远写不上。修复：**撤回自造帧**（用户确认 WebUI 标题行为本来就正确，既有机制是 `openSession(mode="resume")` 重读描述符），改为 TUI 在 `turn_finished` 时调用新增的 `refresh_descriptor()` 只更新身份字段（title/agent/provider/model/mode/context_window），与 WebUI 同一条协议路径。**真实验证**：真实 server 上 `connect` 标题为 session id；一个回合后 `refresh_descriptor()` 返回 `"Python GIL 讨论"`（scripted caption 标题）。回归测试：`test_turn_end_refresh_applies_the_captioned_title`。
- **服务端启动因工作区目录缺失而失败**：F2 把目录校验放进了 `ensure()`，而 `WorkspacesPlugin.apply` 在启动时调用 `registry.ensure(ctx.workspace_root)` → 全新/已删除的工作区目录会让 server 起不来。修复：目录校验只保留在显式 `create()`；`ensure()`（启动期身份登记）与 `attach_session()`（投影）都不校验。回归测试更新为断言 `ensure` 容忍、`create` 报错。

---

### 验证证据
- Python 全量：**1012 passed**（基线 991 + 21 个新增回归测试），零失败。
- Web vitest：**137 passed**（26 文件）。
- Web e2e（mock 协议，桌面+移动）：**71 passed / 1 skipped**。
- 提交状态：**未提交**（按约定等待指令）。

---

## 0. 判据（所有发现引用其一)

1. **单一构造点 + 构造即注册**：对象只在一处构造；构造动作本身即 `Service` 注册，随 fiber 卸载自动释放。
2. **构造期绑定，由依赖门保证**：A 需要 B 必须走构造参数 + `inject` 依赖；正确性不得依赖插件树行序（`xcore.yaml` 明确宣称"行序无语义"）。
3. **状态变更走总线**：写入方 `ctx.emit(类型化事件)`，订阅方 `ctx.on`（fiber 拥有生命周期）；私有 observer 列表、手工 disposer 字段、用 `ensure_future` 传递状态变更均为违规。
4. **身份永久，禁止重绑**。
5. **能力归属主**：工具函数放在其所操作对象的属主包，而非首个使用它的插件。
6. **在责任边界响亮失败**：不静默兜底、不吞异常、不用 `getattr`/`hasattr` 探测内部属性决定行为。

## 1. 跨切面扫描（主代理独立完成，未依赖 subagent）

### CC1. 函数内 import 全仓 23 处（已修复 1 处：`ctx_splice_recorder`）
| 位置 | 类别 |
|---|---|
| `config/service.py:64` load_plugin_tree | **疑似循环依赖绕路**：config.service ↔ config.loader 相互纠缠 |
| `application/child.py:37` start_application | **疑似循环依赖绕路**：application.child ↔ application.app |
| `server/plugin.py:150` create_app | 包内懒加载，可接受但属于绕路信号 |
| `coretools/plugin.py:52-54` | 包内懒加载，可接受 |
| `interactions/plugin.py:121` | 包内懒加载，可接受 |
| `llm/plugin.py:47-49` provider factories | 避免 import 时拉入 SDK，合理 |
| `mcp_plugin/callbacks.py:108` `_merge_response` 包装 `merge_model_chunk` | **能力错位**：一行包装 + 局部导入，应直接使用 `core.messages.merge_model_chunk` |
| `session/session.py:48` datetime、`core/tools.py:170` asyncio | stdlib 局部导入，无意义但无害 |
| `main.py`、`browser/`(playwright)、`loader/runtime.py`、`llm/openai.py`、`anthropic.py`、`tui/textual_widgets.py` | 重依赖/可选依赖懒加载，合理 |

结论：真正的"绕架构"局部导入不是 23 处，而是 **3 处**（config.service、application.child、mcp_plugin.callbacks）——它们反映的是模块边界纠缠而非导入本身。（审计期间修复了第 4 处同类：`agentloop/inbox.py` 的 `ctx_splice_recorder` 与 `session/contracts.py` 的 `_compaction_summary`，均改为模块顶层导入。）

### CC2. 模块级可变全局（跨会话共享、不受 fiber 生命周期管理）
| 位置 | 说明 | 判定 |
|---|---|---|
| `persistence/store.py:250` `_trajectory_states` + `_trajectory_guard` | 进程级 trajectory 解析缓存（LRU），带线程锁 | 有界缓存，**接受**（性能设计），但不在 fiber 生命周期内 |
| `core/filesystem/session_lock.py:61` `_owners` + `_guard` | 进程级会话所有权注册表 | 进程内互斥的固有位置，**接受** |
| `tui/textual_widgets.py:28,34` `_MARKDOWN_CACHE`/`_PLAIN_CACHE` | 渲染缓存 | **接受**，纯提升性 |

结论：无失控的模块级单例；三处都有界且有明确理由。

### CC3. 静默吞异常
全仓仅 `os.unlink` 竞态清理处的 `FileNotFoundError: pass`（原子写清理），合法。未发现 `except Exception: pass` 式兜底。

### CC4. 插件树行序独立性——实验证据（判据 2 的系统级验证）
静态阅读无法证明"行序无语义"。做了一次真实启动实验（`.audit/order_probe.py`，一次性脚本，审计后删除）：用同一棵真实插件树启动应用两次，第二次把 `entries` 整体倒序（最强的顺序扰动，反转全部两两关系）。

| 观测项 | 声明顺序 | 整体倒序 |
|---|---|---|
| 注册插件数 | 45 | 45 |
| `engine` / `agent_inbox` / `thread_metadata` / `loop_state` | 全部存在 | 全部存在 |
| `caption` / `compact` / `jobs` / `tools` | 全部存在 | 全部存在 |
| 一轮 turn 事件序列 | turn_started → delta → assistant_message → turn_finished | 完全相同 |
| assistant 回复 | `order probe reply` | `order probe reply` |
| caption 写入标题 | `order probe title` | `order probe title` |

**结论**：当前实现确实做到了"激活由服务可用性驱动"，不依赖 `xcore.yaml` 行序。这是对历史上"行序正确"隐患的直接否证。

### CC5. 监听器生命周期与 `Service` 使用面
- **67 个 `ctx.on`/`events.on` 监听，手工 `.off(` 注销 0 处** → 监听器全部由 fiber 持有，无手工生命周期管理。
- **10 处 `ctx.dispose(...)`/`ctx.effect(...)`**：逐一检查均为合法的 fiber 清理注册（browser/compact/acp/skills/mcp 的 `_dispose`、会话所有权释放、waiter 注销、`manager.close_all`）。
- `Service` 基类仅 2 处（`ThreadMetadataState`、`LoopState`），对应 46 处 `ctx.set`。**判定：不是违规**——插件在自己的 `apply` 内 `ctx.set` 提供能力服务本身就是 cordis 惯用法（注册即由该 fiber 持有、随 fiber 卸载释放）；`Service` 基类的价值在于"对象身份/生命周期需要构造即注册"的场景，正是这两个状态对象。此差异属风格选择，已记录为观察项而非缺陷。

---

## 2. 核心运行时与状态所有权（agentloop/session/persistence/core/application/loader/agents）

审计方式：全量阅读 7 个包 + XCore 框架全部 4 个核心模块；对跨包调用方做数据流追溯；跑 168 个相关测试确认"发现的问题未被测试覆盖"（而非测试已证伪）。**主代理已逐条复核下列 F1/F2/F3 的代码路径，确认成立。共 5 个 confirmed（其中 2 个低危）+ 2 个 SMELL。**

### F1 —— CONFIRMED（最高严重度）：`LoopState.inbox_items` 事后重绑，`/status` 发布陈旧快照
- 判据 1 / 4
- 位置：`agentloop/contracts.py:108`（构造写）→ `persistence/plugin.py:81`（hydrate 后写）→ `session/session.py:110` 读 → `session/commands.py:29` 用户可见（`/status` 的 `Queued inputs`）
- 权威值在别处：`agentloop/engine.py:441-442`（`pending_input_count` → `len(self.inbox)`）
- 失效路径：resume 一个 `inbox.json` 有 2 条的会话 → reconcile 2 条写入 `state.inbox_items` 并由 `agent_inbox` 承载 → `resume_pending_inputs()` 唤醒 → 两条被 claim/consume → **`engine.inbox` 已空，`state.inbox_items` 仍是 2** → `/status` 报 `Queued inputs: 2`。后续任何队列变化只更新 `agent_inbox`，该字段永久冻结在 hydrate 时刻；无持久化路径则恒为 0。
- 建议：删除该字段，`Session.status()` 向 runtime/engine 取 `pending_input_count`（单一属主）；若确需快照，必须由 `INBOX_SPLICE` 事件驱动（`ctx.on`，fiber 持有），禁止 hydrate 直接赋值。
- **主代理复核**：路径成立（`/status` 可达），确认属"重复权威 + 陈旧捕获"洞族——**即本应已被消除的那一族**。

### F2 —— CONFIRMED：`turn_count` 双属主、双定义，压缩后回退
- 判据 1 / 3
- 写入者 A：`engine.py:1551-1553` 每接受一个 turn 递增（**next-step steer 追加 user 消息但不递增**）
- 写入者 B：`contracts.py:141-143` `_update_turn_count()` 从当前可见表面重算，被 `set_history`/`replace_messages`/`replace_message_range`/`clear|undo|regenerate_history` 调用
- 触发分歧：`compact/service.py:392` 用 `replace_message_range(..., preserve_transcript=True)` 收缩表面 → turn_count 按压缩后的表面重算（如 5 → 1），而 `SessionStats.turns` 被刻意跨压缩保留（`core/timing.py:38-66`）
- 后果：`/status` 的 `History: N turns` 与压缩摘要提示词读取的 `session.turn_count` 互相矛盾；`caption/service.py:112` 甚至用 `getattr(ctx.session, "turn_count", None)` 探测——该探测的存在本身就是语义不清的症状（判据 6）。
- 建议：单一属主 + 单一定义；跨压缩像 `SessionStats` 那样保留计数器，另一处改为读取而非重算。

### F3 —— CONFIRMED（潜伏）：inbox 两个构造点 + engine 静默合成瞬态 inbox
- 判据 1 / 6
- `engine.py:239-243` 的 `inbox or AgentInbox(items=state.inbox_items, sink=state.inbox_sink, ...)` —— 在**构造期捕获**这两个由他人写入的字段（见 F1），且静默降级为非持久 inbox
- 另外两个构造点：`persistence/plugin.py:76`（持久）、`session/plugin.py:77-83`（瞬态）
- 生产路径目前被 `agents/plugin.py` 的 `agent_inbox` 必需依赖挡住——**但这是"恰好挡住"，不是结构性保证**：任何直接构造 `Engine`/`AgentLoopFactory` 的嵌入方（如 `tests/helpers.py:190`）会静默得到空 items / 无 sink 的 inbox，持久待处理输入被静默孤儿化，无任何报错。
- 建议：`inbox` 改为必填构造参数，删除 `or AgentInbox(...)` 回退与 `LoopState.inbox_items`/`inbox_sink`；单一构造点是 `agent_inbox` 服务（持久/瞬态二选一，均由依赖门保证）。

### F4 —— LOW：嵌套 inject fiber 只查 FAILED、不查"被阻塞"
- 判据 6；`loader/runtime.py:84-87` 的 nested 循环只重抛 FAILED，不检查 PENDING。拼错一个 inject 名会让某子系统静默缺失：能力类挂载（`mount_http`、subagents、skills…）永远不报错，核心类则在 `app.py:197` 才以 `AttributeError` 暴露。建议至少在启动遥测中让"被阻塞"与"不需要"可区分。

### F5 —— SMELL（未证实）：短路监听的先到先得与"行序无语义"声明冲突
- 判据 2；`engine.py:282-293` 对 `SHORT_CIRCUIT_EVENTS` 用 `serial`（首个非 None 胜出），监听顺序 = fiber 装载顺序 = 树序。`BEFORE_CONTEXT` 同时有 caption 与 compact 两个监听者；手动压缩时 compact 返回非 None 可能让 caption 永不执行（当前未构造出真实冲突）。主代理的 CC4 倒序实验证明**服务激活**与行序无关，但**短路监听优先级**确实继承注册序。建议为短路事件引入显式优先级/权重契约。

### F6 —— SMELL（未证实）：`ToolsService.register`/`guard` 忽略 `bound_effect` 的 False
- 判据 6；`tool_service.py:50-62,67-95`、`agents/catalog.py:37,55` 在 `apply` 之外注册时静默失去自动释放（当前调用方 skills/mcp 各自手工兜底，故无实际泄漏）。建议 `bound_effect` 失败即报错或返回绑定结果。

### F7 —— LOW：把 `inject` 当排序栅栏用
- `session/plugin.py:39,84` 声明并读取 `artifacts` 却从不使用；`agents/plugin.py:36-41` 声明 `session_launch` 而 `mount_catalog` 从不读。冗余声明让依赖图看起来像"真实构造需求"，恰好误导做 F1/F3 安全性判断的审查者。

### 该分区的合规结论（已验证）
`ThreadMetadataState` 身份永久 + 写入即总线事件（持久化与 manager 均为 fiber 持有订阅）；durable inbox 依赖门按设计工作；受审插件无未声明 `ctx` 读取；无手写 observer/disposer；模块级全局（trajectory 缓存、`_owners`）均为有界且有防护的刻意设计；边界处失败响亮（`_persisted_thread`、`dispatch_operation`、`ToolRegistry.register`、`_dispatch` 类型校验）。

### 与跨切面交叉印证
- 该 subagent 独立确认了 CC2（模块级全局合法）与 CC5（无手工注销、监听器全 fiber 持有）。
- 未独立发现 CC1（函数内 import）——因其范围内依赖图不涉及那 3 处绕路点。
- **主代理自评修正**：我此前把 F1 判为"顺序无关的状态投影"、F3 判为"仅测试路径，可接受"，两处都过于宽松。审计证据表明它们与历史上已修复的 `inbox_sink` 属于同一洞族：**构造期捕获他人写入的字段 + 重复权威状态**。

---

## 3. 能力插件（compact/caption/jobs/subagents/skills/goal/todolist/usage/content_cache/token_manager/workspace_instructions/workspaces）

审计方式：全量阅读 12 组 + 支撑读取；两个可执行复现脚本（未改任何源码）。基线：`test_background_tasks.py test_job_contracts.py test_caption.py` 23 passed；`scripts/check_architecture.py` → `boundaries: ok`（其看不到下列逻辑洞）。**主代理已逐条复核 F1–F4 及 F7/F8/SMELL 的代码路径，确认成立。共 8 个 confirmed（其中 4 个低危）+ 1 个 SMELL。**

### F1 —— CONFIRMED（最高严重度，已复现）：取消"排队中"的后台任务不会取消它——稍后照常执行并**双报完成**
- 判据 3 / 6（把错误状态当事实发布）
- 位置：`jobs/registry.py:250-274`（`cancel`）、`:312-336`（`_execute`）、`:361-369`（`_finish`→`_notify_complete`）、`jobs/plugin.py:89-108`（`publish_completion` 向 inbox 注入完成通知）
- 机制证据（代码层面完备成立）：
  - `start()`（registry.py:116-122）对**每个**任务立即 `create_task(_execute(...))`；排队任务（卡在 `max_concurrent_subagents` 信号量之后）在 `_execute` 内 `await semaphore.acquire()` 期间**仍是 PENDING**。
  - `cancel()`（registry.py:258-259）看到 PENDING 就 `_finish(CANCELLED)`（触发 `on_update` + `on_complete` → 完成通知 #1、客户端事件 #1、inbox 注入 #1）并返回 `cancelled=True`，**从不触碰 task**。
  - 信号量释放后 `_execute` 继续执行，`job.status = RUNNING`（**覆盖 CANCELLED**），runner 真实执行（spawn 子代理子应用 / shell 命令），`_finish` **无条件**再次触发 `_notify_update` + `_notify_complete`（完成通知 #2）。
- 失效路径：`/task stop`、`cancel_subagent` 工具、HTTP stop 取消排在并发限制之后的 subagent → 两次完成通知（两条 `completion_notice` 客户端事件、两条 inbox 注入并持久化），状态 CANCELLED→RUNNING→COMPLETED。`shutdown()` 同型：排队任务在会话关闭后仍会 spawn 执行（hook 已置空故无重复通知，但工作照做且已脱离注册表管理）。
- 复现输出（j2，`limits={SUBAGENT: 1}` 下 j1 运行、j2 排队）：notifications = `pending, stopped, COMPLETE/stopped, running, completed, COMPLETE/completed` —— **runner 在 cancel 之后被调用，`on_complete` 触发两次**。现有测试只覆盖取消 RUNNING 中或从未 start 的任务；排队窗口无测试。
- 建议：`cancel()` 只有在 `job.id not in self._tasks or self._tasks[job.id].done()` 时才能把 PENDING 短路为终止；否则走 RUNNING 分支（`runner_task.cancel()` + `await asyncio.gather(...)`），让 `_execute` 的 `CancelledError` 处理记录一次终止态；并在 `_execute` 于信号量 acquire 后重查 `job.terminal`，杜绝 CANCELLED 被覆盖。

### F2 —— CONFIRMED：workspace 目录投影失败沿会话生命周期总线炸掉 open/fork/rename/delete
- 判据 6（在非属主边界失败）
- 位置：`workspaces/service.py:170-179`（`_register` 要求目录必须存在否则 raise）→ `attach_session`（:117-123）→ `workspaces/plugin.py:43-47`（`SESSION_RESOURCE_CHANGED` 监听直接调用）→ `session/manager.py:480` 的 emit 传播监听器异常（XCore `events.py:279-283` emit 不吞监听器错误）→ `manager.py:486-492` 的 `except BaseException: ... raise` **不弹 `self._sessions[key]`** → runtime 泄漏进活动表，会话 open 失败（fork/rename/delete 同型：manager.py:577, 738, 800）
- 失效路径：会话的 `workspace_root` 目录在磁盘上被删除 → 下一次 open 该会话失败且 runtime 泄漏。目录存在性校验只应属于显式 `create`/`ensure`，不该属于"记录活动会话成员关系"的投影。
- 复现：`registry.attach_session("s", <不存在目录>)` 直接 `ValueError`；raising 监听器从 `ctx.emit` 向外传播。manager 接线为代码阅读确认，未端到端执行。
- 建议：`attach_session` 不要求目录存在，或 `WorkspaceSessionHandlers.changed` 捕获/记日志——目录投影永远不得否决会话生命周期。

### F3 —— CONFIRMED：SKILL.md 的 `allowed-tools` 被解析、校验、存储，然后**静默失效**
- 判据 6（声明的能力是静默 no-op；接受的输入永不生效）
- 位置：`skills/permission_scope.py:91-103`、`skills/plugin.py:174-179, 230-247`；守卫契约 `core/tools.py:16`、`agentloop/tool_runtime.py:350-366`
- 证据：`permission_scope.check()` 命中 allow 返回 `"allow"`，但唯一消费方 `_guard_tool_scope` 只处理 `"deny"` 分支（`GuardDecision` 是 deny-only，连"预批准"都无法表达）；`"allow"` 返回值无人读取
- 失效路径：skill 声明 `allowed-tools: shell(git *)` → 解析/校验/激活全部成功 → 工具守卫对该 skill 的调用**既不限缩也不预放行**，零运行时效果；无任何警告。
- 建议：要么真正执行限缩（不在列表内的工具对激活该 skill 的会话拒绝，与 deny 合并），要么删除死解析并在声明时响亮警告——禁止"声明了却什么都不做"。

### F4 —— CONFIRMED：caption 在模型调用前就置位 `_captioned=True` 并吞掉异常，一次瞬态失败永久禁用自动标题
- 判据 6（吞异常 + 永久状态污染）
- 位置：`caption/service.py:114-127`
- 证据：`self._captioned = True` 在 `invoke_llm` **之前**执行；`except Exception: logger.exception(...); return` 之后 `_captioned` 保持 True → `_is_first_turn` 永远返回 False → 该会话自动标题永不重试。`_apply_title` 成功路径本就再次置位（:143），失败置位是多余的且有害。
- 建议：只在成功应用标题后置位；若需防并发重入，用独立 in-flight 标志（成功/失败都会复位）。

### F5 —— LOW：压缩提交两个非原子 durable 记录；实况事件与回放展示同一份摘要的两种形态
- 判据 1 / 3
- `compact/service.py:381-391` 先写 `compaction/summary` marker（durable=True，fsync），**随后** `replace_message_range(...)`（surface replacement，权威摘要副本）——两写非原子：PRE_COMPACT 钩子产出多条摘要、或两次写之间崩溃 → 孤立 marker 成为幽灵轨迹记录（marker 只读不写，影响有限）。
- `compaction_completed` 实况事件的 `summary` 携带原始 XML，而回放路径（`session/contracts.py` `_compaction_summary`）用正则剥 XML——同一份 durable 副本在实况与回放呈现不一致。
- 建议：写 surface 在前（或 marker 非 durable/由 surface 派生）；实况事件直接发剥净文本。

### F6 —— LOW：JobRegistry 用手写 `on_update`/`on_complete` 回调替代总线事件
- 判据 3
- `jobs/plugin.py` 在 `ctx.set("jobs")` 之后直接赋值回调属性；无 fiber 作用域注销，依赖"apply 期间无 await"这种脆弱不变量。建议改发类型化总线事件或 `ctx.on` 订阅。

### F7 —— LOW：`SubagentLauncher._active` 只增不读不清理
- `subagents/service.py:64, 89` 把每个已完成的子应用对象留在列表里，会话生命周期内持续引用。建议删除该列表或子应用完成即移除。

### F8 —— LOW：`ContentCacheService._cached` 无界增长
- `content_cache/plugin.py:26, 40, 53`：每次超大消息都留一对引用，会话生命周期内无淘汰（身份守卫正确，只是无界）。建议有界化或源对象消亡即清。

### SMELL —— goal 的 `token_budget` 存储后从不执行，而 TURN_END 自动调度无界续跑
- `goal/models.py:20`、`goal/plugin.py` 全程只有创建/解析/存储路径；全仓无任何读取执行点（test_engine 里的 `token_budget_exceeded` 是 engine 自身另一机制）。`on_turn_end`（plugin.py:138-146）仅凭 `_continuation_pending` 旗标自动调度续跑，无预算闸。backlog 文档亦将"预算关联"列为未完成。建议：续跑调度接入预算闸，或删除该字段并响亮说明。

### 该分区的合规结论（已验证）
压缩摘要**durable 只存一份**（surface replacement 为权威副本；marker 仅元数据；回放经 `_compaction_summary` 派生，不强加第二份）；受审插件无未声明 `ctx.X` 读取；全部依赖门激活（无行序依赖）；状态变更走总线（workspaces/metadata/compact/usage）；`invoke_llm` 正确归属 llm 包；无模块级可变状态；`engine.inject` 非唤醒式（完成通知不制造重复 turn，除 F1 的双投递外）。

### 与跨切面交叉印证
- 独立确认 CC4 的依赖门结论在能力组同样成立；确认 CC2/CC5。
- **新增洞族**：**"取消/清理语义"洞**——接受请求时承诺的终止（F1 cancel、F2 会话生命周期、F6 清理注册）与执行期实际行为脱节。这族问题测试面最薄（排队窗口、卸载路径均无测试），是全仓最值得先修的共性风险。
- 未复现路径：F1 shutdown 端到端、F2 完整 manager 链路、StateService 同路径双实例互相清空（代码阅读未见触发，标记 unable-to-verify）。

---

## 4. 传输与集成（server/protocol/commands/config/permissions/sandbox/interactions/llm/prompts/context_builder/coretools/mcp_plugin/browser/acp_plugin/tui/client/web_server）

审计方式：全量阅读 16 个包 + 框架绑定语义；把"transport 是否越权触及 runtime 内部"作为独立检查项。**主代理已复核 F1/F2/F4 及 F9 的代码路径。共 12 个 confirmed + 2 个 SMELL + 16 个 LOW。**

### F1 —— CONFIRMED（**最高严重度，安全类**）：shell 提权靠跨包字符串匹配，且在 permissions 插件缺席时 **fail-open**
- 判据 5 / 6
- 字面量 `"shell"` + 参数 `sandbox_permissions` + 值 `"require_escalated"` 同时出现在**三个包**：`coretools/shell.py:138-155`、`sandbox/policy.py:338-353`、`permissions/system.py:359-365`，另有默认配置 `xcore.yaml:191-193` 与 `permissions/tools.py:79`
- 证据（`sandbox/policy.py:339-344`）：
  ```python
  escalated = (
      tool_call.name == "shell"
      and args.get("sandbox_permissions") == "require_escalated"
  )
  if escalated:
      return None          # 沙箱守卫直接放行
  ```
  （`coretools/shell.py:154-155` 同步把 `active_sandbox` 置 `None`）
- 失效路径：`require_escalated` 的 shell 调用 → 沙箱守卫返回 None（完全无 bwrap）→ 唯一剩余闸门是 `PermissionGuard` → **若 `permissions` 插件被禁用（`plugins.yaml` 的 `disabled: true` 是受支持配置），该调用无沙箱、无审批直接执行**。`sandbox/commands.py:57-58` 的帮助文本还宣称"permission approval cannot bypass it"，对提权 shell 而言是反的：审批本身就是那个"绕过"。
- 建议：提权词汇归 coretools 所有，走显式类型化能力（`Tool` 元数据声明 escalate 参数，或 `SandboxPort.escape(...)` 必须携带由权限守卫签发的审批令牌）；**无审批通道时沙箱守卫必须拒绝而非放行——fail closed**。

### F2 —— CONFIRMED：MCP 工具注册不受 fiber 拥有，unload 时泄漏
- 判据 3
- `mcp_plugin/plugin.py:121-122` 在 `APPLICATION_INITIALIZED` 监听器里注册工具；该事件由 `application/app.py:197` 在所有 fiber apply **之后**发出 → `xcore.current_fiber()` 为 None → `tool_service.py:94` 的 `bound_effect(...)` 静默返回 `False`，自动注销从未绑定；`_dispose`（`plugin.py:332-336`）只断开客户端、清空字典，**从不 `unregister`**（对比 `skills/plugin.py:82-94` 有 `_cleanup_runtime`）
- 失效路径：MCP fiber 未先收到 `SESSION_CLOSE` 就卸载（依赖丢失时的 `_refresh_dependents`，或 `PluginHandle.restart()`）→ `mcp:*` 工具残留并指向已断开的客户端；再次初始化即 `ValueError("Tool ... is already registered")`
- 建议：`_dispose` 补 `_rollback_all()`（对齐 skills）；并让 `ToolsService.register` 在 `bound_effect` 返回 `False` 时报错或记录，使"无主注册"不可能静默发生（同核心组 F6）

### F3 —— CONFIRMED：prompt 片段从事件监听器注册，属主键恒为 `"unknown"`，永不注销
- 判据 3 / 6
- 位置：`prompts/plugin.py:30-32`（触发自 `subagents/plugin.py:38-39` → `subagents/service.py:254-260` 的 `APPLICATION_INITIALIZED` 监听），注册表 `context_builder/builder.py:74-92`
- 证据：`current_plugin_name()` 返回 `"unknown"`，`bound_effect`（xcore/plugin.py:39-64）在 apply 之外是 no-op 且返回值被忽略 → 片段以 `<plugin_instruction name="unknown" ...>` 渲染到每次上下文构建（builder.py:306），卸载 subagents 插件永不移除；多个 out-of-apply 注册者会挤进同一个 `"unknown"` 槽（builder.py:92 覆盖）
- 建议：`PromptsService.add` 在 `bound_effect` 返回 False 或属主为 `"unknown"` 时响亮失败（返回 disposer / 要求显式属主）；`SubagentCatalogPrompt` 改为每次构建贡献动态内容（对齐 `sandbox/plugin.py:46` 的 `contribute_context` 模式）

### F4 —— CONFIRMED：browser 绕过已声明的 `SandboxPort` 协议
- 判据 5；`browser/browser.py:106-111` 调用 `resolve_read_path`/`check_filesystem_access`，而 `SandboxPort`（`sandbox/contracts.py:33-47`）只声明 `enabled`/`network`/`filesystem`/`resolve_filesystem_args` —— 两个方法只在具体实现 `SandboxPolicy` 上
- 失效路径：任何替代 `SandboxPort` 实现 → 首次 `browser_open("file://...")` `AttributeError`，被 `browser.py:87-88` 吞成软 `ToolResult.failure`（而非接线期失败）；`test_contract_conformance.py:30-84` 只查声明成员，看不到该越界
- 建议：把两个成员（或一个 `resolve_readable_file_url`）补进 `SandboxPort`

### F5 —— CONFIRMED：ACP 载体持有硬编码 工具名→kind 分类表，重复多个能力包的词汇并含虚构条目
- 判据 5 / 6
- 位置：`acp_plugin/events.py:243-256`（`_tool_kind`，用于 :62 与 :189）；证据即该函数本身：`"shell_start"`/`"run_command"` 全仓只出现在此处（后者实为 HTTP `operation_id`，commands/protocol.py:59），真实工具（`list_shells`/`wait_shell`/`spawn_subagent`/全部 MCP 工具）静默落入 `"other"`
- 失效路径：工具名由属主书写（coretools/filesystem.py:631-634 等），载体从名字重推导 ACP "kind"；改名/新增一律静默错分类、无任何报错
- 建议：由工具注册表（属主）在 tool-call 事件里声明 kind，载体消费；删除死条目

### F6 —— CONFIRMED：mcp_plugin 重造了 LLM 单次调用约定（`invoke_llm` 同族，第二处）
- 判据 5
- `mcp_plugin/callbacks.py:45-52` 手写 `astream` + merge 循环；`:106-110` 的 `_merge_response` 是个一行包装，内部再**函数内 import** `merge_model_chunk`
- 后果：不透传 `ModelRequestOptions`（输出 token 上限失效）、错误信封与 `invoke_llm` 的 `RuntimeError` 不一致、合并逻辑重复，约定任何改进都要改两处
- 建议：`response = await invoke_llm(model, messages)`，与 `caption`/`compact` 一致（这正是刚刚完成的 `invoke_llm` 归位所针对的问题）

### F7 —— CONFIRMED（判据 5）/ SMELL：permissions 重建 coretools 的参数 schema 来签发会话授权，schema 漂移会静默放宽权限范围
- `permissions/rules.py` 复制工具参数结构铸授权；coretools 参数改动而 rules 未随 → 授权范围比实际更宽，无任何告警。判据 1/5 的"单一定义"直接相关。未演示利用链。

### F8 —— CONFIRMED：provider 适配器把畸形的 tool-call 参数静默转成 `{}`（判据 6）
- 结果为失败/错误的工具调用带"空参数成功"的假象，边界不响亮。建议畸形参数走错误信封返回，不得伪造成功。

### F9 —— CONFIRMED（部分）：TUI 拦截服务端 `provider` 命令、客户端重实现 list/status 且行为发散
- 判据 5；位置 `tui/textual_client.py:909-922`（`_CLIENT_HANDLERS` 拦截 `"provider"`）、`:623-647`（`_show_providers`）、`:978-1002`（`_cmd_provider`）；服务端语法 `llm/commands.py:80-96`
- **主代理修正（重要）**：no-arg 打开选择器**是用户明确要求的交互**（`/provider` 需要交互、`/provider list` 是查看），不是缺陷；选择器最终派发稳定的 `use <name>` 形式，与服务端语法兼容。**真正的缺陷收敛为两点发散**：① `/provider list` 客户端渲染**丢失 `(current)` 标记**（主代理已核实 `_show_providers` 不渲染当前标记，而服务端列表标注当前项）；② `ls` 别名被 TUI 接受而服务端语法不识别。
- 建议：保留 no-arg 选择器；`list`/`status` 一律转发服务端并按服务端语义渲染（含 `(current)`），删除 `ls` 别名与本地自绘差异。

### F10 —— CONFIRMED：TUI unmount 不取消 raw-task 流式/线程视图泵
- 判据 3/6；`on_unmount`（textual_client.py:316-322）只取消 interaction/timer/event worker，从不调 `_cancel_stream_timer()`/`_exit_thread_view()`；`_stream_tick`（:1829 创建）与 `_pump_thread_view`（:768, :798-833）是裸 `asyncio.create_task`，非 Textual worker
- 失效路径：Ctrl-C/关屏于回合中 → `_stream_tick` 每 20Hz 继续渲染，`_render_new_transcript_entries` 撞上无守卫的 `query_one("#transcript")`（:1945-1948）→ 任务内未捕获异常（循环自灭，但泄漏未 await 任务 + 未处理异常）；`_pump_thread_view` 保持线程 SSE 订阅（HTTP 连接 + `_view_cursor`）在 unmount 后仍开着
- 建议：`on_unmount` 镜像 `_exit_thread_view`/`_cancel_stream_timer` 取消两个泵；transcript 查询在泵路径改容忍式（`_safe_query_one`）

### F11 —— CONFIRMED（中低）：ConfigService 构造期捕获 `user_context`，而同树其他读取者实时重读 overlay；会话级配置写永不传播
- 判据 4 + 陈旧捕获形；捕获 `config/service.py:52`、读 `:58-59`；写 `config/plugin_catalog.py:66-109,153-165`（写完**不发事件**）；标签 `config/contracts.py:96` 宣称 `applies_to="current_session"`
- 失效路径：PATCH `/plugin-config/config?scope=session` 写 `user.user_name` → overlay 已写、`GET /policy` 已反映，但新起的 engine 经 `agents/service.py:140` 读 `self._settings.user_context()` 拿到旧身份——同一棵树两个读取者新旧不一，且"current_session"标签被静默违背
- 建议：plugin-config 更新发类型化事件并重绑（或响亮声明 user 身份会话内不可变），并删除 session scope 的 `applies_to="current_session"` 声明

### F12 —— CONFIRMED（LOW）：TUI 消费零服务端生产者的协议事件类型
- `tui/client.py:147,149,292,395` 消费 `hello_ok`/`session_ready`/`status`/`shutdown_ok`，`textual_client.py:852,1505` 消费 `tool_started`（实际发射的是 `tool_calls_started`）；全仓（除 tui/tests）无生产方 → 这些分支永不触发，线程视图永不渲染 `[tool]` 起始行
- 建议：要么服务端补发这些帧（hello/会话打开帧、tool-call 起始帧），要么删除死分支并对齐真实词汇表

### S13 —— SMELL（未证实）：ACP `prompt()` 在正常流耗尽时可能永远 await `completed`
- `acp_plugin/xbot_agent.py:356` `await prompt.completed.wait()` 无超时；setter 仅 :461-472, :551-557, :560-564。`SessionEventStream.close()`（session/event_stream.py:84-92）入队 `None` → `ReplaySubscription.__anext__` 抛 `StopAsyncIteration`（core/replay.py:81-83）→ `_forward_session_events` 的 `async for` **正常结束**，三个 setter 一个没跑，`finally` 的 `_active_prompts.pop` 也不跑 → 会话永久拒绝新 prompt（"a prompt is already running"，:334-338）
- 缺失证据：ACP SDK 是否并发派发 `close_session` 与进行中的 `prompt`（SDK 只读会话不可读）——故未证实，但 hang 边界清晰
- 建议：`_forward_session_events` 在正常耗尽/取消的 `finally` 中给所有活动 prompt 发 failure+completed；或 `completed.wait()` 与超时/会话关闭信号竞速

### S14 —— SMELL（未证实）：`SandboxPolicy.replace_config` 重建副本策略并手工复制私有字段
- 判据 1（第二构造点）；`sandbox/policy.py:84-100` 建一次性 `SandboxPolicy(...)` 再手工枚举复制 `_network`/`_rules`/`_backend`（含同类私有访问）——`_load_config`（:412-424）新增的任何字段若漏进复制清单，`/sandbox set ...` 后永远是旧值（现无字段示范该陈旧）。建议改为单一 `_load_config` 加载器 + 后端重建（一个构造点）

### LOW 清单（L15–L30，全部代码事实，未逐一复述详情）
- L15 browser 网络闸门在 `sandbox is None` 时 fail-open（仅被必需 inject 掩盖）
- L16 `BrowserSession.shutdown` 吞掉关闭失败且不记录
- L17 `client/event` 总线通道无任何监听者（状态变更路径的死半截）
- L18 LLM 协议路由用字符串匹配异常消息推导线上错误码
- L19 server/http.py、session/protocol.py、tui/terminal.py 跨包私有下划线导入
- L20 TUI 渲染层模块级可变缓存（与 CC2 同类，可接受）
- L21 `trace_event` 吞掉全部诊断异常（判据 6 的灰色地带，诊断通道可接受）
- L22 `ConfigService._entry_config` 对缺失/禁用策略条目返回空 `{}`，消费方静默按模型默认值重新校验
- L23 commands/protocol.py 重造 commands 包自身的参数切分
- L24 interactions/protocol.py 导入 core.tools 的私有 `_validated_client_event`
- L25 config/protocol.py 对 policy get/update 跨线程取 `snapshots[0]`
- L26 ACP `replay_history` 解释 engine 内部 `runtime_input` 键并带静默默认值
- L27 ACP 事件任务映射器在会话打开时捕获 `context_window`，provider 切换后陈旧（陈旧捕获形）
- L28 context_builder 静默默认值 + 不对称 fragment API
- L29 `InteractionWaiter._resolve` 静默丢弃重复答案
- L30 config/service.py 与 interactions/plugin.py 的函数内 import（卫生，见 CC1）

### 该分区的合规结论（已验证）
- `server/`+`protocol/`：载体是哑路由持有者，业务路由经类型化 `http/route` 总线事件进入（fiber-bound disposer）；`protocol/` 是纯线框（SSE 把畸形 JSON 显式报 `error` 而非吞掉）。
- `llm/`：provider 特有行为全部留在 `openai.py`/`anthropic.py`/`mock.py` 的 `BaseProvider` 之后；通用约定（`invoke_llm`、`ModelPort`）归 llm 包；`LlmService` 单一构造点、`ctx.set` 于 apply；per-thread `ctx.model` 隔离，无跨会话泄漏、无 provider 陈旧捕获。
- `permissions/`：单调 `ctx.tools.guard` 条目，deny > grant > allow > ask，缺省 `ask` fail-closed；`explicit_allow(constrain_param=...)` 保护提权 case；一次性授权只经 `check_tool_call` 消费。除 F1/F7 的 shell 提权词汇外无硬编码放行。
- `sandbox/`：政策经 fiber 注册（`ctx.set`），更新走 `POLICY_CHANGED` 且身份不变；bwrap 缺失响亮失败（bwrap.py:42-45）；文件守卫拒绝且无审批通道（职责分离正确）。行序无语义成立。
- `acp_plugin/`：载体只消费注入的 `sessions`/`acp_launch`/`runtime_log`，从不重组应用（组装在 `application/acp.py`）；teardown 绑定 fiber（`ctx.dispose(agent.close)`）；`xbot_agent.py` 只经 `SessionsPort` 操作读 runtime 并显式映射 `RequestError`；:520 的 `create_task` 是外向投影循环（不写 runtime 状态，tracked 且在 close 取消）。
- `client.py`/`web_server.py`：纯协议客户端 + 同源代理；遍历守卫（resolve 后 `is_relative_to`）；流式读后类型化错误；不触 runtime 内部。
- `commands/`/`interactions/`/`prompts/`/`context_builder/`/`config/`：每个 `ctx.X` 读取都在 `inject` 声明（逐插件核实）；服务在 apply 内构造并 `ctx.set`；注册 fiber 持有；状态走类型化总线事件（`POLICY_CHANGED`、`BUILD_CONTEXT`、操作处理器）；无 `ensure_future` 状态投递、无模块级可变状态、无构造后重绑。
- `mcp_plugin/`：`inject` 覆盖全部 `ctx.*` 读取；`callbacks.py` 所有能力端口经参数进入；工具走标准 ToolsPort + guard 路径（除 F2 的生命周期泄漏）。
- `browser/`：`inject` 全覆盖；工具于 apply 注册（fiber-bound）；懒浏览器/WebAccess 由 fiber-bound `_dispose` 正确拆除；沙箱/工件读取实时、无陈旧捕获（除 F4 的协议越界）。
- `tui/`：`TuiState.apply_event` 忠实投影线事件；transcript/线程视图重建只读消费线数据；流游标、重连、游标过期恢复、基线重建均为客户端韧性；`CommandRegistry` 每实例（模块表复制、从不修改）；选择器派发稳定命令形式（除 F9/F10/F12）。
- **行序独立性（全包）**：激活由服务可用性驱动（`inject`），loader 对未满足依赖响亮失败（`validate_mounted_tree`，loader/runtime.py:74-100）；全仓无对 `xcore.yaml` 条目顺序的正确性依赖（CC4 实验佐证）。

### 不能验证（已尽力）
- F1 的"permissions 禁用"全链路端到端、S13 的 ACP-SDK 并发触发（SDK 源码不可读，需活体 runtime）
- F8 的真实 provider 畸形参数流（已读码 + 直接调用纯 helper，未对活体 provider）
- F11 的 session-scope `config.user` 补丁是否实际被签发（发散本身代码可证）
- 压缩/权限帧在 TUI 中的逐事件 SSE 路由（投递图部分在 scope 外的 `session/runtime.py`）
- `SandboxPolicy.save()/export_config()/add_rule()` 疑似无调用方（可能死 API）

### 与跨切面交叉印证
- **独立发现了 CC1 中"能力错位 + 函数内 import"的第三处实例（F6，mcp_plugin）**——与我刚修的 `ctx_splice_recorder`、主代理解析出的 `config/service.py` `application/child.py` 属同一类。
- 独立确认 CC2（TUI 缓存）、CC3（L21 的诊断吞异常属可接受范围）。
- **新增了一个跨切面未覆盖的维度：fail-open 语义**（F1/L15）——本报告判据 6 的"响亮失败"应显式包含"缺席依赖时拒绝而非放行"。能力组 F1（取消承诺脱节）与传输组 F1（提权守卫）合并构成全仓最优先修复面。

---

## 5. 汇总

| 分组 | confirmed holes | smells | 最高严重度项 |
|---|---|---|---|
| 核心运行时（分区 2） | 5（F1–F3 高中危，F4/F7 低危） | 2（F5、F6） | F1：`inbox_items` 陈旧重绑 + `/status` 陈旧快照 |
| 能力插件（分区 3） | 8（F1 高危，F2/F3/F4 中危，F5–F8 低危） | 1（goal token_budget） | F1：取消排队任务不取消、稍后执行并双报完成（已复现） |
| 传输与集成（分区 4） | 12（F1 高危安全类，F2/F3/F4/F5/F6 中危，F7–F12 中低危）+ 16 LOW | 2（S13、S14） | F1：shell 提权 fail-open（安全） |
| 跨切面（分区 1） | CC1 中 3 处真实绕路 + 1 处能力错位（已各归位处理） | — | — |

**总览**：25 个 confirmed holes + 5 个 smells（跨切面 3+1 处实锤另行计）。共性风险按家族归类：

1. **取消/清理承诺脱节**（能力组 F1、F2，传输组 F2、F3、F10）：请求时承诺的终止与执行期行为脱节，全部落在测试最薄处（排队窗口、卸载路径、unmount 泵）——**建议最先修**，并补对应窗口的测试。
2. **fail-open 语义**（传输组 F1、L15）：判据 6 应显式补"缺席依赖时拒绝而非放行"。
3. **陈旧捕获/重复权威**（核心组 F1、F2、F3，传输组 F11、L27）：同一洞族第三次出现，修复时以单一属主为验收标准。
4. **能力归属错位**（传输组 F5、F6，CC1 中 mcp_plugin）：词汇/约定从属主包外泄到载体，改一处约定需改多处。
5. 低危与卫生项（F7/F8、F12、L15–L30）：随修复批次顺带清理，不停工专修。

**修复总原则**（遵守用户指令）：不添加检查脚本护栏规则；逐项改正源码；已确认的合规面不动；修复按"最高严重度 → 有复现证据 → 低危卫生"排序，每项给出验收测试。全部修复完成后，由用户指示再提交。