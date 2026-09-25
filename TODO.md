# XBotv2 中期开发计划

> 本文件只记录当前可执行任务、当前权威证据和完成门槛，不作为历史日志。
> 数据模型、字段和依赖方向唯一以
> [`XBotv2/docs/project/data-model-redesign.md`](XBotv2/docs/project/data-model-redesign.md)
> 为准；旧实现、旧测试与 `HANDOVER.md` 都不是设计依据。

## 1. 目标与总完成条件

目标是完成 XBotv2 的底层数据模型迁移，证明并修复 Core、主线插件、HTTP
运行时和主线 TUI。不得以兼容层、动态注入、判空、放宽校验或删除有效测试换取绿灯。

- [ ] 设计文档第 2、3 节定义的每个领域语义只有一个权威类型和 owner；其他层仅持有明确投影或 envelope。
- [ ] 设计文档第 4 节列出的重复类型、动态语义和旧入口全部归零，不保留兼容导出、别名、双读或双写。
- [ ] 依赖遵守设计文档第 5 节；Core 不导出插件聚合对象、不硬编码插件名、不接受插件注入 callback。
- [ ] Core 运行时矩阵有正常、关键失败、恢复和清理证据，测试经过真实 production factory/registry/persistence 路径。
- [ ] 每个主线插件按其**实际职责**验证适用的注册、依赖、成功、失败、恢复、事件、持久化和卸载能力；不存在的 Agent Tool、持久化或资源生命周期不强制虚构测试。
- [ ] HTTP 的关键 streaming、tool、permission/interaction、interrupt、reconnect 和恢复路径由真实 loopback server/client 验证。
- [ ] 主线 TUI 完成真实多轮 PTY、键盘、paste、选择、折叠、滚动、session switch/reconnect、退出和渲染验收。
- [ ] 非法内部状态在 owner 边界明确失败；不得用 `None`、`getattr`、宽泛字典或静默 fallback 表达另一种语义。
- [ ] 最终结论逐项核对生产入口和产物；不能仅以全套测试通过宣告完成。

## 2. 固定约束

- [x] 不修改 `data-model-redesign.md`；发现歧义时记录证据，不自行发明新模型。
- [x] 不保留向后兼容；旧调用方迁移完成后直接删除旧字段、旧事件、旧 policy 和旧导入路径。
- [x] `ResourceResponse[T]` 是设计文档 §3.6 规定的 protocol wire 类型；只在 protocol 边界使用，不扩散为 Core 或插件业务返回包装。
- [x] 不从 `XBotv2.core.tools` 或 Core 公共 API 导出 `AllTools` 等 agentloop/plugin/application 聚合对象。
- [x] 不创建 wrapper executor、伪造 `ToolCall` 或权限旁路；工具统一经过标准 registry、permission 和 execution path。
- [x] 不做 WebUI 工作，也不因 WebUI 测试调整主模型；本轮范围是主要代码、Core/HTTP 运行时、主线 TUI 和单列的可选 Maya TUI。
- [x] Bench 不属于本轮验收；不能证明产品行为的 bench 不修、不迁移。
- [x] 不提交代码；保留用户现有 staged/unstaged 修改，不重置、不覆盖无关工作。
- [x] 不通过删除有效功能测试取得绿灯；只删除锁定已删除语义、私有结构或完全重复证据的测试。
- [x] 真实供应商网络 smoke 仅在凭证和网络可用时 opt-in；未运行时记录原因。真实 SDK 类型、adapter contract 和本地真实 server 路径仍是必需证据。
- [x] Browser 安全验收限定为已声明的 policy、关键协议互操作和资源清理；无界远程/攻击组合矩阵另列安全 backlog，不阻塞本轮。

## 3. 当前权威基线

以下结果只说明对应工作树当时经过了这些套件，不替代下面的逐项退出条件。

