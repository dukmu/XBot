# XBotv2 当前执行计划

> 本文件只保留当前事实、优先级和完成门槛，不是历史日志或设计 gold
> truth。设计文档和生产代码都是待审查材料；判断依据是用户约束、明确的
> owner，以及从生产路径得到的可观察行为证据。

## 已确定要求（不代表已全面验证）

- [x] 主线是 `XBotv2`；不恢复旧实现、兼容双读/双写、wrapper executor 或协议副本。
- [x] Core、protocol、provider、tool、plugin、client 各自拥有自己的语义；不因 UI 或插件需求向底层模型填字段。
- [x] 持久化是可选能力；缺少持久化不得阻断实时 Agent、工具、事件流和客户端。
- [x] StateService 当前是命名空间 KV 的单 JSON 原子快照，不是 append-only log；`d988e4c` 恢复该职责并覆盖已有状态启动。
- [x] 本轮不做 WebUI、bench 驱动优化、外部供应商矩阵或新框架。
- [x] 本轮切片先核对 owner 和生产入口；缺陷先复现，再修复及扩大验证。类型整理以唯一归属和有效状态为准，不以测试数量定义完成。

## 当前稳定基线

- [x] 启动与状态：`d988e4c` 修复已有 JSON KV 状态导致 `/sessions` 404；不得重新把 StateService 日志化。
- [x] 输入语义：`1f81dc5` 保留 steer 身份并只在 model context 添加临时补充说明；原始内容和客户端投影不变，steer 不等于 interrupt。
- [x] 持久化读取：`a5ac52b` 让活跃 reader 增量读取外部追加的 trace 后缀；失败后丢弃缓存并明确重试，不复制完整投影。冷读仍需完整读取。
- [x] jobs：`b989548` 覆盖 runner 启动前取消、唯一终态/通知、wait 释放，以及父代理读取并发子应用的 cancelled/failed 结果。
- [x] TUI：`0ce4cc6` 覆盖默认 Enter steer、真实长历史分页、流式阅读 anchor、80×24 ↔ 100×28 resize、PageDown 回尾和陈旧滚动回调失效；`b72e668` 只强化完成帧断言。集成 TUI + ACP 为 `895 passed in 141.51s`。
- [x] 真实 HTTP 测试宿主已由 `f2286f0` 收回同一 event loop；steer HTTP 投影断言由 `5270207` 覆盖。局部绿测不等于总体迁移完成。

## 1. 底层架构与数据模型收口（对应用户旧项 6，当前最高优先级）

- [x] `fbac3f1` 删除 AgentCreateOptions 的重复启动身份/上下文；SessionLaunch 为唯一来源，前者仅保留 Agent 选择/定义/model override。真实启动、恢复、caption、子应用相关验证通过，无兼容字段。
- [x] `13e1a9f` 将 assistant history 提交置于完成事件之前，覆盖无持久化、持久化及写入失败；`6917e73` 删除该切片顺带增加的状态通知，未放宽原事件次数测试。`5b5de7b` 删除 SSE 内建插件事件名单，保留 canonical 特殊投影与客户端未知事件校验。
- [x] `d1d0e9b` 拒绝 cold resume 的 workspace 冲突；直接 application 启动也在 session 水合边界校验，避免 metadata 与变量/配置分叉，不新增兼容路径。active attach 仍复用既有 runtime。
- [x] `648dbf9` 将 session 事件从 HTTP protocol 收回 `session/events.py`，删除 AgentConfiguredData/QueueUpdatedData/InputDeliveryData 副本。公开 SessionEvent 只有开放事件接口一种含义；输入 accepted 必须明确 target，不再靠 optional 字段混用事件语义。
- [x] `216cd24` 将 loop outputs 收回 `agentloop/outputs.py`；HTTP protocol 只保留工具目录 DTO/路由。进程内 hook 结果删除无人读取的 kind 字符串，lifecycle/turn payload 不再携带可变 session 引用；无旧路径兼容导出。
- [x] `69be637` 将 interaction 请求/结果/通知收回领域模型，HTTP 仅保留请求响应 DTO；ask_user 的选项数规则回到工具边界。删除 session 私有 publish 透传，公开事件入口标注 SessionEvent。
- [x] 审查保留 ToolExecution/ToolMessage 的唯一显式 schema 前向引用解析；它不是业务动态注入，不为移除 model_rebuild 新造执行抽象层。
- [x] `XBotv2/docs/runtime-closure.md` 已记录 application → loader/XCore → loop/session → transport/client 的实际 owner、依赖及生产证据。application 仍是入口，各能力依据依赖规则装配，无必须挂载的插件名单。
- [x] 生命周期审查覆盖启动失败、可选插件失败、依赖缺失、关闭与提交竞争、资源释放；既有初始化/卸载测试与本轮关闭回归分别列证，不依赖析构。
- [x] 输入、runtime notice、tool、interaction、completion 各自保留身份及恢复语义；进程内 hook、loop output、session event 分开，持久化恢复与 SSE 重放语义不同。
- [x] `8d61769` 将现有 InboxTarget 移至 core.domain：inbox、accepted event、pending projection 复用同一枚举，删除重复 Literal 和旧 agentloop 导出。JSON 值不变，TUI 显式展示 value，并删除无效 target 判空。
- [x] 剩余迁移只记录上述已找到的类型/依赖，不从旧 checklist 推导框架、callback 或新状态服务。
- [x] 最终代码 `8d61769`：Core 650 passed、3 deselected（101.94s）。仅忽略 WebUI server 文件与 3 个 CLI web 入口；不再使用误排除 browser/core server 用例的 `-k 'not web'`。

## 2. 运行时失败与恢复（对应用户旧项 2）

- [x] `d9f96cb` 修复关闭期间仍接受输入/启动回合，以及 inbox discard 失败跳过资源清理；复用现有 submission lock 和 closing 状态，保留首个失败。与启动事实迁移合并后 Core + HTTP/fold-in `743 passed, 17 deselected in 172.46s`。
- [x] `70c9fd2` 明确拒绝已关闭 event stream 的新订阅，防止注册永远不会被唤醒的 waiter；不增加额外关闭状态。
- [x] 已按真实入口核对启动/plugin/provider/tool/hook 失败、取消、中断和 close/submit；证据索引见 runtime-closure.md，包含未提交输入恢复与工具 side-effect 不盲目重放。
- [x] permission/question 的 exactly-once resolution、外部 resolution、取消与重复响应有 Core/HTTP 证据；断连后的 overlay/selection 有真实 socket-cut TUI 证据，两类证据分别记录。
- [x] FIFO、claim/commit/rollback、安全 step steer 和 next-turn queue 由 inbox/input-routing/fold-in 生产测试覆盖，不增加第二份输入状态。
- [x] jobs 已覆盖 runner 尚未启动时取消及 cancelled/failed 子应用结果读取；后续只补实际缺失的竞争路径，不造通知框架。
- [x] 真实 HTTP/SSE 验证 sequence、自然 cursor expiry、主动切断 socket 后重连与恢复；transport 测试验证 gap/watchdog/baseline 策略。明确区分网络证据与受控策略测试。
- [x] 最终代码 `8d61769` 的完整 HTTP + fold-in 为 119 passed（96.22s），与上述 Core 共用同一版本；失败点/预期结果见 runtime-closure.md。

