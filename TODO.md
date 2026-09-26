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
- [x] 不做 WebUI 工作，也不因 WebUI 测试调整主模型；本轮范围是主要代码、Core/HTTP 运行时和主线 Textual TUI 插件。
- [x] Bench 不属于本轮验收；不能证明产品行为的 bench 不修、不迁移。
- [x] 不提交代码；保留用户现有 staged/unstaged 修改，不重置、不覆盖无关工作。
- [x] 不通过删除有效功能测试取得绿灯；只删除锁定已删除语义、私有结构或完全重复证据的测试。
- [x] 真实供应商网络 smoke 仅在凭证和网络可用时 opt-in；未运行时记录原因。真实 SDK 类型、adapter contract 和本地真实 server 路径仍是必需证据。
- [x] Browser 安全验收限定为已声明的 policy、关键协议互操作和资源清理；无界远程/攻击组合矩阵另列安全 backlog，不阻塞本轮。

## 3. 当前权威基线

以下结果只说明对应工作树当时经过了这些套件，不替代下面的逐项退出条件。

- [x] 历史扩大回归证据：状态栏改动前 Core+TUI `1296 passed in 149.42s`、HTTP transport `101 passed in 78.30s`。本轮前的 Core `532 passed in 59.99s`、TUI `837 passed in 113.55s` 与 Browser `36 passed in 12.74s` 仅作为历史证据；旧 `829/834/835 passed` 均为更早结果。Settings CSS 和 transcript 后续已通过当前完整 TUI 回归，见下项。
- [x] 2026-09-26 Claude Code 基础显示与 Think 自适应高度改造后，主线 TUI 完整套件 `846 passed in 117.77s`（`-s`，含真实 loopback/tmux PTY）；覆盖无顶部 session bar、`❯`/`●` transcript、固定 composer、单行 status/footer、Think/tool 折叠、三尺寸 provider-compatible PTY、permission/ask-user、paste/follow-up、Settings、resize/focus、session switch/resume 和 plugin/config。
- [x] 2026-09-26 Anthropic-compatible stream 的 `content_block_start` 可合法携带 `thinking: null` / `text: null`；旧 adapter 将 null 存入内部 str state，后续 delta 执行 `+=` 触发用户所见 `NoneType + str`。TDD 先复现准确异常，再在 provider 边界归一为空字符串；adapter 全套 `15 passed`，真实形状 HTTP/SSE→server→TUI fixture 通过，并以真实 MiniMax `xbot once` 短请求得到 `OK`。
- [x] 上述 provider/TUI 修改后的完整 Core 回归在允许本机 loopback socket 的环境中通过：`533 passed in 66.87s`；未删除 browser 或其他功能测试。沙箱内 browser 的 `PermissionError` 已识别为本机 socket 权限限制，并用沙箱外完整重跑取代该无效结果。
- [x] 2026-09-26 对照实际 Claude Code 基础界面重排 transcript：删除顶部重复抬头和 `You/Assistant` meta 面板，用户行使用低对比背景 `❯`，assistant/tool 使用 `●`，composer 使用上下分隔线与固定 `❯`，usage/context/session/model 统一在底部单行 status，快捷键独立 footer；80x24 真实 PTY 已目视检查基础层级。复杂 interaction/permission 卡片和完整复合多轮视觉验收仍未完成。
- [x] queued fold-in 当前 `10 passed in 9.27s`；Textual client host/commands/launcher/plugin focused suite 当前 `63 passed`，另有 loop-affinity/lifecycle focused rerun `7 passed`。
- [x] 最新 Stage B focused runtime/persistence 集合 `39 passed in 16.91s`：覆盖 AgentLoop input/runtime、session ownership 和 persistence；包括 tool turn 的 `TURN_END` 与 durable history 顺序、provider failure 的 `ON_ERROR` 与 durable history 顺序、owner 进程 `os._exit` 后完整回合恢复、SIGKILL 期间未完成 provider stream 的丢弃与 canonical user history 保留，以及工具副作用发生后进程崩溃时不重放该工具。
- [x] 已有真实 HTTP/TUI 证据覆盖 caption switch/resume、compact command/event/trajectory、permission 与 ask-user 回复、usage/reasoning 展示、空会话退出和首轮物化。
- [x] 真实 PTY 已覆盖多行 Unicode 长 paste 后编辑、permission/ask-user、Think 折叠、session switch、Ctrl-C 退出；另以两个同时运行的 CLI TUI 连接同一 server 的两个会话，关闭其中一个 runtime 后由第三个 CLI 从磁盘 resume 并继续下一轮，连续稳定复跑 3 次。
- [x] 2026-09-26 用户反馈后从当前 checkout 重新运行 reasoning/no-reasoning 两条 CLI→plugin→HTTP/SSE→80x24 PTY：`2 passed in 8.86s`。新 capture 在 `/tmp/xbot-current-thinking/`；reasoning fixture 显示 Think，no-reasoning fixture 仅显示临时 `Thinking…`。不能据此宣称用户配置下的 thinking 缺失已解决或整体已对齐 Claude Code。
- [x] 2026-09-26 Settings 真 PTY capture 暴露 80x24 默认按钮/OptionList 边框过高；TDD 先使 Model action 高度断言以 3 行失败，改为 compact 单行控件后，Settings/provider picker、短屏键盘可达、Esc 保留草稿、plugin catalog 应用级测试 `4 passed`。
- [x] Settings action rows、reasoning/Think 与 tool 折叠曾在真实 PTY 检查；历史 capture 根目录 `/tmp/pytest-of-shefrin/pytest-684/`。MiniMax-compatible stub 不代表真实供应商网络验证。
- [x] 2026-09-26 transcript 高度缺陷按 TDD 修复：短回复在 streaming 结束后仍带 `.expanded` 固定 12 行样式，挤出 80x24 的用户 prompt，并在 100x28 展开 Think 时裁掉 Think 标题。`test_short_streamed_reply_does_not_keep_an_expanded_window` 先以 12 行失败，再限定 `.expanded` 只应用于当前可折叠 block；block suite `18 passed`，两个真实尺寸 PTY focused `2 passed in 7.58s`，全套 `843 passed in 119.81s`。完成帧与展开帧保存在 `/tmp/pytest-of-shefrin/pytest-693/test_minimax_thinking_runs_thr0/minimax-pty-captures/`（80x24）、`/tmp/pytest-of-shefrin/pytest-693/test_minimax_thinking_runs_thr1/minimax-pty-captures/`（100x28）、`/tmp/pytest-of-shefrin/pytest-693/test_minimax_thinking_runs_thr2/minimax-pty-captures/`（120x40）。
- [x] 用户授权的一次短 MiniMax 真实请求已从生产 `xbot once` 入口执行，隔离数据目录下返回 `OK`；证明真实网络/provider completion 路径可用，但不把单次是否产生 reasoning 泛化为模型保证。
- [x] 2026-09-26 真实 80x24 CLI → Textual plugin → loopback HTTP/SSE → PTY 验证：当 fixture 明确发出 reasoning delta 时，provider `Think` block 可见并在完成后保留、不重复；最新 capture：`/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th0/thinking-pty-captures/`。这不证明普通无-reasoning 回合有 Claude Code 式的 Thinking 活动态。
- [x] 2026-09-26 无 reasoning、延迟首个正文 delta 的真实 CLI → HTTP/SSE → 80x24 PTY 复现先失败后通过：首帧显示 `✳ Thinking…`，assistant 正文开始后活动行消失；最新 capture：`/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th1/thinking-activity-pty-captures/`；focused PTY `1 passed in 5.76s`。controller 行为测试还验证活动 tool 中让位、工具结束而 turn 仍运行时恢复；该活动行不写入 timeline/history。
- [x] 独立 context-sensitive footer pilot/渲染 focused tests `6 passed`：idle/running/interaction 提示来自 composer facts、按 terminal cell width 裁切；80x24 → 100x28 后 status/footer 行位置正确且草稿和 composer focus 保留。旧 queue/jobs 同排折叠设计已更正：job 仍折叠，queued prompt 现在默认在 composer 上方可见。
- [x] 2026-09-26 Settings 样式修改前最后一次完整主线 TUI 回归 `837 passed in 113.55s`（`-s`）；含 chooser 单入口、Settings policy/plugin catalog typed 读取/显示与 80x24 schema 表单保存、默认 MiniMax provider-thinking 生产接线 PTY（80x24/100x28/120x40）、受控 provider Think 与无-reasoning Thinking activity、resize anchor/focus、usage/context/cache、用户轮次/工具步骤分隔、可见 queue/steer、paste、permission/interaction、session switch/resume 和提示去重。
- [x] MiniMax reasoning production path 在当前 CLI/Textual PTY 以 80x24、100x28、120x40 重复交互；三种尺寸显示 streaming/final Think、context、usage/status、composer/footer 且无终端 cell 溢出，`3 passed in 9.85s`，capture `/tmp/xbot-minimax-layout/`。这不替代 Claude Code 并排整体布局验收。
- [x] 2026-09-26 查清并修正默认 MiniMax-M3 Think block 缺失的上游缺陷：`thinking: adaptive` 被 `model_mode` 混成通用 reasoning effort，Anthropic adapter 随后发送无效的 `reasoning_effort=adaptive`；`_provider_arguments` 又漏掉模型 `thinking` 配置。官方 API 要求 `thinking: {type: adaptive}`。先以请求级断言复现，再修正模式投影与 request extras；LLM adapter/config focused `36 passed in 0.83s`，完整 Core `532 passed in 59.99s`，MiniMax-compatible CLI/Textual provider PTY 覆盖 80x24/100x28/120x40，样式修复前完整 TUI `837 passed in 113.55s`。多尺寸 capture 在 `/tmp/xbot-minimax-layout/`；未访问真实 MiniMax 服务。
- [x] 在上述完整回归后，扩展的 80x24 streamed-thinking → follow-up-turn PTY 场景单独重跑通过 `1 passed in 4.31s`；这次只扩展测试场景，生产代码与上面的全套回归代码相同。
- [x] 2026-09-26 更正 Settings API 判断：公开 client policy-read 已接入 Permissions/Sandbox 页面，pilot 覆盖读取和 typed 权限/沙箱展示；`/status` 保持只读，并断言不请求 provider/agent/policy/plugin config。当前 policy 仍只读；Plugins 已有 producer-schema 支持字段表单、scope/revision typed patch 和 80x24 pilot 保存，仅覆盖可安全映射的标量 schema；conflict 重载/再确认、所有 scopes 的真实 server 写回和 Appearance 仍未完成，不能将 Settings 整体标完成。
- [x] Thinking 真实 PTY 检查后，TDD 删除 composer placeholder/footer 重复的 Enter/queue/steer 提示；34 项 composer/footer/status/policy Settings focused tests 通过，loopback PTY `1 passed in 5.72s`；完整 TUI 回归后的 capture `/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th1/thinking-activity-pty-captures/`。该局部排版修正不代表整体 Claude Code 布局验收完成。
- [x] 更正 Plugins 页错误文案：公开 plugin-config catalog/update API 已存在；只读 catalog 和可支持标量字段的 schema-driven editor 已接入；scope/revision typed patch 在 80x24 pilot 走通。conflict/revision 变化后的保草稿、显式复核与真实 server 写回仍未闭环。
- [x] 已按 TDD 接入公开 plugin-config catalog read；Settings 并发读取 workspace scope，Plugins 页面展示 scope/workspace/applicability/producer 声明的 plugin 名称、id、editable 标志和 schema 字段名，不回显 config raw values。后续加入可支持标量字段的 schema-driven 表单；Apply 固定在滚动内容外，80x24 pilot 验证可达并保存仅变化字段，携带 catalog revision。嵌套/复杂 schema 明确不可编辑；conflict 处理及真实 server 写回仍待验证。
- [x] provider reasoning 仍只由 provider reasoning event 驱动；没有 event 时不伪造 reasoning 文本。
- [x] 从 authoritative running facts + timeline phase 投影独立、瞬态的 `✳ Thinking…` 活动态：无 reasoning payload 时显示，assistant/活动 tool 开始后让位，完成 tool 且 turn 仍运行时恢复，turn 结束后消失；不进入 history，不新增协议事件/数据字段。80x24 无-reasoning 慢响应真实 PTY 与 controller/view 行为测试通过。
- [x] 本环境异步测试在 pytest capture 下可能卡在 default-executor teardown；`-s` 不跳过测试或改变断言，Core/HTTP/TUI 扩大回归统一使用 `-s`。
- [x] WebUI 未运行且不在范围内；真实供应商网络 smoke 尚未运行。用户已授权一次短 MiniMax 请求，但执行路径当前受限；不能据本地 SDK adapter 测试宣称网络互操作。

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