- [x] 最新扩大回归证据：Core+TUI `1296 passed in 149.42s`、HTTP transport `101 passed in 78.30s`（均使用 `-s`；交互测试走真实 loopback/PTY）。
- [x] queued fold-in 当前 `10 passed in 9.27s`；Maya plugin suite 当前 `37 passed in 2.36s`。
- [x] 最新 Stage B focused runtime/persistence 集合 `39 passed in 16.91s`：覆盖 AgentLoop input/runtime、session ownership 和 persistence；包括 tool turn 的 `TURN_END` 与 durable history 顺序、provider failure 的 `ON_ERROR` 与 durable history 顺序、owner 进程 `os._exit` 后完整回合恢复、SIGKILL 期间未完成 provider stream 的丢弃与 canonical user history 保留，以及工具副作用发生后进程崩溃时不重放该工具。
- [x] 已有真实 HTTP/TUI 证据覆盖 caption switch/resume、compact command/event/trajectory、permission 与 ask-user 回复、usage/reasoning 展示、空会话退出和首轮物化。
- [x] 真实 PTY 已覆盖多行 Unicode 长 paste 后编辑、permission/ask-user、Think 渲染与折叠、session switch、Ctrl-C 退出；另以两个同时运行的 CLI TUI 连接同一 server 的两个会话，关闭其中一个 runtime 后由第三个 CLI 从磁盘 resume 并继续下一轮，连续稳定复跑 3 次。渲染捕获保存在 `/tmp/xbot-tui-real-run-20260925`，尚未满足包含 compact/caption/tool 的完整复合路径。
- [x] 本环境异步测试在 pytest capture 下可能卡在 default-executor teardown；`-s` 不跳过测试或改变断言，Core/HTTP/TUI 扩大回归统一使用 `-s`。
- [x] WebUI 未运行且不在范围内；真实供应商网络 smoke 尚未运行，不能据本地 SDK adapter 测试宣称网络互操作。

## 4. 阶段 A：设计符合性与基础迁移

阶段 A 合并原“建立账本”和“消除剩余重复语义”。每个 vertical slice 都执行：定位 owner/consumer → 写行为测试 → 迁移完整生产链 → 删除旧定义 → 静态搜索归零 → 扩大回归。不得先建立第二套模型再等待以后迁移消费者。

- [ ] **消息 / 工具 / 历史**：核对 canonical message、parts、`ToolOutcome`、`ToolExecution`、history/trajectory、persistence codec、session record 和客户端投影；删除旧 message 字段、平行 history 列表和重复 record payload。
- [ ] **provider / usage / token / timing**：核对 `ModelRequest`、stream terminal、`ModelExchange`、`RequestObservation`、usage、token budget 和 timing 的单向数据流；删除 metadata 注水、第二份 measurement 和 context fallback 链。
- [ ] **event / interaction / permission**：核对 typed Loop/plugin events、application registry、session frame、SSE codec、interaction request/resolution 和 permission policy；删除 `type/data`、动态 payload 和互斥 optional 字段。
- [ ] **job / subagent / shell**：核对 `JobIdentity/JobState/JobView`、owner spec/result、shell artifact 输出和 child application result；删除 metadata 业务语义、重复 job snapshot 和重复 child result。
- [ ] **config / agent / runtime**：核对各层配置、唯一 `ResolvedRuntimeSelection`、thread metadata、application/session attach 和 permission/sandbox 单调合成；删除 effective config、route/model/context 的副本。
- [ ] **独立插件 slices**：goal、todolist、workspaces、commands、loader、browser、skills/MCP 等逐个从 parser/store 到 event/client 闭环；每个 slice 只验证实际拥有的能力。
- [ ] 完成 Core/session/application/protocol/server/LLM 的 forbidden-pattern 搜索：重复模型、`dict[str, Any]` 承载已知结构、动态属性、判空切换语义、旧 envelope、兼容 alias/re-export 和反向依赖。
- [ ] 为每个发现保留“设计小节 + owner + 可观察行为 + 当前调用者”四项证据；运行时路径图与 owner/consumer 清单合并为同一份账本，不重复维护两个视图。

