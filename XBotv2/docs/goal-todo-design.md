# Goal / todo 工作流设计

这里记录产品取舍，不以参考产品的实现为规范，也不改变核心插件可选的约定。

## 参考与取舍

- [x] [Claude Code tasks](https://code.claude.com/docs/en/agent-teams#assign-and-claim-tasks)：任务状态与依赖用于协调工作。XBot 保留 pending/in_progress/completed，阻塞从依赖图派生；不新增一份 blocked 状态，不将 thread-local todo 升格成共享团队调度器。
- [x] [OpenCode todo](https://opencode.ai/docs/tools/#todowrite)：todo 用于多步骤进度管理。XBot 保留有稳定 ID 的增量工具，计划允许修订，不要求每个简单问题建清单。
- [x] 本轮采用 Codex/DSH 的原会话续跑与显式目标状态更新方向，不采用 Claude Code 的独立小模型裁判；不新增另一套消息转换、模型调用或调度框架。

## 开源实现核查（2026-09-29）

只以用户指定的 Codex、OpenCode、Claude Code、DSH 为参考。以下是源码阅读事实，不是运行验证；OpenCode 在线链接指向可变分支，Claude Code 此处只有官方产品文档，不作为内部源码证据。

- [x] OpenCode [`tool/todo.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/todo.ts) 接收模型提交的列表；[`session/todo.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/todo.ts) 在事务中替换会话任务并发布更新事件。字段为 content/status/priority，位置用于排序。这条路径不调用独立评估模型，也没有依赖图调度。
- [x] OpenCode [`session/prompt.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts) 的 runLoop 检查停止原因、工具调用和用户消息归属决定正常退出；todo 更新与此处退出判断不是同一职责。
- [x] Codex 本地对象库 `output/codex-research/codex`，固定提交 `d583e73c4d1204f1e9f654ef87065e9f0dd07ac7`：`codex-rs/ext/goal/src/runtime.rs::continue_if_idle` 检查目标状态并调用原线程 `start_turn_if_idle`；`templates/goals/continuation.md` 要求执行模型核查当前证据后调用 `update_goal`。`tool.rs::handle_update` 更新持久状态并通知；`extension.rs::on_turn_stop` 处理用量、空响应与执行失败，不另发模型判断请求。`core/src/tools/handlers/plan.rs` 解析列表后发送 `PlanUpdate`，不是目标完成判定器。
- [x] DSH 本地 `output/deepseek-harness`，干净提交 `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`：`packages/goal/tool-goal/src/index.ts` 提供 create/get/update，complete/blocked 由执行模型调用；`goal-round-driver/src/index.ts` 在原 Agent 空闲时使用 `followup`，校验目标 id/revision 与排队输入归属，不另发模型判断。`goal/src/types.ts` 分离持久 phase 与进程内 activation；不能从恢复 active 状态直接推导自动续跑授权。
- [x] DSH `packages/todo/tool-todo/src/index.ts` 单独接受 content/status 列表，记录 `todo/write`，UI 从事件投影；允许并行 in_progress 是配置策略。此版本的 todo 投影在下一 turn/start 清空，不能不加取舍地搬进跨回合工作计划。
- [x] [Claude Code 内置 goal 官方文档](https://code.claude.com/docs/en/goal#how-evaluation-works) 明确采用每轮小模型评估条件与会话。它与 Codex/DSH 不同；之前笼统声称主流都不采用独立评估是不成立的。该方案不满足用户本轮拒绝重复全量评估的要求，不作为 XBot 的实现目标。
- [x] Goal 拥有目标控制状态和续跑许可，Agent 通过标准工具报告完成或阻塞；todo 不替目标判定完成。续跑使用既有 RuntimeInput/NEXT_TURN；陈旧输入在既有 ON_TURN_INPUT 接受边界拒绝，不新增 hook。冷恢复不自动恢复续跑许可，也不清零统计。实现与测试尚在下列未勾选项中。

## 第一切片：可修订计划与可信完成依据

- [x] `task_update` 可添加和撤回两个方向的依赖，不删除任务来绕开旧依赖。
- [x] 在最终候选图上校验领取条件，支持一次请求解除依赖并开始工作；不能一边开始一边添加尚未完成的前置任务。依赖修改不能让现有进行中任务变成被阻塞的状态。
- [x] 任务 ID 不变，一次有效修改只提交一个 version 和 TaskChanged；无实际变化不写 snapshot、不发事件、不重发完成提醒。
- [x] 完成提醒只在清单从未完成变为全部完成时出现，要求核对验收证据；任务标题不证明验证已做，不强制调用 subagent。移除 verification_hint，保留 verification_nudge 开关。旧自定义配置需删除 verification_hint，不保留别名。
- [x] 删除 render_transcript、辅助 evaluator 和专属 verdict/retry/checkin；标准 goal 工具明确更新目标状态，同会话执行可续跑到完成，不额外发送判定请求。
- [x] 保留现有 KV 快照、RuntimeInput、TaskChanged/GoalChanged，不新增事件、日志或自动执行器。Goal 状态与用量基线在同一次 KV 写入中提交，不保留两个互相依赖的写入。

## 后续切片（尚未完成）

- [x] Goal 的用户控制：`/goal pause` 保留目标，`/goal resume` 继续原目标，`/goal clear` 清除；用户 `/goal <objective>` 可替换目标，模型 create_goal 不覆盖活动目标。目标 id/revision 校验更新，输入接受边界拒绝撤销的续跑。工具明确要求用户授权，不因任务很长擅自创建 goal。
- [x] 只统计整个 goal 执行过程，不提供 token 预算、用量上限或达到额度自动停止。统计包含所属回合中完成工具后的收尾请求；回合结束后固定结果，后续无关对话不计入。暂停/恢复累计已消耗用量，不清零、不重复计数。
- [x] TUI 显示 goal 实际状态、执行次数、usage 与原因，只消费公开投影，不通过 UI 推断或推进业务状态。
- [ ] Todo 的依赖/可执行任务展示尚未新增专门交互验收，不以 goal 渲染证据代替。
- [x] Goal 生产链路验收包含模型工具执行、通知、持久化恢复与真实 server/TUI；不以工具 schema 或提示词字符串断言代替产品流程验证。

## 本轮验证与格式变更

- [x] `test_goal.py` 14 项包含显式终态、陈旧输入、两轮计费、暂停恢复累计、失败原子性与真实插件卸载后的未领取输入清理；`test_todolist.py` 16 项包含修订、失败回滚和持久化恢复。
- [x] 最新受影响 core/startup/todo/TUI 文件组合 276 passed；此前核心回归 655 passed、TUI 901 passed。这里记录工作树验证，不声称已提交的版本验收。
- [x] 本轮公开 GoalChanged → reducer → EntryWidget 的 80×24 SVG 经浏览器实际渲染查看；恢复后显示 resume required、执行次数及 input/output/total tokens。不是新 goal 全流程 PTY 交互验收。
- [x] 删除空 GoalConfig；goal 不再接受旧 evaluator 配置。旧 goal 持久状态格式不提供兼容读取或隐式迁移；不自动删除用户历史。新 schema 使用 objective 与明确状态，不保留 condition/achieved/failed 的别名。
- [x] 真实 uvicorn + Textual 主流程：composer 输入 `/goal`，受控模型从可见输入与 get_goal 结果取得身份，两轮执行并 update_goal 完成；最终 Ready、2 次工具、24/9/33 tokens。主代理实际查看本轮 `/tmp/xbot-goal-real-tui/goal-browser.png`，未使用旧截图。
- [x] 新增 goal 场景后复跑整个真实 server/TUI 文件：49 passed（101.74s），原有流式、恢复与权限交互测试未回退。
- [x] 完整 HTTP 109 passed；最后补充的 steer 原文可见断言 focused 通过。测试覆盖 compact、后台任务完成、运行中 steer、精确计费与冻结；shell 执行函数受控，JobRegistry/通知/模型请求链为生产路径。
- [ ] 本轮未调用外部付费 provider，也未新增独立终端 PTY 的 goal 专项用例；不把 Textual 交互测试写成这两项证据。
