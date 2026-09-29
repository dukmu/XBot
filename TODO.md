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
- [ ] 每个切片先核对 owner 和真实生产入口，再写失败测试、最小修复、focused 验证及相称的扩大回归。

## 当前稳定基线

- [x] 启动与状态：`d988e4c` 修复已有 JSON KV 状态导致 `/sessions` 404；不得重新把 StateService 日志化。
- [x] 输入语义：`1f81dc5` 保留 steer 身份并只在 model context 添加临时补充说明；原始内容和客户端投影不变，steer 不等于 interrupt。
- [x] 持久化读取：`a5ac52b` 让活跃 reader 增量读取外部追加的 trace 后缀；失败后丢弃缓存并明确重试，不复制完整投影。冷读仍需完整读取。
- [x] jobs：`b989548` 覆盖 runner 启动前取消、唯一终态/通知、wait 释放，以及父代理读取并发子应用的 cancelled/failed 结果。
- [x] TUI：`0ce4cc6` 覆盖默认 Enter steer、真实长历史分页、流式阅读 anchor、80×24 ↔ 100×28 resize、PageDown 回尾和陈旧滚动回调失效；`b72e668` 只强化完成帧断言。集成 TUI + ACP 为 `895 passed in 141.51s`。
- [x] 真实 HTTP 测试宿主已由 `f2286f0` 收回同一 event loop；steer HTTP 投影断言由 `5270207` 覆盖。局部绿测不等于总体迁移完成。

## 1. 底层架构与数据模型收口（对应用户旧项 6，当前最高优先级）

- [ ] 逐项核对设计文档第 2–5 节：每个领域事实只有一个权威类型、owner 和持久化来源；删除重复 DTO、动态语义、旧入口、兼容导出及隐式 callback。
- [ ] 从 application → loader/XCore → session/runtime → protocol/client 做依赖审计；Core 不聚合插件对象、不硬编码插件名，protocol 只表达 transport contract。
- [ ] 核对 lifecycle 的唯一 owner：boot、apply、start、failure rollback、dispose、process/session/thread close 均只有一次明确清理，不依赖析构或静默 fallback。
- [ ] 核对 human、runtime input/notice、tool、interaction、completion、workspace/session event 的身份与恢复语义；不能因“都像消息”共享错误路径。
- [ ] 建立剩余迁移清单时只记录实际重复或错误职责；不得从旧 TODO checkbox 反推需要新增抽象。
- [ ] 完成后运行 Core 与关键 layering/contract 测试，并记录仍未迁移的具体类型/入口，而非只报告数量。

## 2. 运行时失败与恢复（对应用户旧项 2）

- [ ] 建立 production runtime 失败矩阵：启动中插件失败、provider/tool 异常、hook 异常、task cancellation、interrupt、close 与并发提交；验证未提交输入可重试且资源只清理一次。
- [ ] 覆盖 pending interaction 的恢复与竞争：permission/question 在断连、重连、取消、外部 resolution 和重复响应下只能完成一次，陈旧请求不可重新出现。
- [ ] 覆盖多条 pending input 的 FIFO、claim/commit/rollback 与 turn 边界；steer 在安全 step 领取但不自动 interrupt，显式 queue 留到下一 turn。
- [x] jobs 已覆盖 runner 尚未启动时取消及 cancelled/failed 子应用结果读取；后续只补实际缺失的竞争路径，不造通知框架。
- [ ] 用真实 loopback HTTP/SSE 验证 sequence gap、cursor expiry、watchdog reconnect、恢复后的 authoritative snapshot 和清理；不得以 scripted backend 代替关键断线行为。
- [ ] 运行相称的 Core + HTTP + fold-in 回归，并逐项说明失败注入点与可观察结果。

## 3. 持久化与复杂度（对应用户旧项 3）

- [x] 区分职责：StateService 是 KV 快照；session trajectory/inbox 是追加记录。撤回的 StateService JSONL 设计不再作为候选方案。
- [x] 外部 trace writer 的已知前缀可增量读取，损坏后缀不发布部分结果；缓存丢弃后的重试重新验证磁盘事实。
- [ ] 审计 metadata、usage、插件状态、transcript 和 trajectory 是否重复持有同一权威事实；静态身份只存一次，动态变化沿用现有事件，避免第二份日志。
- [ ] 测量生产读写的记录数与字节数：追加、分页、compact、外部 writer、缓存轮换、close/reopen、多 session；累计 n 次交互不得因完整历史复制或重放形成 O(n²)。
- [ ] 验证冷历史首次加载、损坏完整记录、部分尾写、fsync/replace 失败、进程恢复及 cursor 连续性；不能用热缓存耗时阈值代替。
- [ ] 明确每个 persisted artifact 的格式、owner、恢复投影和清理边界；格式变化必须有迁移决策，不暗加兼容双读。