## 3. 持久化与复杂度（对应用户旧项 3）

- [x] 当前普通对话生产路径量测 10/20/40 回合：history 为 10,501/21,049/42,147 bytes，KV 累计写入 1,931/3,701/7,241 bytes，metadata 均 3 次写入；未发现新 O(N²)，不据此更换存储格式。此证据不覆盖所有插件/大目录场景。临时测量脚本 `/tmp/runtime_persistence_audit.py` 未提交。
- [x] 区分职责：StateService 是 KV 快照；session trajectory/inbox 是追加记录。撤回的 StateService JSONL 设计不再作为候选方案。
- [x] 外部 trace writer 的已知前缀可增量读取，损坏后缀不发布部分结果；缓存丢弃后的重试重新验证磁盘事实。
- [x] usage 累计快照包含辅助请求及已实际消耗的 provider usage，不等于成功写入 messages 的用量之和；history 写入失败不应回滚已消耗的 usage。未据此新增日志或更改统计语义。
- [x] 已核对 metadata、usage、插件 KV、transcript/trajectory 和 artifact 的 owner、格式与恢复路径，见 `XBotv2/docs/storage-plugin-closure.md`。usage 包含辅助请求，不是 messages 的冗余副本；不新增 requests 日志。
- [x] 现有计数测试核对 warm compact、12 个活跃路径越过 8 项缓存、外部追加分页与 close/reopen：活跃 reader 只读后缀；compact 每次只 fold 新记录；失去缓存后允许一次线性冷读，不宣称零成本恢复。具体测试及字节依据已写入上述证据表。
- [x] `3e72524` 使 cold trajectory 与热读使用同一 canonical fold，拒绝身份重复/无效 replacement；复用 append owner 修复生命周期部分写。`3c0fb15` 验证已有 committed bytes 在失败后原样保留且可重试。
- [x] 子代理改由 SessionManager 的 canonical thread metadata、trajectory、inbox 和运行时状态表达；删除重复的 `threads.jsonl` lifecycle、schema/port/store/path 及测试。持久化未挂载时，实时 child runtime 和协同链路仍可工作。

## 4. 插件职责与生命周期（对应用户旧项 4）

- [x] `5848db3` 修复 skills 注册 namespace 与 guard 不一致导致无法连续加载 Skill；生产 factory/标准工具路径验证，普通未允许工具仍被拒绝。`1d2a180` 保存真实 MCP stdio 自动回归：发现→模型工具调用→结果→销毁后子进程退出。
- [x] `XBotv2/docs/storage-plugin-closure.md` 已按实际能力记录 compact、goal、todolist、skills、MCP、browser 的注册、事件、恢复、失败和卸载证据；subagent child lifecycle 与既有 jobs 终态验证分属各自 owner。
- [x] 公开链路验证 goal/todo 状态先保存再通知、close/resume 恢复、goal 唤醒自主回合、todo/compact 提醒不唤醒回合；证据表列出实际运行的 8 个 HTTP 用例（9 个参数化结果）。不新增通知框架。
- [ ] 早前 goal/todo 的具体异常反馈没有原始 trace，尚未复现；不能将上述公开链路通过写成该历史问题已修复。
- [x] 已核对部分初始化失败的注册回滚及卸载清理；MCP 真实 stdio 子进程和 Browser 本地 HTTP/Chromium 清理通过。不虚构无持久状态插件的恢复能力，不扩展无界安全矩阵。

## 5. Textual TUI 产品验收（对应用户旧项 5）

- [x] 真实 CLI/tmux 已覆盖 permission→question→长 paste/follow-up、session switch、已有状态 resume、compact→新进程 resume、长历史分页、流式 anchor、折叠和动态 resize。
- [x] Enter 默认使用现有 `delivery="steer"`，Shift+Enter 换行；显式 queue/interrupt 独立，不改用户原文、不发送额外事件。
- [x] 当前产品契约保留分页历史、只读 `/thread`、紧凑单列布局、受限 Think/tool 展开窗口、永不折叠 final reply、可见 context trace、typed permission/question modal，以及 usage/context/cache 状态。
- [x] `bb88087` 真实 CLI/tmux 在 permission pending 时主动切断 TCP，重连后选区及待回答交互保留，继续 question/reply/follow-up，公开 history 无重复；只增加测试侧透明转发器，无生产后门。主代理已读取本次稳定 permission-reconnected capture。
- [x] `19fa747` 补齐 streaming/question/idle/switch 的真实 socket cut 及自然 cursor expiry；长历史 Running 阶段由测试侧 provider gate 控制。`e395c96` 修复历史阅读中提交消息强制跳尾，移除 modal 下硬编码命令提示。
- [x] `0a80344` 验证 Settings rev-1 conflict → reload rev-2 → 保留 draft → retry success → 恢复 composer draft/focus。scope 由公开 catalog 提供，无写 API 的权限/sandbox 保持只读，不为填页面扩展服务端。
- [x] 真实 80×24 Unicode args/result 双场景、80×24 ↔ 100×28 流式历史 anchor，以及已有宽屏 pilot 均有证据；本轮不宣称支持无界终端尺寸矩阵。具体路径见 `XBotv2/docs/tui-closure.md`。
- [x] `da1c937` 补齐部分 Think/text 后 provider failure、Esc interrupt、shell 非零退出的真实 server/TUI 渲染与后续成功输入；主代理已实际查看 provider failure SVG 的本地渲染。
- [ ] 外部 reasoning-capable provider 未在本轮请求；受控 loopback 只验证本地交互路径，供应商互操作单独保留未验证项。
- [x] 最终代码 `8d61769` 的 TUI + ACP 为 903 passed（165.00s）。真实用例检查公开 canonical history；主代理读取 `/tmp/xbot-final-target-20260929/` 当轮锚点、Unicode 和重连 capture，并查看三个异常终态 SVG 的实际渲染。

