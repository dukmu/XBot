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
- [x] 第 2 批：loader 活跃根挂载及 session/thread 生命周期；退出旧 application、MountedAgentApplication 和单用途 factory。
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
- [x] 第 2 批需修的已复现缺陷：新空 thread 的 `trajectory_page` 直接要求磁盘 thread 而返回 404；`delete_session` 先 close 清空空会话目录、随后再次 rmtree 抛 `FileNotFoundError`。原始失败 `/tmp/xbot-network-stress.json`，主代理核对了两个 owner 调用路径；未绕过失败或提前修补生产生命周期。
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
- [x] 在作用域/契约稳定后完成 loader/session/thread 单进程根接入，退出 application/host/factory 旧层；保留标准执行路径与必要行为验收。
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
- [x] 唯一Sol已接第2批：按设计mount_process/mount_session/mount_thread接入活跃进程根及独立session/thread owner，收口loader/session/persistence最后3owner布局与真实API，退出application/host/factory；当前无低级别代理并发。LLM/Agent共享资源须保留workspace/session覆盖和thread选择隔离，loader不接管业务构造，直接复用已验证XCore pre_stop机制。

### 迭代 3：共享进程根与执行所有权（本批已验收）

- [x] 唯一Sol继续具体实施；主代理已核对当前工作树及实时代理状态。loader/session/persistence正在收口API和scope，Engine构造回agentloop，session打开任务用shield保护共享初始化，session锁已改独立运行owner，不按PID复用。此处是进行日志，未提交的实现尚未作为最终验收代码。
- [x] 主代理直接读取中间smoke的报告和实际目录：`/tmp/xbot-scopes-smoke.json`指向`/tmp/xbot-scopes-gxumd5ha`，两个thread共享root、相同配置LLM目录复用而MODEL独立，fiber数21→21→0，session目录无持久会话；存在配置/内置skill及会话锁文件。该中间证据不代表后续修改已通过，不替代最终CLI/HTTP/ACP和关闭验收。
- [x] 审查已要求并进入实施：可选facet发现/配置失败独立回滚，真实provide发布约束，外部impl/plugin发现；process唯一jobs registry按thread身份绑定，查询/授权不复制两套；删除纯record构造额外引入的SESSION_VIEWS转发服务，唯一无状态构造函数直接归API。权限正常配置的meet现象已定位，阻塞worker验收改走真实审批路径，不使用别的工具名或私改具体服务来放行。
- [x] 收口为单一公开AGENT_LOOP执行能力，Engine及Task/Lock留实现，移除THREAD_RUNNER式公开内部集合；删除application/Host/Factory残留。max_iterations直接支持None并取消旧200默认回退，保留显式有限值行为。
- [x] 最终验证并记录实际代码提交：并发打开/取消等待、不同workspace/session覆盖、不同thread任务授权、必需依赖失败回滚、外部facet、审批后worker真实排空与锁释放、HTTP/SSE/ACP/CLI及受影响PTY。当前中间测试与目录迁移不视为这些交付已经全部完成。
- [ ] 后续配置/权限语义核对的具体发现：ConfigService把bundled到各覆盖阶段都作为权限meet；新增工具即使正常覆盖default_decision=allow，仍可能被bundled默认ask限制。worker验收已走真实审批，不以私改服务或套用内建工具名绕过。第5批要区分默认值合成与明确父策略约束，验证全局/workspace/session/Agent配置及模式放行的实际含义。
- [ ] 后续缓存验收的具体对象：共享ProviderDirectories._catalogs与LlmService._clients当前按完整resolved配置复用，仅在process关闭清空；本批不声称缓存有界。第3/5批补活跃引用/pin与有限保留，验证不同配置反复打开/关闭后的驻留和连接资源，不以fiber计数代替内存/FD证据。
- [x] 主代理核验中间10项scope用例源码与原始日志`/tmp/xbot-runtime-scopes-tests.log`；已覆盖pre-stop失败保留scope/锁并可重试、旧jobs绑定在关闭/同ID重开后失效等。另读取`/tmp/xbot-scopes-cli-acceptance-v2/report.json`（4次生产adapter HTTP请求、历史4→8、附件30B）。代码随后仍在修改，以上不作为最终工作树全绿声明。
- [x] 主代理读取真实active-SSE退出中间产物`/tmp/xbot-active-sse-shutdown/report.json`及其data/logs：客户端保持200事件流时SIGTERM，约0.209秒退出(-15)，日志含thread关闭和root destroyed，随后锁可重取。当前继续去除listener直接调用SessionManager的耦合，最终版本需复跑；进程退出码/锁释放不能单独证明业务排空。
- [x] 继续收口的具体边界：按当前Agent引用重解析恢复并只保留明确用户选择；ACP与HTTP/TUI共用进程入口；nested provide不能扩大outer facet提供名单；disabled本层facet仍须隔离其私有服务，不能退回同名process服务（例如COMMANDS）。完成后再跑必要组合并提交实现。
- [x] 真实SSE退出验收加强前提后已由主代理读取v3报告：`/tmp/xbot-active-sse-shutdown-v3/report.json`含stream_live_before_signal=true，约0.161秒退出、锁可重取。当前已直接核对`/tmp/xbot-scopes-rebind-acp.log`20 passed及`/tmp/xbot-scopes-session-final.log`27 passed；两者是迁移中的不同组合，不能相加或替代最终全量。
- [x] HTTP测试迁移范围纠正：主代理对比基线AST发现原108个test函数变为78，31个被删函数中大量涉及原文、身份、SSE中断、pending编辑/FIFO、附件/再生成、子thread跨重启、todo/usage恢复及关闭竞态。要求恢复这些有用行为并走新公共API，给出逐项映射；只有memory-only、旧receipt具体包装、关闭丢弃已接受输入等与目标冲突项可删除/替换。少量scope绿测不能补足缺失的HTTP行为证据。
- [x] 归档接口取舍已明确：HTTP消息读取统一canonical transcript，active/closed/resume同源；compact验证归档原文仍可读、模型surface使用summary，不恢复旧活跃/关闭读不同内容的分支。clear/undo/regenerate仍须分别验证其规范可见投影。
- [x] 主代理再次对比基线AST，误删的有效验收已恢复到源码：HTTP原108个test函数→105，仅退出2项memory-only、1项旧receipt包装和1项close丢输入（新增保留已接受inbox的替代项）；startup原55→53，factory优先级改为公开模型覆盖验收，另外退出2项memory-only。恢复后的合跑尚有失败正在处理，此条不代表108个参数项已全绿。
- [x] 主代理读取`/tmp/xbot-scopes-multi-server.json`：2个真实监听、2次请求，第二owner拒绝、原输入保真和另一个session完成的invariant均为true、errors为空，进程FD为7→7。该中间工况不代表不同配置catalog驻留或最终修改后的资源上界。
- [x] 第3批明确修正空会话元数据：当前rename依赖persisted_thread_ids，空活跃main尚未物化会报main_thread_not_found；DeferredMetadata也不能独立保证只改title的事实被保存。SQLite session行应把明确的用户title/workspace事实与首条聊天分离，不为中间格式新增另一套持久状态。
- [x] 主代理发现并由Sol修复创建中关闭的具体窗口：thread创建取消原本不取消被shield保护的session owner初始化，可能在close返回后晚注册owner/持锁；现在关闭覆盖并等待_session_opening，普通等待者取消仍不破坏共享打开。受控session facet验收已进源码；`/tmp/xbot-scopes-lifecycle-focused.log`为16项scope与7项恢复/取消组合，共23 passed。
- [x] 恢复后HTTP108个参数项已通过；主代理读取`/tmp/xbot-scopes-http-startup-final.log`的实际结果为166 passed、1 failed，失败属于startup的默认工具注册用例，不能把日志文件名中的final当成冻结完成。core入口迁移中另有36 passed、5 failed（`/tmp/xbot-scopes-core-routing.log`），继续按实际调用与目标语义修正。
- [x] SessionManager最后的业务初始化调用正在退出：agents自行通过现有ready完成初始化，XCore补scope的ready边界，loader按真实owner的activation错误区分required/optional。必须覆盖嵌套THREAD_INITIALIZED回调失败后的后续初始化与最终工具投影，不给异常附加owner属性，不改变正常事件的错误传播，不让后台任务继承永久的初始化错误隔离语义。生产代码尚未冻结。
- [ ] 共享缓存的准确现状：AgentCatalog仍按thread持有已展开定义，原Markdown解析由模块级lru_cache(128)共享；这不是进程共享完整可变catalog。第3/5批与provider目录/连接缓存一起落实明确owner、按字节预算和来源失效清退，不把thread变量展开值错误共享。
- [x] Sol已明确冻结生产源码、结束全部工具并完成交接；XCore ready/owner失败归属、scope新工作门槛、Browser/MCP pre-stop与真实worker等待已落实，未自行提交。原始生产组合229 passed、最后Browser/scope/只读恢复组合50 passed、core Browser7 passed，日志分别为`/tmp/xbot-scopes-production-focused.log`、`/tmp/xbot-scopes-freeze-focused.log`、`/tmp/xbot-scopes-browser-handoff.log`；这些是不同组合，不相加为全量。
- [x] 已确认Sol状态completed后切换两个6.1 Terra，未与Sol混跑：一位仅迁移core/非TUI owner测试，另一位仅迁移integration/TUI/ACP。生产源码保持冻结；主代理独立指纹为`/tmp/xbot-root-scopes-freeze.json`，含817条路径状态（含待删除路径），不混入用户设计稿或worktree。
- [x] 主代理冻结后独立源码CLI验收通过：`/tmp/xbot-root-scopes-source-cli/report.json`，进程590265/590294、4次生产OpenAI adapter的HTTP请求、重启4条/续跑8条规范消息、附件30B；实际日志均含session关闭和root destroyed。没有外部供应商请求。
- [x] 主代理冻结后独立活跃SSE退出通过：`/tmp/xbot-root-scopes-source-sse/report.json`确认stream_live_before_signal=true，SIGTERM约0.158秒退出(-15)，锁可重取；实际运行日志含thread关闭与root destroyed。完整core/integration/TUI及当前提交wheel仍待本批最终收口。
- [x] 最终回归抓到并修复真实goal残留：热卸载仍调用已退出的remove_input，现只调用既有AGENT_LOOP.update_pending_input，并只处理明确queue_item_not_found；不补兼容alias。真实热卸载、已claim后的替换及其他错误传播均保留验收。该文件是生产冻结后唯一代码变更，主代理已核对源码指纹。
- [x] 最终core/非TUI owner/XCore为797 passed、3个旧WebUI入口deselected，159.11s（`/tmp/xbot-scopes-core-final.log`）；随后加强父ready监听器descendants=True前提，相关13项通过（`/tmp/xbot-scopes-xcore-ready-final.log`）。首次778 passed/17 failed仅是迁移诊断，不替代最终结果。
- [x] 完整integration/ACP137 passed、99.24s（`/tmp/xbot-client-integration-acp.log`）；fold-in收尾删除旧config.yaml、用root.close永久释放、受控工具改公开grant_once精确授权后11 passed（`/tmp/xbot-foldin-final2.log`）。完整TUI938 passed、155.00s，含真实server/PTY（`/tmp/xbot-client-tui-final.log`）；首次937/1的picker用例恢复“标题等于ID”原前提后复跑通过。Goal代码修复后HTTP/真实TUI相关10 passed（`/tmp/xbot-client-goal-final.log`）。各组合不额外相加。
- [x] 精确最终实现提交为`b8b4974726eab84b440676104faacce44c18b41b`，XCore独立提交为`5e6a96e`；此后1246条源码/测试/配置/文档指纹未变（`/tmp/xbot-root-scopes-evaluated.json`）。git diff --check及211个改动Python文件编译通过。用户未跟踪设计稿与.worktrees未加入提交。
- [x] 从b8b4974干净git archive构建两wheel并安装到全新uv环境`/tmp/xbot-scopes-installed`；清单`/tmp/xbot-scopes-wheel-manifest.json`为XBot507/XCore20项，新API、YAML、类型标记、插件文档和skill实际入包；application、factory、旧session/loader/persistence根模块及tests/Web均不在产物。仓库外移除PYTHONPATH后已实际核验导入来自新site-packages、被删模块find_spec为None。
- [x] 最终安装CLI通过：`/tmp/xbot-scopes-wheel-cli/report.json`，进程603231/603268、4次provider HTTP请求、历史4→8、附件30B；安装版活跃SSE退出通过：`/tmp/xbot-scopes-wheel-sse/report.json`，确认读取仍挂起后SIGTERM，约0.136秒退出、锁可重取，实际日志含root destroyed。此产物与验收仅对应b8b4974，不冒充真实供应商互操作或后续存储工况。
- [x] 第2批已完成并具备独立安装证据，插件最后3owner已迁移。
- [ ] 总体layout/契约仍保留3处core.filesystem共享实现导入待存储/安全边界收口。主代理最终审计均退出1且如实报告3项（`/tmp/xbot-root-scopes-architecture.txt`、`/tmp/xbot-root-scopes-contracts.txt`），未隐藏规则。继续第3批SQLite/JSONL、工作集与流式artifact；512轮/4096步/20次压缩/64个child生命周期及200MB工况、统一wait/team、媒体预算、真实MiniMax和最终TUI改造仍未完成，goal保持active。