## 4. 插件职责与生命周期（对应用户旧项 4）

- [ ] 按主线插件实际职责建立最小证据表：注册/依赖、成功路径、关键失败、事件、可选持久化、卸载；不存在的能力不虚构测试或列为缺陷。
- [ ] 核对 compact、goal、todolist、subagents、skills、MCP、browser 的公开服务与事件 ownership；插件不得读写 runtime 私有状态或让 Core 识别插件名。
- [ ] 复核 compact/goal/todolist 完成通知不会重复创建 turn、恢复后不会重复投递；只有真实复现的缺陷才修改实现。
- [ ] 验证插件卸载、依赖重绑和部分 apply 失败后的 disposer 顺序；健康能力继续可用，失败诊断保留原始 owner 信息。
- [ ] Browser 只验收已声明 policy、关键协议互操作和资源清理；无界攻击矩阵留作独立安全 backlog。

## 5. Textual TUI 产品验收（对应用户旧项 5）

- [x] 真实 CLI/tmux 已覆盖 permission→question→长 paste/follow-up、session switch、已有状态 resume、compact→新进程 resume、长历史分页、流式 anchor、折叠和动态 resize。
- [x] Enter 默认使用现有 `delivery="steer"`，Shift+Enter 换行；显式 queue/interrupt 独立，不改用户原文、不发送额外事件。
- [x] 当前产品契约保留分页历史、只读 `/thread`、紧凑单列布局、受限 Think/tool 展开窗口、永不折叠 final reply、可见 context trace、typed permission/question modal，以及 usage/context/cache 状态。
- [ ] 在真实 PTY 中主动制造中途 socket 断网，验证 reconnect 后 transcript、pending interaction、cursor、focus、selection、折叠与 scroll anchor 不重复、不跳尾。
- [ ] 完成 Settings 可用性闭环：真实数据来源与 scope、可发现导航、支持的 schema mutation、revision conflict/reload、返回会话后的草稿与 focus；未实现页面明确标注。
- [ ] 继续核对 80×24、100×28 和宽屏的 Unicode、长代码、超长 tool output 与信息优先级；不追求像素仿制，也不复制 Claude 命令表。
- [ ] 若凭证与网络可用，单独记录一次 reasoning-capable 外部 provider smoke；受控 loopback 不冒充供应商互操作。
- [ ] 最终读取当轮 capture 和 canonical server history，确认实际产物后才能关闭 TUI 验收。

## 6. 子代理 `send_message`（对应用户旧项 1，后置）

- [x] 当前事实：interactions 的同名工具只是用户进度通知；ChildApplicationSession 单轮后释放应用，尚不支持交互式代理通信。
- [ ] 由 subagents 拥有 `send_message(target, message, delivery="steer")`；target 使用 spawn 返回的稳定 thread ID，不以 definition、display name 或某次 job ID 充当身份。
- [ ] 复用 InboxTarget：运行时 steer 在安全 step 边界领取且不取消当前 provider/tool；queue 在当前 turn 后运行；interrupt/cancel 始终独立。
- [ ] 子代理 thread 可继续，但每次执行 job 保持单向终态；完成后新消息恢复原 thread 并创建新 job，不复活旧 job。提交、结束和关闭由唯一 runtime owner 串行化。
- [ ] 使用现有 RuntimeInput/RuntimeNotice 与 inbox persistence，记录发送者、目标、delivery 和消息身份；不伪装 HumanInput、不增加第二份消息日志。
- [ ] 工具成功只表示接受投递，并返回 target thread、input ID 和关联 job；未知目标、越权、关闭竞争明确失败。默认只允许父代理到其子代理。
- [ ] TUI 只根据标准工具结果和既有事件显示 interaction；`/thread` 保持只读，主会话不复制 child transcript，不增加客户端业务分发。
- [ ] 红测覆盖运行中 steer、queue、已完成 child 继续、多消息 FIFO、结束竞争、无重复消费、权限/取消/恢复，以及 wait/read 指向新执行结果；再做真实 server/PTY 验收。

## 最终交付门槛

- [ ] 1–5 的未完成项均有实现证据或明确排除理由；第 6 节按用户后续授权单独推进。
- [ ] focused、Core、关键 integration、TUI/ACP 均在同一最终提交上运行；任何代码变更后受影响结果重新验证。
- [ ] 记录未运行的外部 provider、WebUI 或安全矩阵及原因；不以旧 capture、旧 PNG 或历史测试数作为当前证据。
- [ ] 最终检查无多余抽象、兼容路径、生成产物或无关用户修改，`git diff --check` 通过。
