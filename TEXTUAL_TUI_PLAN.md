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
- [x] 主线 TUI 边界静态审查与 layering 测试通过：业务数据走公开 API，state/status/timeline 不 import Textual，view 不 import controller/transport，controller 不 import app，transport 不 import view/app/controller。工具显示使用声明的类别，命令来自公开目录。

## 已验证产品基线

- [x] 紧凑单列布局使用 `❯` user/composer、`●` assistant/tool、底部 status/footer，无永久顶部 session bar。
- [x] reasoning delta 渲染独立 Think block；无 reasoning 时只显示 transient `Thinking…`。Think/tool 使用受限高度 disclosure；收起显示标题和最多两行 preview，final assistant reply 永不折叠。
- [x] tool call 与 result 为独立 disclosure；permission 和有限 question 使用 typed modal/chooser，开放问题使用 typed answer flow，不伪造消息或 slash command。
- [x] RuntimeNotice/context 注入在 transcript 中以来源明确的紧凑 trace 可见，内部 XML/JSON 不作为用户消息展示。
- [x] status 保留 usage、context/window、cache、activity 和必要 session/runtime selection；窄屏按优先级裁剪，不伪造正常比例。
- [x] Enter 默认 steer，Shift+Enter 换行；显式 queue 和 interrupt 独立。长 paste 保留原文与 canonical identity，pending input 来自服务端权威列表。
- [x] session/thread switch、只读 `/thread`/subagent view、jobs hydration、compact summary、退出和跨进程 resume 走公开 API。
- [x] `0ce4cc6` 修复 `history_window=50` 小于 `transcript_limit=100` 时 PageUp 加载却不显示旧页，以及 PageDown 不回 live tail；局部 generation 阻止已取消 scroll callback 复活。
- [x] 真实 30 轮历史后，第 31 轮流式期间保持旧页；80×24 ↔ 100×28 resize 保留 anchor、fold、composer focus/selection，完成后仍留旧页，PageDown 回尾。
- [x] `b72e668` 的完成帧同时等待 collapsed Think marker、preview、final 和 Ready；它强化证据，不改变 block 语义。
- [x] 最近集成 `bb88087` 的 TUI + ACP：`895 passed in 143.59s`。包含真实 uvicorn/HTTP/SSE、tmux 及 permission pending 中途断网；主代理读取 `/tmp/xbot-integrated-tui-20260929-final/` 当轮渲染。不代表外部 provider 或其他断网状态已验收。

## 当前验收矩阵

### 当前 Markdown、复制和输入修复

- [x] 查阅 Codex 的 Markdown/streaming/math、OpenTUI Markdown/incremental parser 与 Glow/Glamour 的源码；采用源码与显示行分离、按终端宽度排版的方向，不替换 Python 客户端框架。
- [x] Rich Markdown 准备在后台执行；DOM batch 仅用于应用准备完成的结果，准备期间输入与滚动仍可响应。
- [x] Markdown 按显示行缓存、可见行绘制；宽度变化后台重排，保留原显示直到新宽度准备完成。缓存仅保留两个宽度，不随会话增长。
- [x] Markdown 接入 Textual 的选区 offset、选区高亮和 Ctrl+C；首行标题使用原始源码解析，speaker marker 在显示层添加。
- [x] 工具结构参数以 YAML literal block 呈现真实换行；字面量反斜杠保留，40/80 列长路径与中文自动折行，展开块仍受最大高度限制。
- [x] Shift+Enter 替换选区并移动光标；Ctrl+E 恢复行尾操作，Ctrl+O 展开折叠块，不抢占标准多行编辑键。
- [ ] 流式 Markdown 的稳定前缀/可变尾部增量解析尚未实现；不得把后台全篇解析称为增量解析。
- [ ] 公式/流程图尚未增加排版能力；比较成熟实现与 Python 集成成本后决定，不能用代码高亮充当图形支持。
- [x] 本轮真实 CLI/PTY 49 项通过；修复后台准备遗漏整组 remount 的未变化条目，权限→问题→回复与 reconnect 后原输入完整显示。TUI 全套 888 项、包含新增中文鼠标复制的重点 208 项、Core/集成 813 项通过。