## 6. 子代理协同与只读 thread 查看

- [x] 原 interactions `send_message` 已更名为职责明确的 `notify_user`；subagents 独占 `send_message(target, message)`，不保留旧同名语义或 delivery 兼容参数。
- [x] `spawn_subagent` 返回稳定 direct-child thread ID 和单次执行 job ID；definition/display name/job ID 均不充当消息目标。
- [x] `send_message` 写入现有 `RuntimeInput`/canonical inbox：运行中在安全 step 边界领取，空闲时保留且不唤醒新 turn；`followup_task` 在同一 thread 新建单向终态 job，不复活旧 job。
- [x] `SessionManager` 是 child runtime、resume、transcript、inbox 和关闭的唯一 owner；旧 `ChildApplications`/`ChildApplicationSession` 一次性执行器及其 contracts 已删除，不增加第二 mailbox 或消息日志。
- [x] 默认只允许父代理寻址其 direct child；成功结果返回 target thread、input ID 和当前关联 job（若有），未知目标和 closing 明确失败。
- [x] `/thread` chooser 显示 main/subagent、current、working/idle/closed 和稳定 thread ID，当前项预选；child transcript 只读，Esc 返回 main 且保留 draft。
- [x] 真实 server/TUI 覆盖 child 创建、运行中被第二客户端发现、只读查看、返回 main 和跨进程恢复查看；生产 server 测试覆盖运行中消息、空闲不唤醒、FIFO follow-up、同 thread 新 job 和无重复消费。
- [x] 生产路径额外覆盖 inactive child 原 ID 恢复 follow-up、越权 sibling child 明确拒绝、父关闭只清理其 direct child；没有为这些用例新增协议或调度层。
- [x] 服务进程完全退出并重新启动后，父 thread 与既有 child 都从持久化身份恢复；`followup_task` 在原 child ID 上建立新 job，第二次响应可由 `read_subagent` 读取。
- [x] submit 已进入 session port、父 thread 随即关闭的窄竞态有确定失败结果：child 先由唯一 owner 关闭，迟到投递返回 `subagent_message_failed`，不遗留 pending input 或孤儿 runtime。

## 7. Goal / todo 产品设计对齐（当前工作）

- [x] 对照 Claude Code 官方 task/goal 与 OpenCode todo 文档，取舍见 `XBotv2/docs/goal-todo-design.md`；不把实验性 agent teams 的共享调度搬入线程任务列表。
- [x] Todo：支持撤回依赖、同次修订后领取；一次有效变更发布一个快照，重复请求不制造进度；完成提醒不猜标题、不强制委派。真实应用工具路径、失败原子性及 close/resume 测试 16 passed。
- [x] 按用户指定补查 Codex 固定提交的 goal/plan、DSH 固定提交的 goal/round-driver/todo、OpenCode todo/会话循环，以及 Claude Code 内置 goal 官方文档；路径、版本和证据边界见 `XBotv2/docs/goal-todo-design.md`。移除第三方续跑插件作为设计依据，并将源码阅读与运行验证分开记录。
- [x] Goal：按 Codex/DSH 原会话续跑与显式状态工具实现，删除全量 transcript/evaluator 及其专属定时器；5.6 Sol 实现，独立 5.6 Sol 迁移生产路径测试，主代理审查现有事件/inbox 归属与恢复竞态。无 GoalConfig、无兼容旧 evaluator 的分支。
- [x] Goal 控制与统计：明确暂停/继续/替换/清除和已排队轮次归属；沿用现有 inbox，不增加调度器。只统计整个 goal 过程（含所属回合收尾），不提供 token 预算/用量限停；暂停恢复不清零或重复累计，结束后无关会话不计入。
- [x] Goal HTTP 验收：真实工具链、两轮自动续跑、显式完成/阻塞、close/resume/compact、后台 JobRegistry 完成与运行中 steer 原文到达模型；精确 usage 结算与终态冻结。完整 HTTP 109 passed，最后新增 steer 内容断言单独复跑通过。
- [ ] Todo TUI 依赖/可执行任务展示的专门交互验收尚未新增；本轮仅完成任务修订与恢复生产路径，不以 goal 测试代替。
- [x] 本轮 goal TUI 消费适配与渲染：公开 GoalChanged → reducer → EntryWidget → 80×24 SVG，主代理用浏览器渲染后查看 `/tmp/xbot-goal-tui-render/goal-browser.png`；active/disarmed 明确显示需要 resume，未运行不会伪装运行。TUI 901 passed；新 goal 的完整交互另行验收。
- [x] Goal 完成工具计数、运行中替换目标归属、原子状态/计费基线写入、后台 job/compact/steer 组合已修复并验证。核心回归 655 passed；最终受影响 core/startup/todo/TUI 组合 276 passed，含 goal 14 项。无付费 provider 请求，不将受控模型测试写成供应商互操作验证。
- [x] 新 goal 真实 server/TUI 主流程：composer 输入 `/goal` 后两轮执行、标准 get_goal/update_goal、完成后 Ready，画面与公开 history 验证。主代理查看 `/tmp/xbot-goal-real-tui/goal-browser.png`，显示 2 次执行、2 次工具、24 in / 9 out / 33 total；这是 uvicorn + Textual 交互测试，外部 provider 与独立 PTY 分别验收。
- [x] 最后新增 goal 场景后，主代理复跑整个 `test_app_real_server.py`：49 passed（101.74s）；`git diff --check` 通过。已有 `.worktrees/` 与本地运行产物不纳入提交。
- [x] 当前工作树另行直接启动 `.venv/bin/xbot tui --provider llamacpp --no-plugins`：空会话进入 Ready，Enter 提交后 composer 清空、用户消息与 Thinking 出现、状态进入 Running；provider 67 秒无 delta 后 Esc 产生 `turn_cancelled` 并恢复 Ready，Ctrl+C 后 tmux/process 正常退出。该次只验证真实启动、提交、中断和清理，不记录为 provider 完成响应。
- [x] Provider operation 新增 YAML `request_timeout_seconds`（默认 60 秒），范围包含 adapter 内部重试；超时发布 typed `provider_timeout`。直接启动真实 TUI 指向不可用 loopback provider，1 秒后 transcript 显示 error、状态进入 Error，随后正常退出。
- [x] 删除重复的 `api_key_env` 配置路径；凭证统一使用配置加载期 `$${env:NAME}` 展开。MiniMax 临时配置仅使用 `api_key: "$${env:MINIMAX_API_TOKEN}"`，真实 TUI 请求返回 `env-expansion-ok`，Think/final/usage/context 均完成渲染。
- [x] 状态栏的大 context window 使用紧凑单位（MiniMax M2.7 显示 `204.8k`），有 usage 且 cache read 为零时明确显示 `cache:0%`；`/status` 继续保留精确 token 数。

