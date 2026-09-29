# Textual TUI 当前计划

> 本文件是 `TODO.md` 第 5 阶段的执行清单，不是历史日志或 UI 设计 gold
> truth。TUI 只消费公开 API、typed protocol/event 和 producer-owned models；
> 不为补 UI 修改 Core/session/plugin 业务语义。设计文档和当前实现都需要审查；
> 判断依据是用户约束、明确的 owner 和生产路径的可观察行为证据。

## 已确定边界与当前装配证据

- [x] `xbot tui` 通过 client profile 装配唯一 `XBotClient`、commands 和 Textual terminal client；CLI 外层只创建一个 asyncio loop。
- [x] Textual、transport、controller/reducer 和 SSE 在同一 client loop；退出由 TUI stop → worker cancel/gather → host destroy → client close 顺序完成。
- [x] transport 只负责公开 HTTP/SSE、cursor、sequence 和 reconnect；state/reducer 拥有语义投影；view 只拥有 DOM、focus、selection、scroll 和折叠显示。
- [x] human、assistant、reasoning、tool、interaction、queue、history、job、compact 与 runtime notice 使用 producer-owned typed models；未知事件不得按裸 payload 猜语义。
- [ ] 最终全仓边界审计：TUI 不读 session 文件、plugin/runtime private state，不硬编码工具名、插件名或服务端命令表；state/status/timeline 不 import Textual，view 不 import controller/transport，controller 不 import app，transport 不 import view/app/controller。

## 已验证产品基线

- [x] 紧凑单列布局使用 `❯` user/composer、`●` assistant/tool、底部 status/footer，无永久顶部 session bar。
- [x] reasoning delta 渲染独立 Think block；无 reasoning 时只显示 transient `Thinking…`。Think/tool 使用受限高度 disclosure；收起显示标题和最多两行 preview，final assistant reply 永不折叠。
- [x] tool call 与 result 为独立 disclosure；permission 和有限 question 使用 typed modal/chooser，开放问题使用 typed answer flow，不伪造消息或 slash command。
- [x] RuntimeNotice/context 注入在 transcript 中以来源明确的紧凑 trace 可见，不把内部 XML/JSON 原样冒充用户消息。
- [x] status 保留 usage、context/window、cache、activity 和必要 session/runtime selection；窄屏按优先级裁剪，不伪造正常比例。
- [x] Enter 默认 steer，Shift+Enter 换行；显式 queue 和 interrupt 独立。长 paste 保留原文与 canonical identity，pending input 来自服务端权威列表。
- [x] session/thread switch、只读 `/thread`/subagent view、jobs hydration、compact summary、退出和跨进程 resume 走公开 API。
- [x] `0ce4cc6` 修复 `history_window=50` 小于 `transcript_limit=100` 时 PageUp 加载却不显示旧页，以及 PageDown 不回 live tail；局部 generation 阻止已取消 scroll callback 复活。
- [x] 真实 30 轮历史后，第 31 轮流式期间保持旧页；80×24 ↔ 100×28 resize 保留 anchor、fold、composer focus/selection，完成后仍留旧页，PageDown 回尾。
- [x] `b72e668` 的完成帧同时等待 collapsed Think marker、preview、final 和 Ready；它强化证据，不改变 block 语义。
- [x] 最近集成 TUI + ACP：`895 passed in 141.51s`。证据包含本地真实 uvicorn/HTTP/SSE 和 tmux；不代表外部 provider 或中途断网已验收。

## 当前验收矩阵

### 输入、流式与 transcript

- [x] 普通输入、多行 bracketed paste、继续编辑、canonical history 和重复输入检查已有真实 PTY 覆盖。
- [x] Think/text/tool/permission/question/final/follow-up 的可见顺序与键盘响应已有真实 server/tmux 覆盖。
- [x] reader 离开尾部时，连续 stream、完成和 resize 保持同一可见 entry；显式 PageDown 才恢复 tail follow。
- [ ] 用 Unicode、长代码和超长 tool args/result 验证 terminal-cell 宽度、block 内滚动和窄屏密度，不让 payload 挤走 composer/status。
- [ ] 对异常的部分 assistant/tool stream、provider failure 和 interrupt 终态做当前生产 capture，确认 error 可见且不伪造 turn 完成。

### History、switch、reconnect 与 resume