### 输入、流式与 transcript

- [x] 普通输入、多行 bracketed paste、继续编辑、canonical history 和重复输入检查已有真实 PTY 覆盖。
- [x] Think/text/tool/permission/question/final/follow-up 的可见顺序与键盘响应已有真实 server/tmux 覆盖。
- [x] reader 离开尾部时，连续 stream、完成和 resize 保持同一可见 entry；显式 PageDown 才恢复 tail follow。
- [x] 真实 80×24 的 Unicode 双场景已验证：80 次重复的超长 output 保持可操作；12 次重复场景同帧可见 tool header、JSON args、result、Think、final 与 composer。按 terminal-cell 宽度检查，不按字符串长度。
- [x] `da1c937` 用真实 uvicorn/TUI 验证部分 Think/text 后 provider failure、Esc interrupt、shell exit 7：内容/错误保留，Running 结束，后续消息正常完成。主代理实际查看当轮 provider failure SVG 渲染；未把部分内容伪装为成功完成记录。

### History、switch、reconnect 与 resume

- [x] PageUp 从真实 server 加载到历史开头；加载页小于 DOM window 时仍立即显示新页，分页期间不闪回尾部。
- [x] session switch、已有 JSON 状态启动、双客户端隔离、compact 后退出/新进程 resume 和继续第六轮已有真 PTY 证据。
- [x] switch/rebuild 后 authoritative thread read 恢复 turn count、runtime selection、usage、jobs、pending inputs/interactions 和 event cursor。
- [x] `bb88087` 在真实 CLI/tmux 的 permission pending 阶段主动切断 TCP，服务端/session 不重启；新连接恢复后原选区可操作，继续 question/reply/follow-up，公开 history 无重复。转发器仅位于测试侧，无生产测试 API。
- [x] `19fa747` 真实 socket 中断覆盖 reasoning streaming、question pending、idle 和 session switch。第 31 轮改用测试 provider gate 保持 Running 验证窗口，不再依靠抢瞬时帧；未改生产 provider 接口。
- [x] 公开 API 正常发送回合自然淘汰 SSE cursor，旧 after 返回可重试 409；既有 transport 验证 baseline rebuild/失败 switch/旧 reader 失效。生产转场与 transport 恢复策略分别列证，不宣称所有竞争排列已穷举。
- [x] `e395c96` 删除 submit 无条件跳尾：阅读历史时继续保持 anchor，原本 following-tail 时继续跟随；视图明确实现已有 reader_at_end 契约。

### Resize、focus、selection 与 overlay

- [x] pilot 覆盖滚离尾部的 entry/行偏移、composer draft/focus/selection；真实 PTY 覆盖 80×24 ↔ 100×28 长历史流式 resize。
- [x] permission/question modal 在 80×24 保持完整边界与键盘操作；外部 resolution 关闭陈旧 overlay 并恢复 composer focus。
- [x] Ctrl+O 展开/收起 Think/tool 后不改变 reader intent；短 block 不预留固定 12 行空白。
- [x] 当轮 PTY capture 验证重连后的 question Tuesday 选区、permission modal、idle Ready 和长历史 anchor；稳定条件取帧，原始产物路径见 `XBotv2/docs/tui-closure.md`。
- [x] 本轮可操作尺寸门槛为 80×24 与 100×28，现有 pilot 覆盖宽屏；不宣称低于 80×24 的所有终端可操作，不为此新增响应式框架。

### Settings 与状态信息