当前已闭环的设计切片：

- [x] `ModelRequest` 仅由 `core.provider` 定义；agentloop 重导出已删除。
- [x] `AllTools/ToolSelection` 已从 Core 迁到 agentloop owner，现存消费者已迁移。
- [x] LLM service 构造期持有 typed `LlmConfig(default_provider, providers)`；raw config 副本、可选 configure 和 parser 重导出已删除。
- [x] 图片只在 ContextBuilder 成为 `ResolvedImagePart`；provider 不再持有或动态注入 ArtifactStore。
- [x] OpenAI/Anthropic stream 状态、terminal 和 SDK error 解码已收口到 provider adapter 边界，未知结构不再靠 `getattr` 猜测。
- [x] Browser 使用唯一 `BrowserConfig(SearchPolicy, NetworkPolicy, BrowserSessionPolicy)`；平行网络配置和旧字段已删除。
- [x] Content cache 复用已有 `INPUT_ACCEPTED`，没有新增输入事件；长输入 canonicalization 只产生带逻辑 `ArtifactRef` 的新 message。
- [x] SessionManager 的 application factory 是构造期必需依赖，不再以 optional callback 延迟失败。
- [x] `TokenCounters` 和 `SessionStats` 使用 owner-defined typed addition，不再遍历字段或动态读取属性。
- [x] Compact transaction end 使用 `TransactionOutcome` 判别联合，不再用 optional error + bool 推导语义。
- [ ] 上述完成项仍需纳入最终全量 consumer/forbidden-pattern 审计；局部搜索不能替代阶段 A 退出。

阶段 A 退出条件：

- [ ] 五个基础工作组和全部在范围插件均完成 vertical slice；每个旧模型引用归零，生产者、持久化、transport 和消费者只使用 canonical model 或单向投影。
- [ ] 设计文档第 4 节每个禁止项都有可复核搜索结果；同一概念不存在第二份数据或解释逻辑。
- [ ] Core 公共 API 只暴露 Core 拥有的概念，依赖方向符合设计文档第 5 节。

## 5. 阶段 B：Core、HTTP 与插件运行时证据

### B1. Core 运行时矩阵