当前推进顺序（用户纠偏，优先于本阶段其他未完成项）：

- [x] 复核当前受控 PTY 事实：有 reasoning event 时真实 Think block 可见；无 reasoning 数据时只显示 Thinking activity。loopback 绑定需在受限 sandbox 外批准的测试执行中完成，验证仅监听本机临时端口。
- [x] 查实 MiniMax provider 请求层 Think 缺失根因并按 TDD 修正；adapter 测试检查 wire 参数与 reasoning stream。已运行授权的真实短请求并得到 `OK`；nullable block-start 崩溃也由 adapter、真实形状 SSE 和生产 smoke 分层验证。
- [ ] 当前第一优先级是端到端对话流程和 transcript，而不是配置入口：attach/resume → composer 编辑/多行 paste → Enter 发送 → user turn 可见 → Think/stream → tool/permission → final → follow-up → scroll/resize/session switch；逐帧检查布局层级、滚动/焦点、顺序和持久历史。
- [x] Claude Code 基础单列结构第一轮已落地：无常驻顶部抬头；`❯` user、`●` assistant/tool；固定双分隔线 composer；单行底部 runtime/usage/context/session/model；独立快捷键 footer。80x24 真 PTY 已检查，复杂 interaction/permission、长会话和动态 resize 仍作为未完成产品验收项。
- [x] 队列中待发送的 prompt 默认显示在 composer 上方，逐条保留服务端内容和 target，最高 4 行后可滚动；jobs 可保持紧凑摘要。80x24 真实 PTY 同时显示 queued content、next-turn/next-step 和 `queued:2`，capture：`/tmp/xbot-current-queue/test_real_cli_enter_queues_dur0/queue-pty-captures/queue-and-steer.txt`。
- [x] Think block 行为已按 TDD 修正：reasoning stream 完成后默认折叠；stream 中手动折叠/展开会持续生效；显式展开高度为 `min(标题 + 实际内容, 12)`，仅长内容块内滚动；普通 assistant 回复不会随 Think policy 自动折叠。一行和五行 Think 的测试先以实际高度 12 失败，再分别固定为 2/6 行；长块仍受 12 行上限约束。
- [x] Think 自适应高度已进入真实 CLI→server→tmux PTY 验收：80x24、100x28、120x40 均显示标题下一行的单行 reasoning，下一行立即进入 assistant 回复；`3 passed in 11.27s`，capture 位于 `/tmp/pytest-of-shefrin/pytest-704/test_minimax_thinking_runs_thr*/minimax-pty-captures/`。
- [x] MiniMax-compatible production PTY 的完成帧 prompt、折叠标记和快捷键提示断言在 80x24/100x28/120x40 通过；最新 capture 见上一条。本机 stub 不代表真实供应商网络互操作。
- [x] 真实复合 PTY 覆盖 permission→ask-user→长 paste 编辑→follow-up→session switch/back；capture `/tmp/pytest-of-shefrin/pytest-693/test_real_cli_tui_pty_complete0/pty-captures/follow-up.txt`。compact 与 reconnect/resume 尚未并入这一完整交互路径，仍开放。
- [ ] Settings 与 `/status`/`/config` 入口布局、字段作用域、真实 mutation、保存冲突和返回 composer 体验列为次级工作；schema editor 的 Apply 已有 80x24 pilot 证据，冲突与更多页面未闭环，但不抢占当前对话流程/layout/transcript。
- [ ] 上述主线仍须由同一真实 CLI/plugin + server + tmux PTY 复合多轮交互覆盖 compact、session switch/reconnect、resume 与退出；真实 provider Think 和整体 Claude Code 结构/视觉对照也仍待完成。不以分散场景或历史 capture 替代。