- [x] F2、`/status`、Model picker、只读 session policy、plugin catalog 和支持的标量 schema 已有基础 pilot/PTY；Esc 保留草稿。
- [x] Settings 使用现有公开 selection/catalog API；plugin config 明确 workspace scope，permissions/sandbox 因无公开写 API 保持只读。复杂 schema 明确 unavailable，无 raw JSON 旁路。
- [x] `0a80344` 验证 rev-1 冲突 → 重读公开 catalog 得到 rev-2 → 保留编辑草稿 → 再次 Apply 使用 rev-2 成功 → Esc 恢复 composer 草稿及焦点。既有实现已支持该链路，无须新增状态或重试框架。
- [x] Appearance 只影响当前进程，不承诺持久化；80×24 导航、键盘可达和退出后的焦点/草稿有当前 pilot 证据。
- [x] `e395c96` 删除 modal 下硬编码 `/approve`、`/answer` 提示；composer 直接提示在现有 dialog 操作。

### Commands、queue 与 subagent 展示

- [x] commands 来自 client commands port/服务端目录；TUI 不复制 `/compact` 等服务端 vocabulary。
- [x] pending input panel 展示服务端 target；Enter/Alt+S 默认 next-step steer，显式 queue 仍可由公开 API 使用。
- [x] `Ctrl+T` 使用公开 thread catalog；subagent thread 只读，Esc 回 main，主会话只显示紧凑 job 状态，不复制 child transcript。
- [x] subagent `send_message`/`followup_task` 只产生标准工具结果及既有 input/message/job events；TUI 不新增寻址、delivery 或唤醒逻辑，`/thread` 继续使用服务端 thread catalog 只读查看。

## 下一执行顺序

- [x] 2026-09-30：provider 超时改为 SDK transport inactivity timeout，沿用统一重试；真实 SDK 本地 SSE 验证长思考、header/read timeout、重试恢复和 partial 不重放。
- [x] 2026-09-30：同一 Markdown 文档的测量/绘制复用 Rich 输出，保留样式、代码高亮及 resize 重排；思考更新不重解析未变化的答案，未变化的状态/提示/工具详情不刷新。
- [x] 2026-09-30：真实双客户端/resume 和输入确认链路复验；选择性刷新只用于 assistant，用户 delivery 变化仍更新 inline 标记。性能证据和限制见 `XBotv2/docs/tui-closure.md`。

- [x] TUI 已随 session、loop、interaction 类型迁移到唯一 owner；InboxTarget 显式渲染 value，没有保留旧 import、DTO 或客户端兼容分支。
- [x] 第一切片：真实 socket 中断、switch、cursor expiry 和 Running gate 已完成；主代理读取 post-outputs 的长历史/Unicode 渲染，最终集成仍需重跑并读取当轮产物。
- [x] 第二切片：Settings scope、mutation、revision conflict/retry 和返回会话可用性已完成。
- [x] 第三切片：Unicode/超长 tool output 与 80×24 尺寸门槛已完成；不承诺无界终端矩阵。
- [x] 已定位的事件/类型问题回到 producer owner 修正，真实断网测试只使用测试侧 TCP 转发器；未增加生产测试后门或 TUI 业务旁路。

## 完成门槛

- [x] 上述 attach/resume、输入、stream、交互、compact、switch/reconnect、退出转场分别由真实 server/PTY 或真实 server/Textual pilot 覆盖，证据见 `XBotv2/docs/tui-closure.md`；不宣称单个用例穷举全部排列。
- [x] 80×24、100×28 的当轮 PTY 与既有宽屏 pilot 验证 transcript/composer/status、resize/fold/focus/selection/overlay/scroll intent；未承诺更小终端。
- [x] Settings 的 scope/conflict/error 可见，无公开写入口的能力明确只读，不新增客户端业务旁路。
- [x] 正常与异常用例均经过 client/server/worker 清理；最终 tmux 核查无 xbot 测试 session，未清理用户自己的 tmux session。
- [x] 精确最终代码 `8d61769`：TUI/ACP 903 passed（165.00s），同版本 Core 650、HTTP/fold-in 119 passed。主代理读取 `/tmp/xbot-final-target-20260929/` capture，并实际查看 provider failure、interrupt、tool failure SVG 渲染；本地产物不提交。
- [x] 外部 provider、所有终端模拟器及低于 80×24 的矩阵未验证；这轮证据仅描述实际执行路径，不以旧截图或全绿数量替代。