- [x] AgentLoop 覆盖纯文本、stream reasoning/text/usage、单/多工具、typed 工具失败后继续、provider failure、输出前 retry、partial 后不 retry、retry exhaustion、中断和同 runtime 恢复。
- [x] queued fold-in 覆盖 busy turn 多消息入队、顺序消费、合并回复、regenerate 不重复提交和 interrupt 后恢复；HTTP edit/remove/steer 会改变实际 `ModelRequest` 与 canonical history。
- [x] tool turn 的 `TURN_END` hook 观察到完整 durable conversation，顺序为 human → assistant tool call → tool → final assistant。
- [x] provider failure 的 `ON_ERROR` hook 观察到与 durable history 相同的错误时点和内容，随后同 runtime 可恢复。
- [x] 完整回合写入后 owner 进程 `os._exit`，新 application 可 hydration canonical history 并完成下一轮真实 provider request。
- [x] 超长用户输入与 Tool result 先完整写 ArtifactStore，再以 preview + 逻辑 ref 进入 canonical history；多轮、restart、Skills expansion、写失败保留原文均有 E2E。
- [x] 大型嵌套 Agent-authored tool args 不被 externalize/truncate；附件仅在请求构建期解析绝对路径，持久消息只保留逻辑 ID。
- [x] RuntimeNotice 与 HumanInput 保持不同持久类型；后台 completion 不独立唤醒模型，下一真实用户回合只消费一次。
- [x] reconnect、resume、mailbox delivery 和 persisted history 有各自语义与行为测试，不因都包含 message 而合并。
- [x] session ownership 覆盖进程内引用计数、并发启动共享 owner、真实 subprocess 跨进程排他和 owner 退出后锁释放。
- [x] persistence 覆盖 message identity round-trip、重复 ID、surface/transcript、trajectory cursor/page 和 malformed JSON 明确失败。
- [x] 进程终止时的未完成 provider turn、工具调用和 durable pending inbox 已由真实 subprocess + 新 application/SessionManager resume 验证：SIGKILL 发生在 provider stream 等待中时，stream 不自动重放，已接受 HumanInput 保留在 canonical history 并进入下一次 follow-up ModelRequest；独立的未消费 durable NEXT_TURN item 经 `SessionManager.open_session(mode="resume")` 自动恢复、执行一次并清空；另在工具外部副作用已落盘而 ToolMessage 尚未落盘时杀死 owner，resume 会追加 `ToolFailed(session_restarted)` 而不再次执行该工具。此语义是 at-most-once，不能保证崩溃前工具副作用与 ToolMessage 原子提交，因此外部副作用结果在该窗口内仍可能不确定。
- [x] session open/close/reopen、不可读 catalog 隔离、undo/regenerate、interrupt、并发 message 已由 production manager/HTTP 路径覆盖；产品没有 redo 操作，不虚构该语义。单 session 多 thread 关闭现会在一个 close 失败后继续释放其余 runtime，按 thread identity 去重并最终聚合错误。
- [x] trajectory fold 对 position gap、unknown replacement source、duplicate message identity 和无 `entry` envelope 的 legacy flat record 均拒绝；新 writer 的 envelope/schema 由 HTTP 持久化路径断言，未增加兼容 decoder。证据：`tests/core/test_persistence.py` 与 `tests/integration/test_http_transport.py::test_typed_history_undo_fork_and_clear_persist_atomically`。
- [x] `SessionRuntime.close()` 的 engine lifecycle hook 失败会传播给调用方，同时仍销毁 mounted application、关闭 event stream、设为 `closed` 并释放 session ownership；由真实 application + runtime 测试验证。
- [x] `SessionManager.close_all()` 在一个 runtime close 失败后仍继续关闭其他 runtime、清理无记录 session 并停止 reaper，最后以 `ExceptionGroup` 报告失败；`test_close_all_finishes_every_runtime_and_stops_reaper_after_close_error` 覆盖多 runtime 行为。
- [x] 异常退出、正常退出、启动后立即退出、打开已有会话后退出均有 runtime/HTTP/subprocess/真实 TUI 证据；覆盖 owner `os._exit`/SIGKILL 后 lease 恢复、空 session 清理、resume 后关闭、task/event stream 清理，以及单 runtime、单 session 多 thread、全 manager 三层关闭错误传播。
- [x] provider usage/timing/cache/context → canonical exchange → service snapshot/event → HTTP/TUI consumer 已做同值断言；真实 socket HTTP 测试覆盖两步 model + shell tool、五类 token、SSE/history/resume 的 timing/usage 一致，token manager 从 production application service 读取实际最终 exchange diagnostics，compact 只按最终待 invoke `ModelRequest` 自行估算。

### B2. Interaction 与 HTTP 边界

- [x] 真实 loopback 覆盖 streaming、tool、permission 等待、ask-user、中断、reconnect snapshot/cursor replay、重复回答拒绝和最终 canonical history 顺序。
- [x] multi-tool permission 覆盖首项 deny 无副作用、后续独立 allow 继续执行，并按序产生 typed response、tool outcome 和 history。
- [x] policy patch 以 typed schema 持久化并在 reopen 后保持语义；HTTP command/goal/skills/history/trajectory 使用当前 wire contract。
- [ ] 覆盖多个同时 pending interaction/permission 的 replay、断连、拒绝、cancel 和 waiter error；断言 durable outcome/event，不断言私有 Future 容器。
- [ ] 审计 HTTP fixture/helper 和实际 response/event，清除旧 envelope、旧字段和动态 payload 构造。

### B3. 唯一插件证据账本

状态只依据插件**适用能力**；表中缺口是唯一插件待办，不在其他章节重复扩写。