### 迭代 4：SQLite、规范日志与工作集（实施中）

- [x] 第2批代码和安装产物验收完成后，已让同一Sol接具体存储实施；两个Terra均结束，无混跑。新段以b8b4974为已验收代码基线，前一批green不替代本段新实现的验证；主代理继续只负责边界审查、TODO、整合和独立验收。
- [x] 第一段落地真实生产SQLite/rollout与索引：唯一根DB worker、异步短事务和队列背压，session/thread权威元数据、规范正文与可重建投影、输入幂等和损坏/提交恢复、归档/工作集分离、fork/delete及独立rename事实。退出XBot的持久state.json和完整归档缓存，不引入旧格式自动双读写或同步阻塞桥。
- [ ] 后续存储段继续流式artifact、从启动即spool的shell、固定范围引用与pin，以及指定预算下的驻留/写入/大结果实测；不以第一段局部验证提前勾选整个第3批。
- [x] 已确认进程插件的workspace membership/archive/显示顺序由同一DB worker管理的根plugin-state.sqlite3保存，不复制session title/workspace或建立第二份可写会话目录；每session仍独立state.sqlite3。此选择落实物理存储归属，不把业务规则交给backend。
- [x] 主代理以b8b4974独立安装wheel完成真实loader/thread的存储组件基线：W恒为1条23B摘要，H为4/16/64MiB时tracemalloc current为4,780,449 /19,059,506 /76,164,905B，RSS为139,554,816 /155,471,872 /219,537,408B，首条消息weakref均存活；报告`/tmp/xbot-storage-baseline-b8b4974/report.json`、脚本`/tmp/xbot-storage-baseline.py`。此实验直接操作history，没有模型请求，不算4096次模型步或真实模型compact验收。
- [x] 主代理读到首个SQL切片真实产物：空thread未物化；仅rename写session权威行，输入/assistant进入单rollout与SQL refs，消费后inbox为空；无thread.json/messages.jsonl/inbox.jsonl/state.json。原型日志`/tmp/xbot-sqlite-first.log`及`/tmp/xbot-sqlite-first/data/`。随后代码仍变更，早期4项contract及单项fork通过不作为本段最终验收。
- [x] 审查纠正了实际恢复缺口：未决接受只补SQL会漏掉活跃inbox；未决assistant/ToolResult只补SQL会让活跃W仍是旧值。当前已加强同ID消费/FIFO及标准工具副作用后同scope恢复验收，禁止用关闭重开掩盖；worker返回DB对象、title旧事件覆盖rename、turn计数按输入数漂移等也按负责边界修正。物理worker/生命周期组合15 passed的原始日志`/tmp/xbot-sqlite-worker-lifetime.log`，仍非最终owner拆分后的全量结论。
- [x] 用户明确强调存储必须是真正backend并参考XBot_cpp。主代理只读核对本地dev-mem的851b936：persistence/docs/design.md说明物理/事务/文件归backend，agentloop/src/history_index.cpp实际拥有schema与project，session/src/sql_catalog.cpp拥有元数据，usage/todolist自有repository；未修改C++仓库。
- [x] 据该审查修正本段架构：persistence只留worker、受限SQL、通用日志/offset、durable I/O、claim/lease与快照/复制/发布/删除原语；agentloop实际拥有typed codec、输入/turn/surface/lineage投影及历史repository/HTTP/import-export；session实际拥有元数据schema/repository与fork业务协调；各领域owner处理自己的fork状态。不能只移动文件后由backend硬调业务实现，也不照搬StatementId/SqlDomain/ModuleResource框架。
- [x] 本段仅将todo物理状态改为自有表；现有全量TaskList读取、完成项占max_tasks及500行事务预算问题尚未完成设计8.2。紧接存储切片落实active graph、独立get/完成项分页和只计未完成的活跃名额，不把抬高上限当成有界清退。
- [x] 根据用户backend边界要求，选择业务repository首次持久修改时显式调用session的公开ThreadRepository.ensure；不把session准备逻辑作为backend.state(before_write=...)隐式注入，也不新增Host/包装执行器。默认解析和读操作不触发ensure落盘；受限SQL事务回调仍只是调用方明确提供的短SQL工作。
- [x] owner拆分后补一个不启动AgentLoop的自定义owner实测：自己的表和日志kind经backend公开API完成事务、append/flush、范围读/恢复与关闭，证明后端不需要Input/Turn/ThreadMetadata业务对象；不能仅凭文件搬迁或字面导入检查宣称backend已纯化。
- [x] owner拆分后的首个真实loader路径再次通过（`/tmp/xbot-sqlite-owners-first.log`）：空sessions树、公开rename独立权威行、AGENT_LOOP接受/模型回复、ARCHIVE读取中文原文及root关闭。历史schema/project已归agentloop，session/threads及ThreadFacts已归session；该日志仍为中间证据，fork/exchange和新句柄生命周期尚在接通，不代表本段冻结。
- [x] 拆分审查发现通用SQL/KV句柄需要继续承担物理admission、实际claim和drain，不能把调用者外层use当成唯一保护；已要求旧scope句柄在同ID重开后不能获得新owner写权。另需由历史/会话owner分页跨越单次500行SQL预算，不能让后台预算变成未说明的消息或线程数量上限。
- [x] 主代理读取独立消费者源码及实际日志`/tmp/xbot-backend-independent-owner.log`：验收组合3 passed（1项独立owner、2项worker）。实际只挂载persistence的process facet，custom_values表与custom.completed日志经actual claim写入，清空自有派生表和通用offset后重放恢复中文值，session drain开始后缓存domain明确拒绝新读；随后释放claim并root.close。fork/exchange仍在迁移，未将此结果描述为最终全量验收。
- [x] owner拆分后的语义组合首轮为12 passed / 1 failed（`/tmp/xbot-storage-owners-contract-first.log`），未以此前green覆盖。关闭probe加强后发现物理drain等待调用者整个Task，而调用者finally等待close，会形成等待环；改为等待已接纳物理lease实际退出，仍须以真实阻塞读取gate复跑close/delete。旧probe只观察thread inactive，不能证明session物理drain已开始。
- [x] 主代理中间静态审计实际退出非零：architecture为5项（`/tmp/xbot-storage-owners-architecture-interim.txt`），其中新增Goal/Permissions漏声明THREAD_REPOSITORY注入，已交实施代理修正；contract audit仍为原3项workspace filesystem越界（`/tmp/xbot-storage-owners-contracts-interim.txt`）。不移动workspace工具业务到persistence以清除诊断，也不把此中间审计作为最终源码验收。
- [x] 物理关闭最新专测`/tmp/xbot-storage-owners-drain-v3.log`为2 passed /3.19s，实施代理已poll确认退出0，主代理已读实际测试和日志。修正包括逐lease等待、最终drain在root owner pre_stop的顺序，以及归档页解码阶段释放writer mutex但保留受跟踪读lifetime；阻塞读取未释放时close/delete仍等待。此前third组合18 passed /1 failed仅是v3修复前诊断，当前源码合跑仍待完成。
- [x] 主代理独立后端探针实际复现CAS引用校验缺口：ArtifactRef.id内摘要A与sha256字段B不同，但正文符合B时，import_file接受并把B写入objects/A。实际报告`/tmp/xbot-cas-identity-probe-h1uoed1x/report.json`为accepted=true，脚本`/tmp/xbot-cas-identity-probe.py`，只挂载persistence且退出0；交下一位Terra做小修复及拒绝发布验收。首次沙箱线程I/O挂起进程已明确终止并poll到143，不计结果；正式探针在可用线程I/O环境运行。
- [x] Sol第一段最终21 passed /33.31s，进程退出0（`/tmp/xbot-storage-production-freeze-tests.log`）；主代理独立核对65条变动生产Python指纹无差异（`/tmp/xbot-storage-production-freeze.json`）。覆盖metadata-only/all-thread fork、target claim冲突回滚、标准工具读取readonly CAS、真实I/O gate与新目录发布同步。此为随后Terra收尾前的代码点，不冒充最后提交。
- [x] 确认Sol结束后切换3个6.1 Terra，分别迁移core及CAS小修、integration/ACP/TUI及脚本、排版文档及两组明确小修；没有Sol/Terra混跑。临时Ruff 0.17.3只装在/tmp工具环境，59个授权生产文件及最后3个文件逐文件格式化AST等价，未增加项目依赖；旧ThreadPersistence/StoredInboxRecord及JSON私有结构检查按新公开owner行为收敛，不补alias。
- [x] CAS修复以1MiB流复制到临时文件，核对ID/声明/正文摘要和size后原子发布；错误不改既有对象或fork硬链接。公开import/export针对性1项通过；主代理同一独立探针复跑退出0并明确拒绝，`/tmp/xbot-cas-identity-probe-51ezl5b_/report.json`为accepted=false / Artifact identity and hash mismatch。
- [x] 全量客户端暴露并修复终止事件时序：在engine finally、inbox reconcile和状态发布完成且turn锁释放后才广播turn_ended；开始前取消不再构造非法第零轮终止帧。core恢复测试不再锁定scope返回瞬间尚未claim的调度快照，而验证原通知保存、实际恢复与再次重开只出现一次。受控gate证明开始前关闭仍保留已接受输入、无伪造终止帧。
- [x] 再修两个真实公共边界：空trajectory先验证before/cursor且不物化（6项分页及独立空loader通过；真实HTTP另1项通过）；busy再生复用OperationError(thread_busy)，真实阻塞工具下HTTP409/retryable=true且释放后原turn/队列/输出不受损。日志`/tmp/xbot-empty-trajectory-pagination.log`、`/tmp/xbot-empty-trajectory-probe.log`、`/tmp/xbot-storage-client-empty-trajectory-http.log`、`/tmp/xbot-thread-busy-foldin-retryable.log`、`/tmp/xbot-thread-busy-start-probe.log`。
- [x] 最终core正常退出0：288 passed、指定旧Web CLI 3项deselected（`/tmp/xbot-storage-core-verified.log`），Web静态资源/代理测试仍运行；non-client owner正常退出0：378 passed（`/tmp/xbot-storage-core-owners-postfix.log`）；XCore118 passed（`/tmp/xbot-storage-core-xcore.log`）。浏览器/server及其它旧测试漏close root会留下非daemon DB线程，已按真实测试所有权关闭，最终线程探针无残留；先前中断的诊断进程不算通过。
- [x] 完整integration/ACP在V2冻结后正常退出0：138 passed /127.16s（`/tmp/xbot-storage-client-integration-final-v3.log`）；完整TUI正常退出0：938 passed /179.97s，含49项真实server/PTY（`/tmp/xbot-storage-client-tui-final-v2.log`）。TUI运行期间有上述两处局部错误边界修复，已按V2分别补真实HTTP验证，不将其说成单一指纹全程跑完。新空轨迹HTTP用例收集晚于integration，单列不相加。
- [x] 真实session_exchange命令export/import均退出0，两个独立runtime核对源/目标8条历史和30B附件原样；`/tmp/xbot-storage-client-exchange/verify-report.json`。V2生产指纹`/tmp/xbot-storage-root-final-freeze-v2.json`覆盖572条生产/资源/脚本，aggregate=334884233c371961d24c162c99d41f38d6546e71e9810494bc4bdd45e0fac9d7；前一版后仅空轨迹和busy错误两文件变更。
- [x] 主代理V2源码独立CLI退出0：`/tmp/xbot-storage-root-final-source-cli/report.json`，进程723563/723606、4次生产OpenAI adapter的真实本地HTTP请求、重启4条/续跑8条历史、附件30B；活跃SSE退出0：`/tmp/xbot-storage-root-final-source-sse/report.json`，确认读取仍活后SIGTERM约0.161s退出(-15)、锁可重取。未请求外部provider；当前新wheel仍待构建安装验收。
- [x] 主代理按64→256→1024逐级观察组件内存及写入，旧对象weakref均已释放；W固定1条23B，H=4/16MiB的tracemalloc约1.940/1.943MB、RSS约115.66/115.74MB，未随归档同比增长。增量快照主要是一块pathlib.py:404/sys.intern触发的1,922,416B分配；报告`/tmp/xbot-storage-sqlite-1024-diagnostic/report.json`及traces-*.pickle。此实验直接History写入，无模型请求，不算4096模型步或真实模型压缩；4096条/64MiB完整同规格对照仍在运行。
- [x] 4096条组件对照已实际退出0，`/tmp/xbot-storage-sqlite-4096/report.json`及日志同前缀.log；每条16KiB、每64条以1条23B摘要替换W，H=4/16/64MiB时tracemalloc current为1,939,826 /1,942,232 /1,950,042B，RSS为115,740,672 /115,806,208 /115,929,088B，首条旧消息weakref均释放。原b8b4974同规格H增长时分别保留4.78/19.06/76.16MB traced且旧对象均存活；此处只证明历史组件驻留，不能冒充4096模型步/64子线程/慢客户端或200MB输出验收。
- [x] 同组件写入实测留给下一存储段优化/验证：64MiB逻辑正文对应rollout 68,011,346B、SQLite共2,179,072B、采样时WAL为0；Linux进程write_bytes=1,720,791,040B、cancelled_write_bytes=973,606,912B。逐批计数约25.6倍正文且线性，没有全H重写迹象；这些是进程I/O记账，不冒称设备实际写入量或已经满足延迟/磨损目标。需在保留单worker和有界连接/资源前提下评估短事务/连接/WAL开销，不能换成无界连接缓存。
- [x] 最终主代理architecture/contracts均实际退出1且各保留原3项workspace文件实现越界（`/tmp/xbot-storage-root-architecture-final.txt`、`/tmp/xbot-storage-root-contracts-final.txt`）；98个改动Python编译和git diff --check通过。主代理复核572条V2生产指纹未变，并读取源码CLI/SSE的真实日志尾均为application.destroyed，非只依据摘要推断资源收尾。
- [x] 验收脚本单独提交`d8cb056`；本段精确最终实现提交`70fb6ded9925910b5daab3deb28de09d7459d74b`，提交前后572条生产/资源/脚本指纹未变（`/tmp/xbot-storage-root-evaluated.json`）。用户设计稿和.worktrees未加入；此后仅本日志可追加，下一段代码变更须重新验证其影响。
- [x] 从70fb6de干净git archive构建两wheel，独立新uv环境`/tmp/xbot-storage-installed`离线安装；清单`/tmp/xbot-storage-wheel-manifest.json`核对XBot520/XCore20项，新backend及session/agentloop repository API、默认YAML、py.typed、owner文档、skill实际入包；旧persistence/store/archive/client_http及application/factory、tests/Web/生成bundle未入包。仓库外移除PYTHONPATH后实际导入来自该env的site-packages，旧模块find_spec为None；没有新增项目依赖。
- [x] 最终安装CLI实跑退出0：`/tmp/xbot-storage-wheel-cli/report.json`，进程732076/732155、4次本地provider HTTP请求、重启4条/续跑8条、30B附件；安装版活跃SSE验收退出0：`/tmp/xbot-storage-wheel-sse/report.json`，确认流仍活后SIGTERM约0.160s、exit=-15、锁可重取，两份运行日志末尾均application.destroyed。此产物仅对应70fb6de，不冒充外部provider或后续存储切片验收。第3批整体及goal继续active。