- [x] PageUp 从真实 server 加载到历史开头；加载页小于 DOM window 时仍立即显示新页，分页期间不闪回尾部。
- [x] session switch、已有 JSON 状态启动、双客户端隔离、compact 后退出/新进程 resume 和继续第六轮已有真 PTY 证据。
- [x] switch/rebuild 后 authoritative thread read 恢复 turn count、runtime selection、usage、jobs、pending inputs/interactions 和 event cursor。
- [ ] 在真实 CLI/tmux 中主动断开并恢复 socket，覆盖 stream 中、permission/question 中和 idle 三种状态；检查无重复 entry、重复响应或 sending 残留。
- [ ] 覆盖 reconnect 与 session switch 交错、失败 switch 保持原 stream、cursor expiry baseline rebuild 后 reader intent 明确重置或保留。

### Resize、focus、selection 与 overlay

- [x] pilot 覆盖滚离尾部的 entry/行偏移、composer draft/focus/selection；真实 PTY 覆盖 80×24 ↔ 100×28 长历史流式 resize。
- [x] permission/question modal 在 80×24 保持完整边界与键盘操作；外部 resolution 关闭陈旧 overlay 并恢复 composer focus。
- [x] Ctrl+E 展开/收起 Think/tool 后不改变 reader intent；短 block 不预留固定 12 行空白。
- [ ] 在真实断线/重连时保持或有意重建 overlay、focus、selection、fold 和 scroll anchor；用稳定屏幕条件取 capture，不能抢中间帧。
- [ ] 验证超窄终端的最小可操作行为；无法完整展示时给出明确限制，不静默裁掉 interaction action。

### Settings 与状态信息

- [x] F2、`/status`、Model picker、只读 session policy、plugin catalog 和支持的标量 schema 已有基础 pilot/PTY；Esc 保留草稿。
- [ ] 统一 Settings 的真实 scope 与数据来源，完成 provider/model/effort/agent、permissions/sandbox 和可编辑 plugin config 的成功/失败反馈。
- [ ] 对 plugin config revision conflict 实现 reload/重试选择；复杂 schema 不猜测、不提供 raw JSON 逃生口。
- [ ] 未有公开持久化入口的 Appearance 只允许当前实例预览，不能声称跨进程保存；未实现页明确标注 unavailable。
- [ ] 复核 80×24 的导航可发现性、键盘可达、返回焦点/草稿和 status/footer 单行优先级。

### Commands、queue 与 subagent 展示

- [x] commands 来自 client commands port/服务端目录；TUI 不复制 `/compact` 等服务端 vocabulary。
- [x] pending input panel 展示服务端 target；Enter/Alt+S 默认 next-step steer，显式 queue 仍可由公开 API 使用。
- [x] `Ctrl+T` 使用公开 thread catalog；subagent thread 只读，Esc 回 main，主会话只显示紧凑 job 状态，不复制 child transcript。
- [ ] 后续 subagent `send_message` 若实现，TUI 只显示标准工具结果和既有 input/message events；不得在客户端新增寻址、delivery 或唤醒逻辑。

## 下一执行顺序

- [ ] 先等待底层数据模型、运行时恢复、持久化和插件职责收口；TUI 不为未稳定 contract 建兼容层。
- [ ] 第一 TUI 切片：真实 socket 中断/reconnect 的三状态矩阵，并读取当轮 capture 与 canonical history。
- [ ] 第二 TUI 切片：Settings scope、mutation、revision conflict 和返回会话可用性。
- [ ] 第三 TUI 切片：Unicode/长代码/超长 tool output 与超窄终端限制。
- [ ] 若发现服务端/协议缺口，提交最小生产证据并回到 owner 修复；不得在 TUI 旁路公开 API。

## 完成门槛

- [ ] attach/resume → edit/paste → steer/queue → Think/text/tool → permission/question → follow-up → compact → switch/reconnect → exit 的各转场均有当前真实生产路径证据。
- [ ] 80×24、100×28 和宽屏下 transcript/composer/status 可操作；resize、fold、focus、selection、overlay 和 scroll intent 行为明确。
- [ ] Settings 只呈现可由公开 API 正确读取或修改的能力，scope/conflict/error 均可见。
- [ ] 退出后无残留 client worker、tmux session、server process 或 UDS；失败路径同样清理。
- [ ] focused 与完整 TUI/ACP 在同一最终提交上通过；读取实际 capture 后记录未运行的外部 provider/终端矩阵。
- [ ] 不以“像 Claude Code”、旧 PNG、旧 worktree 或单纯全绿测试替代上述行为证据。