| 插件/边界 | 当前生产路径证据 | 尚未完成 | 状态 |
|---|---|---|---|
| `compact` | application/AgentLoop command、Agent Tool、自动阈值、context-overflow、veto/cancel、transaction、history/reopen、HTTP event 和真实 TUI trajectory 闭环；`test_automatic_compaction*` 走最终 `MODEL_REQUEST_READY` 的待 invoke `ModelRequest`，只由 compact hook 估算一次 | 无 | [x] |
| `interactions` | typed request/answer、wait、interrupt、reconnect replay、duplicate resolution | 多 pending 和其余 waiter failure/cancel durable/event 组合 | [ ] |
| `permissions` | typed policy、allow/deny/interrupt、multi-tool 顺序、reopen | 多 permission replay/断连和 waiter error 组合 | [ ] |
| `jobs` | shell Agent Tool、成功/失败/cancel、单次 notice、session-close、HTTP list/stop/error、typed `OutputPage` | 运行中输出读取；共享 ArtifactStore 生命周期项 | [ ] |
| `subagents` | parent 标准 spawn/wait/read → child application；success/provider failure/cancel 和 lease cleanup | 多 child 并发/同时取消；失败/cancel 后 parent read 语义 | [ ] |
| `goal` | achieved/impossible/interrupt、typed retry、round cap、clear、close/resume、terminal stats | 恢复失败边界 | [ ] |
| `todolist` | production application factory/registry 下的 create、typed failure 后继续、config schema/limit、依赖阻塞/循环；runtime notice 真实进入 AgentLoop step 并持久化；stale counter 与任务 snapshot close/resume；真实 compact command 触发 unfinished-task reminder，第二次完成后 compaction 不重复注入，trajectory 经 ThreadPersistence reopen 验证 | 最终扩大回归 | [x] |
| `caption` | canonical request、auto/access 组合、failure retry、Tool get/set、server→TUI title/switch/resume | metadata 持久化失败；真实 reconnect 后标题 UI 恢复 | [ ] |
| `usage` | 五类 counter、auxiliary request、event/snapshot、close/resume、真实 socket 两步 model + tool timing/usage 同值、TUI display | 最终扩大回归 | [ ] |
| `context_builder` / `prompts` / `workspace_instructions` | logical artifact/image 解析、Skill slash、AGENTS.md reload/disabled/invalid UTF-8 | 完整 component 顺序、身份和最终 request；OS read failure/cleanup | [ ] |
| `token_manager` | typed `ModelRequest` estimate、tool schema、provider observation、output reservation、negative remaining、unload；production application service 可读取实际最终 exchange diagnostics；compact 按最终待 invoke 请求估算 | 最终扩大回归 | [ ] |
| `coretools` | filesystem malformed boundary、standard tool path、shell artifact/output page | 按各工具实际能力核对 config→registry→permission/sandbox→result/cleanup | [ ] |
| `browser` | canonical policy、Agent fetch/search、Chromium interaction/screenshot、sandbox、DNS pinning、client cleanup | HTTPS CONNECT 互操作；cancel/异常 close teardown；不扩展成无界攻击矩阵 | [ ] |
| `content_cache` | user/tool externalization、完整原文、multi-turn/restart/Skills、write failure、identity | ArtifactStore orphan/GC/corruption 的共享策略；plugin unload | [ ] |
| `sandbox` | policy focused tests及 Browser 子资源禁网 | 真实 filesystem/shell 的 workspace allow、越界 deny、提权和资源清理 | [ ] |
| `skills` | registry/service 与 HTTP prompt expansion | discovery/load → slash/model tool → schema/args → failure/unload | [ ] |
| `mcp_plugin` | focused contract tests | config/discovery/handshake/schema/args、disconnect/timeout/reconnect cleanup | [ ] |
| `workspaces` | focused catalog 与 SSE | add/remove/switch、membership/event、exit/reopen consistency | [ ] |
| `agents` / `llm` / `persistence` / `session` / `application` / `server` | focused tests、真实 factory、SDK-typed adapters、完整 HTTP carrier | 完整 factory→request→trajectory→carrier 失败/恢复矩阵；真实 provider 仅 opt-in | [ ] |