### 迭代 5：活跃 todo 与 SQLite I/O（实施中）

- [x] 70fb6de的独立安装验收结束后启动两个6.1 Terra，未与Sol混跑：一位实施todo active/completed分离，一位针对实际25.6倍进程写入记账评估最小有界连接复用。第一段证据保留在原实现点，不能把旧green应用于本轮新改动；主代理继续维护TODO、边界审查与独立整合。
- [x] todo运行驻留只含未完成任务及必要摘要，完成全文保存在自有SQL表按ID/明确分页读取；max_tasks只计未完成，500+完成项不触发隐含总量上限。GET_TODOS/TaskChanged明确active视图和completed_count，必要TUI状态消费只做相应最小调整，不在此批扩整体布局。
- [x] SQLite保持唯一worker和真实短事务、预算/背压、取消排空及后端边界；以小样本确认连接/WAL开销，只有有界且能正确关闭/删除/fork/发布/重建的复用才保留，不增加无界session连接字典、不减少fsync以改善数字。当前未有本轮最终验证。
- [ ] 随后的流式shell/artifact、固定范围物化/pin、归档/事件缓存字节预算、workspace大集合和指定产品压测继续待办；以上两项完成也不勾选整个第3批或整个goal。
- [x] todo实际owner切片：TaskList/GET_TODOS/TaskChanged仅active任务、completed_count及必要完成依赖ID；完成详情独立get，GET_COMPLETED_TODOS/HTTP和task_list(completed=true)以稳定数字ID分页。active/completed计数与任务变更同事务，索引查询不扫描全部完成正文/历史边；拒绝更新、重新激活、循环/阻塞及fork均由todolist处理，backend未混入业务。
- [x] 主代理收回了未要求的兼容范围：删除旧counter的COUNT fallback、索引探测双路径和自动ALTER迁移，旧schema明确失败且原行/表结构不变，不为旧布局保留运行路径。依赖增删参数与任务正文一起计入原有队列字节预算，未截断Agent参数；真实4096B预算下45条依赖增删超限，任务/图不变。最终focused 25 passed、exit0（`/tmp/xbot-todo-active-budget-final.log`），含521项分页/释放/名额、重开/fork/取消、HTTP及TUI计数。
- [x] worker最多一条连接，按路径及(dev,inode)切换，连接创建/查询/commit/rollback/close都在同一worker；session drain、删除、准备目录发布前关闭对应连接。实际close在释放前抛错时保留连接并拒开第二条，可重试释放。主代理发现并纠正了“直到关闭前保留目录”的弱验收：最终还证明下一次prepare或root drain在成功关闭后回收整个准备根目录；串行准备、最多一个待清理项，失败持续时阻止新的准备，无通用journal或业务回调。
- [x] 主代理空闲引用探针实际复现worker持有上一次1MiB返回值（`/tmp/xbot-worker-value-zwdoxi_d/report.json`），修后调用者释放时即回收（`/tmp/xbot-worker-value-nkosi6uk/report.json`）；worker仍运行、无需下一笔工作。完成路径主动释放work/future/callback/value/error引用，不形成未记账结果缓存。最终worker/owner/rollout组合28 passed、exit0（`/tmp/xbot-db-io-idle-verified.log`），含回调捕获对象、故障/取消、同ID删除重建和准备失败清理。
- [x] 生产冻结`/tmp/xbot-collections-io-freeze.json`，569条生产/资源/脚本，aggregate=298867725ae811d9f5774c639326b3e33a901bf509ecec3643af11a5252b5795；之后两个Terra仅做整合验收，未与Sol混跑。完整core/nonclient owner/XCore正常退出0：796 passed、指定旧WebCLI 3项deselected /240.88s（`/tmp/xbot-collections-core-full.log`），Web静态资源/代理仍跑。
- [x] 完整integration/ACP正常退出0：139 passed /115.36s（`/tmp/xbot-collections-client-integration-rerun.log`）；完整TUI含真实server/PTY正常退出0：939 passed /166.19s（`/tmp/xbot-collections-client-tui.log`）。首轮138/1是从active快照读取已完成项的旧断言，改为active空、completed_count及完成分页后单项和全套复跑；保留原失败日志，不给产品补兼容字段或把focused与全套相加。
- [x] 主代理最终冻结源码同规格4096条组件测量退出0（`/tmp/xbot-collections-io-4096/report.json`）：H64MiB/W1条23B时traced=1,966,899B、RSS=120,299,520B、旧对象释放；活跃态最后采样write_bytes=437,313,536B、cancelled_write_bytes=36,864B、主DB=2,162,688B、WAL=4,210,672B。原70fb同采样点为1,720,791,040 /973,606,912B；均为进程记账，非设备真实写入，不承诺p95或把直接History写入当模型步/真实压缩。
- [x] 主代理源码CLI退出0（`/tmp/xbot-collections-io-source-cli/report.json`）：进程765561/765643、4次真实本地provider HTTP请求、历史4→8、附件30B；活跃SSE退出0（`/tmp/xbot-collections-io-source-sse/report.json`）：确认流仍活后SIGTERM约0.170s、exit=-15、锁重取。双根及独立子进程缓存WAL探针均退出0，owner删除/重建同ID后读者读到new、WAL仍在且再次重启值仍new（`/tmp/xbot-two-root-wal-hso7zjvm/report.json`、`/tmp/xbot-cross-process-wal-l2tjuezq/report.json`，子进程772314正常退出0）。
- [x] 提交前独立复核569条生产指纹未变、改动Python编译及diff检查通过；实际CLI两次退出与活跃SSE的data/logs均含application.destroyed。architecture及contract审计均退出1，仍仅3处已登记的core.filesystem导入（`/tmp/xbot-collections-architecture.txt`、`/tmp/xbot-collections-contracts.txt`）；未为绿测隐藏规则，也未把workspace读写/权限业务移入backend。
- [x] 本轮精确代码提交`e1ebe0c352be542958a7320ad0466b91f44b17c1`，提交后569条指纹未变（`/tmp/xbot-collections-io-evaluated.json`）。干净archive构建两wheel，离线新uv环境`/tmp/xbot-collections-installed`安装，清单`/tmp/xbot-collections-wheel-manifest.json`核对XBot520/XCore20条、新API/文档/YAML/py.typed/skill在包内，已删模块/tests/Web不在包内；仓库外实际导入来自新site-packages。
- [x] 安装CLI两进程789696/789747、4次本地provider HTTP请求，重启历史4条/续跑8条、附件30B（`/tmp/xbot-collections-wheel-cli/report.json`）；安装版活跃SSE确认仍在读后SIGTERM约0.172s、exit=-15、锁可重取（`/tmp/xbot-collections-wheel-sse/report.json`）。实际运行日志均含application.destroyed；非外部provider验证。当前代码点完成，本段剩余流式/范围/预算及整体goal继续active。