## 当前交付门槛

- [x] `8d61769` 是进入第 6 节前的历史稳定基线，不是当前工作树的最终验收代码；其 Core 650、HTTP/fold-in 119、TUI/ACP 903 结果不得用于替代当前子代理改造后的验证。
- [x] 第 6 节当前工作树已完成 `/thread` 只读查看、稳定 child thread、send/follow-up、跨服务重启 resume、权限边界和关闭竞态；同时删除一次性 child executor 与重复 lifecycle persistence。
- [x] 当前验收树的完整非 WebUI Core 为 654 passed、3 个 CLI WebUI 入口 deselected，`test_web_server.py` 未运行；完整 HTTP 为 111 passed。完整 TUI + ACP 首轮有 911 passed 和 1 个仍断言原始 context 整数的失败；更新为紧凑单位后，完整真实 TUI 文件 49 passed。
- [x] HTTP 全量复跑发现 goal + compact + resume 的旧断言漏算自动摘要请求；生产 usage 显示该 goal-owned resume 回合实际为 6 input / 3 output（状态工具请求、自动 compact、最终 invoke）。测试现按 UsageManager 前后增量验证，完整 HTTP 复跑通过。
- [x] 未删除失败测试来换取绿测，未以 mock backend 替代上述真实 HTTP/TUI 路径；测试隔离导致的 loopback bind 失败已在允许本地 socket 的相同命令下复跑通过。
- [x] 未运行 WebUI（用户排除）和无界安全/终端矩阵。本轮执行 3 次短 MiniMax 请求：首次确认真实完成，第二次确认 `$${env:...}` 配置展开，第三次确认 `cache:0%` 与 `ctx:3.8k/204.8k` 的最终状态栏。
- [x] `git diff --check` 与受影响 Python 文件编译检查通过；真实终端测试结束后无 xbot 测试 tmux session。本地 capture、已有 worktree 和用户无关修改不提交。

## 8. 整体设计实施迭代（2026-10-27 起）

本节按用户提供的 `Python_XBot_整体分析与设计.md` 第 14—15 节推进，追加记录，不改写上文历史结论。上文的旧范围和旧测试结果不代表本轮验收；本轮允许按新方案迁移存储、运行关系和客户端。用户指定在 `refactor-all` 工作，覆盖原分支命名约定，不合并或推送 `main`。

- [x] 固定起点：`8d318ad8eea8bfeed24a1d9c5a1e94a5f1792009`，与设计审查基线一致；由当前基线创建 `refactor-all`。本地未跟踪设计稿和 `.worktrees/` 保留，不纳入实施提交。
- [x] 阅读整体方案与仓库约束；按批次顺序推进，每批先确认业务 owner、依赖和可观察验收，再实施和删除旧路径。用户已授权按该方案推进，核心取舍采用稿中选择。
- [x] 第 0 批：修复 accept/stress 启动参数漂移；网络 MockLLM 经生产 provider adapter；核验独立安装入口。先跑可定位问题的工况，保留精确提交和原始结果位置。
- [x] 第 1 批基础能力：XCore 类型化引用、独立 owner、服务/事件作用域与可等待卸载；两线程隔离、失败回滚和重复开关资源回收。整仓 API/layout 接入继续见下文，不将框架原语通过等同所有业务生命周期已完成。
- [ ] 插件 layout 与公开契约迁移：按第 5 节落实各 owner 的 api/impl/doc/tests，更新跨插件导入、loader、开发说明及检查脚本，移除旧包根转出口和实现混入 API。用户明确要求与整体重构一同推进；先于存储/TUI 扩展收口。
- [ ] 第 2 批：loader 活跃根挂载及 session/thread 生命周期；退出旧 application、MountedAgentApplication 和单用途 factory。
- [ ] 第 3 批：SQLite/JSONL/artifact、投影恢复、工作集与归档分离、流式大结果；退出全状态重写与完整归档常驻。
- [ ] 第 4 批：连续 child thread、统一 wait、jobs 有界清退和可选 team；移除重复结果与通知路径。
- [ ] 第 5 批：通用媒体、共享预算、动态 API、provider/权限边界与日志收尾。
- [ ] 第 6 批：TUI 与客户端契约验收、文档和最终 wheel 产物核验。

### 迭代 0：评估入口与基线证据