共享缺口：

- [ ] 定义并验证 ArtifactStore 对 content cache、job output、browser screenshot 等 owner 的统一 orphan/GC/corruption 策略，避免每个插件发明一套生命周期。
- [ ] 对每个插件审计现存测试：保留证明当前领域行为的用例；删除只验证旧模型、私有结构或完全重复的用例；缺口按上表新增行为测试。

阶段 B 退出条件：

- [ ] B1、B2 的未完成矩阵全部闭环；Core 与 HTTP 全套在当前生产代码上通过。
- [ ] 插件账本每行的适用成功、关键失败、恢复/清理均有生产入口证据；无适用能力时明确记为“不适用”，不得虚构测试。
- [ ] 审阅者可从每个测试指出实际经过的 factory、registry、provider/tool、persistence/event 或 HTTP 边界。

## 6. 阶段 C：主线 TUI 产品验收

先以组件/状态行为测试固定语义，再做真实 PTY。TUI 不得以本地状态掩盖 runtime/server 缺字段。

- [x] provider terminal validation 错误已修复；OpenAI/Anthropic adapter 以具名 `response=` 构造 typed terminal event。
- [x] picker index 按实际选项数有界 wrap；超过元素数的 down 后不产生虚假位置。
- [x] 多行 Unicode paste、尾换行和 paste 后编辑只提交一条原样 input；composer 有固定高度上限。
- [x] `/approve`、`/deny`、`/answer` 使用标准 HTTP endpoint，不伪装成普通 user message。
- [x] Thinking 与 Tool block 可折叠；展开为固定高度只读滚动窗口，默认折叠隐藏 payload。
- [x] usage/context/cache 的唯一 owner 是 session bar；footer 仅显示 transient status/queue/activity。
- [x] caption 在首轮/多轮、failure retry、已有标题、session switch 和 resume 后进入 session bar。
- [x] 空会话 Ctrl-C/HTTP close 不创建 session/catalog；首个有效 input 后才物化；SSE task cancel 会等待 reader 并关闭 generator。
- [x] Compact 真实 server/TUI 五轮后执行 `/compact`，completion event、summary render 和 `SurfaceReplaced` trajectory 一致。
- [x] 长多行 composer 改变布局后，提交动作明确返回 transcript 实时尾部；真实 PTY 第二轮回复不再已持久化却落在视口外。
- [x] 两个真实 CLI TUI 同时连接同一 server 的不同 session，消息、usage、turn 与持久历史互不串线；其中一个关闭 runtime 后由新 CLI 从磁盘 resume、恢复历史和 `turn:1`，再完成 `turn:2`，另一个连接全程保持可用。
- [ ] 验证 streaming reasoning delta → completed record 时 Think block 不重复、不丢失且保持折叠/滚动状态。
- [ ] 在 step/turn 语义边界加入稳定间距或分隔，不依赖偶然 widget 顺序；验证长会话和窄终端。
- [ ] 覆盖 resize、长内容、stream update、session switch、reconnect、interrupt 后的 block/focus/scroll 状态。
- [ ] 完成 caption 的真实 reconnect 后恢复，并确认 session picker 与 session bar 使用同一服务端 title projection。
- [ ] 验证 TUI 异常进程退出、打开已有会话后退出和 reconnect 时旧订阅关闭；进程锁仍归 SessionManager/server，不由 TUI 假释放。
- [ ] 用 Textual pilot/render snapshot 检查结构；快照只辅助定位，不能替代真实交互。
- [ ] 启动真实 server 与真实 TUI，实际完成一条复合多轮路径：普通回复 → streaming Thinking → tool → permission/interaction → 后续追问 → compact → session switch/reconnect → 退出。
- [ ] 在上述真实会话中亲自操作键盘、长多行 paste 后编辑、picker、复制、Think/Tool 折叠与滚动，并核对 canonical history/event。
- [ ] 保存不提交的本地截图或 terminal render capture：初始页、paste、streaming Think、Tool 折叠/展开、turn 分隔、caption、usage/context/cache 和 reconnect 恢复。
- [ ] 退出后检查磁盘、进程、task、subscription 和 session lock，无空会话或资源残留。