### 迭代 6：流式 artifact 与 shell 输出（实施中）

- [x] 在e1ebe0c3独立安装验收后，由唯一Sol实施流式存取、从启动spool的前后台shell及有限预览、归档下载生命周期；不与Terra混跑。长输入规范化必须在持久确认前满足单份原文和原请求幂等，不能把content_cache业务移到物理后端。固定范围/pin/中央缓存预算仍分后续切片验收，不提前勾选整个第3批。
- [x] 主代理先以e1ebe0c3独立wheel完成同规格shell基线（真实CLI+本地HTTP OpenAI adapter+规范Tool调用，非直接调用runner）：1MiB/16MiB ASCII输出的CLI峰值RSS分别150,437,888 /181,846,016B，启动后约145.9/145.7MB；完整artifact下载hash/size正确、规范日志约15KB。报告`/tmp/xbot-stream-shell-before-1m/report.json`及`/tmp/xbot-stream-shell-before-16m/report.json`，并非新流式实现证据。另明确配置shell ask，通过1次真实permission_request/allow响应完成同样链路（`/tmp/xbot-stream-shell-before-ask/report.json`）；未将默认allow误报审批验收。
- [ ] 已明确的后续输入/媒体边界：submit_message目前在AgentInbox之前立即put image/attachment，改变同inputId的上传或预日志失败可能留下未引用对象。本轮long-text publication作用域不等于所有上传都能回滚；后续输入/媒体切片处理共享CAS/多对象发布顺序及实际失败清理，不向backend注入业务callback，也不以抬高存储预算掩盖。
- [x] 主代理两个中间真实CLI探针通过：1MiB非法UTF8/NUL shell的原字节hash一致、标准ask/allow、有限事件/模型预览（`/tmp/xbot-stream-shell-mid-1m/report.json`）；12MiB原文及编辑版本分别12582913B、对象各1、规范log28417B，pending和消费/重启后两版本均可下载，原payload重试不增模型请求、冲突清楚返回（`/tmp/xbot-long-input-mid/report.json`）。这些均为修改期间smoke，仍需冻结后验证，不算最终生产结果。
- [x] 真实慢下载发现并保留失败证据：16MiB原bytes与完整下载hash正确，但第三次下载仅收首64KiB后不再读，SIGTERM等待10s超时（`/tmp/xbot-stream-shell-mid-16m/report.json`、`.log`，accepted=false）。日志已有application.destroyed仍未退出，不能据此判断关闭完成。诊断专用CLI的SIGUSR1/USR2栈（`/tmp/xbot-stream-shell-shutdown-diagnostic/console.log`）显示业务/worker已结束、Task-1仍在uvicorn._wait_tasks_to_complete等连接；listener需处理未发送socket缓冲，修复后先复测此小工况再扩大200MiB。
- [x] listener修复后主代理同规格16MiB真实socket复测退出0：`/tmp/xbot-stream-shell-16m-shutdown-fix/report.json`，未读完下载时SIGTERM约0.224s、CLI=-15、新root可重取锁；显式abort未发送transport缓冲，不添加任意退出超时或绕过存储drain。
- [x] 主代理冻结快照`/tmp/xbot-streaming-root-freeze.json`含569条生产/资源/脚本、aggregate=1c0685a954e8bbfb4e5525b01e8a91c49552da0a9b15dd6dc827a34bb428a116，独立200MiB源码验收后未变。实际CLI进程810379经标准ask/allow、2次本地OpenAI adapter HTTP执行shell，209715200B含非法UTF8/NUL的原字节与完整慢下载hash一致；单CAS对象209715200B、spool空、规范log11352B、模型tool body8529B、最大事件8755B（`/tmp/xbot-streaming-source-200m/report.json`，验证进程exit0）。
- [x] 该200MiB工况CLI启动后RSS119316480B、kernel/sample峰130695168B；保持第三次下载未读完时SIGTERM约0.202s、CLI=-15，独立root锁探测exit0，实际日志含application.destroyed。测量为本地MockLLM/真实工具与socket的单次工况，不将其称外部provider、4096模型步、硬件无关RSS门槛或最终wheel证据。
- [x] Sol已明确freeze并结束全部工具，最终必要focused组合99 passed、exit0（`/tmp/xbot-stream-freeze-focused.log`），另context/input routing41 passed及相关HTTP/后台9 passed均exit0，不与最终组合相加。已观察真实host/bwrap enabled、标准审批、失败/timeout的ref及byte分页、进程回收、同hash跨store并发发布、>8MiB原/编辑/fork/exchange、预日志清理/未确认保留和真实socket关闭。确认Sol completed后仅启两个Terra做core与client完整整合，未混跑。
- [x] 冻结点主代理长输入CLI复验退出0（`/tmp/xbot-streaming-source-long/report.json`）：进程811301/811355，原/编辑各12582913B、CAS各1、log28669B；pending/消费/重启后两版本流式下载hash一致。重启时preview_chars改2048，原payload retry仍不增模型请求；冲突400且实际objects文件集不变。正常两轮仅2次本地provider请求，非私有store模拟。
- [x] 冻结后的常规源码CLI/SSE验证均真实退出0：`/tmp/xbot-streaming-source-cli/report.json`进程813193/813315、4次本地provider HTTP、恢复历史4条/续跑8条、附件30B；`/tmp/xbot-streaming-source-sse/report.json`确认流仍活后SIGTERM约0.178s、CLI=-15、锁可重取，实际日志均含application.destroyed。旧记录中空会话rename及active todo两项现按已验收的迭代4/5勾选，保留原发现文字作历史。
- [x] 客户端完整整合退出0：integration/ACP139 passed /129.88s（`/tmp/xbot-streaming-client-integration.log`）；TUI含真实server/PTY939 passed /179.18s（`/tmp/xbot-streaming-client-tui-rerun.log`）。首轮938/1是错误message拼接的旧断言，迁移为exitcode摘要、output预览和下载原bytes，原可见Error/展开/后续输入仍通过；首轮日志保留，另定向1 passed不与全套相加。
- [x] core/nonclient owner/XCore完整重跑812 passed、指定旧WebCLI3项deselected /261.17s，pytest与外层handle均exit0（`/tmp/xbot-streaming-core-full.log`及`.exit`）；Web静态/代理两项运行通过。首轮811/1仅compact测试helper未await异步put，迁移后定向及完整重跑；原失败保留`/tmp/xbot-streaming-core-first.log`，未补生产兼容桥。
- [x] 提交前569条生产指纹未变、改动及新增Python编译和git diff --check通过；architecture与contract各退出1，仍仅原登记的3处core.filesystem跨实现导入（`/tmp/xbot-streaming-architecture.txt`、`/tmp/xbot-streaming-contracts.txt`）。业务原文规范化/引用授权/投影属于agentloop/content_cache，底层只持文件流/发布/物理lease与受限SQL；未把业务塞进backend来消除导入问题。
- [x] 精确代码提交`7300ec8131c5e0b3fb85cf24689e6c0caf10775b`，提交后及wheel验收后569条指纹不变（`/tmp/xbot-streaming-root-evaluated.json`）。干净archive构建两wheel，离线新uv环境`/tmp/xbot-streaming-installed`实际安装；清单`/tmp/xbot-streaming-wheel-manifest.json`核对XBot520/XCore20项、async API/owner doc/默认YAML/py.typed/skill入包，已删模块/tests/Web不在包内。仓库外无PYTHONPATH实际导入新site-packages；与旧对比env的Python3.12.12及74项依赖版本完全一致，未新增依赖。
- [x] 安装版CLI/SSE/200MiB/12MiB长输入/1MiB与16MiB对比共6组验证均exit0，报告为`/tmp/xbot-streaming-wheel-{cli,sse,200m,long,1m,16m}/report.json`。常规CLI进程825491/825569、4次本地provider HTTP、历史4→8、附件30B；活跃SSE约0.164s退出(-15)且重取锁。长输入原/编辑各12582913B、对象各1、log28668B，改preview后重启幂等及冲突无新对象、两版本下载仍通过；实际退出日志均含application.destroyed。
- [x] 安装版200MiB保真、标准ask/allow及2次本地provider请求通过：完整209715200B对象/下载hash一致、无spool残留、log11349B；未读完下载时SIGTERM约0.213s退出(-15)、新root重取锁。新env同规格1/16MiB ASCII的kernel峰149598208 /151945216B，200MiB二进制为152301568B（启动后147423232B）；旧env1/16MiB ASCII为150437888 /181846016B。分开保留原报告与运行环境，只报告观察平台，不将单次样本称延迟分位或绝对内存保证。
- [ ] 下一切片继续固定不可变范围、按需原子物化、请求至工具批次及后台job接管pin、中央读取/事件缓存字节预算；图片/附件预put清理、workspace大集合、jobs/wait/team、媒体/provider/权限和最终TUI/产品工况仍待后续。第3批及整体goal保持active。