- [x] 已确认两个脚本传入 `start_server_application()` 不接受的 provider/workspace/no_plugins 参数；stress 还直接注入 `MockLLM`，其结果不能证明生产 provider 网络协议经过验证。
- [x] 委派唯一 Sol 实施子代理处理 scripts 及聚焦回归；主代理负责边界审查、独立安装入口、实际命令验收与日志。子代理不自行提交，不创建后代代理。
- [x] 用户补充：不必保留旧行为或针对旧行为的冗余检查。由当前职责决定取舍，删除 `accept_runtime.py` 的旧 Goal evaluator、失效日志矩阵及重复全局产物检查，改为短的真实 CLI / 网络 provider / 进程重启恢复验收，不给旧脚本加兼容入口。
- [x] `eeba2ea` 修复安装资源：声明默认 `xcore.yaml`、两包 `py.typed` 和文档，排除 Web 源码/依赖目录的 Python 包发现。原始 clean wheel 缺 YAML 和类型标记；独立环境启动确实因缺 YAML 失败。旧 checkout 的 `build/` 还会掩盖类型标记缺失，因此本轮从 `git archive` 干净源码构建。
- [x] `2681cf0fe5e933c6b7dc8a35515402893b9970d5` 为本批最终代码提交。stress 删除旧 `ModelChunk` 导入、直接 `MockLLM` 注入与预生成响应列表；loopback 网络 mock 通过正常 LLM 配置及生产 OpenAI adapter 请求。响应按请求生成并冻结身份；session overlay 将工具工作区绑定到临时目录。脚本改用当前 typed response、history 和事件契约。
- [x] 最终网络回归：`.venv/bin/python -m pytest XBotv2/tests/integration/test_stress_harness.py -q`，11 passed / 9.17s，原始输出 `/tmp/xbot-network-harness-pytest-final.txt`。观察到 text/reasoning 和工具参数分片、标准 shell 执行结果、usage、真实 provider 断流和 XBot SSE 截断后的后续输入；HTTP 503、非法 JSON/UTF-8、请求超时均经生产 adapter 返回失败并可继续。启动失败与关闭后 listener/socket/临时目录释放。
- [x] 扩展验证曾运行上述文件及 `test_llm_client.py`、`test_provider_errors.py`，37 passed / 9.21s（`/tmp/xbot-network-focused-pytest.txt`）；之后仅冻结网络 mock 请求身份，已复跑受影响的 11 项，不把旧 37 项描述为新一轮全量验收。
- [x] 子代理实际执行的 CLI：`--scenario streaming,replay,tools --mock-tools --turns 2 --concurrency 1`、`--scenario stream_failure --failure-mode provider`、`--scenario incomplete` 均退出 0；主代理读取 JSON 中每个场景的 errors/invariants。报告分别为 `/tmp/xbot-network-core.json`、`/tmp/xbot-network-provider-failure.json`、`/tmp/xbot-network-sse-truncate.json`。故障场景分别调用以免一次性注入被其他场景消费；不将 `--scenario all` 写成全绿结果。
- [x] 源码 console 验收：`.venv/bin/python -m scripts.accept_runtime --root /tmp/xbot-refactor-cli-source` 通过。两个独立 CLI 进程、4 次真实 provider HTTP 请求，历史由 4 条恢复后续增至 8 条，30 字节附件下载原样，原输入未重复；报告 `/tmp/xbot-refactor-cli-source/report.json`。
- [x] 安装产物：从最终代码的干净 archive 运行 `uv build --all-packages --wheel`，产物 `/tmp/xbot-refactor-final-wheels/`；独立 uv 环境 `/tmp/xbot-refactor-installed/` 安装两包及依赖，从仓库外启动且移除 `PYTHONPATH`。检查默认 YAML、类型标记、文档、内置 skill 实际在 wheel，tests/Web/node_modules/生成 bundle 未进入；清单 `/tmp/xbot-refactor-final-wheel-manifest.json`。最终 console 验收结果见下条。
- [x] 最终安装 console 验收：`.venv/bin/python -m scripts.accept_runtime --root /tmp/xbot-refactor-final-acceptance --xbot /tmp/xbot-refactor-installed/bin/xbot` 通过，进程 405913 / 405978，4 次 provider 请求，4 → 8 条规范历史与 30 字节附件恢复；原始报告 `/tmp/xbot-refactor-final-acceptance/report.json`。该产物对应 `2681cf0`，之后只提交本日志。
- [x] `git diff --check` 与受影响脚本编译检查通过。运行日志、wheel、缓存和临时环境留在 `/tmp`，不进入提交；用户设计稿与既有 worktree 未改动。
- [ ] 第 2 批需修的已复现缺陷：新空 thread 的 `trajectory_page` 直接要求磁盘 thread 而返回 404；`delete_session` 先 close 清空空会话目录、随后再次 rmtree 抛 `FileNotFoundError`。原始失败 `/tmp/xbot-network-stress.json`，主代理核对了两个 owner 调用路径；未绕过失败或提前修补生产生命周期。
- [ ] 未运行：全量 core/integration、Web/TUI/PTY、真实外部 provider/MiniMax、512 轮/4096 步长压测及后续批次验收。本批仅改评估基础设施和打包，尚未迁移运行关系/存储；正常进程重启通过不代表 scope 异步排空、故障恢复或历史驻留上界已验证。

### 迭代 1：XCore 作用域与动态注册所有权

- [x] 按用户指示创建持续 goal，覆盖整体方案剩余批次；goal 保持 active 直到实际交付完成。用户补充简单任务优先使用 6.1 Terra；当前复杂 XCore 改造仅委派一个 Sol，后续简单任务改用 Terra，不叠加 Sol 并发。
- [x] 核对现状：`extend()` 沿用父 fiber，事件 bus 只按名字分发；独立关闭不清理父 effect 中的 child disposer，无法直接作为共享运行根的 session/thread 所有者。
- [x] `20dc817`：唯一 Sol 实施 XCore 的 ServiceKey、EventKey、独立 scope owner、pre-stop/await close；共享唯一 ServiceStore/EventBus/Registry，事件桶按 `(scope,event)`、依赖按 `(label,name)` 索引。主代理审查并补查关闭期间的借用视图、state 属性及并发关闭。完整 XCore 117 passed（原107 + 新10项），涵盖排空失败保留 owner/重试、半激活回滚、兄弟隔离及20轮开关资源基线/弱引用释放。
- [x] `75dbff2`：Terra 接入 tools/commands 显式动态 owner，Skills/MCP 不再使用生产 `cleanup="caller"`；删除 Skills 长期手工注册清单，保留初始化失败和 MCP 断开的必要回滚。注册身份保证旧 owner 不删除同名后续注册；关闭/排空期间拒绝迟到注册。最后 owner import/日志调整后的相关16项通过，含真实 MCP stdio 子进程退出和工具移除。
- [x] `9379c81`：另一 Terra 收口空会话读取/删除边界。更正迭代0的归因：当时 `close_session` 没有调用 `_discard_empty_session`（只有 close_all 调用）；目录从未落盘也可导致显式删除失败。现在 close 清理空目录，delete 在锁内完成关闭/删除并处理未物化对象，保留有持久内容会话；活跃空 thread 轨迹返回空页，未知 thread 和非法 anchor 仍明确失败。聚焦生命周期/目录13项、占用锁10项通过。
- [x] 本批最终代码 `9379c81`：所有代理结束修改后完整产品 core 为 714 passed / 105.16s（`/tmp/xbot-refactor-scopes-core-final.txt`）；完整 integration 为133 passed / 102.60s（`/tmp/xbot-refactor-scopes-integration.txt`），含网络 mock 经生产 adapter 的11项。整合前710项仅作为中间结果，不替代最终714项。
- [x] 实际 CLI stress：`--scenario lifecycle,streaming,replay,tools --mock-tools --turns 2 --concurrency 1` 全部通过，主代理读取每项errors/invariants（`/tmp/xbot-refactor-scopes-cli.json`），上一批两个空会话失败已在同类入口消失。真实 console 重启恢复验收也通过（`/tmp/xbot-refactor-scopes-acceptance/report.json`，4次provider请求、4→8条历史、30字节附件原样恢复）。
- [ ] 后续整合：通用 pre-stop 已可用，仍需各业务所有者在实际卸载路径停止/排空在途工具、命令、交互与 I/O；不以注册移除测试宣称所有在途调用已经回收。共享进程运行根、完整 API 导入、SQLite/历史驻留、真实外部provider及最终wheel/PTY仍按后续批次验证。

