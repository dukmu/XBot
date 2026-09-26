# Textual TUI 插件化重建方案

## 当前优先级纠偏（2026-09-26）

本节高于后续低优先级功能切片；不能把历史局部通过等同于整体完成。当前主线是用户实际走过的多轮对话流程、单列布局和 transcript，不是 `/status` / `/config` 入口收敛。

- [x] 本次复核确认：Think block 只有 provider 实际产生 reasoning delta 时才显示正文；无 reasoning 的慢回合只有 transient `✳ Thinking…`。MiniMax 请求层漏传 `thinking: adaptive` 已修复，并已完成 compatible-provider PTY 和一次真实 MiniMax 短请求（返回 `OK`）。
- [x] 已核对 Claude Code 官方交互、status line 与 Settings 文档；该参考保留为后续设置入口设计依据，不代表 `/status` 与 `/config` 当前高优先级，也不阻塞 transcript/布局主线。
- [ ] 当前主线按完整用户流程验收：attach/resume → composer 编辑/paste → Enter 提交 → user turn 留在 transcript → Thinking/Think 与 stream → tool/permission → final → follow-up → scroll/resize/session switch。每个转场同时核对可见顺序、焦点/滚动和服务端 canonical history；复合 PTY 验收仍未完成。
- [x] TDD 修复 Think 块高度：完成后默认收起，流式中可手动折叠/展开且用户选择持续有效；展开高度为 `min(标题 + 实际内容高度, 12)`，仅超出上限时成为块内滚动窗口，不再把一行 Think 强制撑成 12 行。
- [x] 短 streamed reply 高度缺陷经 TDD 修复：普通回复在 streaming 时短于 12 行、不属于可折叠 block，却错误保留 `.expanded` 固定高度；它撑满窄 transcript 并挤走用户消息。新增行为测试先观察到高度 `12`（预期 `1`），再将 expanded CSS 限定为当前确实可折叠的 block。block suite `18 passed`，80x24/100x28 focused real PTY `2 passed in 7.58s`。
- [x] 新旧 transcript 检查涵盖 canonical completion 复用同一 widget、Think 完成后折叠、短回复不保留空白窗口；真实 80x24 完成帧保留用户 prompt，100x28 展开 Think 后标题和 final 同屏。captures：`/tmp/pytest-of-shefrin/pytest-693/test_minimax_thinking_runs_thr0/minimax-pty-captures/`（80x24）、`/tmp/pytest-of-shefrin/pytest-693/test_minimax_thinking_runs_thr1/minimax-pty-captures/`（100x28）、`/tmp/pytest-of-shefrin/pytest-693/test_minimax_thinking_runs_thr2/minimax-pty-captures/`（120x40）。
- [x] MiniMax-compatible CLI→server→HTTP/SSE→PTY 在 80x24/100x28/120x40 检查 streaming/final Think、prompt、折叠快捷键和 cell 宽度；仅本地兼容 upstream，不代表真实 MiniMax 网络互操作。
- [x] 从真实 permission→ask-user→reply 与 streaming Think→follow-up PTY 帧发现 Think 双竖线和折叠 tool 半截参数 JSON；去掉 reasoning 内嵌竖线，tool 默认折叠时隐藏 payload，展开后在固定高度窗口完整浏览。focused PTY `5 passed in 17.29s`，capture 根目录 `/tmp/pytest-of-shefrin/pytest-684/`。
- [x] 按真实 80x24 Settings 截图新增紧凑行回归：修复前 Model actions 为 3 行高，导航 OptionList 有整块默认边框；修复后 actions 使用单行 compact Button，导航取消默认边框。应用级 Settings/provider picker、短屏键盘可达、Esc 保留草稿、插件 catalog 共 `4 passed`。
- [x] 当前 Settings compact action rows、Think 折叠与 tool/permission transcript 已由真实 CLI → server → tmux PTY 重新运行并保存 capture；Settings row glyph 检查按 action row 限定，避免将 dialog 导航边框误判成按钮边框。
- [x] 用户授权的一次短 MiniMax provider 请求已从真实 `xbot once` 生产入口执行，隔离数据目录下提示 `Reply with exactly OK.` 返回 `OK`；这证明当前真实 provider stream 可完成，不单独证明每次请求都会返回 reasoning block。
- [x] 重新从当前工作树运行真实 CLI → Textual plugin → loopback HTTP/SSE → 80x24 PTY 的 reasoning 与 no-reasoning 场景：`2 passed in 8.86s`。本次新产物在 `/tmp/xbot-current-thinking/`，不是旧 PNG 或旧 worktree 截图。
- [x] 修复实际 Settings 可达性 bug：Plugins `Apply changes` 移出滚动表单，固定在页面底部操作区；保存 action 直接携带公开 `PatchPluginConfig`，避免 scope/revision/config 平行字段。80x24 pilot 证明按钮可达并只保存变更字段；schema/config focused tests `4 passed`。
- [x] Settings 样式修正前完整 TUI suite：`837 passed in 113.55s`（`-s`，包含 loopback/PTY），作为历史基线。
- [x] Claude Code 基础显示与 Think 自适应高度改造后的完整 TUI suite `846 passed in 117.77s`（`-s`）；包括 loopback server 与 tmux PTY、interaction/paste/follow-up/session switch、Thinking/Think、80x24/100x28/120x40、Settings 和 resize 行为。
- [x] Think 展开高度的真实生产路径在 80x24、100x28、120x40 均验证为标题 + 单行 reasoning 后立即显示 assistant 回复，而不是预留 12 行；focused `3 passed in 11.27s`，capture `/tmp/pytest-of-shefrin/pytest-704/test_minimax_thinking_runs_thr*/minimax-pty-captures/`。
- [x] 同一工作树完整 Core 回归 `533 passed in 66.87s`；包括 Anthropic nullable block-start、provider/config、client host、protocol、persistence 和 browser 真实本机 HTTP 路径。
- [x] 当前代码在 producer 实际发送 reasoning delta 时确实渲染独立、可折叠的 `Think` block；无 reasoning delta 的慢回合只显示 transient `✳ Thinking…`，不会伪造思考正文。这只证明两条受控 fixture 路径，不足以说明用户所见的真实 provider 行为已解决。
- [x] 明确当前产品验收未通过：80x24 帧只证明了局部控件和行为，未证明整体接近 Claude Code；用户反馈的 thinking 缺失/视觉不对齐仍是开放缺陷，不能用受控 fixture 的存在将其关闭。
- [x] 查实默认 MiniMax-M3 provider 的请求配置缺陷：`thinking: adaptive` 被 `ModelConfig.model_mode` 错误并入通用 reasoning effort，`AnthropicProvider` 因此发 `reasoning_effort=adaptive`；同时构造 adapter 参数时完全漏传 `ModelConfig.thinking`。MiniMax 官方 [Anthropic API](https://platform.minimax.io/docs/api-reference/text-anthropic-api#thinking-control) 说明省略 `thinking` 即不返回 thinking blocks，需要 `thinking: {type: adaptive}`。`_provider_arguments` 现在把该模型参数归入现有 `extra_body`，`model_mode` 只投影真正的 reasoning effort。
- [x] TDD 请求/stream 回归先复现缺失 `thinking` 字段后通过；LLM adapter/config focused suite `36 passed in 0.83s`。真实 XBot server + production Anthropic SDK adapter + 本机 MiniMax-compatible SSE upstream + CLI/Textual PTY 在 80x24/100x28/120x40 均通过；Settings 样式变更前完整 TUI `837 passed in 113.55s`。多尺寸 capture `/tmp/xbot-minimax-layout/`。验证 wire `thinking.type=adaptive`、无误发 `reasoning_effort`、streaming Think 和完成态 Think；无真实 MiniMax API 调用。
- [x] 已用真实 MiniMax 凭证运行一次最短生产 smoke，模型返回 `OK`；同时以真实形状的 nullable Anthropic block-start fixture 覆盖 SDK→adapter→server→TUI。该 smoke 证明网络和完成链路，不把单次是否产生 reasoning 泛化为模型保证。
- [x] 默认 MiniMax-compatible 生产路径在真实 CLI/Textual PTY 的 80x24、100x28、120x40 尺寸复验：三种尺寸都有 streaming Think、完成态 Think、context/status、底部 composer/footer；列宽按 terminal cells 校验，focused `3 passed in 9.85s`，capture `/tmp/xbot-minimax-layout/`。这是多尺寸功能/溢出证据，不等同于 Claude Code 并排视觉对齐验收。
- [x] 第一轮基础结构对照已完成：移除 transcript 顶部永久 session bar 和 `You/Assistant` 面板标题；用户行改为低对比背景 `❯`，assistant/tool 使用 `●`；composer 为上下分隔线内的固定 `❯` 输入区；运行统计集中到底部单行 status，快捷键独立一行。真实 80x24 PTY 已检查 prompt→Think→reply→composer→status/footer 的层级。
- [ ] 继续以 80x24、100x28、宽屏真实 PTY 逐屏验收复杂 transcript：permission/interaction 已从日志式 Notice 演进为 typed chooser；长会话、动态 resize、compact/reconnect/resume 仍需同一复合路径。当前基础结构改造不等于整体产品对齐完成。
- [x] 队列内容默认直接显示在 composer 上方（多条时逐条列出并限高滚动），而不是只显示可折叠计数；80x24 当前生产 CLI→loopback HTTP/SSE→tmux PTY 已看到 prompt 与 next-turn/next-step target。
- [x] 基础 transcript/layout 已先用失败行为测试固定 `❯`/`●`、无 role meta、无顶部 session bar、单行底部统计、composer 分隔与提示分层，再由真实 CLI→server→PTY 验证；复杂 interaction/permission/queue 仍按下一项继续 TDD。
- [x] Permission 与有限选项 user-input 已改为 typed 小型 chooser：请求仍来自 reducer 的 `pending_interactions`，选择直接调用现有 permission/user-input response API，不构造消息或 slash command；开放问题使用独立文本回答框。外部 resolution 会关闭陈旧弹窗并恢复 composer focus。真实 server 三路径 `3 passed`，真实 tmux permission→question→final→paste→follow-up `1 passed`；改造后的完整 TUI `849 passed in 110.71s`。
- [x] 已按官方产品证据收敛 subagent 设计，而非自行发明：Codex 将 active subagents 放在 composer 上方并允许打开独立 agent thread，CLI 用 `/agent` 在 agent threads 间切换；Claude Code 的 background subagent panel 位于输入区附近，`/tasks` 选中后 Enter 打开 transcript。XBot 采用二者共同模式“主线程紧凑活动 + 可打开独立 thread”，但依据现有 `ThreadSummary.kind=subagent` 将 child transcript 保持只读，不复制 Claude agent-team 的直接发消息语义。参考 [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[Codex developer commands](https://learn.chatgpt.com/docs/developer-commands)、[Claude Code subagents](https://code.claude.com/docs/en/sub-agents)。
- [x] Subagent 查看机制：`Ctrl+T` 从公开 thread catalog 打开统一 picker；选择 `subagent` thread 后由 server thread kind 驱动 read-only composer/status；`Esc` 返回 main，保留 parent draft并恢复 focus。生产测试实际执行 `spawn_subagent`/`wait_subagent`，打开 child transcript 后重新 attach 同一 session 仍可查看 child；focused real server `1 passed`。
- [x] 真实测试发现并修复 child inspection 的 API 路由错误：transport 原来把 child 当主线程调用 `open_session`，导致持久化的 subagent agent 被拒绝；现在仅依据公开 `ThreadSummary.kind` 走既有 typed `open_thread` endpoint，不修改协议、不增加 fallback，也不用 `JobView.label` 猜 child thread id。
- [x] Subagent/resume 已纳入真实 CLI/tmux render：100×28 中 parent 完成后 `Ctrl+T` 打开 child，显示独立 transcript、read-only composer、`subagent:reviewer-*` 状态和 `Esc main`；关闭 TUI、关闭 runtime、启动新 CLI 后 parent 与 child 均恢复。capture：`/tmp/pytest-of-shefrin/pytest-727/test_real_cli_subagent_thread_0/subagent-pty-captures/`。
- [x] 真实帧暴露并修复 transcript 泄漏：model-facing `RuntimeNoticeRecord` 曾把 `<runtime_event>` 与 `job_completed` JSON 原样显示，并与 typed job state 重复；现不再把内部输入投影成对话 turn，live job 状态仍由既有 `JobUpdated`/`JobCompletionNotice` 和 jobs panel 展示，不解析内部 XML。
- [x] 上述 subagent、resume、typed interaction 与 transcript 清理后的完整 TUI suite：`856 passed in 120.27s`（含真实 uvicorn/HTTP/SSE 与 tmux）。
- [x] Active subagent 状态不再只依赖“这个 TUI 恰好看过”的 SSE：每次 attach/session switch/thread switch 都从既有公开 `list_jobs` API authoritative replace reducer jobs。真实双客户端场景中，第一个 TUI 启动仍在运行的 child，第二个 TUI attach 后立即显示 Tasks 与 `1 task running`；切换 thread 的测试证明旧任务不会泄漏。
- [x] 状态栏对超出 context window 的 authoritative/estimated observation 不 clamp、不伪装为正常比例：紧凑栏用红色 `ctx:~5.7k/4096!`，保留精确 window；完整 Status 页明确写出 `over`。该紧凑表示同时保住 100 列 caption；真实 caption/retry `2 passed`。
- [x] Active jobs hydration、精确 context overflow、caption 共存后的完整 TUI suite：`862 passed in 121.44s`，包含真实双客户端、uvicorn/HTTP/SSE 与 tmux。最新 subagent/status capture：`/tmp/pytest-of-shefrin/pytest-736/test_real_cli_subagent_thread_0/subagent-pty-captures/`。
- [x] Transcript 密度与 block 语义修正：entry 不再逐项强加空白行；收起且零 preview 的 tool details 不再挂一行空 body；final assistant reply 永不进入折叠体系，只有 Think 与 Tool details 使用固定高度可折叠窗口。
- [x] Tool 展示拆成两个独立 disclosure：`● tool(key: value…)` 本身展开完整 args/params，`⎿ Running/Done/Failed · duration` 本身展开 result；`shell(command)`、`edit(path)`、`web_search(query)`、`read(path)` 都由通用参数投影生成，不按工具名分支。running call 切换 canonical ToolRecord 时 timeline 原位替换 identity，复用同一 EntryWidget/ClampedBlock，不再删除、跳尾和重建闪烁。
- [x] Resume/history 不向 `ToolRecord.call` 追加违反计划的 args 字段：按 `data-model-redesign.md`，ToolRecord 保持 `ToolCallRef(id,name)`；TUI ToolEntry 明确保留独立 `call_id`，页内以其关联 `AssistantRecord.tool_calls`，若 assistant/tool 恰被 history page 边界拆开，则加载上一页后原位补齐同一工具行参数。live 路径继续使用 `tool_calls_started`。
- [x] Permission chooser 的 Esc 通过既有 typed permission response API 发送 `deny/once`，不再只关闭弹窗而遗留 `Approval required`；通用 SelectionScreen 只接收 owner 给出的 cancel value，没有硬编码 permission 业务。
- [x] 手动上滚后，连续 tail streaming 更新保持同一首个可见 EntryWidget 及其屏幕行偏移；仍在跟随尾部时，post-layout tail intent 跨刷新保留，长 paste 的高度重排不会把已持久化的第二轮 final 留在视口下方。
- [x] 真实 CLI→server→HTTP/SSE→tmux 复合路径重新验证 permission→question→final→长 bracketed paste→第二轮 final，并在 80×24 动态 resize 检查弹窗完整左右边框与 cell 宽度。最新完整套件 capture：`/tmp/pytest-of-shefrin/pytest-758/test_real_cli_tui_pty_complete0/pty-captures/`。
- [x] `send_message` Agent Tool 的公开描述明确限定为非阻塞 progress update，禁止在主会话中代替 canonical assistant final reply；TUI 不靠识别工具名修补 final 展示。
- [x] 两段 tool disclosure 修改后的完整 TUI suite：`870 passed in 138.15s`（含真实 uvicorn/HTTP/SSE、tmux、80×24 resize、长 paste、多轮 interaction）；最后的单复数显示修正后 block/entry focused `66 passed in 2.29s`。同一工作树完整 Core `533 passed in 60.50s`；`git diff --check` 通过。
- [x] 新增真实 CLI/tmux 连续路径：完成 5 轮 → `/compact` → 退出首个 TUI/runtime → 新进程从磁盘 resume → 完成第 6 轮；同时核对 persisted Human/Assistant 与 `SurfaceReplaced` trajectory。该测试先暴露 resume 状态栏从 turn 5 回退到 turn 4，修复后通过。
- [x] `turn_count` 恢复不再从 compact 后裁剪过的 model surface 重算，也不新增持久计数字段；persistence 从 append-only trajectory 的 canonical HumanInput `MessageAppended` 记录恢复 lifetime count，active ThreadSummary 读取 `SessionRuntimeState.turn_count`。
- [x] `CompactionSummaryMessage.summary` 恢复为纯 durable summary，不再持久化 model-facing `<historical_context>` XML；context compiler 在构建 provider request 时才包装，真实 TUI 的 compaction notice 只显示人类可读摘要。focused Core `37 passed`，真实 compact/PTY `2 passed`；capture：`/tmp/pytest-of-shefrin/pytest-768/test_real_cli_compaction_survi0/compact-resume-pty-captures/`。
- [x] 上述 lifetime turn 修复后的完整 Core `533 passed in 68.82s`；新增 compact/resume 测试后的完整 TUI `871 passed in 137.80s`。随后 summary ownership 修正由其直接受影响的 Core/PTY focused suites覆盖。
- [ ] 下一优先级按 Claude Code/Codex 的信息层级继续核对 status、来源明确的 permission/question 小弹窗和长会话/resume 视觉；不扩充其命令表。
- [ ] 对 `/config`/Settings 建立可用性验收：可发现的页面导航、真实数据来源和作用域、冲突处理、返回会话后的焦点与草稿。插件 schema 编辑已有 80x24 pilot 保存证据；复杂 schema、revision 冲突与更多可实现页面仍未闭环。
- [ ] `/status`、`/config` 与 Settings overlay 统一属于后续入口/配置体验；仅在多轮交互、布局和 transcript 验收推进后再做，不抢占当前主线。

本轮外部参照为 Claude Code 官方 [交互模式](https://code.claude.com/docs/en/interactive-mode)、[status line](https://code.claude.com/docs/en/statusline) 和 [settings](https://code.claude.com/docs/en/settings) 文档。文档确认 `/status` 与 `/config` 复用同一 dialog、只是初始页不同；`/config` 是精简偏好入口。仓库环境没有安装 `claude` 命令，且没有同场景并排 TTY capture，因此不能声称像素级或实机对照完成。目标是落实其对话、输入、状态、快捷键和配置的产品层级，同时具体字段与 mutation 继续受 XBot 公开模型/API 限制；不移植 Claude 专属 status-line shell 脚本、cost、Git 或 rate-limit 字段。

## 1. 目标与约束

本方案的目标是在现有 Textual TUI 基础上完成真正的客户端插件化，并以 Claude Code 的核心终端交互体验作为参考。实现必须遵守以下边界：

- TUI 只通过公开 API、SSE 事件、协议模型和 XCore 服务连接系统。
- 不为 TUI 修改服务端/session/Agent/plugin 的业务职责。
- 不硬编码插件 id、服务端命令目录、工具名业务语义或插件私有数据。
- 不保留 Maya fallback、shim、兼容 import、双实现开关或迁移适配层。
- 不借机重构无关模块。
- 状态、协议、transport、controller、view 和 Textual DOM 的 ownership 必须明确且可机械检查。
- 测试是证据，不以“测试绿”替代真实生产链路和 PTY 验收。

## 2. 已确认的仓库事实

- [x] 当前权威提交 `ba5a2ad` 已落盘；该提交中的 `XBotv2/tui/**` 和 `XBotv2/tests/tui/**` 是 Textual 恢复与迁移基线。
- [x] Textual 目录与测试已从当前分支 `HEAD` 恢复到工作树；没有从旧 worktree 回退或覆盖。
- `.worktrees/tui-rewrite@827dd03` 早于 `ba5a2ad`，只用于历史比较，不是代码或协议基线。
- [x] `ba5a2ad` 中的 Textual 基线已经包含纯 reducer、stable-id timeline、分页历史、SSE 恢复、命令目录、权限/用户输入、compaction、真实 uvicorn、Textual pilot 和 tmux 真终端测试资产。
- [x] 当前通用边界已落地为 `ClientLaunch`、异步 `TerminalClient`、`run_client_application()`、`load_client_tree()` 与 `client_transport`；没有 `_ClientLoop`、`ClientRuntimePort`、`client_runtime` 或 `run_coroutine_threadsafe()` 生产路径。
- [x] `client_transport` 创建唯一 `XBotClient` 并通过 `client_api` 发布；Textual plugin 只从公开服务取依赖。
- [x] CLI TUI 分支只在最外层调用一次 `asyncio.run()`；`TextualTerminalClient.run()` 直接 await `TuiApp.run_async()`。Textual 8.2.8 会更改 loop task factory，adapter 在退出时恢复原值。
- [x] XCore `boot_application()`、`Context.start()` / `Context.destroy()`、`XBotClient.close()` 和 Textual unmount 均有异步生命周期，因此可在 CLI 主线程的同一个 `asyncio.run()` 内顺序完成。
- [x] 当前公开客户端 API 覆盖 session/thread、bounded history、trajectory、commands、providers/agents/effort、jobs、pending inputs、interrupt、permission/user-input response、session policy read/update、plugin config catalog/update、regenerate 和 SSE。
- [x] 当前 `OpenedThread` 携带 identity、metadata/runtime selection、usage、status slots、history page、pending inputs、pending interactions 和 authoritative event cursor。
- [x] 当前 `ServerEvent` envelope 携带 protocol version、session/thread identity、sequence、typed scope、kind 和 payload。
- [ ] 当前工作树的协议/数据模型迁移仍需逐项纳入生产 TUI contract 回归；不能假定 `HEAD` Textual 的历史 payload shape 与当前 producer models 一致。
- [x] client host/plugin 生命周期 focused suite 曾 `7 passed`；CLI/host/launcher/commands/plugin focused suite曾 `63 passed`；一次 2026-09-26 回归 Core `532 passed in 59.99s`、TUI `835 passed in 109.07s`（均 `-s`，TUI 含 loopback/PTY）；之后 TUI 扩大到 `837 passed in 113.55s`，均为本次 Settings CSS 前的历史证据。`829/834 passed` 是更早结果。
- [x] 2026-09-26 真实 80x24 CLI → Textual plugin → loopback HTTP/SSE → PTY 测试证明：当服务端实际发出 reasoning delta 时，`Think` reasoning block 可见、完成后不重复；最新 capture：`/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th0/thinking-pty-captures/`，Think 两帧均为 24×80。该测试证明的是 fixture 提供 reasoning 时的渲染链，不证明默认/真实 provider 会出现思考区，也不代表整体布局对齐 Claude Code。
- [x] 2026-09-26 以真实 CLI → HTTP/SSE → 80x24 PTY 新增无 reasoning、延迟首个正文 delta 的复现用例；实现前失败，屏幕只有 user 和最终 assistant，无中间 Thinking 活动态。
- [x] 同一无-reasoning PTY 修复后通过：最新 capture `/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th1/thinking-activity-pty-captures/`；`Thinking…` 在 Running 且无 assistant/tool 输出时可见，正文出现后消失。
- [x] 2026-09-26 新增 80x24 → 50x28 Textual app resize 行为测试：读者滚离尾部时，窄宽重排后保持同一可见 timeline entry，不跳尾，composer 草稿和 focus 保留。修复前 anchor 从 `m3` 漂到 `m2`，修复后 focused pilot 通过。
- [x] 两项修复后的完整 TUI suite 使用 `-s` 通过：`825 passed in 97.32s`，含真实 HTTP/SSE/PTy 与 Textual app resize anchor/focus 行为。
- [x] 该缺口已按原有状态/数据实现：controller 从 authoritative running facts 与 timeline 尾部阶段导出 transient `thinking` 投影；Textual 在 transcript 尾部显示 `✳ Thinking…`，assistant/活动 tool 开始后移除，完成 tool 且 turn 仍 running 时再显示，turn 结束后移除。不新增事件、persisted entry 或 reasoning payload。controller、view 及真实 PTY 行为测试覆盖这些转换。
- [x] 无 reasoning、延迟首个正文 delta 的真实 CLI → HTTP/SSE → 80x24 PTY 已复验通过 `1 passed in 5.72s`；当时全套 TUI 回归也再次捕获在 `/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th1/thinking-activity-pty-captures/`。首帧可见 `✳ Thinking…`，回复出现后消失。
- [x] 2026-09-26 根据真实 80x24 运行帧发现 delivery hint 被 composer hint、input placeholder 和 footer 重复呈现。TDD 移除重复：placeholder 固定为 `Message`，send/queue/steer 只由 composer hint 说明，footer 保留 interrupt/全局操作；composer/footer 与 status/policy Settings focused 测试 `34 passed`，真实 PTY capture 显示底部提示不再重复。
- [x] 历史 80x24 queue disclosure 证据已由当前可见队列界面替代：Enter 产生 `next-turn`、Alt+S fallback 产生 `next-step`，prompt/target/status 同屏显示并与 server pending-input API 一致；capture：`/tmp/xbot-current-queue/test_real_cli_enter_queues_dur0/queue-pty-captures/queue-and-steer.txt`。
- [x] Settings 首个可用切片有 80x24 真 PTY 证据：F2 打开、Model 页复用 provider picker、Esc 关闭且保留草稿；`/status` 为只读且不请求 provider/agent catalogs。最新 capture 位于 `/tmp/pytest-of-shefrin/pytest-663/test_real_cli_settings_overlay0/settings-pty-captures/`。
- [x] 2026-09-26 更正上一条 Settings 能力记录：公开 `client_api.get_session_policy()` 已存在。Permissions/Sandbox 页现经 `SessionBackend → TransportSession → TuiController` 读取并展示 typed session/effective policy；pilot 覆盖权限规则/沙箱值投影。`/status` 仍只读，并断言不读取 provider/agent/policy/plugin config。当前 policy 只读；策略修改/冲突处理、schema-driven plugin editor/revision write、Appearance 仍未完成。
- [x] 2026-09-26 核对发现 `list_plugin_config` / `update_plugin_config` 公开 API 已存在；最初 Plugins 页误称 API 不存在，随后接入 catalog 与 producer-schema 标量编辑。
- [x] TDD 将公开 catalog 接入 `SessionBackend → TransportSession → TuiController → SettingsData`；Plugins 页显示 typed scope/workspace/applicability/plugin id/name/editability/schema field names，不回显 raw config 值。编辑器只接受明确支持的标量 schema；Apply 固定在滚动区域外并通过 80x24 pilot，保存使用 catalog revision 构造 `PatchPluginConfig`。复杂 schema 不猜测、不生成 raw JSON 编辑器。冲突复核和真实 server 写入仍未闭环。
- [x] 状态栏 detail 顺序为 activity → subagent/queue → usage/cache → agent/mode → generic slots → cwd；累计宽度按 terminal cell width 计算。status-bar suite `70 passed`，且 80x24 真 PTY 显示 context 与 in/out/cache。
- [x] 已按 entry role 直接渲染 prompt 与 tool-step 分隔，不增加 turn ID 或持久语义；permission→ask-user→answer→follow-up 真 PTY capture：`/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_complete0/pty-captures/follow-up.txt`（120×40）。窄屏/长会话/resize 分隔密度仍未验收。
- [x] Settings 样式变更前，完整主线 TUI 套件于 2026-09-26 使用 `-s` 通过：`837 passed in 113.55s`，包含真实 loopback/PTY 场景、默认 MiniMax-compatible provider Think（80x24/100x28/120x40）、无-reasoning Thinking activity、resize anchor/focus、role separators、status usage、chooser、Settings policy、plugin catalog 与 80x24 scalar schema save、可见 queue rows 和 footer/composer 提示去重。唯一 picker screen builder已集中在 `TuiApp._selection_screen()`。
- [ ] 用真实 provider 凭证跑一次可复核的 reasoning-capable stream；受控 loopback 不证明具体 provider、模型或配置会发送 reasoning。
- [ ] 完成单列 Claude Code 风格整体布局验收：step/turn separators、状态/会话字段窄屏优先级、settings 全部可实现页、resize/focus/scroll 和完整复合多轮操作仍未闭环。当前 Think/queue/footer/Settings 的局部 PTY 证据不得描述成整体已对齐。

## 3. 最终装配形态

```text
xbot tui
  -> asyncio.run(application.client.run_client_application(ClientLaunch))
  -> load_client_tree(profile="client")
       -> commands
       -> client_transport       # 唯一 XBotClient owner
       -> textual-tui            # 唯一 terminal_client provider
  -> await TerminalClient.run()
  -> await Textual App.run_async()  # 同一 loop，foreground/main thread
```

目标插件树：

```yaml
- id: commands
  name: commands
  profiles: [agent, server, client]

- id: client-transport
  name: client_transport
  profiles: [client]

- id: textual-tui
  name: tui
  profiles: [client]
```

`TextualTuiPlugin` 只依赖以下公开服务：

- `client_api`
- `client_launch`
- `commands`

它只需要发布：

- `terminal_client`

不再建立 Maya 式 widget factory，不让 CLI、server、session 或 Agent runtime import Textual，也不为 Textual 新建第二套插件系统。

## 4. 单 event loop 与数据流

结论：Textual 应与 XCore、唯一 `XBotClient`、SSE、controller/reducer 和 DOM 共用 CLI 主线程上的单一 event loop。仓库中没有阻碍：`run_async()` 是可 await 的，XCore lifecycle 与 HTTP client 都是 async；反而同 loop 可消除跨 loop Future、线程安全 DOM 投递和 shutdown join。

```text
asyncio.run(run_client_application(launch))  # CLI 主线程，唯一 loop
  -> await boot_application(ctx, client tree)
       -> client_transport 创建唯一 XBotClient
       -> textual-tui 发布 TerminalClient
  -> await terminal_client.run()
       -> await app.run_async()
       -> Textual on_mount 创建 SSE/watch/render tasks
       -> XBotClient / SSE
  -> FrameTranslator（producer-owned models）
  -> reducer（同 loop 顺序写入语义状态）
  -> 同 loop 的 dirty/version 合并刷新
  -> DOM（同一主线程）
  -> Textual on_unmount stop + cancel + gather 自己的 tasks
  -> await context.destroy()
       -> client_transport disposer await XBotClient.close()
```

反向动作：

```text
Textual action
  -> await controller/transport action
  -> await XBotClient public method
  -> result/event
  -> reducer
  -> 同 loop 合并刷新
```

必须满足：

- [x] `run_client_application()` 为 async；`TerminalClient.run()` 契约为 `async def run(self) -> None`；同步 CLI 边界只调用一次 `asyncio.run(...)`。
- [x] 删除 `_ClientLoop`、`ClientRuntimePort`、`client_runtime` service、`run_coroutine_threadsafe()`、线程 join 和跨线程 DOM bridge。
- [ ] XCore boot、plugin apply、`XBotClient` 创建/使用/关闭、SSE、reducer、Textual mount/unmount 全部发生在同一 loop 和 CLI 主线程，并由实际挂载的 Textual app 测试证明；目前已证明 host/client/terminal run/dispose 的 loop identity。
- [ ] Textual action 直接 await controller/public client action；任何同步 CPU/文件工作显式保持很短或移出事件处理热路径，但不得为 HTTP/SSE 重建常驻线程。
- [ ] semantic event 全部先进入 reducer；同 loop 的 dirty/version 或 Textual timer 只合并重复绘制，不建立跨线程 mailbox，也不能丢事件。
- [x] `TuiApp.on_unmount()` 先 stop transport，再 cancel/gather SSE/watch/render worker；`TerminalClient.run()` 返回后，host 在 `finally` 中 `await context.destroy()`，最后由 `client_transport` disposer `await XBotClient.close()`。
- [x] host 的 normal exit、terminal exception、task cancellation 与 boot failure 有 teardown 测试；真实 PTY 覆盖 Ctrl-C 退出。UI worker 清理与 context/client teardown 由各自 owner 实现。
- [ ] Textual 退出后没有残留 async task、server process 或 UDS；单-loop 架构本身不存在 client runtime thread。

建议的宿主契约形状：

```python
class TerminalClient(Protocol):
    async def run(self) -> None: ...
    def request_stop(self, reason: str) -> None: ...

async def run_client_application(launch: ClientLaunch) -> None:
    context: Context | None = None
    try:
        context = await boot_application(...)
        terminal: TerminalClient = context.require("terminal_client")
        await terminal.run()
    finally:
        if context is not None:
            await context.destroy()
```

`request_stop()` 只负责让同 loop 上的 Textual app 退出，不执行 context teardown；teardown 始终由 host 的 `finally` 唯一拥有。CLI 自动拉起的 server 仍是独立进程，其清理由同步 CLI wrapper 在 `asyncio.run()` 返回后负责，不改变 server ownership。

实际约束不是“必须双 loop”，而是单 loop 的协作式调度纪律：DOM diff、语法高亮和本地文件读取不能长时间同步占用 loop；SSE reader、watchdog、interrupt 与 Textual message pump 才能公平推进。另外，当前 Python 3.12 + Textual 8.2.8 的 `run_async()` 会把正在运行 loop 的 task factory 设为 eager factory；adapter 已在退出时恢复进入 `run_async()` 前的 factory，并有 launcher 测试覆盖。

## 5. 模块 ownership

```text
XBotv2/tui/
  plugin.py          # XCore root plugin，唯一装配点
  config.py          # TextualTuiConfig：窗口、驻留、刷新、主题
  terminal.py        # async TerminalClient adapter：await run/request_stop
  app.py             # Textual layout、bindings、DOM 接线
  controller.py      # 同 loop UI action -> transport；持有 state 并生成 view models
  transport.py       # 公开 HTTP/SSE 调用、sequence/cursor/reconnect
  status.py          # derive(facts)，纯函数
  timeline.py        # stable-id timeline/window，纯数据
  commands.py        # local CommandsPort + remote catalog 的组合视图
  settings.py        # generic settings view-model、schema 表单和公开 API mutations
  attachments.py     # 本地文件 -> ImageInput/AttachmentInput
  theme.py           # TUI-owned palette/tokens；不冒充 server/session 配置
  view/...
```

依赖纪律：

- [ ] `state/status/timeline/events/protocol/commands` 不 import Textual。
- [ ] view 不 import app/controller/transport。
- [ ] controller 不 import app。
- [ ] transport 不 import view/app/controller。
- [ ] wire payload 只在 `protocol.py` 校验。
- [ ] reducer-owned facts、timeline 和 stream identity 只有 `state.py` 可写。
- [ ] 禁止 `except: pass` 和“只记日志继续”。
- [ ] `protocol.py` 不声明 producer wire `BaseModel` 的副本。
- [ ] view 不读取 plugin state、session 文件或 runtime private fields。

## 6. 能力边界

| 能力 | 权威 owner / 公共模型 | TUI 责任 | TUI 不负责 |
|---|---|---|---|
| 用户消息 | `MessageRequest`、`MessagePublishedEvent`、`HumanInputRecord` | 以 request id 乐观显示，收到 canonical record 后落地；显式传 `delivery` | 不把本地显示当持久化结果，不按文本猜匹配 |
| 助手正文 | `AssistantTextDelta`、`AssistantCompleted`、`AssistantMessage` | 流式尾块，完成后采用 canonical message identity | 不自造 assistant wire model |
| provider reasoning | `AssistantReasoningDelta`、`ReasoningPart` | 有数据时用独立 Think block 流式显示、可折叠 | 不把 thinking 当普通回复，不推断 provider 模式，不在无数据时伪造内容 |
| thinking activity | `StatusFacts` 的 authoritative running 与已有 timeline 阶段 | 在首个 assistant/tool 输出前显示临时 `Thinking…` 活动态；有 assistant/tool 后让位，turn 结束即消失 | 不写入 timeline/history，不新增事件/字段，不展示或臆造 reasoning 文本 |
| tools | `ToolCallsStarted`、`ToolCallArgumentsDelta`、`ToolCompleted/ToolExecution` 和 typed outcomes | 以 call id 对账；参数和结果折叠；忠实显示 outcome | 不按工具名写业务分支，不解析插件私有 args |
| error | `LoopError`、`XBotClientError`、协议拒绝、本地 IO 错误 | transcript/status 中可见，提供重试或退出路径 | 不因 error 猜 turn 已结束，不吞未知帧 |
| interactions | `PermissionRequest`、`UserInputRequest`、recorded events、snapshot pending interactions | inline/dialog；allow once/session、deny、answer；resume 后重建 | 不执行权限策略，不伪造超时/取消 |
| queue/steer | `delivery`、`PendingInputData`、queue/input events 和 pending-input API | 显示 accepted/claimed/consumed；编辑、移除、steer 走 API | 不从本地状态猜输入已消费 |
| history | `HistoryPage[ConversationRecord]`、opaque `Cursor` | bounded attach、older paging、stable ids、驻留释放、rewrite 后重建 | 不读写持久文件，不制造 cursor，不把窗口当历史 |
| resume | sessions/threads/open session、`event_cursor` | session/thread picker、CLI launch facts、失败切换回滚 | 不扫描 session 目录 |
| commands | `CommandDescription/CommandExecution` | 动态目录、completion/palette、按 kind/effects 执行 | 不复制 server command vocabulary |
| interrupt | interrupt API、`InterruptResponse`、turn terminal event、thread read | 显示 Interrupting，并等待权威终态 | 不在本地宣布已取消，不用断开 SSE 代替 interrupt |
| compaction | compact public events、history rewrite、动态 server command | Compacting 状态、完成/失败提示、summary/history 重建 | 不按 token 自触发，不写 summary，不调用 compact internals |
| plugin feature state | `status_slots`、公开 typed event、动态 command | generic 展示或明确 unsupported notice | 不判断 plugin id/name，不 import implementation internals |
| provider/model/effort/agent | provider/agent catalogs、selection API、`ResolvedRuntimeSelection` | 从服务端目录生成 picker；成功响应或权威 snapshot 后更新 | 不内置 provider/model 列表，不假设某模型支持 reasoning |
| session permissions | `get_session_policy()`、`update_session_policy()`、`PermissionPolicy` | 展示显式值和 effective 值；只提交 `allow/deny/ask` patch | 不实现策略求值，不创造 Claude Code permission mode |
| sandbox | `SessionPolicyResponse`、`SandboxConfig` 和 typed patch keys | 编辑公开的 enabled/network/access 值并显示 effective 值 | 不控制 OS sandbox，不增加服务端未声明的能力 |
| plugin config | `PluginConfigCatalog`、producer schema、scope、revision、`applies_to` | generic schema 表单；冲突重载；标注何时生效 | 不硬编码 plugin 表单，不自行写配置文件，不声称即时生效 |
| appearance | Textual theme/token 与 TUI config | 当前客户端实例内预览、切换、无障碍显示选项 | 没有公开持久化入口时不伪造跨进程保存 |

### 6.1 未知事件

- [ ] transport terminator 或被权威 projection 取代的事件只能在显式 `IGNORED_FRAMES` 中登记，并为每项写明理由。
- [ ] 其他未知 kind 产生可见 protocol-compatibility error，不静默跳过，也不改变 feature state。
- [ ] 不根据裸 `payload` 字段猜测未知事件语义。
- [ ] TUI 不为 goal、todolist、skills、MCP 等按 plugin id/name 写专用分支；优先使用通用 `status_slots`、动态命令或公开 typed model。

## 7. 命令边界

`HEAD` Textual 的 `_command_handlers` 和 builtin table 应迁移到现有 client `CommandsPort`，避免 app 内再维护第二套命令系统。

- [x] Textual plugin 通过 `CommandsPort.register(Command(kind="client", ...))` 注册纯 UI 命令。
- [x] app 删除命令名到 handler 的重复映射。
- [x] local command catalog 由 client profile 的 `commands` service 提供。
- [x] remote command catalog 只来自 `GET .../commands`。
- [x] 合并目录时本地 UI 命令优先，但不复制或猜测服务端命令。
- [x] `kind="prompt"` 的整行文本原样走 message endpoint。
- [x] `kind="server"` 的整行文本以 `{raw}` 交给 `run_command()`。
- [x] `/help [command]` 通过同一动态目录解析 name/slash/alias，详情直接投影 `CommandDescription` 的 description、usage、parameters 和 examples；未知命令与多余参数明确报错，不维护帮助副本。
- [ ] 按 `effects` 精确刷新 history、thread、jobs、sessions、policy 或 commands。
- [ ] session/thread/agent/provider 切换后重新读取远端目录。
- [ ] `/compact`、`/clear`、`/undo`、`/fork` 等只在服务端目录提供时出现。
- [ ] 本地命令只限 UI ownership，例如 help、exit、clear-screen、thinking/details、session/thread picker、attach 和 copy。

当前已证明 client command 注册、local/server 目录合并与优先级、prompt 原样提交和 server command raw forwarding；按 effects 的全资源刷新矩阵仍未完成。

## 8. Claude Code 体验参照

只对齐交互原则，不复制 Claude Code 的命令表、内部状态或后端语义。官方参考：

- [Claude Code interactive mode](https://code.claude.com/docs/en/interactive-mode)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude Code commands](https://code.claude.com/docs/en/commands)
- [Claude Code status line](https://code.claude.com/docs/en/statusline)
- [Claude Code settings](https://code.claude.com/docs/en/settings)
- [Claude Code permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code model configuration](https://code.claude.com/docs/en/model-config)
- [Claude Code terminal configuration](https://code.claude.com/docs/en/terminal-config)

以上页面只证明参考体验：Claude Code 采用 transcript + bottom composer、快捷键/动态 slash commands、底部状态信息、settings scope、permission controls、model/theme 选择。它的 status-line shell script、cost/rate-limit 字段、permission modes、sandbox 命令、配置层级和模型别名不是 XBot 契约，不能直接移植。

2026-09-26 复核官方 CLI 文档后的布局基准：fullscreen transcript 与 composer 是主界面；运行时排队输入显示在 composer 上方；status line 与快捷键 footer 是不同信息层；设置页要说明真实配置来源和作用域。XBot 只采用这些层级与交互原则，字段和设置 mutation 仍由 XBot 公开模型/API 决定。参见 [interactive mode](https://code.claude.com/docs/en/interactive-mode)、[status line](https://code.claude.com/docs/en/statusline)、[settings precedence](https://code.claude.com/docs/en/settings)。

### 8.1 整体布局

以“对话优先、固定底部输入、低噪声状态、临时 overlay”为布局原则，不逐像素仿制：

```text
│ transcript：❯ user / ● assistant / Think / tool / error │
│ pending interaction card（需要时出现在上下文附近）      │
│ optional strip：queue / jobs / compatibility warning   │
├─────────────────────────────────────────────────────────┤
│ ❯ multiline composer + attachment chips                │
├─────────────────────────────────────────────────────────┤
│ status：activity · usage/cache · context · session/model│
└ footer hints：当前可执行快捷键（dialog 时替换）           ┘
              picker / command palette / settings overlay
```

- [x] 当前 Textual 主区为单列 transcript，底部 composer/status 行；queue/jobs 不创建常驻侧栏。
- [x] jobs 继续作为一行折叠 disclosure；queue 默认逐条显示实际待发 prompt 与 target，在 80x24 不要求额外展开即可阅读。最新真实 PTY capture：`/tmp/xbot-current-queue/test_real_cli_enter_queues_dur0/queue-pty-captures/queue-and-steer.txt`。
- [x] 建立独立 context-sensitive footer：根据当前 idle/running/interaction facts 更新快捷键提示，与 session statistics 和 transient status 分层；pilot 在 80x24 → 100x28 验证位置、cell-width、草稿与焦点，真实 PTY 验证可见。
- [x] 删除常驻顶部 SessionBar；单一底部 StatusBar 从 reducer 投影 runtime、累计 in/out/cache、context、session/thread、provider/model，footer 只承载操作提示，不存在第二份计数 owner。
- [x] user/assistant/tool 采用低噪声 `❯` / `●` transcript 标记，不再显示 `You`/`Assistant` 标题、粗竖线和每轮整宽分割线；80x24 真 PTY 已检查基础层级，长会话与动态 resize 仍待验证。
- [x] composer 固定在底部，支持多行增长并有最大高度限制。
- [x] permission/user-input 在相关 timeline 位置显示简卡，并把焦点交给 typed modal；选择或外部 resolution 关闭 modal 后焦点回到 composer。
- [ ] command completion、session/thread/provider/model/agent picker 和 settings 复用同一 overlay/focus/escape 规则。
- [ ] 宽度不足时先隐藏 footer hints、optional strip 和低优先级状态段；transcript 与 composer 始终可用。
- [x] footer 专项 pilot 覆盖 80x24 与 100x28 resize，不丢 composer 草稿或焦点。
- [ ] 整体布局仍需以 80x24 为最低尺寸、100x28 为常用基线验收；resize 还要覆盖选区、scroll anchor 和 modal 状态。

当前不应标成“整体已对齐 Claude Code”：基础 transcript/composer/status 层级已有真 PTY 证据；interaction/permission 控件、整体窄屏优先级、全布局 resize 与复合多轮视觉验收仍未完成。

### 8.2 状态栏字段、权威来源与刷新

状态只由底部单行 `StatusBar` 承载，所有值来自 reducer facts 或公开模型；“未知”应省略或明确标记，绝不补猜。

| 位置 / 优先级 | 字段 | 唯一来源 | 刷新触发 |
|---|---|---|---|
| status / 必保留 | `Connecting/Ready/Running/Waiting/Approval/Compacting/Interrupting/Error/Disconnected` | `derive(StatusFacts)`；facts 来自 connection、`ThreadSummary.turn_status`、typed turn/interaction/compact events、interrupt response | snapshot、SSE event、watchdog thread read、action result |
| status / 高 | context used/window | `UsageSnapshot.latest_turn_observation` + `ResolvedModelSelection.context_window`；provider measured 为精确、estimated input 标 `~` | usage event、snapshot/rebuild |
| status / 中 | session title/id、thread id/kind | `OpenedThread`、`ThreadSummary` | attach/resume/switch、authoritative rebuild |
| status / 中 | provider/model/mode、agent | `ResolvedRuntimeSelection`、agent-configured event、selection response 后的 authoritative thread read | attach、selection success/event、reconnect rebuild |
| status / 中 | queue depth、pending interaction、running jobs | pending-input snapshot/events、pending interactions、job snapshot/events | 相应 typed event、API mutation、reconnect rebuild |
| status / 中（紧随 activity 与 queue） | session 累计 input/output tokens 与 cache hit rate | `UsageSnapshot.total_counters`；绝不复用为当前 context 占用 | 每个 authoritative usage update、snapshot/rebuild |
| status / 中 | generic plugin slots | `OpenedThread.status_slots` / authoritative slot event | snapshot、slot replacement event、watchdog read |
| status / 低 | workspace、thread kind | `ThreadSummary.workspace_root/kind` | attach/switch/thread read |
| status / 低 | local elapsed/spinner | 仅根据已观察到的 turn start 与本地 monotonic clock 投影，显式视为客户端时间 | 250ms–1s UI tick；不触发网络读取 |
| warning | reconnect/gap/protocol/config conflict | transport/reducer 的显式错误状态 | error/recovery result |

- [x] 单行 status 优先保留 activity → subagent/queue → usage/cache → context，再容纳 session/thread → provider/model → agent/mode → generic slots → cwd。`usage/cache` 是 snapshot 累计值，context 是最新 turn observation + context window，两种量不混用。
- [ ] status 严格不换行；按优先级整体丢弃段，最后才对保留段加省略号，不能切坏 Unicode grapheme 或 ANSI 宽度。
- [x] context 只在分母有效时显示；精确值与估算值以 `~` 区分，超窗显式标 `over`；累计 input/output/cache 与当前 context observation 使用不同字段和 segment，不互相冒充。
- [ ] 不展示 cost、rate-limit、Git branch/PR、prompt cache 等 Claude 字段，除非未来有对应公开 owner model；不得通过 shell 命令暗采集。
- [ ] SSE/snapshot/action 每次只推进 reducer version；同 loop flush 合并连续 dirty changes，目标 100ms 内可见，但不因重绘 debounce 丢语义事件。
- [ ] watchdog 使用既有 authoritative thread-summary 周期；不能为刷新状态栏另建轮询风暴。
- [ ] elapsed/spinner 的本地 tick 只更新视觉投影，不修改语义状态、event cursor 或 persisted history。
- [ ] 不实现 Claude Code 的任意 status-line shell script；XBot 的可扩展状态入口是 generic `status_slots`，未来扩展也必须先有公开契约。

### 8.3 Settings / 配置 UI

统一 `SettingsScreen` 使用 tabs 或窄屏 stacked navigation。`/status` 与 `/config` 打开同一个 dialog，分别定位 Status 与 Config；Status 完全由已持有的 client state 投影，不等待 provider/model/policy/plugin catalog 请求，用户进入配置页时才读取公开 catalogs。F2 可直达 Config。`/settings` 不作为第二个本地入口；不接管 server command 的带参数形式，也不直接编辑 YAML/JSON 文件。该入口行为对齐 Claude Code 文档，具体设置范围仍只限 XBot 公开 API。

| 页面 | 展示与编辑范围 | 公开来源 / 写入口 | 生效与限制 |
|---|---|---|---|
| Status | connection、session/thread/workspace、runtime selection、usage、queue/jobs、协议错误 | controller 从 reducer state 生成的只读 view models | 共享 dialog 内的 Status 页；只读、无网络请求，与底栏同源 |
| Model | provider、model、reasoning effort、agent | provider/agent catalogs；select provider/effort/agent API | 只列 catalog 声明项；选择成功后以权威 selection/snapshot 为准 |
| Permissions | 每个公开 permission key 的显式值与 effective 值 | `get_session_policy` / `update_session_policy` | 仅 `allow/deny/ask`；remove 表示回到继承值；无 Claude mode 映射 |
| Sandbox | enabled、network、external/workspace read/write 的显式值与 effective 值 | 相同 session-policy API 与 `SandboxKey`/`SandboxValue` | TUI 只提交策略，实际隔离由 sandbox owner 执行 |
| Plugins | plugin id/name、editable、schema、scope raw/effective config、unavailable reason | `list_plugin_config` / `update_plugin_config` | schema 驱动；revision 冲突重载；按 `applies_to` 显示 current/new session |
| Appearance | theme、thinking/tool details、密度/动画/快捷键提示 | TUI plugin config 与运行时本地状态 | 首版可当前进程即时预览；无公开 client-config 持久化 API 时明确标“本次运行” |

- [x] settings 打开时并行读取 provider/agent catalog、session policy 和 workspace plugin config；单项请求失败不阻塞其他页，并显示各自错误。
- [ ] Model picker 的 model、effort、input modality 关系完全由 `ProviderCatalog` 声明；不写 Anthropic/OpenAI 等品牌判断。
- [ ] provider/model/effort/agent mutation 显示 pending，成功后采用响应/权威 snapshot；失败时保留原 selection 和未提交选择。
- [ ] Permissions/Sandbox 表单同时展示 override 和 effective 值；用户可 set 或 remove，不把“继承”编码成 `ask`/`deny`。
- [ ] 权限交互中的 Allow once / Allow session / Deny 仍响应具体 `PermissionRequest`；Settings 修改的是 session policy，二者不得共用伪造的本地状态。
- [ ] Plugin 页仅为 `editable=true` 且存在 producer schema 的条目生成控件；未知 schema keyword 显示明确 unsupported，不降级为不受控 JSON 写入。
- [ ] plugin config 保存携带 catalog revision；HTTP 409 保留用户草稿、重新加载 catalog 并要求用户确认重试。
- [x] Plugins catalog 按 `PluginConfigCatalog.applies_to` 明示“当前 session”或“仅新 session”；不得宣称现有 runtime 已热重载。
- [ ] schema 中标记敏感/write-only 的字段不回显明文；没有 secret update 契约时不提供 secret 编辑器。
- [ ] theme 使用 Textual 可验证的 token/palette，保证默认/暗色/高对比下 status、diff、error、permission 都不只靠颜色传意。
- [ ] appearance 持久化只有在 client plugin/profile 提供公开 typed persistence 边界后才实现；首版不得绕过宿主自行写 `xcore.yaml` 或 data-dir 配置。
- [ ] settings modal 的 Apply/Cancel/Reset 语义逐页明确：Reset 只清除当前表单草稿，除非用户确认并通过公开 remove patch 恢复继承值。

### 8.4 快捷键与命令映射

目标映射：

- [ ] idle 时 Enter 正常提交。
- [x] running 时 Enter 使用 `delivery="queue"`；真实 80x24 PTY + server API 验证待处理项为 `next-turn` 并显示 Queue strip。
- [x] Ctrl+Enter 将当前 composer 输入经公开 message API 作为 `delivery="steer"`；pilot 覆盖。无法区分该组合键的终端用可见 Alt+S fallback；真实 tmux PTY + server API 验证目标为 `next-step`。
- [ ] 从 composer 上方可见的 Queue row 选择一个已排队条目并经公开 pending-input API retarget 为 steer；当前只支持将 composer 中的新输入作为 steer。
- [ ] Esc 有 dialog 时关闭或按公开协议处理；无 dialog 且 turn running 时调用 interrupt。
- [ ] Ctrl+C 在 running 时 interrupt；idle 且 composer 有文本时清空；二次确认或 Ctrl+D 退出。
- [ ] Shift+Enter 和 Ctrl+J 插入换行。
- [ ] `/` 打开动态 local + server command catalog。
- [ ] Alt+P 或可配置替代键打开由 XBot catalog 驱动的 model picker；不复刻 Claude model aliases。
- [ ] `/status` 与 `/config` 打开同一个 overlay 的不同初始页；Status 可在 turn 运行中立即打开且不读 catalogs，Config 页只读/写公开能力；不注册重复的 `/settings` alias。
- [x] reasoning 在 streaming 时默认跟随新内容，完成后默认折叠；用户在 stream 中手动折叠/展开会保留其选择。assistant 正文不受此 policy 影响；Think/tool 展开时按内容增长，到最大高度后在只读块内滚动。
- [x] ctrl+e 的 block summary 明示当前可执行动作（扩展/折叠），包括 stream 中；focused block 仍可用其局部 Enter/Space 操作。
- [ ] 权限和 ask-user 以 inline/dialog 完成，不要求用户手抄 interaction id；slash command 仅保留为可访问性路径。
- [ ] 没有公开 API 的交互不通过 private manager 或服务端改造补齐。

### 8.5 明确不做

- [ ] 不实现 Claude Code 独有的 shell status-line customization、cost/rate limit、Git/PR fetch、plan/accept-edits/bypass permission modes、managed settings 或 sandbox 命令语义。
- [ ] 不为了填满设置页增加 server endpoint、协议字段或 provider 特例；公开 API 缺失时 UI 标为 unavailable 或省略。
- [ ] 不把 settings UI 变成通用配置文件编辑器，不读取 plugin private state，不直接触发 runtime 内部 reload。

## 9. 分阶段实施

### 阶段 0：保护现场与冻结契约

- [ ] 由所有者先处理当前大规模 staged/unstaged 变更，记录协议重构的确定提交。
- [ ] 从最新测试过的 `main` 建立 `dev-textual-tui`；不直接在 `main` 或当前 `fix-*` 分支开发。
- [ ] 将 `ba5a2ad` 的 Textual 文件作为恢复来源；禁止复制 `.worktrees/tui-rewrite`。
- [ ] 建立 `HEAD TUI expected model -> current producer-owned model` 差异表。
- [ ] 先写 contract tests，锁定当前 `XBotClient`、`OpenedThread`、history、pending interactions、event cursor、provider/agent catalogs、session policy、plugin config catalog 和 event schemas。
- [ ] 用真实 `XBotClient` + uvicorn 获取 open snapshot 和 SSE，确认公开 API 足够。
- [ ] 将任何公开契约缺口记录为 blocker；不调用 private method，也不修改 server 业务职责迁就 TUI。

完成条件：

- [ ] 所有 TUI 必需操作都有明确的公开入口和 owner model。
- [ ] 旧 `type/data/request_id` wire shape、旧 ToolRecord/AssistantRecord payload 假设不能重新进入新实现。

### 阶段 1：插件装配与生命周期

- [x] 增加 `tui/plugin.py`、typed config 和 terminal adapter。
- [x] 将 generic `run_client_application(ClientLaunch)` 改为 async，并让 CLI 只在最外层执行一次 `asyncio.run(...)`。
- [x] 将 `TerminalClient.run` 改为 async protocol；Textual adapter 直接 `await app.run_async()`。
- [x] 删除 `_ClientLoop` / `ClientRuntimePort` / `client_runtime` service 以及 Maya 需要的 Future/thread 调度代码，不为同步 terminal client 保留兼容分支。
- [x] `xcore.yaml` client profile 用 `textual-tui` 替换 `maya-tui`。
- [x] 确认 `commands` 和 `client_transport` 为 client profile 依赖。
- [x] 恢复 `textual` 依赖并移除 `maya-py`。
- [x] 删除 `maya_tui/**` 和 `tests/maya_tui/**`，不保留 fallback、shim、feature flag 或兼容 import。
- [x] plugin apply / single API client / async terminal / error、boot failure、cancellation cleanup 与 CLI launch facts 均有 focused tests；真实 CLI PTY 覆盖 client profile 生产启动和 Ctrl-C exit。
- [ ] 用显式 loop/thread identity 测试证明 plugin apply、Textual mount/unmount、HTTP/SSE、reducer、DOM 与 disposer 的全链路同 loop；当前 host/client/terminal run/dispose identity 已证明，真实 PTY 全路径已执行但不是 identity assertion。
- [x] 测试 UI 正常退出、terminal exception、boot 中途失败与主 task cancellation 均恰好销毁一次 context/client；`TuiApp.on_unmount()` 停 worker 后 host 才销毁 context。
- [ ] 用外部 server 和自动 UDS server 各跑一次空 Textual shell 的生产入口。

完成条件：

- [x] CLI/app host 不 import Textual 或 Maya。
- [x] Textual 只由 client plugin tree 选择。
- [x] Maya 名称在依赖、默认树、生产 import 和测试中为零。
- [x] `ClientRuntimePort`、后台 client loop、跨线程 Future/DOM bridge 在生产和测试中为零。
- [ ] loop-affinity 测试证明 XCore、HTTP/SSE、reducer 与 DOM 共用 CLI 主线程的唯一 event loop。

### 阶段 2：协议桥、transport 与 reducer

- [ ] 以当前 producer models 重写 `HEAD protocol.py` 的过期映射，不加双版本 fallback。
- [ ] 实现 attach、stream、sequence、cursor、reconnect、watchdog、switch、submit 和 interrupt。
- [ ] 保留 server thread read 为 running/idle 权威来源。
- [ ] 保留 stable-id upsert 和纯 window 语义。
- [ ] 保证 streamed assistant 始终为 timeline 后缀。
- [ ] 保证错误可见，但 error 本身不被解释为 turn terminal。
- [ ] 为 core frame typed translation、foreign frame、malformed payload、unknown kind 写测试。
- [ ] 为 duplicate/replay/gap、cursor expired、rewind、baseline rebuild、bounded failure 写测试。
- [ ] 为 mid-turn attach、丢终态后的 watchdog 收敛写测试。
- [ ] 为 assistant text/reasoning、多个 tool calls 和全部 typed outcome 写测试。
- [ ] 继续用定向 mutation 证明 I1-I8，不仅检查分支覆盖率。
- [ ] 用真实 uvicorn + MockLLM 直接驱动 transport/reducer。
- [ ] 用高频 SSE + 慢视图测试同-loop 公平性：绘制合并但每个 semantic event 都进入 reducer，watchdog/interrupt 仍能调度，不能静默造成 cursor 过期。

完成条件：

- [ ] 当前生产事件集合中的每个事件都被翻译或有显式忽略理由。
- [ ] TUI 内没有 duplicate wire `BaseModel`。
- [ ] reducer 不依赖 plugin id、tool name 或 server command name。

### 阶段 3：整体布局、Transcript 与状态栏

- [x] 基础单列布局先写失败测试，再落地无顶部 session bar、`❯`/`●` transcript、双分隔线 composer、单行 status 和独立 footer；80x24 真 PTY 已检查。100x28、宽屏和动态 resize 的完整复合场景仍开放。
- [x] 主 DOM 为 transcript、可见 queue rows、折叠 jobs disclosure、固定 composer、单一底部 status 与独立 context-sensitive footer；Settings 与 picker/modal 的 overlay/focus 规则仍未统一，故不视为整体布局完成。
- [x] 恢复 stable-id transcript diff。
- [x] 分块显示 user、assistant、reasoning、tool、notice 和 error。
- [x] reasoning、tool args 和 tool result 默认折叠；展开块高度为 `min(实际内容高度, BLOCK_MAX_LINES)`，超过上限才块内滚动。
- [x] 恢复 streaming tail、自动跟随和“用户滚动后不抢位置”。
- [x] 长工具输出 clamp，展开后仍可完整阅读。
- [x] 按 8.2 的来源和优先级使用单行 status 承载 session/runtime/context/usage/jobs/queue/slots，并保留独立动态 footer hints。
- [x] 用 Unicode cell width 而非 Python 字符数做截断，并保证 status/footer 永不换行。
- [x] 状态 detail 与 usage/cache 按优先级和 Unicode cell-width 累计已有行为回归；80 列下保住 activity/queue/usage 后，较低优先级 agent/mode/slots/cwd 可整体丢弃。
- [x] 真实 80x24 PTY 同屏显示 `ctx:~…/…`、累计 `in/out` 和 `cache:%`，最新 capture 在 `/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th0/thinking-pty-captures/thinking-completed.txt`。
- [ ] 为 measured/estimated/unknown context、窄屏字段降级、generic slot replacement 和本地 elapsed tick 写纯渲染测试。
- [ ] 测试每个状态字段只能由其列出的 snapshot/event/action source 改变，尤其禁止 error、spinner 或 cost 猜测 turn 状态。
- [x] 用 Textual `run_test` 驱动真实 widgets，不断言私有字段。
- [x] 测试非尾部 entry 更新只刷新自己的 widget。
- [ ] 测试 scroll intent、resize、窄终端、Unicode/中文/emoji、长代码和超长 tool output；目前已有 80x24 → 50x28 的 stable visible-entry anchor/focus pilot，PTY resize、超窄宽、Unicode/长代码重排仍未验收。
- [ ] 测试 completed history 与 live stream 的视觉投影一致。
- [x] 有 reasoning payload 时，将 `AssistantReasoningDelta` / completed `ReasoningPart` 渲染为独立 Think block；loopback 真 PTY 验证了提供 reasoning 的 fixture。
- [x] 无 reasoning payload 的普通 running turn 在 transcript 显示独立 `✳ Thinking…` 活动态；真实慢响应 PTY 确认首个 assistant delta 到来后消失。已有 reasoning fixture 的 PTY capture 仍只证明 provider reasoning block，不作为此活动行的替代证据。
- [x] 用真实 CLI PTY + loopback server 验证带 reasoning fixture 的 message -> streamed delta -> Think block -> completed assistant；最新 capture 在 `/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_shows_th0/thinking-pty-captures/`。该场景不再作为“Thinking 活动态完整”或“Claude Code 整体布局已对齐”的证据。
- [x] 根据 timeline role 为 user prompt 与 tool step 增加不同边界；真实多轮交互 capture 验证两类线条实际显示，未引入额外领域状态：`/tmp/pytest-of-shefrin/pytest-663/test_real_cli_tui_pty_complete0/pty-captures/follow-up.txt`。
- [ ] 在 80x24 和长会话滚动/resize 下验证 role separators 的密度、对可读区域的影响和 scroll anchor。
- [ ] 用真实 server 跑包含 reasoning、tool、result、assistant 的完整复合回合。

完成条件：

- [ ] transcript 顺序与 server history 一致。
- [ ] Ready/Running/Waiting/Approval/Compacting/Interrupting 只来自 `derive(facts)`。
- [ ] 80x24 下 transcript/composer 可操作，状态栏不换行，resize 不丢草稿/scroll anchor/focus。
- [ ] 所有 DOM 调用发生在唯一 CLI 主线程 loop；不使用 `call_from_thread` / `post_message` 充当跨线程桥。

### 阶段 4：Composer、动态命令、queue/steer 和 interrupt

- [ ] 恢复 multiline composer、附件、提交和 input history。
- [x] 将所有 local commands 迁入 client `CommandsPort`。
- [x] remote commands 通过公开目录动态 discover/execute。
- [ ] completion、palette 和 picker 共用统一 catalog/selection model。
- [ ] `/settings`、`/status`、`/theme` 等纯 UI 命令只经 `CommandsPort` 注册；同名 remote command 不被伪执行。
- [x] Enter 在运行中默认 queue，Ctrl+Enter/Alt+S 显式将 composer 当前输入作为 steer；已有队列项目的 edit/remove/retarget UI 仍未完成。
- [ ] 实现 Esc/Ctrl+C/Ctrl+D 的明确状态机。
- [ ] 按 command effects 精确刷新相关资源。
- [ ] 测试 Enter、Shift+Enter、Ctrl+J、Ctrl+Enter、Esc 和 Ctrl+C（Enter/Ctrl+Enter/Alt+S 已有 app + PTY 证据；Ctrl+J、完整 Esc/Ctrl+C 状态机仍待测）。
- [ ] 测试 local command 优先、collision、prompt command 和 server command raw forwarding。
- [ ] 测试 catalog 切换后替换旧目录，不残留陈旧项。
- [ ] 测试 queue accepted/claimed/consumed/edit/remove/steer。
- [ ] 测试 interrupt response、terminal event、watchdog 和重复按键竞态。
- [ ] 用真实慢 turn 排队与 steer 已由 80x24 PTY 覆盖；编辑、取回、已有队列 item retarget 和 interrupt 的复合序列仍未完成。

完成条件：

- [x] app 中不存在 `_command_handlers` 第二词表。
- [x] TUI 不包含服务端插件命令的硬编码名字。
- [x] queue/steer 文案与实际发送语义一致：Enter 队列、Ctrl+Enter steer、Alt+S 是真实 TTY fallback；文案与服务端 target 有实链路验证。

### 阶段 5：Interactions、history、resume 与 compaction

- [x] 实现 inline permission 和 user-input dialogs。
- [x] 从 snapshot pending interactions 重建未完成交互；attach 行为测试从 authoritative snapshot 打开 chooser 并经 typed response API 回答。
- [ ] 恢复 bounded history attach、older paging 和 retention/release。
- [ ] 恢复 session/thread picker 和 failed switch rollback。
- [ ] 使用 compact typed events 显示 started/completed/failed，并处理 history rewrite/summary。
- [ ] regenerate、undo、clear 和 fork 只走动态 server command 或公开 API。
- [ ] 测试 allow once/session、deny、answer、stale id 和多个并发 interactions。
- [ ] 测试 disconnect/reconnect 后 interaction 恢复且只能响应一次。
- [ ] 测试 history cursor append-stable、rewrite invalidation 和释放后重新加载相同 ids。
- [ ] 测试 failed session switch 保持原 stream。
- [ ] 测试 manual/automatic compact、completed/failed 和 canonical summary identity。
- [x] 测试 subagent read-only 只由 `ThreadSummary.kind` 决定；真实 child switch 走公开 thread catalog/endpoint。
- [x] 用真实 server 跑 permission -> tool -> completion。
- [x] 用真实 server 跑 ask_user -> answer -> resumed turn。
- [ ] 用真实 server 分页到历史开头。
- [ ] 退出进程后用 `--session` resume 并继续对话。
- [ ] compact 后关闭再 resume，核对 summary/history。

完成条件：

- [ ] TUI 不访问 session 文件、plugin state 或 internal manager。
- [ ] history/resume/compact 能在新进程中复现。
- [ ] interaction id、message id 和 turn id 不混用。

### 阶段 6：Settings、权限/沙箱、模型与主题

- [ ] 先为只读 Status 页、各 settings tab 的 loading/error/dirty/pending/success/conflict 状态写 view-model 和 Textual pilot 失败测试。
- [ ] 实现统一 `SettingsScreen`，宽屏用 tabs、80 列用 stacked navigation；Esc/Cancel 不丢失底层 composer 草稿和 transcript anchor。
- [ ] Status 页只投影 controller 当前 view models，和底栏字段使用同一 formatter/source map。
- [ ] Model 页从公开 provider/agent catalogs 构造 provider/model/effort/agent 控件；选择只调用对应 public API。
- [ ] Permissions 页用 `get_session_policy()` 显示 override/effective，并用 typed `allow/deny/ask` set/remove patch 保存。
- [ ] Sandbox 页只暴露 `enabled`、`network`、`external_read`、`external_write`、`workspace_read`、`workspace_write` 及其模型允许值。
- [ ] Plugins 页按 producer `config_schema` 生成通用控件，支持 global/workspace/session scope、raw/effective、revision 和 `applies_to` 提示。
- [ ] Appearance 页实现当前进程 theme preview、thinking/tool details 和高对比可访问性；没有公开 persistence 边界前明确标“本次运行”。
- [ ] 每个 mutation 都显示 pending、屏蔽重复提交、在成功响应后 adopt 权威值；错误保留草稿并可重试。
- [ ] plugin config 409 测试证明 catalog 重载、草稿保留和显式二次确认；unsupported schema/unavailable plugin 不产生 PATCH。
- [ ] 测试 permission override/remove/effective、sandbox 类型和值、provider/model/effort capability 约束和 failed selection rollback。
- [ ] 测试 plugin config 三种 scope 和 `new_sessions/current_session` 标签，证明 UI 不伪装热重载。
- [ ] 测试 default/dark/high-contrast theme 的焦点、错误、permission、diff 和 disabled 状态无需仅靠颜色区分。
- [ ] 用真实 uvicorn + `XBotClient` 打开并更新 session policy，再重新 GET 核对 canonical response。
- [ ] 用真实 uvicorn 获取 catalog、切换 provider/model/effort/agent，并从 thread snapshot 核对 selection。
- [ ] 用真实 uvicorn 读取可编辑 plugin schema、提交一次 revision patch；冲突时核对 HTTP 409 路径。

完成条件：

- [ ] Settings 的每个可编辑字段都能追溯到公开 catalog/model/API；没有 private state 或配置文件直写。
- [ ] policy、sandbox 和 selection 的实际语义仍由其服务端 owner 决定，TUI 只提交并展示权威结果。
- [ ] appearance 是明确的 TUI ownership；未实现的跨进程持久化不会被文案伪装为已保存。

### 阶段 7：真实生产链路与 PTY 验收

验证层级：

- [ ] 纯函数/reducer 测试。
- [ ] scripted backend transport 测试。
- [ ] Textual pilot 测试。
- [ ] 真实 uvicorn + `XBotClient` + MockLLM 测试。
- [ ] 真 PTY/tmux，经 `.venv/bin/xbot tui` 完整 CLI/plugin/UDS 路径。

PTY 场景：

- [ ] 新 session 启动、Ready、输入、thinking、tool、final。
- [ ] slow turn 下 queue、steer 和 interrupt。
- [x] permission/user-input 全键盘完成；真实 tmux 覆盖 permission chooser → question chooser → final → follow-up。
- [ ] `/` 动态目录、picker 和 completion。
- [ ] 打开 Settings，在 provider/model/effort/agent 间导航；保存成功和失败都能回到 composer。
- [ ] 修改 permission override、恢复继承、修改 sandbox 后重新打开 Settings 核对 effective/override。
- [ ] 编辑 schema-driven plugin config，验证 current/new-session 生效标签与 revision conflict。
- [ ] 切换 default/dark/high-contrast theme，resize 后配色、focus 和状态对比仍正确。
- [ ] 状态栏在 connect、running、permission、interrupt、compact、reconnect 各阶段使用权威状态；context/queue/jobs/slots 随真实事件刷新。
- [ ] PageUp 历史、滚动停留和返回 tail。
- [ ] compaction。
- [ ] 两个 TUI 并行且 session 隔离。
- [ ] 退出后 resume。
- [ ] 80x24、100x28、宽屏和运行中 resize；逐一核对 session/status/footer 不换行且 composer 草稿、scroll anchor、modal focus 不丢。
- [ ] 服务端启动失败、协议错误和断网重连。
- [ ] Ctrl+C、Ctrl+D 和终端关闭后的清理。

执行验证时必须显式使用待验收 checkout 的工作目录和 `PYTHONPATH`，避免 editable `.venv` 从另一个 worktree import 代码。

建议验证命令：

```bash
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/tui -q
PYTHONPATH=XBotv2 .venv/bin/pytest \
  XBotv2/tests/core/test_client.py \
  XBotv2/tests/core/test_cli.py \
  XBotv2/tests/core/test_plugin_loader.py -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/integration/test_http_transport.py -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/core -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/integration -q
```

完成条件：

- [ ] focused、core、integration、real-server 和 PTY（含 settings/layout/status/theme 场景）全部通过。
- [ ] 整套关键测试连续运行至少 5 次，无竞态和 flaky。
- [ ] 没有遗留线程、task、server process 或 UDS。
- [ ] 记录准确的 evaluated development commit；之后任何代码改动都使结果失效，需重跑受影响验证。

## 10. 风险与控制

### 协议漂移

- 风险：`ba5a2ad` Textual 对 Assistant/Tool/Event payload 的理解可能落后于当前协议重构。
- [ ] 只复用它的架构、纯状态、view 和测试意图；protocol/transport 必须按最终 producer models 迁移。
- [ ] 禁止以兼容分支同时接受新旧 wire shape。

### 单 event loop 生命周期与亲和性

- 风险：意外保留 Maya 的后台 loop、在错误 loop 关闭 `XBotClient`，或 UI 异常时跳过 XCore dispose。
- [ ] 为 boot/apply、client construction/call/close、Textual mount/unmount 和 context dispose 记录 loop/thread identity，并在测试中断言完全相同。
- [ ] host 只负责 `await terminal.run()` 与 `finally: await context.destroy()`；Textual plugin 只负责 app workers 的 stop/cancel/gather，双方不重复关闭 client。
- [ ] cancellation/KeyboardInterrupt 路径保护 teardown，并同时保留 primary failure 与 cleanup failure，不因清理异常覆盖原始错误。
- [ ] 捕获并在 `run_async()` 返回后恢复原 event-loop task factory；验证 Python 3.12 下 Textual eager task factory 不泄漏到后续 host lifecycle。

### 慢渲染导致 replay cursor 过期

- 风险：单 loop 中同步 DOM diff、highlight 或文件 IO 占用过久会饿死 SSE reader；async 共享 loop 不等于自动并行。
- [ ] reducer 与 network reader 不等待 render 完成。
- [ ] 使用同 loop 的 dirty/version 定时 flush 合并绘制，不引入跨线程 render mailbox，不丢 semantic events。
- [ ] 对 transcript diff/长 tool output 设定可测时间预算并分块/yield；本地附件读取不得阻塞 event-loop 热路径。
- [ ] backlog/gap/recovery 都有可见状态和真实限速测试。

### history rewrite 与在途事件竞态

- 风险：undo/clear/compact 后把旧窗口和新 surface 拼在一起。
- [ ] snapshot/history rewrite 作为完整 baseline replacement 处理。
- [ ] cursor invalid 时明确 rebuild，不推断 offset。

### 动态插件事件

- 风险：TUI 为每个 plugin 增加硬编码分支。
- [ ] 优先使用公开 owner model、通用 status slots 和动态 commands。
- [ ] unsupported event 明确可见，不猜测、不静默。

### Settings 契约与生效范围

- 风险：UI 将 override 当 effective、将 `new_sessions` 当热更新，或在 revision conflict 后覆盖他人配置。
- [ ] 所有 settings view-model 同时保存 source scope、raw/effective、revision 和 applies-to；显示层不丢失这些语义。
- [ ] mutation 只采用成功响应，不做不可回滚的 optimistic commit；409/typed error 保留草稿并要求重新确认。
- [ ] appearance 与 server-owned settings 分仓建模，防止 theme 等本地偏好被误发到 session policy 或 plugin config。

### 布局与终端能力差异

- 风险：不同终端对 Alt、Shift+Enter、Unicode 宽度、颜色和 resize 的支持不同，pilot 无法完全模拟。
- [ ] 为不可区分键提供可发现的替代 binding，并在 footer/help 显示当前终端实际可用组合。
- [ ] 使用 terminal cell width 和 theme tokens 做布局/对比测试，再以至少两种真实终端或 tmux 配置验收关键路径。

### 真终端环境差异

- 风险：pilot 通过但快捷键、alt-screen、paste、resize 或 startup 失败路径不可用。
- [ ] 保留并更新 `HEAD` 的 tmux/PTY 测试，不用 pilot 代替生产入口。
- [ ] 保存失败时的 screen capture 和 server/client 日志为本地忽略产物。

## 11. 最终完成判据

- [ ] `xbot tui` 默认由 `textual-tui` client plugin 提供；CLI 不直接构造 TUI。
- [ ] `client_transport` 是唯一 HTTP client owner。
- [ ] CLI 只创建一个 asyncio loop；XCore、唯一 `XBotClient`/SSE、reducer 和 Textual DOM 同 loop 运行，`ClientRuntimePort`、后台 client thread 与跨线程 mailbox 为零。
- [ ] async `TerminalClient.run()`、Textual workers 停止、`Context.destroy()`、`XBotClient.close()` 的顺序在正常/异常/取消退出中均被证明。
- [ ] Maya 代码、依赖、测试、配置和兼容路径全部删除。
- [ ] `ba5a2ad` 的有效行为与测试资产被保留或有明确替代；没有从旧 worktree 回退。
- [ ] message、thinking、tool、error、interactions、history、resume、commands、interrupt 和 compaction 全部走公开 API、事件和 owner models。
- [ ] 单列 transcript、固定 composer、单行 status、context-sensitive footer、overlay 与 80x24 响应式降级均通过 pilot 和真 PTY。
- [ ] 状态栏每个字段都有唯一公开来源和刷新触发；没有伪造 cost、rate-limit、Git 或 provider/plugin 私有字段。
- [ ] provider/model/effort/agent、permission、sandbox 和 schema-driven plugin config 均通过公开 API 完成并采用权威响应。
- [ ] theme/appearance 明确由 TUI ownership 管理；没有公开 client persistence API 时只承诺当前进程生效。
- [ ] TUI 不包含 plugin id 分支、server command 词表、tool-name 语义表或重复 wire schema。
- [ ] server/session/plugin 业务职责没有任何为适配 TUI 而产生的改动。
- [ ] unsupported/invalid state 可见，不静默跳过。
- [ ] status、timeline、window、DOM 和 transport ownership 可机械检查。
- [ ] 真 PTY 证明生产入口，而不只证明 Textual pilot。
- [ ] 文档更新说明 plugin ownership、整体布局、状态字段、settings 生效范围、按键、queue/steer、history/resume、interaction 和故障恢复。
- [ ] 最终变更在同一 `dev-*` 分支完成、审核、验证，并以一次有意义的 merge commit 合入 `main`。