### 迭代 7：不可变范围与物化使用权（实施中）

- [x] 上轮为已提交并实测的推进：代码7300ec81、验收日志ccfa33a8；本轮先核对工作树及代理权威状态，原设计稿/.worktrees仍未跟踪且不改动，所有前轮代理均completed。由唯一Sol接范围引用/物化/pin的具体实施，不与Terra混跑；主代理保留TODO、边界审查和独立验证。
- [x] 范围选择在创建时固定已持久上界，支持连续seq范围及明确ID集合，源身份随session fork/resume相对重绑定；正文不再为每次完整读取复制一份永久导出。backend只解释物理选择/文件/租约，业务格式、选择规则与授权归owner；临时物化原子发布、失败/取消清理，并按字节预算清退未pin缓存。
- [x] pin覆盖请求构建、模型返回和对应工具批次，实际使用路径的后台shell接管使用权；通用read可重新解析托管引用，正常标准权限/沙箱仍生效。验证缓存淘汰/命中/重建、模型→工具窗口及后台使用、并发发布中固定切点、关闭/fork/恢复和原始工具参数保真；中央replay双预算、team/wait等仍为后续明确切片。
- [x] 主代理不启动AgentLoop的自定义owner小样本通过（`/tmp/xbot-range-owner-mid-small2/report.json`，exit0）：连续范围/显式逆序IDs/空/缺失选择，源后续append不扩原cut，pin/路径重建，纯物理fork后删源、在目标按相对stream重读原12条均正确。此为修改中smoke，不替代最终冻结或真实模型链。
- [x] 小队列实测修正了只按128条分页的缺口：600条source、显式逆序529个约215B的ID、16KiB队列下首次超预算（`/tmp/xbot-range-owner-mid-budget.log`）；内部页同时按条数和字节切分后完整通过（`/tmp/xbot-range-owner-mid-budget2/report.json`，exit0），无500总业务上限。首轮父验证脚本提前释放delete的claim已修正，未给生产添加绕过；同session/stream/record ID删后重建实际读到新正文（`/tmp/xbot-range-owner-mid-recreate/report.json`）。
- [x] 新范围reference导入实际发现破坏目标权威日志：source的4条SOURCE-*包导入已有2条TARGET-*的同名journal，目标原log被替换但owner SQL仍是TARGET-*（`/tmp/xbot-range-import-conflict-reference/report.json`，exit1）。现已校验现存源固定前缀且不替换/追加业务owner源；仅backend标记的只读导入快照可验证追加，短2/长6目标冲突均明确拒绝、原log hash及owner SQL不变（`/tmp/xbot-range-import-conflict-fixed{2,6}/report.json`，exit0）。先前误调用bytes export_file/import_file的探针仅提前拒绝Unknown artifact kind，已另存classification标invalid，不作为reference导入安全证据。
- [x] 主代理大范围实测`/tmp/xbot-range-owner-mid-large/report.json`退出0：4096条自定义记录固定76732250B，源继续到5120条95915866B；529逆序IDs完整，实际pin文件、清退后重建文件、物理fork删源后目标文件均用64KiB页读取，SHA均b8a7760d2fabb9575ee3e2613ee42f6692ba711e5684c68896b8191eb35f17fb。读后tracemalloc current2153305B/peak3155585B、RSS46919680B；无AgentLoop/模型请求，不称4096模型步或最终冻结结果。
- [x] 真实Archive owner exchange验证退出0：foreign stream旧4条/新8条两个固定cut一同导出导入，再session fork、删除原和imported、全新冷root仍能读取两ref原字节（`/tmp/xbot-range-foreign-fork-cold-mid/report.json`）。backend只携带源前缀/selector，agentloop自有引用闭包与授权；0模型请求，不以领域格式硬编码后端。
- [x] 真实源码CLI中间验证`/tmp/xbot-range-cli-mid/report.json`退出0：进程869308、实际bwrap enabled及标准ask/allow、10次本地provider HTTP；0字节idle缓存预算下model生成/返回后read审批间隙/turn结束后后台shell期间路径均存活，后台输出原1548B/hash，job结束后才淘汰；原read逻辑URI和shell参数未改写。冷root不打开thread仍可由Archive和URI重新物化至新进程路径、release即清退（`/tmp/xbot-range-cold-mid.json`，exit0）。
- [x] 模型仍挂起及后台shell仍等待时分别SIGTERM，CLI进程879923/879956实际返回-15，约0.136/0.223秒，原pin文件及整个进程cache namespace消失，实际日志含application.destroyed（`/tmp/xbot-range-cli-stop-{model,background}-mid/report.json`）；报告及子进程终态已核实，上层验证handle因输出截断未保存退出码，不补造历史exit0，最终冻结点另验。
- [x] 新缓存不以全局锁包住物化I/O：主代理门控阻塞B文件写入时，已缓存A的pin/完整读取仍在释放B之前完成，`/tmp/xbot-range-cache-concurrency-final/report.json`实际exit0。固定预算含正文、ref/path编码及每条2KiB元数据，空文件也计费；托管只读文件不进入workspace乐观编辑的历史路径版本map。
- [x] 主代理架构审计曾新增1项ARCHIVE依赖未声明（`/tmp/xbot-range-architecture.txt`4项），已由agentloop补公开服务注入；未修改规则。冻结572个生产/资源/脚本，`/tmp/xbot-range-root-freeze.json` aggregate=4607269fc8c8bce84387fe59ba526d41ee98551ed1c44afede987b2042257e56，整合测试允许迁移测试但不得悄悄改生产。
- [x] 冻结点源码CLI/exchange/模型关闭/后台关闭四验证wrapper及实际句柄均退出0（`/tmp/xbot-range-source-{cli,exchange,stop-model,stop-background}-final.log`及`.exit`）。CLI进程885466用10次本地provider HTTP和真实bwrap/审批复验全pin窗口、后台原1548B及完成后淘汰；关闭进程885727/885774各返回-15、约0.131/0.205秒且cache namespace消失。两个foreign cuts实际exchange→fork→删两源→冷root原字节仍可读；这些是相应路径证据，不称外部provider互操作或全产品压测。
- [x] Sol最终54项lifecycle/compact/filesystem及注入修正后的单项真实loader均exit0（`/tmp/xbot-range-final-{lifecycle,loader}.log`）；94项此前组合保留为中间证据，不相加。确认Sol completed且所有自有句柄已退出后，才启两个原Terra接core/XCore与integration/ACP/TUI完整整合；不得改生产或用旧轮green替代。
- [x] 冻结点独立physical owner大范围、16KiB队列长ID与冷root URI三验证wrapper/句柄均exit0（`/tmp/xbot-range-source-{owner,budget,cold}-final.log`及`.exit`）。4096条76732250B经实际物化/重建/fork文件的64KiB读取SHA仍均b8a7760d2fabb9575ee3e2613ee42f6692ba711e5684c68896b8191eb35f17fb，读后tracemalloc2190786B/peak3205246B、RSS45551616B；source继续增长不扩cut，600条长ID源及529项逆序选择完整。冷root重新物化最终CLI原1548B/hash且release即清退，无thread打开或模型请求。
- [x] 冻结点静态审计实际各exit1，architecture与contract均仍仅登记3处core.filesystem跨实现导入（`/tmp/xbot-range-{architecture,contracts}-final.txt`及`.exit`）；572项指纹不变、401个生产Python编译及git diff --check均通过。独立安装env先固定与上一轮相同的Python3.12.12和72项外部依赖，源码提交后再离线构建/安装两wheel，后续不以源码运行冒充产物验证。
- [x] core/nonclient owner/XCore原完整组合实际退出0：825 passed、指定旧WebCLI 3 deselected，759.35s（`/tmp/xbot-range-core-first.log`及`.log.exit`），Web静态/代理确实运行。观察到record fixture每例均fsync605条的重复成本后，默认减为8条，仅跨500总数的显式IDs用605；固定cut/并发append/原子取消/fork/4→8 foreign/短2长6冲突断言保留，改后8项focused实际exit0、42.69s（`/tmp/xbot-range-core-focused.log`及`.log.exit`）。未将这次测试输入调整后描述为再次全套；生产572项仍完全不变。
- [x] 客户端首轮integration/ACP为138 passed/1 failed、117.95s，失败仅错误假设并发job按启动顺序完成；改为Event控制two先入公开inbox、再放行one，验证独立ID、无自发模型轮及two→one消费/历史，定向通过，保留首轮`/tmp/xbot-range-client-integration.log`和`.exit`。TUI完整939 passed、242.88s、实际exit0（`/tmp/xbot-range-client-tui-first.log`和`.exit`），不再重复。
- [x] 同时运行core大量fsync与TUI时，integration重跑135 passed/4个1～3秒等待TimeoutError、415.27s（`/tmp/xbot-range-client-integration-full.log`及`.exit`）；core worker只读诊断观察jbd2_log_wait_commit及wait_on_page_bit_common，general teardown仍在sync_parents。待所有其他验证退出后，4项原等待预算的隔离定向全部通过、5.90s、exit0（`/tmp/xbot-range-client-integration-diagnostic.log`及`.exit`）。未抬超时或改生产，保留失败产物；随后只启integration/ACP串行全套，不把中间失败隐去。
- [x] 无其他验收写入时的完整integration/ACP为139 passed、146.55s、actual exit0（`/tmp/xbot-range-client-integration-serial.log`及`.exit`）；与TUI939、core825和修改后records8项分别计数，不累加定向重验。两Terra均已停止编辑、确认全部运行句柄终态，冻结572项指纹及git diff --check仍通过；提交前已审查物理后端/领域owner、标准工具注册、配置文档、失败/取消和真实CLI产物证据，无兼容包装或新增依赖。
- [x] 精确代码提交`cce37b158e373f0caa1e3d035e8237e3217dc0b5`，此前源码快照对应关系记录于`/tmp/xbot-range-root-evaluated.json`。干净git archive离线构建两wheel并装入全新`/tmp/xbot-range-installed`，XBot523/XCore20项，534项实际打包文件已核对源码；资源、类型标记、owner文档、skill和新API入包，旧模块/tests/Web不在包中。仓库外清除PYTHONPATH后实际导入新site-packages，Python3.12.12及72项外部依赖与上一轮相同，未加依赖（`/tmp/xbot-range-wheel-manifest.json`、`/tmp/xbot-range-installed-imports.json`）。
- [x] 安装版pin/常规CLI/SSE/模型关闭/后台关闭/foreign exchange+fork+冷读/URI冷读/200MiB共8组wrapper及实际句柄均退出0，`/tmp/xbot-range-wheel-{pin,cli,sse,stop-model,stop-background,exchange,cold,shell-200m}.log`及`.log.exit`。pin进程905703经10次本地provider HTTP、真实bwrap/标准审批，模型→工具间隙和后台使用均存活，后台原1546B相同、结束后淘汰；新root URI按持久ref重建。常规CLI进程905796/905850，历史4→8、附件30B；活跃SSE约0.161秒退出并重取锁。
- [x] 安装版模型/后台关闭进程906248/906340均实际-15、约0.136/0.232秒、pin文件及进程缓存目录清退。200MiB进程906661保留209715200B原始非法UTF8/NUL与慢下载完整一致，log11347B、模型tool body8628B、maxevent8755B；RSS启动145682432B、kernel峰150523904B；活跃下载SIGTERM约0.214秒、锁可重取。实际日志均见application.destroyed。此为本地provider及对应工具/生命周期证据，不称外部provider或全产品工况。
- [x] 用户最新约束覆盖此前代理计划：后续只允许GPT-6.1 Terra，不启动或续派Sol；不重复验证，不过度设计、校验或hash。本批结果已充分，停止追加全套/复跑/hash；后续只对实际修改做必要定向验证，文档日志修改不重复验代码。
- [ ] 本切片已交付；整体goal保持active。接续收口现有3处workspace filesystem跨实现依赖和中央replay字节预算，随后推进工作集其他集合、jobs/wait/team、媒体/provider/权限、TUI及指定产品工况；不以这轮缓存/安装验收替代上述剩余要求。