### 迭代 2：插件 layout 与运行关系

- [x] 用户再次强调插件 layout，当前将其作为明确实施项。目录按实际职责建立，不添加空模板或只做文件搬移；跨插件从 owner/api 导入，api 不承载初始化/路由实现，不保留旧根转出口作为兼容层。
- [x] 第一组由一个 Terra 实施 usage/token_manager/todolist，提交 `a8867bf`：实际声明与业务实现分离、插件内测试和文档、loader 正规 impl/plugin 发现，以及通用测试发现/打包约定。删除三者旧根模块与转出口；usage 的 USAGE 类型化引用已被真实消费者使用；todo 的图验证属于规范模型，参数解析/路由属于 impl，未新增同义服务。公开API/三插件/loader/config 聚焦75项通过，两个HTTP恢复/状态场景通过；主代理实际console重启验收 `/tmp/xbot-refactor-layout-acceptance/report.json` 通过。
- [x] `a8867bf` 独立安装核验：从该提交干净archive构建两包（`/tmp/xbot-refactor-layout-first-wheels/`），XCore的api模块及类型标记实际入包，XBot的三插件README/doc/impl实际入包，无旧根模块、插件tests及Web；独立安装后真实console重启恢复通过（`/tmp/xbot-refactor-layout-first-installed/report.json`，4个provider请求、4→8条历史、附件30字节）。此证据只对应这个提交。
- [x] 第一组布局检查事实：架构检查27项、契约审计57项尚未迁移违规，均退出非零，不用聚焦绿测替代；主代理实际输出 `/tmp/xbot-refactor-layout-architecture.txt`、`/tmp/xbot-refactor-layout-contracts-final.tsv`。审计按实际api目录识别边界和Protocol，不为每插件硬编码特例；tests不计为生产跨插件导入。
- [x] 第二组由一个 Terra 实施 goal/content_cache/caption/prompts/workspace_instructions，提交 `4f21fba`。GoalChanged与状态模型分开，prompts用实际PROMPTS引用；workspace_instructions没有公共契约，因此未创建空api。各owner的旧根模块/转出口已删除，专有测试/文档迁入owner，开发skill中的旧详情改为指向owner文档的导航，减少重复维护。
- [x] `4f21fba` 验证：五owner及loader/startup/TUI协议状态聚焦272 passed（`/tmp/xbot-five-pytest-final.log`）；Goal HTTP 9 passed（`/tmp/xbot-five-http-goal.log`）；Goal TUI专测1 passed（`/tmp/xbot-five-tui-goal.log`）；TUI收集938项（仅收集，`/tmp/xbot-five-tui-collect.log`）。包含整个test_view_entries的大合跑后段未完成，由子代理终止（`/tmp/xbot-five-pytest-rerun.log`），不计通过也不据此推断新缺陷。
- [x] 基础契约组由唯一 Sol 实施 core/agentloop/context_builder 的api/impl与全仓消费路径；实际LoopState/工作集/metadata状态构造归impl，规范数据及最小消费者契约归api，统一XCore事件调用契约。物理filesystem所有权留给存储批次，不通过API转出口伪装实现已分离。
- [ ] 按同一约定完成其余插件、core 与 XCore 的公开入口消费，处理 LoopState 等混在 contracts 的实际实现，更新架构/导入检查及内置开发指南。
- [ ] 在作用域/契约稳定后完成 loader/session/thread 单进程根接入，退出 application/host/factory 旧层；保留标准执行路径与必要行为验收。
- [x] 基础契约组提交 `ea786b4`：移除 core/agentloop/context_builder 旧根模块和转出口；History/Metadata/LoopState 只保留真实消费者契约，实例、工作集、状态发布和恢复归 agentloop/impl。provider 重试基类归 llm/impl，工具函数执行收回唯一 registry/guard/dispatcher 路径；compact 使用实际注入的 ContextCompiler。Loop 与 context hooks 接入共享 EventKey[payload]，TOOLS/LOOP_STATE/INBOX/CONTEXT_BUILDER 在生产发布、依赖和消费处接入共享 ServiceKey。单用途 factory 及旧 application 尚待下一批退出，不以移目录代替其删除。
- [x] `ea786b4` 冻结源码的最终验证：产品core与owner测试709 passed / 94.79s（`/tmp/xbot-layout-product-core-final.txt`），完整integration133 passed / 90.88s（`/tmp/xbot-layout-integration-final.txt`），XCore117 passed（`/tmp/xbot-layout-xcore-typed.txt`）；源码真实CLI重启验收通过（`/tmp/xbot-layout-source-acceptance/report.json`，4次provider HTTP请求、4→8条历史、30字节附件）。与此前714项相比删掉5项锁定旧包根__all__/私有模块路径的断言，未删除行为测试换取通过。主代理核对345条冻结路径SHA256，提交前后无实现变化。
- [x] `ea786b4` 审计仍报告25项架构违规、46项契约违规（`/tmp/xbot-layout-architecture-final.txt`及`/tmp/xbot-layout-contracts-final.tsv`）；包括其他owner、物理filesystem、旧application，以及新递归检查发现的2处config.UserContext依赖。不隐藏依赖、不将聚焦通过描述为整仓边界已收口。外部provider、PTY和后续存储/协作工况未在本组运行。
- [x] `ea786b4` 干净archive独立安装核验：两wheel构建于`/tmp/xbot-refactor-core-layout-wheels/`，清单`/tmp/xbot-refactor-core-layout-wheel-manifest.json`确认新API/impl/doc/类型及默认YAML入包，旧根模块、tests/Web不在包中。独立uv环境真实console验收通过（`/tmp/xbot-refactor-core-layout-installed/report.json`，进程452868/452943、4次provider请求、4→8条历史、30字节附件）。此证据仅对应本提交，不借用上一轮安装结果。
- [x] 后续布局由三个Terra分别实施config/llm/agents、permissions/sandbox、commands/interactions/compact；只写各自owner，主代理统一跨插件导入和验证，避免并发覆盖消费者。无Sol并行，不创建后代代理。
- [x] 第三组提交 `ab95466`：上述8个owner完成api/impl/doc分离及全仓消费迁移；SETTINGS/LLM/MODEL/AGENT_CATALOG/AGENT_RUNTIME/COMMANDS/INTERACTIONS/PERMISSIONS/APPROVAL/SANDBOX用于真实发布、注入和Context消费。UserContext的唯一规范定义归core.api.domain；沙箱审批使用permissions的ApprovalPort，不再跨插件调用approval实现。10个专有测试文件迁入owner，跨owner集成保留；无旧根转出口和兼容模块。
- [x] 第三组完整回归原始结果：core/owner 708 passed、1 failed（`/tmp/xbot-layout-third-core-final.txt`），integration133 passed（`/tmp/xbot-layout-third-integration-final.txt`）；TUI/ACP 944 passed、1 failed（`/tmp/xbot-layout-third-clients-final.txt`，含真实PTY）。不将失败改写成全绿历史。缺失ServiceKey诊断在XCore责任边界修复并单独提交`7120718`；主代理复跑完整XCore、config及该CLI失败项，122 passed /3.22s（`/tmp/xbot-layout-third-fixes-final.txt`，其中XCore118项）。
- [x] TUI唯一失败独立真实tmux复现：80×24展开长工具内容时标题滚出视口，测试要求标题与参数/结果/回复同屏。只改测试为展开检查内容、折叠再查标题，保留cell宽度和实际历史Unicode内容断言；两个参数场景2 passed /5.65s（`/tmp/xbot-unicode-verified2.log`，原始屏幕在`/tmp/xbot-unicode-verified2/`），主代理读取折叠/展开产物。未为旧同屏假设添加产品状态或布局分支。PageUp对已挂载长内容的导航限制留给TUI批次，未在本组解决。
- [x] `ab95466`生产CLI验收通过（`/tmp/xbot-layout-third-source-acceptance/report.json`，4次provider HTTP请求、历史4→8、30字节附件）。架构检查剩14项（`/tmp/xbot-layout-third-architecture-final.txt`）、契约审计剩29项（`/tmp/xbot-layout-third-contracts-final.tsv`）；检查脚本接入新的实际路径及两种ServiceKey构造语法，HTTP适配器按统一router/http边界识别。未掩盖物理filesystem、会话记录及旧组装层等剩余违规。
- [x] 文档由Terra补核八owner职责、effects、状态及配置；权限/沙箱详情归owner/doc，内置skill只保留导航，28个本地链接有效；移除未经验证的缓存有界/定义共享表述。源码编译和diff检查通过。本组未重复独立wheel、真实远程provider或长压测，最终安装与后续运行根一起验收。
- [x] 第四组提交 `6446213`：三个Terra完成server/protocol/client_transport/ACP、coretools/browser/skills/MCP、jobs/subagents/workspaces，共11个owner。纯wire契约归protocol/api，无假plugin；skills无公开契约，不建空api。workspace重放/订阅实现归impl，Job保留唯一身份/规格/状态，Task和runner归registry；真实JOBS及workspace三服务键接入生产消费。旧根模块及转出口删除，专有测试迁入owner，中央保留真实浏览器、MCP stdio、进程/dispatch期限和跨owner服务集成；迁出后无关helper/fixture/导入已清理。
- [x] 第四组验证：XCore/core/owner同一命令827 passed /97.27s（`/tmp/xbot-layout-fourth-core-final.txt`），完整integration/ACP140 passed /96.48s（`/tmp/xbot-layout-fourth-integration-final.txt`）。分散测试用pytest标准importlib模式避免同名模块冲突，XCore+XBot共1907项可一并收集（`/tmp/xbot-layout-fourth-collect-final.txt`）。不靠改文件名或删测试避免冲突。
- [x] 第四组TUI协议/state/transport/plugin与真实server/PTY聚焦为232 passed、1 failed、1 deselected（`/tmp/xbot-layout-fourth-clients-final.txt`）；未重跑上一组已覆盖、无相关逻辑改动的长历史视口场景，也未重跑所有未改动的view测试。失败是80×24问题卡片等待画框字形超时，问题/选项/操作提示实际可见；单独复现先通过，不据此推断首次失败原因。按用户允许删除旧冗余检查，去掉这一缩放阶段的盒绘字符条件，保留内容/操作/尺寸/实际答复验证，不声称修复resize布局。
- [x] 第四组收尾修正：QUERY_STATUS使用实际EmptyRequest作为EventKey载荷，bail发送请求、handler仍返回ServerStatus；不把响应类型假标为请求。主代理复跑server、SSE及上述真实PTY，共14 passed /13.62s（`/tmp/xbot-layout-fourth-fixes-final.txt`）；清理迁出helper后的5个中央集成文件由Terra复跑19 passed。SOURCE console重启恢复通过（`/tmp/xbot-layout-fourth-source-acceptance/report.json`，进程472146/472241、4次provider请求、历史4→8、30字节附件）。
- [x] `6446213`架构检查仍13项（`/tmp/xbot-layout-fourth-architecture.txt`）、契约审计25项（`/tmp/xbot-layout-fourth-contracts.tsv`），主要为core.filesystem、session公开记录及旧application。未把目录迁移或mock-MiniMax PTY写成真实外部provider互操作。测试日志/截图保持本地，wheel和长压测本组未重复。
- [x] 两个Terra完成TUI实现目录/专有测试与XCore公开入口消费，提交`2df39c1`。TUI前台TerminalClient接口及TERMINAL_CLIENT由tui/api唯一声明，presentation/state/transport/view归impl；27个专有测试文件迁入owner，真实server/PTY保留中央。XCore根不转出口，真实构造类型从xcore.api消费，不造Context代理；错误唯一定义归api，typed事件协议及serial/bail载荷overload完整接入。
- [x] `2df39c1`验证：XCore行为115 passed /1.56s（`/tmp/xbot-xcore-tests.log`），删掉3个旧root清单/版本字面断言，未删生命周期/隔离行为测试；Terra完整TUI owner889 passed /59.92s。主代理真实CLI/PT Y交互与reasoning、client启动生命周期、loader39 passed /9.13s（`/tmp/xbot-layout-last-smoke.txt`），SOURCE console重启恢复通过（`/tmp/xbot-layout-last-source-acceptance/report.json`，4次provider请求、4→8历史、附件30字节）。受限sandbox下TUI停滞被终止，不算结果；允许线程/socket环境的独立运行才计通过。
- [x] `2df39c1`干净archive的两wheel构建/独立uv安装完成（`/tmp/xbot-refactor-layout-complete-wheels/`，清单`/tmp/xbot-refactor-layout-complete-wheel-manifest.json`）：XCore20个条目、XBot495个条目，新api/impl/doc及资源实际入包，旧已迁移根模块、tests/Web不在包中；XCore旧errors路径及根转出口实际退出。安装后的真实CLI重启验收通过（`/tmp/xbot-refactor-layout-complete-installed/report.json`，4次provider HTTP请求、历史4→8、附件30字节）；旧application仍在该快照，留给下一批明确删除，不将此快照称最终产物。
- [ ] 唯一Sol已接第2批：按设计mount_process/mount_session/mount_thread接入活跃进程根及独立session/thread owner，收口loader/session/persistence最后3owner布局与真实API，退出application/host/factory；当前无低级别代理并发。LLM/Agent共享资源须保留workspace/session覆盖和thread选择隔离，loader不接管业务构造，直接复用已验证XCore pre_stop机制。

