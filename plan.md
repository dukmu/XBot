收到，这次我先不给仓库动刀。先把你要的东西完整给出来，而且把我看到的架构问题一并摊开讲清楚。

**结论**
按 [task.md](/home/shefrin/repo/XBot/task.md:1) 的目标，这个项目现在的核心矛盾不是“功能不够”，而是“运行时中心”还没有转成“state 中心”。`task.md` 要的是“极小内核 + 文件状态图 + 可重放日志 + 可替换 loop”，而当前实现更像“LangGraph 驱动的内存会话运行时”。

**我看到的主要架构问题**

1. 状态中心不对。当前 `HermesInteraction.create()` 直接用 `InMemorySaver` 和 `InMemoryStore` 启动图，核心状态只活在进程内存里，不满足 `task.md` 里要求的 file-backed state / replay / recovery。[interaction.py](/home/shefrin/repo/XBot/xbot/interaction.py:117)
2. 编排职责耦合过重。当前 [graph.py](/home/shefrin/repo/XBot/xbot/graph.py:40) 同时负责 system prompt 拼装、runtime state 注入、tool guardrail、interrupt、cache hook、compaction，这会让 loop 不可替换，也很难演进成 `task.md` 里说的“少而清晰、可插拔阶段”。
3. 配置上下文是全局可变的。`configure_runtime_paths()` 通过模块级全局 `_RUNTIME_PATHS` 改运行时路径，这对多线程、多 session、subagent、重放都不友好。[config.py](/home/shefrin/repo/XBot/xbot/config.py:68)
4. 持久化模型不统一。子任务记录已经是文件化 manifest 了，但主任务 turn/run state 仍然不是；也就是说仓库里同时存在“两套 state 哲学”。[tools.py](/home/shefrin/repo/XBot/xbot/tools.py:136)
5. cache 不是持久资产。大工具结果缓存目前只是进程内字典，进程一停就没了，这和 `task.md` 里“artifacts / events / graph 可回放”的方向冲突。[cache.py](/home/shefrin/repo/XBot/xbot/cache.py:21)
6. 测试偏边界，缺少状态正确性验证。现在测试已经把权限和 sandbox 边界守得不错，但还没有覆盖“事件日志能否重放出同一状态”“跨重启能否恢复任务”等架构级断言。[tests/test_runtime_boundaries.py](/home/shefrin/repo/XBot/tests/test_runtime_boundaries.py:16)

**重构目标**

1. 把“模型驱动运行”改成“状态驱动运行”：任务目录成为唯一可信状态源。
2. 建立显式任务状态分层：`goal / plan / graph / state / events / artifacts`。
3. 把 loop 拆成少数几个稳定阶段，至少做到 `prepare -> act -> tool/interrupt -> verify/persist` 可替换。
4. 消除隐式全局运行时依赖，让 session/thread/task 都有显式上下文对象。
5. 让工具结果、interrupt、compaction、subagent 都进入统一事件流。
6. 建立“可重放、可对账、可验证”的测试体系，而不是只验证单次调用行为。

**建议的重构计划**

1. Phase 1: State 落地先不推翻 LangGraph，只在外层补 `task.md` 需要的任务目录：`task.yaml / goal.md / plan.yaml / graph.jsonl / state.yaml / events.jsonl / artifacts/`。这一步的目标是“先把真相写下来”。
2. Phase 2: Runtime 契约收口抽出 `Run / Turn / Interrupt / ToolResultRef / RuntimeFrame` 几个稳定契约，把 [interaction.py](/home/shefrin/repo/XBot/xbot/interaction.py:64) 从“会干很多事的 runtime”压成“调度器 + 事件出口”。
3. Phase 3: Loop 解耦把 [graph.py](/home/shefrin/repo/XBot/xbot/graph.py:209) 的 compaction、prompt build、tool guard、interrupt 处理拆成可组合阶段。LangGraph 可以继续留着，但只能当某一种 loop executor，不该继续承载全部架构职责。
4. Phase 4: 持久化与回放把 `ToolResultCache` 改为文件或 SQLite-backed，补 `replay` 和 `materialize state` 能力，保证 `events.jsonl + graph.jsonl` 能还原 `state.yaml`。
5. Phase 5: Plan/DAG 化
   把计划从 markdown/临时脑内状态，升级成版本化 `plan.yaml` 和可执行 DAG，这才真正贴近 [task.md](/home/shefrin/repo/XBot/task.md:138) 的设计前景。

**验证手段**

1. 状态一致性验证同一任务目录下，`events.jsonl` 重放结果必须和 `state.yaml` 一致；`graph.jsonl` materialize 结果必须和当前 DAG 视图一致。
2. 恢复验证中断在 tool confirm、user ask、compaction 后重启进程，任务必须能从文件状态恢复，而不是依赖内存 checkpointer。
3. 边界验证保留现有权限和 sandbox 测试，同时增加“持久状态不越界”“不同 session/thread 不串状态”的测试。
4. 回归验证同一输入任务，重构前后用户可见行为应保持等价：消息顺序、interrupt 时机、tool deny/ask 语义、compaction 可见性都不能回退。
5. 人工验收
   实际跑一个 session，检查任务目录是否能被人直接读懂：目标、计划、事件、产物、当前状态是否一眼可见。

**我建议的实施边界**
第一轮不要碰多 agent、mailbox、rewind，也不要先重写所有工具。最合理的是先做 Phase 1 和 Phase 2，把“文件即 state”和“运行时契约”立住，这样后面的重构才不会变成散修。

这轮我没有改代码。下一步我建议先把“Phase 1 的目标 state schema”和“需要新增/拆分的模块列表”写成一版非常具体的改造清单，再进入实际重构。