### 迭代 8：公共文件操作边界与事件窗口预算（实施中）

- [x] 从已验收cce37b15及日志04f6d6e0继续，遵从最新约束只启动两个明确指定GPT-6.1 Terra的实施代理：分别收口3处filesystem跨实现导入、给现有session/workspace replay加字节预算。两者文件范围分开，无Sol运行或续派；主代理负责边界审查、TODO和提交。
- [x] 文件操作共享实现通过最小公共边界提供；工具模式映射归coretools owner，permissions/sandbox沿公开契约工作。保留标准工具/权限/bwrap路径，不将workspace业务移入persistence、不造新executor或框架。
- [x] 事件窗口沿现有中央replay和cursor/单槽唤醒实现帧数与字节双预算；越界明确要求重取快照，保留业务原文。只做必要定向验证，不再次全套、wheel、压测或hash。
- [x] filesystem Terra公开core.api.filesystem的PATH_ACCESS/execute/WORKER_PATH及coretools.api.filesystem的工具模式映射，删除worker内的工具业务映射与旧私有导出，3处消费者改用公共契约，文档同步。一次相关47项测试含真实bwrap通过，未加测试；首次受限环境运行停滞后已中止并确认退出，授权环境完成。主代理本轮唯一架构脚本实际exit0、architecture boundaries: ok（`/tmp/xbot-iteration8-architecture.txt`及`.exit`），原登记3项解除；不重复contract audit、全套、wheel或hash。
- [x] replay Terra沿现有session/workspace窗口实现默认512帧/8MiB的UTF-8完整内部帧计费，仅保存整数大小、不缓存另一份正文、不做hash；workspace删除每订阅者payload队列及回放副本，改为中央窗口游标/单槽唤醒。超大帧推进序号但不保留，旧游标明确过期、需要重取快照。10项原有及新增组合通过，后补普通字节清退2项分别通过，不重复前10项；额外仅选workspace真实HTTP/SSE重放与含tool/usage的provider流各1项通过，3.86s、actual exit0，未扩全套。第一次受限socket运行已中断并确认130退出后授权环境完成。
- [x] 两Terra已停止编辑，主代理已读实际API/worker调用链、计费与游标实现、相关测试；无新executor、缓存框架、持久化业务回调或依赖。filesystem先独立提交`dc471ca4`；本轮新改动不再复用cce37b15的wheel结论，不重复构建旧验收。后续继续其他集合清退与协作等任务，整体goal仍active。
- [x] 本轮组合的精确被评估开发提交为`8e7798fca17615e7989855d6c6761916ea1922d1`；该提交包含filesystem公共边界与事件双预算两项，证据为本轮定向测试及一次架构检查。后续此处只有日志记录变更，无需重复验证。最新用户约束持续生效：仅Terra、必要实施和定向检查，不重复全套/过度设计/校验/hash。