### 迭代 3：共享进程根与执行所有权（实施中）

- [x] 唯一Sol继续具体实施；主代理已核对当前工作树及实时代理状态。loader/session/persistence正在收口API和scope，Engine构造回agentloop，session打开任务用shield保护共享初始化，session锁已改独立运行owner，不按PID复用。此处是进行日志，未提交的实现尚未作为最终验收代码。
- [x] 主代理直接读取中间smoke的报告和实际目录：`/tmp/xbot-scopes-smoke.json`指向`/tmp/xbot-scopes-gxumd5ha`，两个thread共享root、相同配置LLM目录复用而MODEL独立，fiber数21→21→0，session目录无持久会话；存在配置/内置skill及会话锁文件。该中间证据不代表后续修改已通过，不替代最终CLI/HTTP/ACP和关闭验收。
- [x] 审查已要求并进入实施：可选facet发现/配置失败独立回滚，真实provide发布约束，外部impl/plugin发现；process唯一jobs registry按thread身份绑定，查询/授权不复制两套；删除纯record构造额外引入的SESSION_VIEWS转发服务，唯一无状态构造函数直接归API。权限正常配置的meet现象已定位，阻塞worker验收改走真实审批路径，不使用别的工具名或私改具体服务来放行。
- [ ] 收口为单一公开AGENT_LOOP执行能力，Engine及Task/Lock留实现，移除THREAD_RUNNER式公开内部集合；删除application/Host/Factory残留。max_iterations直接支持None并取消旧200默认回退，保留显式有限值行为。
- [ ] 最终验证并记录实际代码提交：并发打开/取消等待、不同workspace/session覆盖、不同thread任务授权、必需依赖失败回滚、外部facet、审批后worker真实排空与锁释放、HTTP/SSE/ACP/CLI及受影响PTY。当前中间测试与目录迁移不视为这些交付已经全部完成。
- [ ] 后续配置/权限语义核对的具体发现：ConfigService把bundled到各覆盖阶段都作为权限meet；新增工具即使正常覆盖default_decision=allow，仍可能被bundled默认ask限制。worker验收已走真实审批，不以私改服务或套用内建工具名绕过。第5批要区分默认值合成与明确父策略约束，验证全局/workspace/session/Agent配置及模式放行的实际含义。
- [ ] 后续缓存验收的具体对象：共享ProviderDirectories._catalogs与LlmService._clients当前按完整resolved配置复用，仅在process关闭清空；本批不声称缓存有界。第3/5批补活跃引用/pin与有限保留，验证不同配置反复打开/关闭后的驻留和连接资源，不以fiber计数代替内存/FD证据。
- [x] 主代理核验中间10项scope用例源码与原始日志`/tmp/xbot-runtime-scopes-tests.log`；已覆盖pre-stop失败保留scope/锁并可重试、旧jobs绑定在关闭/同ID重开后失效等。另读取`/tmp/xbot-scopes-cli-acceptance-v2/report.json`（4次生产adapter HTTP请求、历史4→8、附件30B）。代码随后仍在修改，以上不作为最终工作树全绿声明。
- [x] 主代理读取真实active-SSE退出中间产物`/tmp/xbot-active-sse-shutdown/report.json`及其data/logs：客户端保持200事件流时SIGTERM，约0.209秒退出(-15)，日志含thread关闭和root destroyed，随后锁可重取。当前继续去除listener直接调用SessionManager的耦合，最终版本需复跑；进程退出码/锁释放不能单独证明业务排空。
- [ ] 继续收口的具体边界：按当前Agent引用重解析恢复并只保留明确用户选择；ACP与HTTP/TUI共用进程入口；nested provide不能扩大outer facet提供名单；disabled本层facet仍须隔离其私有服务，不能退回同名process服务（例如COMMANDS）。完成后再跑必要组合并提交实现。
- [x] 真实SSE退出验收加强前提后已由主代理读取v3报告：`/tmp/xbot-active-sse-shutdown-v3/report.json`含stream_live_before_signal=true，约0.161秒退出、锁可重取。当前已直接核对`/tmp/xbot-scopes-rebind-acp.log`20 passed及`/tmp/xbot-scopes-session-final.log`27 passed；两者是迁移中的不同组合，不能相加或替代最终全量。
- [ ] HTTP测试迁移范围纠正：主代理对比基线AST发现原108个test函数变为78，31个被删函数中大量涉及原文、身份、SSE中断、pending编辑/FIFO、附件/再生成、子thread跨重启、todo/usage恢复及关闭竞态。要求恢复这些有用行为并走新公共API，给出逐项映射；只有memory-only、旧receipt具体包装、关闭丢弃已接受输入等与目标冲突项可删除/替换。少量scope绿测不能补足缺失的HTTP行为证据。
- [x] 归档接口取舍已明确：HTTP消息读取统一canonical transcript，active/closed/resume同源；compact验证归档原文仍可读、模型surface使用summary，不恢复旧活跃/关闭读不同内容的分支。clear/undo/regenerate仍须分别验证其规范可见投影。
- [x] 主代理再次对比基线AST，误删的有效验收已恢复到源码：HTTP原108个test函数→105，仅退出2项memory-only、1项旧receipt包装和1项close丢输入（新增保留已接受inbox的替代项）；startup原55→53，factory优先级改为公开模型覆盖验收，另外退出2项memory-only。恢复后的合跑尚有失败正在处理，此条不代表108个参数项已全绿。
- [x] 主代理读取`/tmp/xbot-scopes-multi-server.json`：2个真实监听、2次请求，第二owner拒绝、原输入保真和另一个session完成的invariant均为true、errors为空，进程FD为7→7。该中间工况不代表不同配置catalog驻留或最终修改后的资源上界。
- [ ] 第3批明确修正空会话元数据：当前rename依赖persisted_thread_ids，空活跃main尚未物化会报main_thread_not_found；DeferredMetadata也不能独立保证只改title的事实被保存。SQLite session行应把明确的用户title/workspace事实与首条聊天分离，不为中间格式新增另一套持久状态。
