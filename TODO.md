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