### 迭代 9：连续子线程、jobs 清退与统一等待（实施中）

- [x] 上轮为实际源码与验收推进；本轮核对HEAD70451387/生产8e7798fc及工作树，除用户设计稿/.worktrees无未提交改动。现有subagents仍将每次输入包装Job并复制完整reply，jobs终态仍保留runner/Task/events，wait也未接入规范steer；据此继续设计第10节，而不重复已完成的layout/缓存验收。
- [x] 仅启动一个明确GPT-6.1 Terra实施该关联切片，避免两个代理交叉修改同一生命周期；主代理保留TODO/边界审查/提交。持续遵循只用Terra、最少概念和必要定向验证，不新建WaitService/执行器、不做全套/wheel/压测/hash。
- [x] subagents改为稳定child thread的提交、继续、读取和等待，取消每turn Job及全文结果副本；session级限额计入所有存活后代，关闭释放、重开校验。
- [x] jobs完成即清退执行资源，只留有界轻量终态，等待中的目标结果能保留，过期查询清楚返回不可用；child额度配置移交subagents，取消旧jobs subagent并发字段。
- [x] 统一wait支持冻结active集合、默认any、all/empty/timeout/steered，遵循canonical inbox claimed/consumed与queue区别，model生成期间steer能让出，A的其他完成通知与本集合完成按来源身份区分，wait取消不取消目标、child代次不被followup偷换。完成通知保留NEXT_STEP/wake=False。
- [x] jobs/inbox共享接口先稳定后，实施按owner拆为三个Terra：jobs与共享wait、AgentLoop执行事实/归档、session准入；连续child接线由原child Terra协调，任一时刻最多三个Terra且无Sol。主代理未替代子代理写生产实现。
- [x] jobs基础采用终态100项上限，等待者保留结果；完成清退runner/Task/completion Event，所有owner的终态spec统一仅kind/有限label，不再硬编码shell。wait结果携带轻量结果快照，wait_shell通过标准ToolOutput.artifacts及对应逻辑ID交接输出，cache清退不丢ref/exitcode。原基础23项、后续受影响37项及各新增小回归分别记录，不作为同一全套累加；`/tmp/xbot-collaboration-targeted.log`、`/tmp/xbot-wait-cancel.log`、`/tmp/xbot-shell-wait-artifact.log`均有实际exit0。文档改示例不重验。
- [x] 共享等待归agentloop.api.wait公开小函数，没有WaitService/通知队列；Inbox订阅仅持Event，pending排除claimed，恢复CommitUncertain/retarget的同步也会唤醒但不重发输入生命周期。真实loader+Engine+标准wait_shell验证model生成wait期间steer→steered→下一请求消费一次、目标仍running（`/tmp/xbot-wait-integration.log`，1 passed/exit0）；恢复观察单项通过（`/tmp/xbot-inbox-recovery-observer.log`）。
- [x] AgentLoop owner新增canonical execution开始/结束、输入生命周期与最终message引用的可重建投影，公开latest_input/execution_for_input/latest_execution/message；终态事件在同一规范Task释放锁后发布。实际修正领取后启动前错误的自动重复执行，失败/取消不带旧reply，不以最后assistant猜完成。索引沿用canonical seq及实际查询索引；省去冗余schema预检/单用途常量，不加hash或缓存框架。成功/启动前失败/冷读/未完成与取消、按输入查询各有定向证据（`/tmp/xbot-execution-facts-failed-node.log`、`/tmp/xbot-execution-facts-final.log`、`/tmp/xbot-input-facts-test.log`）。这些不是fork/exchange的新增验收；旧投影升级路径未做自动迁移。
- [x] session owner原子准入涵盖直接open、并发opening、所有活跃后代及已存parent恢复，set_child_limit由subagents owner显式配置。关闭最深后代先于父；直接close_thread也处理后代及已登记opening，失败不提前释放名额。先前相关3项通过，随后opening竞态仅重验新单项，1 passed/2.44s、handle50488 actual exit0；未重跑3项或全套。
- [x] 连续child删除Job包装/全文结果副本与旧job ID工具路径；8项受影响组合exit0（`/tmp/xbot-child-final.log`）覆盖标准工具两轮、权限、取消后继续、冷归档、关闭重开、冻结代次及两条HTTP协作/恢复。中间关闭时通知仍提交导致teardown失败已保留`/tmp/xbot-child-verification.log`，改为先提交规范通知再完成wait，并仅重验受影响组合。不把通过组合替代尚在收口的完成观察越界/冷状态。
- [x] 受限环境DB/async多次等待SQLite future不作为行为证据，相关句柄均确认结束；后续此类验证初次即在授权环境运行，避免环境诊断循环。遵守最新用户约束，停止全套/wheel/大压测/hash，只有实际缺口产生的定向检查。
- [x] 最后按实际canonical execution_id区分live与归档未结束事实，cold/idle无匹配执行时返回interrupted；有界事件游标过期时先重订阅再查冻结turn_id的规范记录，不把丢replay当执行失败或偷换后续代次。新增边界组合5 passed/0.26s，`/tmp/xbot-child-final-boundaries.log`，actual exit0；owner身份/精确读取接口单项通过，`/tmp/xbot-execution-identity-test.log`。原8项不重复。
- [x] 删除launcher历史child ID集合，插件关闭从Sessions现有live scopes派生；resolve用thread_summary，默认wait用一次live scopes快照同步筛选，不扫描全部历史或在筛选期间await改变切点。仅原HTTP默认等待单项通过，`/tmp/xbot-default-child-wait.log`，1 passed/5.30s、handle59790 actual exit0。主代理已读这些实际调用和SQL/规范事实边界，提交前一次git diff --check通过；未生成新hash快照或重新运行旧套件。
- [x] 本轮持久业务事实/投影归agentloop，child协作归subagents，通用准入归session，persistence未加入业务逻辑；没有新WaitService/runner框架。各代理明确全部测试句柄退出并停止编辑。后续仍需指定产品工况、真正供应商及最终安装产物验收，不用本轮局部证据替代完整goal；已有projection升级/旧格式保留未新增自动兼容路径。
- [x] 本轮精确被评估开发提交`dd76884cde21ea683800b44a8e4604d506976ab2`：33个任务文件，已结合实际owner实现和上述定向证据审查；最终SQL索引/冗余预检/通用spec清退等减法未重复运行旧组合。后续仅更新日志不重验代码。整体goal保持active，下一段继续可选team/共享板与剩余集合、媒体/provider/配置、TUI及最终产品工况，不重定义为本轮已做的范围。

### 迭代 10：可选 team 与共享板（实施中）

- [x] 从日志提交f4861758/生产dd76884c继续，整体goal仍active。只委派GPT-6.1 Terra：team代理负责成员/共享板/HTTP/标准工具；执行owner代理补工具发布所需的canonical assistant消息与call关联。主代理负责边界审查、日志和集成，不重复上一轮测试、wheel、压测或hash。
- [x] team按session拥有成员和板正文；latest/get/after按既定顺序与固定持久切点迭代，不设隐性总条数上限。正文与成员投影由team拥有，backend仍只负责物理流/SQL/引用。
- [x] 发布先提交板正文再逐目标投递同一范围引用；同发布ID重试幂等、冲突明确、逐目标报告接受或失败。使用既有inbox和权限路径，不增加自动补投服务或全局outbox。禁用插件后的历史引用、恢复和fork由必要定向验证确认。
- [x] 仅运行本轮实际改动的必要组合；记录真实退出码和未覆盖范围。遵循用户最新约束：不重复验证，不过度设计，不过度校验，不过度hash；仅Terra代理。
- [x] 执行owner公开隐式ToolInvocation，engine传已持久化assistant消息ID，provider call ID保持原值；不改ToolCall协议，不绕标准registry/guard。新增单项pytest输出1 passed/2.84s（`/tmp/xbot-tool-invocation.log`）。最终句柄73137已结束，但退出码观察丢失；此前两次fixture许可失败日志被同路径覆盖，不能补作历史证据，也不为此重新测试。team组合另实际覆盖相同执行链。
- [x] session owner在实际open_session入口保留`__`前缀，避免对话线程与非对话物理流占用同一ID；未改backend或加入team硬编码。新增单项1 passed/2.21s、handle90212 actual exit0（`/tmp/xbot-reserved-thread-ids.log`），拒绝发生在持久化之前，普通ID可正常打开；不重复旧组合。
- [x] team首次组合的fixture缺口已修正：显式Mock所有线程并禁用caption，避免自投递唤醒默认provider、消耗预设回复。Terra报告2项组合5.11s/handle53242 exit0、默认禁用单项1.88s/handle50054 exit0；包含606条读取、发布中固定cut、顺序/缺失ID、HTTP 400/404/409、部分投递补试、fork删源及不挂team的新root冷读。首轮2项失败/handle79547与中间41651/80345/52479均已终止，但这些输出只保留在子代理工具会话、未保存磁盘日志；不补造日志、不为证据归档重跑。
- [x] 主代理审查补出Agent重试入口缺口：新的模型调用身份不能用来重试旧发布。新增标准team_retry/typed retry，按原消息ID恢复原mentions，复用投递身份和同一正文ref，不新增板消息、不要求模型重传正文。仅修改并重验受影响节点，1 passed/4.07s、handle67570 actual exit0，日志`/tmp/xbot-team-retry.log`；主代理已读实际日志、最终投递/存储/工具/HTTP/禁用路径。原另外两项不重复。
- [x] 工具身份与session边界分别独立提交389b984d、b63780be；team保持api/impl/doc/tests布局、默认关闭，元数据/schema/投影及逐目标投递都在team owner，persistence无新增业务。未引入额外cache/outbox/执行器/依赖。真实外部provider、慢客户端与最终安装产物等仍属整体待办，本轮结果不替代它们，goal保持active。
- [x] 本轮精确被评估开发提交`52175e963a1d6e4c2dc9732156554fab4d09c4dd`，含工具身份/session内部ID边界/team三项责任提交。最终暂存检查通过，无生成产物或用户无关文件；之后仅追加本日志，不重复验证。下一切片继续目录/连接缓存清退、媒体/provider/权限配置与剩余产品验收，完整goal不缩为本轮team范围。