- [x] provider terminal validation 错误已修复；OpenAI/Anthropic adapter 以具名 `response=` 构造 typed terminal event。
- [x] picker index 按实际选项数有界 wrap；超过元素数的 down 后不产生虚假位置。
- [x] 多行 Unicode paste、尾换行和 paste 后编辑只提交一条原样 input；composer 有固定高度上限。
- [x] `/approve`、`/deny`、`/answer` 使用标准 HTTP endpoint，不伪装成普通 user message。
- [x] Thinking 与 Tool block 可折叠；展开按实际内容增长，最多 12 行后在只读块内滚动，默认折叠隐藏 payload。
- [x] 删除重复的顶部 SessionBar；底部单一 StatusBar 显示 activity、累计 in/out/cache、context/window、session/thread、provider/model，再按优先级容纳 agent/mode/slots/cwd；footer 只显示操作提示，统计只有一个 view projection。
- [x] caption 在首轮/多轮、failure retry、已有标题、session switch 和 resume 后进入底部 status 的 session 段。
- [x] 空会话 Ctrl-C/HTTP close 不创建 session/catalog；首个有效 input 后才物化；SSE task cancel 会等待 reader 并关闭 generator。
- [x] Compact 真实 server/TUI 五轮后执行 `/compact`，completion event、summary render 和 `SurfaceReplaced` trajectory 一致。
- [x] 长多行 composer 改变布局后，提交动作明确返回 transcript 实时尾部；真实 PTY 第二轮回复不再已持久化却落在视口外。
- [x] 两个真实 CLI TUI 同时连接同一 server 的不同 session，消息、usage、turn 与持久历史互不串线；其中一个关闭 runtime 后由新 CLI 从磁盘 resume、恢复历史和 `turn:1`，再完成 `turn:2`，另一个连接全程保持可用。
- [x] 历史 PTY 验证 streaming reasoning delta → completed record 时 Think block 不重复；当前控件语义更正为完成后默认折叠，流式期间的手动展开/折叠保留。完成帧 prompt 可见性已有 reducer→Textual viewport 行为断言。
- [ ] 折叠选择的生命周期仍需在真实多轮 PTY 中确认，特别是 entry update、session switch 与 reconnect；不能在未验证前宣称其跨会话恢复或持久化。
- [x] Jobs 以一行 disclosure 保持紧凑；queue 改为直接显示 queued prompt，限高 4 行后可滚动，不把可见性藏在计数后。
- [x] 真实 80x24 CLI → HTTP/SSE → server 验证运行中 Enter 将后续输入排为 `next-turn`，Alt+S 将输入作为 `next-step` steer；正文、target 和 status `queued:n` 同屏可见，pending inputs 由公开 API 核对。当前 capture：`/tmp/xbot-current-queue/test_real_cli_enter_queues_dur0/queue-pty-captures/queue-and-steer.txt`。
- [x] 状态栏 optional detail 顺序符合 activity → subagent/queue → usage/cache → agent/mode → generic slots → cwd；累计宽度统一按 terminal cell width 计算，Unicode 溢出回归覆盖。status-bar suite `70 passed`；Settings 样式变更前完整 TUI suite `837 passed in 113.55s`。
- [x] 视图直接按已有 timeline role 投影低噪声标记：UserEntry 为背景行 `❯`，Assistant/Tool 为 `●`；不增加 turn ID 或持久化语义，也不再用整宽粗/虚分隔线吞噬 transcript 空间。
- [x] 2026-09-26 80×24 → 50×28 的 Textual app resize pilot：非尾部阅读时窄宽 reflow 保持同一可见 timeline entry 和非尾随状态，且 composer 草稿/focus 保留；修复前 `m3` 漂到 `m2`，focused 用例通过。真实 TTY resize、Unicode/超长内容滚动仍待验收。
- [x] Permission 与 user-input 已使用 reducer `pending_interactions` 和现有 typed response API 呈现小型 chooser/回答框；snapshot attach 可重建未完成 chooser，外部 resolution 关闭陈旧弹窗并恢复 composer focus。真实 server allow/deny/answer 与真实 tmux permission→question→final→follow-up 已通过。
- [x] Subagent 查看按 Claude Code/Codex 的共同模式落地：主界面 jobs 保持紧凑活动摘要，`Ctrl+T` 从公开 thread catalog 切换独立 transcript；`ThreadSummary.kind=subagent` 唯一决定只读，`Esc` 返回 main，草稿与焦点保留，不从 job label 猜 thread id。
- [x] 真实 server 生产路径实际执行 `spawn_subagent`/`wait_subagent`，TUI 查看 child transcript、返回 main，并在关闭首个 TUI 后重新 attach 同一 session 再次查看 child。该测试发现 transport 错把 child 交给 `open_session`；现已改走既有 typed `open_thread`，focused real server `1 passed`。
- [x] 真实 CLI/tmux 100×28 已完成 subagent thread 切换与进程级 resume；child 为独立只读 transcript，状态栏显示 `subagent:reviewer-*`，footer 显示 `Esc main`。真实帧同时发现并移除 model-facing runtime XML/`job_completed` JSON 的 transcript 泄漏；typed jobs state 仍由既有事件和 jobs panel 呈现，不解析内部 prompt 文本。capture：`/tmp/pytest-of-shefrin/pytest-727/test_real_cli_subagent_thread_0/subagent-pty-captures/`。
- [x] 本轮最终完整 TUI suite `856 passed in 120.27s`，包含真实 uvicorn/HTTP/SSE 和 tmux；不是仅凭 scripted backend 判定完成。
- [x] Attach/session switch/thread switch 通过公开 `list_jobs` authoritative replace reducer task state；真实双 TUI 连接同一主线程时，后连接客户端无需等新 SSE 即可显示已运行的 subagent 和 `1 task running`，切换后不保留旧 thread jobs。
- [x] Context observation 超过声明 window 时不 clamp 或显示成正常比例；紧凑 status 使用红色 `ctx:~5.7k/4096!` 并保留精确 window，完整 Status 页写明 `over`。100 列仍同时保留 caption，真实 caption/retry `2 passed`。
- [x] Active jobs hydration、精确 context overflow、caption 共存后的完整 TUI suite `862 passed in 121.44s`；包含真实双客户端、uvicorn/HTTP/SSE 与 tmux。最新 capture：`/tmp/pytest-of-shefrin/pytest-736/test_real_cli_subagent_thread_0/subagent-pty-captures/`。
- [x] Transcript 密度与 block ownership 已修正：entry/收起 tool 不再产生多余空行；final assistant reply 始终是普通 transcript，不可折叠；Think/Tool details 保持固定上限窗口。Tool call 现在有两个独立 disclosure：`● tool(key: value…)` 展开 args/params，`⎿ Done/Failed` 展开 result；shell/edit/web_search/read 均由通用参数投影回答“做什么”，无工具名分支。
- [x] History/resume 的工具参数遵守 canonical model：不扩展 `ToolRecord.call=ToolCallRef`；TUI ToolEntry 保留计划已有语义中的独立 `call_id`，页内关联 `AssistantRecord.tool_calls`，assistant/tool 被分页边界拆开时在加载上一页后原位补齐 args；live 使用既有 `tool_calls_started`。分页/reducer/view focused `167 passed`。
- [x] 工具执行不再因 call-id→record-id 删除/追加而跳位闪烁：Timeline 原位替换 identity，TranscriptView 复用同一 mounted widget；streamed assistant final 同样保持原顺序与控件身份。
- [x] Permission chooser 按 Esc 走现有 typed deny API；不再仅 dismiss 后持续显示 `Approval required`。`send_message` 工具描述已禁止代替主会话 canonical final reply。
- [x] `/help [command]` 已按动态 local+server command catalog 实现单命令详情；name/slash/alias 共用执行解析语义，description/usage/parameters/examples 只来自公开 `CommandDescription`。TDD 红测 4 项后 app/registry `140 passed`，真实 HTTP/server catalog 的 `/help undo` `1 passed`，完整 TUI（含 loopback/PTY）`876 passed in 139.33s`。
- [x] 在 80x24 与长会话/scroll/resize 下验证分隔密度、可读性和 anchor 稳定性：手动上滚后的连续 tail update 保持同一可见 entry 与屏幕行偏移；长 paste 后第二轮自动跟随 final；permission/question 卡片在 80×24 保持完整边框。capture：`/tmp/pytest-of-shefrin/pytest-758/test_real_cli_tui_pty_complete0/pty-captures/`。
- [ ] 按 `TEXTUAL_TUI_PLAN.md` 完成 Claude Code/Codex 风格单列对话布局：基础 transcript/composer/单行 status/footer 与 typed interaction 控件已完成；下一步是 subagent/resume 的真实 tmux render、长会话密度和同一真实路径的动态 resize/compact/reconnect/resume。完整布局须保持 80x24 可用。
- [x] 状态栏只由 reducer facts 与公开 snapshot/event/API projection 驱动；activity → subagent/queue → usage/cache → context → session/thread → provider/model → agent/mode → generic slots → cwd 的优先级、窄屏裁切、Unicode cell-width、active jobs attach hydration 与 context overflow 均有行为测试和真实路径证据。
- [ ] Settings overlay 覆盖只读 Status、Model、Permissions、Sandbox、schema-driven Plugins 和 Appearance；所有服务端值走现有公开 API，冲突保留草稿，未持久化的外观选项明确标为本次运行。
- [ ] 状态栏/Settings/布局通过 80x24、100x28、动态 resize 的 pilot 和真 PTY 验收；不得只以快照断言或单元测试代替交互观察。
- [ ] 覆盖 resize、长内容、stream update、session switch、reconnect、interrupt 后的 block/focus/scroll 状态。
- [ ] 完成 caption 的真实 reconnect 后恢复，并确认 session picker 与底部 status 使用同一服务端 title projection。
- [ ] 验证 TUI 异常进程退出、打开已有会话后退出和 reconnect 时旧订阅关闭；进程锁仍归 SessionManager/server，不由 TUI 假释放。
- [ ] 用 Textual pilot/render snapshot 检查结构；快照只辅助定位，不能替代真实交互。
- [ ] 启动真实 server 与真实 TUI，实际完成一条复合多轮路径：普通回复 → streaming Thinking → tool → permission/interaction → 后续追问 → compact → session switch/reconnect → 退出。Thinking 前置渲染已有独立真实 PTY 证据；本复合路径尚未完成。
- [ ] 在上述真实会话中亲自操作键盘、长多行 paste 后编辑、picker、复制、Think/Tool 折叠与滚动，并核对 canonical history/event。
- [ ] 保存不提交的本地截图或 terminal render capture：初始页、paste、streaming Think、Tool 折叠/展开、turn 分隔、caption、usage/context/cache 和 reconnect 恢复。
- [ ] 退出后检查磁盘、进程、task、subscription 和 session lock，无空会话或资源残留。