阶段 C 退出条件：

- [ ] TUI 全套通过；真实可控 server 完成复合多轮；render evidence 确认信息层级、固定高度、滚动、caption 和统计确实可见；退出资源检查通过。

## 7. 阶段 D：可选 Maya TUI 产品轨道

Maya 是独立插件产品，不阻塞 Core/HTTP/主线 TUI 退出。验收限于
`maya-tui/README.md` 中基于现有 XBot 公共 API 的有界能力矩阵；需要新后端契约的能力只记录 blocker，不伪造实现。

- [x] 能力/证据矩阵、注册 YAML、独立运行说明和 API blocker 已写入 `maya-tui/README.md`。
- [x] 已有真实 XBot HTTP/SSE vertical test 覆盖 ask-user、edit permission、文件副作用和 reconnect backoff；插件 suite `37 passed`。
- [x] 已实现当前 API 可支持的 `/fold`、`/follow`、stats footer、child thread、destinations、palette、OSC52 copy 和主题配置。
- [ ] 安装真实 `maya-py` wheel 并启动 UI；当前环境缺少 wheel 时明确记录外部未验证，不以 import stub 或仿真截图替代。
- [ ] 通过真实 Maya TTY 验证 render、keyboard、paste、picker/modal、copy、fold/scroll、resize/focus、live SSE 多轮和退出清理。
- [ ] 能力矩阵中每个由现有 API 可实现的核心工作流都有真实交互证据；后端未支持项有具体、可核查的 contract blocker。

## 8. 阶段 E：最终审计与交付

- [ ] 重跑 Core、逐插件 focused suites、queued fold-in、完整 HTTP 和完整主线 TUI；Maya 独立报告。
- [ ] 对 streaming、tool-call、permission/interaction、reconnect 和主线 TUI 做真实 loopback/PTY smoke；真实供应商仅在凭证可用时 opt-in。
- [ ] 执行最终静态搜索，确认旧类型名、旧字段、动态注入、兼容 fallback 和反向依赖归零。
- [ ] 检查 `git diff --check`、用户/开发/API 文档和注册配置；不包含 cache、log、session、截图或生成 bundle。
- [ ] 回答 AGENTS.md 的五个提交前问题，但本轮不执行 commit。
- [ ] 输出验证报告：命令、结果、production path、真实交互观察、未运行项及原因、残余风险。任何缺少证据的项目保持未完成。

## 9. 验证命令

在仓库根目录使用 `.venv`；Core/HTTP/TUI 使用 `-s` 规避本环境 capture teardown hang：

```bash
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/core -q -s
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/integration/test_foldin_queued.py -q -s
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/integration/test_http_transport.py -q -s
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/tui -q -s
PYTHONPATH=XBotv2 .venv/bin/pytest maya-tui/tests -q -s
git diff --check
```

## 10. 每个切片的记录规则

以下是每个切片都必须满足的检查项；完成单个切片时记录证据，最终审计确认所有切片均满足后统一勾选。

- [ ] 写明要证明的用户可观察行为、对应设计小节和 owner。
- [ ] 先取得命中预期缺陷的失败测试证据；fixture 或测试自身错误不算红测。
- [ ] 只修改拥有该语义的边界，并删除被替代旧路径。
- [ ] 记录 focused 与扩大回归的准确命令和结果。
- [ ] 记录静态搜索结果，证明没有留下第二种定义或 fallback。
- [ ] 汇报新增测试真正证明的 production path、删除的旧语义、未运行验证和原因。