### 迭代 11：目录解析与 provider 资源释放（实施中）

- [x] 从日志1aa867cf/生产52175e96继续；上一轮有实际源码提交及定向证据，工作树只有原用户设计稿/.worktrees未跟踪。只启动两名Terra分别实施provider资源生命周期和Agent源解析缓存，文件责任分离；共享板及旧验证不重跑。
- [x] provider目录按使用配置共享，thread绑定与在途请求持有明确使用权；替换/关闭释放，最后使用者完成后await关闭client和清退目录，不靠GC、不清退仍在使用的连接、不新增通用池框架。
- [x] Agent未展开源解析结果归process owner，按字节预算清退、变更替换旧版本、实际重读目录时清理已删source；线程变量独立展开，直接loader调用没有模块全局缓存。现有fiber Catalog只持当前定义，不把它重写成历史缓存。
- [x] 仅运行本轮实际变化的必要定向组合，保存独立日志和准确退出码；不重复旧套件、wheel或hash。整体goal仍active，媒体/provider互操作、配置/客户端及最终产品工况继续待办。
- [x] AgentSources移除模块级128项lru_cache，process实例按路径保留单一stat版本并在关闭清空；默认4MiB计费预算，计费为源字节64倍加顶层对象/键浅层大小，是保守驻留估算，不能作为精确Python堆上限或已测最优参数。超预算单源仍可读取但不缓存。相关12项一次通过/0.43s、actual exit0，日志`/tmp/xbot-agent-sources-test.log`；预算用例为100/7000/100000B，覆盖共享解析/独立展开、变更删除、清退及process关闭。最终数值仍待后续同产品工况测量，不追加大压测。
- [x] LLM公开ProviderUse（close/astream）和acquire，替代无主create缓存；MODEL以async replace接管使用权，外部override仍由caller关闭。目录由scope计数共享，client由绑定/流持有，idle立即释放，不加另一个有界池。thread关闭先释放绑定再目录，root等待在途流和正在close的资源；关闭错误不跳过其他client。主代理已读实际引用计数/流清理/目录dispose及跨owner调用。
- [x] Terra首轮3项中2 passed/1 failed（handle58213 exit1，`/tmp/xbot-provider-lifetime-test.log`）；失败为fixture替错openai工厂，minimax实际走anthropic，修正后只该节点1 passed/2.27s（26294 exit0，`/tmp/xbot-provider-loader-retry.log`）。关闭失败/原工厂8项0.82s及生命周期3项2.38s分别通过（91097、86866 exit0）。公共use接口收口后受影响组合11项2.13s（57255 exit0，`/tmp/xbot-provider-lease-final.log`），与前述有重叠，不累计作全套。
- [x] 取消选择单项在真实loader中阻塞旧client.close再取消，确认完成释放、新绑定与metadata一致、各client只关一次：1 passed/1.93s、77439 exit0（`/tmp/xbot-provider-cancel-selection.log`）。最后补公开use提前关闭stream时await底层finally，未提前释放pin或关闭client；仅该新增单项1 passed/0.88s、14337 exit0（`/tmp/xbot-provider-early-close.log`），未重复其他已通过节点。所有句柄均终态。证据为本地fake/SDK构造，不声称真实供应商互操作或adapter内部网络流已新增验收。
- [x] Agent解析owner已独立提交85007d5a；本轮没有修改persistence、队列、媒体协议或TUI，不因相关测试通过勾选那些整体待办。公共API/生命周期文档同步，未保留create兼容桥；下一切片继续真实剩余集合和媒体/provider等完整目标。
- [x] 本轮精确被评估开发提交`78af9efa7b396d8537443b453cd22a6ba06f00bd`，包含Agent解析缓存及provider资源两个责任提交；最终暂存检查通过，无生成产物。之后仅提交本迭代日志，不重跑旧验证或生成hash快照；完整goal保持active。

### 迭代 12：规范媒体与输入契约（实施中）

- [x] 从日志b03a0e7f/生产78af9efa继续，上轮有实际源码提交及定向证据；工作树除原用户设计稿/.worktrees无未提交改动。两名Terra分别实施核心/adapter媒体模型与输入/客户端契约，先约定MediaPart.artifact、ResolvedMediaPart.artifact和统一media字段，主代理处理边界审查、TODO与提交。
- [x] 删除ImageRef重复元数据和ImagePart专用持久类型，统一MediaPart→完整ArtifactRef；媒体引用授权、范围/导出/fork/恢复及请求绝对路径沿既有ArtifactStore处理。provider发送前明确拒绝不支持模态，保留现有文本与图像格式，不新增音视频adapter。
- [x] wire/typed输入统一MediaInput与media字段，session记录保留完整ArtifactRef，标准read/ACP/TUI实际消费者同步；上传base64仅为transport数据，不进入持久对话。不保留旧别名、双字段或自动旧格式兼容。
- [x] 只跑实际受影响的必要定向组合，保存独立日志与准确exit，初次DB/socket/async在授权环境运行；不扩全套/wheel/hash。既有媒体/附件pre-put失败清理仍是明确待办，本轮契约迁移不假称已解决，也不把多个prepare_stream直接嵌套造成重复digest/逆序锁死。完整goal仍active。
- [x] 核心/adapter Terra定向31项5.60s、handle87586 actual exit0（`/tmp/xbot-media-focused-2.log`）：文本/图像格式、resolved绝对路径/原bytes、未知MIME及audio/video发送前拒绝、标准read、真实loader下工具媒体授权/fork/resume/exchange保留完整ref。首次命令名称错误actual exit4/no tests ran（`/tmp/xbot-media-focused.log`），不算生产失败或通过证据。
- [x] 输入/客户端组合首轮71 passed/1 failed（`/tmp/xbot-media-client-tests.log`），唯一失败是新HTTP fixture模型未声明图像能力。修正声明后仅该节点1 passed/2.51s（`/tmp/xbot-media-http-retest.log`），下载原bytes、历史中name/size/sha和media-only提交正确；原无效base64/空请求、ACP映射、controller/queue等已通过节点不重跑。另真实服务TUI上传及App节点2 passed/3.36s（`/tmp/xbot-media-tui.log`），不是完整PTY或外网供应商验收。
- [x] 主代理已读实际MediaPart/ArtifactRef、投影/导出闭包、context pin、adapter编码、wire/SDK/ACP/TUI路径及日志；生产代码旧Image*类型与.images引用归零，归档设计草案明确非当前契约、保留历史不重写。HTTP当前文档已同步。移除read媒体路径未使用result_data及其重复SHA计算、未使用_success.data，以及能力检查后冗余的全局image-only分支；这些减法未重跑已通过组合，不声称删除后有新全套结果。
- [x] 核心/维护的Python客户端媒体契约提交da3d7a37。后续发现当前文档仍明确支持xbot web，故同一Terra补Web源码的media/ArtifactRef/media_count、name及历史/live/tool媒体下载映射，保留有用的图像视图模型，不机械改CSS或Markdown词汇。未生成web_dist、未装依赖或跑全量npm/audit/e2e。
- [x] Web定向vitest首组3文件67项通过（handle47718 exit0，`/tmp/xbot-web-media-vitest.log`）；实际补live/tool显示后仅受影响2文件13项通过（64089 exit0，`/tmp/xbot-web-media-vitest-last.log`），有重叠不累计。TypeScript fixture旧字段及一次错误工作目录导致92180/92787 exit2，修正后23111及最终30237 exit0；最终日志`/tmp/xbot-web-media-tsc-last.log`。主代理已读源码与最终日志，不重跑这些组合。
- [ ] 真实互通限制：Web仍有本轮之前的role/content历史形状、open history数组及SSE envelope契约债务，与当前Python的kind/history.items等不一致；上述媒体映射/tsc通过不能证明完整Web互通。阶段6必须按当前Python契约收口并实际驱动浏览器，不能以文档“支持”或现有stub测试替代证据。
- [x] 首次真实MiniMax媒体冒烟由Terra完成，只使用现有环境MINIMAX_API_TOKEN（存在性/来源记录、不输出值）与合成payload；Python评估提交`da3d7a37b11cad237335beb967e265807c9702bc`。标准open_runtime/loader/AGENT_LOOP、生产Anthropic adapter、仅read，3次真实POST `/anthropic/v1/messages`均200、0重试、max_output_tokens512；文本轮read成功、最终cobalt，256px红PNG轮最终red。主代理已读安全报告及保留的canonical rollout实际文本，不从脚本exit0或关键词命中猜成功，也未重发请求。
- [x] 真实媒体历史中一个MediaPart保留完整ArtifactRef（image/png、media、red.png、851B、sha字段），ArtifactStore原bytes相等；session/root均await关闭，handle44735 actual exit0。报告`/tmp/xbot-minimax-accept/report.json`；usage input/output依次1189/35、55/4、130/110，供应商cache_read计数128/1317/1372，只记这次观测，不推导延迟/驻留或普遍命中率。响应未提供request-id/x-request-id，不能补造关联字段。官方说明`https://platform.minimax.io/docs/api-reference/text-chat-anthropic`，实际仓库模型名Minimax-M3被服务端接受；未改用户配置。
- [x] 本轮开发提交`4b3a0684ccf917700f5b88254dafeed8290578b0`包含Python媒体和Web字段两责任提交；对应范围的定向证据见上，Python真实供应商证据仍精确归da3d7a37（后续仅Web/文档变化）。无生成产物/依赖变化，最终暂存检查通过。整体goal保持active；下一段优先上传接受失败的物理发布清理，并继续Web真实协议、配置/预算和TUI/最终产品工况，不缩为本轮已完成的媒体路径。