- [x] 当前切片完整 TUI suite `870 passed in 138.15s`，包含真实 uvicorn/HTTP/SSE 与 tmux；最后的显示语法修正后 focused `66 passed in 2.29s`；同一工作树完整 Core `533 passed in 60.50s`；`git diff --check` 通过。尚未因此勾选 compact/reconnect/resume 合并为同一复合流程、退出资源审计等更大退出条件。
- [x] 真实 CLI/tmux 已连续完成 5 轮、手动 compact、退出 runtime、新进程 disk resume 和第 6 轮；核对 canonical history 与 `SurfaceReplaced` trajectory。该路径发现并修复 compact 后 lifetime turn 从 5 回退 4：现从 append-only HumanInput trajectory 恢复，不新增第二份 persisted counter。
- [x] Compaction summary ownership 已纠正：canonical/persisted summary 为纯文本，provider 专用 `<historical_context>` 只在 context compiler 构建请求时产生；TUI 不解析内部 XML，真实 compact 帧仅显示可读 summary。capture：`/tmp/pytest-of-shefrin/pytest-768/test_real_cli_compaction_survi0/compact-resume-pty-captures/`。
- [x] lifetime turn 修复后的完整 Core `533 passed in 68.82s`；新增 compact/resume 路径后的完整 TUI `871 passed in 137.80s`；其后的 summary ownership 修正通过直接受影响的 Core `37 passed` 与真实 PTY `2 passed`。

阶段 C 退出条件：

- [ ] TUI 全套通过；真实可控 server 完成复合多轮；render evidence 确认信息层级、固定高度、滚动、caption 和统计确实可见；退出资源检查通过。

## 8. 阶段 D：最终审计与交付

- [ ] 重跑 Core、逐插件 focused suites、queued fold-in、完整 HTTP 和完整主线 TUI。
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
