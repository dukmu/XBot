# TUI 现状全面分析与重构建议

状态:**分析阶段产出,未改动任何生产代码。**
分析对象:`XBotv2/tui/`(7821 行)、其测试(8873 行)、以及它所依赖的 C/S 协议面。
方法:通读全部 TUI 源码与协议生产端;对每条结论给出 `文件:行`;对可疑行为写脚本复现(下文标注「已复现」的均为本机实际执行输出,非推断)。

---

## 0. 结论摘要

1. **当前 TUI 不存在状态机。** 它只有一个可变字符串 `TuiState.status`,由 **19 处代码直接赋值**,外加一个带 4 条早退例外的重算函数 `_refresh_status()`。"运行中"这个事实被压缩成一个本地布尔 `turn_active`,而这个布尔**在协议里没有权威来源可以恢复**——`open_session` 快照不带 `turn_status`。用户报告的「明明在 running 却显示 Ready」不是一个偶发 UI 抖动,而是**架构必然**:任何一次快照采纳(会话切换、断线重连、游标过期重建)都会把它清零,而下一次任何事件都会把状态刷新成 `Ready`。

2. **分页与流式的交互会破坏已渲染的历史内容。** 当读者上翻到历史里(`at_tail = False`)时,`append_message()` 直接 `return`,而 `append_assistant_delta()` 仍会执行 `self._streaming_assistant_index = len(self.messages) - 1`,把流式目标指向**保留下来的最后一条旧消息**。结果:正在流入的新回复会**就地覆盖一条历史消息**。已复现。

3. **窗口外的实时内容被静默丢弃,且没有任何消费方。** `pending_newer` 只被 `+= 1`,在全部生产代码中**没有任何读取点**(只有测试断言它)。用户消息、通知、错误在 `at_tail = False` 期间全部消失,UI 不做任何提示;`record_error()` 甚至返回空字符串表示"丢弃成功"。

4. **性能设计反过来毒化了状态机。** 事件消费循环与渲染在同一个 asyncio 循环里串行执行。渲染积压(每次流式帧全量重渲染整段 Markdown,实测 8.8KB 内容 23% 主线程占用)会通过 `asyncio.Queue(maxsize=512)` 反压到 SSE 读取协程,服务端 512 帧的回放窗口随之溢出 → `session_event_cursor_expired` → `_rebuild_session_baseline()` → `restore_history()` → `reset_history()` → **`turn_active = False`** → 状态显示 `Ready`,而服务端仍在跑。这条因果链把第 1 条的缺陷从"边界情况"变成"长会话必现"。

5. **测试数量与测试价值严重脱节。** TUI 主测试三文件 196 个用例全绿(83.7s,本机实测),却能同时与上述所有 P0 缺陷共存。原因具体而可查:40 个手写 `FakeSession`;`turn_status` 在全部测试中出现 **0 次**;`delivery` 语义 **0 次**;图片/附件提交 **0 次**;45 处测试直接伪造 `app.state.turn_active` / `app.state.status` / `app._pending_messages`;假会话在 `send_message` 里**同步回显** message 事件,因此无法表达"steer 在流中途插入"这类时序。

6. **TUI 宣称的 queue 语义是虚构的。** `TerminalSession.send_message()` 从不传 `delivery`,而 `XBotClient.send_message()` 的默认值是 `"steer"`。于是 UI 提示「Turn running — type to queue a follow-up」、Queue 面板、测试名「queues and drains in FIFO order」,全部描述的是一个**客户端从未向服务端请求过的行为**。

7. **结论:应当重写 TUI 的"状态与渲染"内核,而不是继续修补。** `TextualTuiClient`/`App` 的接线、命令系统、主题、选择器可以保留;`TuiState` + `AgentTranscriptPane` + `TranscriptSurface` 这三块(合计 6346 行中的绝大部分复杂度)应当按 opencode 的「稳定 ID + 服务端权威状态 + 幂等 upsert」与 Codex 的「单一事件总线 + 双区流式 + 显式回放缓冲」重建。理由见 §6.11。

---

## 1. 代码地图与数据流

### 1.1 模块与规模

| 文件 | 行数 | 职责 |
|---|---|---|
| `tui/textual_client.py` | 2966 | `TextualTuiClient` + `XBotTextualApp`:事件循环、SSE 收集、命令、分页、交互、状态刷新 |
| `tui/textual_widgets.py` | 2281 | `BoundedText`、`TranscriptScroll`、`AgentTranscriptPane`、`TranscriptSurface`、各类 widget 工厂 |
| `tui/client.py` | 1099 | `TuiState` 与 `apply_event`、`TuiMessage/TuiTool/TuiJob/TuiNotice`、窗口淘汰 |
| `tui/terminal.py` | 458 | `TerminalSession`:`XBotClient` 之上的会话门面(游标、分页、命令) |
| `tui/textual_theme.py` | 308 | Textual CSS |
| `tui/command.py` | 252 | 命令注册表 |
| `tui/command_palette.py` / `selection.py` / `completion_popup.py` | 152/124/119 | 模态 UI |
| `tui/session_config.py` / `trace.py` / `__init__.py` | 30/30/2 | 配置、跟踪、导出 |

合计 **7821 行**。TUI 测试合计 **8873 行**:197 个测试函数定义,`--collect-only` 展开为 **200 个用例**(含参数化)。

### 1.2 数据流

```
HTTP/SSE ──▶ XBotClient.stream_events
              │  session/event_stream.py 分配单调 sequence,deque(maxlen=512)
              ▼
        TerminalSession.session_events()      tui/terminal.py:258
              │  每帧推进 self._event_cursor
              ▼
   _collect_session_events()                  tui/textual_client.py:1736
              │  独立 reader 协程 ──▶ asyncio.Queue(maxsize=512)  ← 唯一的反压点
              ▼
   _consume_stream_event(event)               tui/textual_client.py:1889
              ├─ state.apply_event(event)     tui/client.py:169   ← 语义状态 + 渲染簿记
              ├─ _handle_stream_event(event)  tui/textual_client.py:2209
              │     └─ _render_new_transcript_entries()
              │           └─ TranscriptSurface.sync()   tui/textual_widgets.py:1829
              └─ _start_interaction_response(event)     tui/textual_client.py:1958
```

**关键结构事实**:`TuiState.apply_event` 是一个 **280 行、21 个 `elif event_type` 分支**的巨型分派,它同时做两件性质完全不同的事:

- 维护**语义状态**(消息、工具、任务、通知、turn);
- 维护**渲染簿记**:`transcript: list[TuiTranscriptEntry]`(键是**十进制索引字符串**)、`evicted_messages/notices/errors/transcript/transcript_tail`、`inserted_messages/transcript`、`_streaming_assistant_index`、`_streaming_tool_ids`、`_changed_tool_ids`、`_tool_id_renames`、`_tool_transcript_keys`。

`TranscriptSurface` 无法直接知道发生了什么,只能**通过差分这些计数器反推**(`reconcile_evictions`,`tui/textual_widgets.py:1693`):插入 → 窗口下移;`evicted_transcript` → 窗口上移或判废;`evicted_transcript_tail` → 窗口缩短;`evicted_messages` → widget 缓存左移。**这就是"大量相互耦合"的具体形态**:语义状态与渲染状态之间没有边界,靠 9 个计数器 + 1 个 `revision` 做带外同步。

---

## 2. 缺陷清单(全部已复现或已定位到行)

### P0 — 正确性崩塌

#### P0-1 翻旧页时,实时流式文本会覆盖已渲染的历史消息

- **位置**:`tui/client.py:638-657`(append_message 早退)、`tui/client.py:780-795`(append_assistant_delta)
- **机制**:
  ```python
  def append_message(...):
      if not self.at_tail:
          self.pending_newer += 1
          return                      # ← 新消息根本没进 self.messages
  ```
  ```python
  def append_assistant_delta(self, content, reasoning=""):
      if self._streaming_assistant_index is None:
          self.append_message("assistant", "")          # 静默丢弃
          self._streaming_assistant_index = len(self.messages) - 1   # ← 指向旧消息!
  ```
- **实测**:
  ```
  翻页前 messages = [('user','Q0'), ('user','Q1'), ('assistant','A1(历史回复)')]
  翻页后 messages = [('user','Q0'), ('user','Q1'), ('assistant','这是新回复')]
  ```
- **影响**:用户在读历史时,一条**已经在屏幕上的历史回复被新内容就地改写**。这不是"丢数据",是**展示错误内容并持久留在窗口里**(直到整表重锚)。

#### P0-2 `turn_active` 在快照采纳后丢失 → running 却显示 `Ready`(用户报告的核心症状)

- **位置**:`tui/textual_client.py:433`(`self.state.status = "Ready"`)、`tui/client.py:751-778`(`reset_history` 清零 `turn_active`)、`tui/client.py:257-269`(所有 `_set_live_status` 都被 `if self.turn_active` 守卫)
- **触发路径(任一)**:
  1. **启动/附加到一个正在跑的会话**:`_connect()` 采用快照 → `reset_history()` → `turn_active=False`;`_connect` 随后显式写 `status = "Ready"`。此后 delta 到达时 `if self.turn_active:` 为假,状态永不更新。
  2. **`/session` 切换、`/resume`、thread 切换**:同上。
  3. **游标过期基线重建** `_rebuild_session_baseline()` → `_apply_open_session()` → `restore_history()` → `reset_history()`。重建本身只打一条 `logger.warning`。
- **实测**(场景 A2,模拟重建后的下一个事件):
  ```
  status = Ready | turn_active = False | 内容仍在流入
  ```
- **根因**:`OpenSessionResponse`(`session/protocol.py:94`)携带 `history/history_cursor/pending_inputs/pending_interactions/event_cursor/status_slots`,**独独没有 `turn_status`**。而服务端**有**这个权威字段(`session/manager.py:1364`:`turn_status="running" if active.turn_lock.locked() else "idle"`),Web 客户端也已经用它做了 5 秒看门狗(`web/src/state/useXBot.ts:888-905`)。TUI 只在**中断确认**这一条路径上问服务端(`tui/textual_client.py:872-905`,15 秒后)。

#### P0-3 窗口外的实时内容被静默丢弃,`pending_newer` 无消费方

- **位置**:`tui/client.py:608-628`(`record_error`/`record_notice`)、`:963-971`(`_ensure_tool_transcript`)
- **实测**:
  ```
  messages = ['older','old','done']
  pending_newer = 2   (没有任何生产代码读取它)
  live-1 是否存在于 state: False
  record_error 返回 key = ''   errors = []
  ```
  `grep -rn pending_newer --include=*.py .` 的结果:生产代码 4 处 `+= 1`,1 处 `= 0`;**读取只出现在测试里**。
- **影响**:`at_tail=False` 期间用户自己发出的 steer 消息、错误、通知全部从 transcript 消失,UI 无任何提示。错误尤其严重:`record_error` 返回 `""` 表示"成功丢弃",`_record_error` 只把它当作已渲染。

#### P0-4 事件消费循环可被未决交互任务阻塞

- **位置**:`tui/textual_client.py:1958-1974`
  ```python
  if self._interaction_response_task is not None:
      await asyncio.gather(self._interaction_response_task, return_exceptions=True)
  ```
  该 task 即 `_submit_live_permission/_submit_live_input`,内部 `await self._permissions_decisions.get()` / `await self._answers.get()`,**在用户作答前永不返回**。
- **影响**:同一 turn 内出现第二个 `permission_request`/`user_input_required`(并行工具调用很常见)时,整个事件消费循环停在这里:transcript 停止更新、后续 `turn_cancelled` 无法处理,直到用户回答第一个问题。用户此时看到的界面还会因为 `_choice_mode_active()` 而隐藏输入框。

### P1 — 明显错误行为

#### P1-1 `error` 事件不收尾 pending 工具

- **位置**:`tui/client.py:439-451`。`error` 分支调用 `_clear_pending_interactions()`(只处理 `permission_pending` 的工具),**没有**调用 `_finish_pending_tools()`;后者只出现在 `turn_finished`/`turn_cancelled`。
- **实测**:`tool.status = pending, finished_at = 0.0` → 工具行永远显示 pending,`elapsed()` 持续增长。

#### P1-2 子代理/后台任务在跑,状态栏却显示 `Ready`

- **位置**:`tui/textual_client.py:2133-2152`(`status_renderable` 只读 `self.state.status`),`_refresh_job_panel`(`:2154`)另行渲染 Tasks 面板。
- **实测**:`turn_finished` 后 jobs 仍 `running` → `status = Ready`。
- **影响**:正是"有子代理时却显示 ready"。状态栏与 Tasks 面板是两个互不知道的事实来源。

#### P1-3 复制反馈是死代码

`_copy_feedback`(`tui/textual_client.py:917-925`)写 `status = "Copied N chars"` 后调用 `_refresh_status()`;后者无条件重算为 `Ready`。实测输出 `copy feedback -> Ready`。

#### P1-4 `at_tail` 是单向闩锁,导致每次回到尾部都整表重建

- 写入点只有两处:`client.py:604`(`prepend_history` → `False`)、`client.py:765`(`reset_history` → `True`)。没有任何"回到尾部"的复位。
- 后果:`pane.load_newer()`(`textual_widgets.py:1214`)在窗口已经走回最末尾后仍走 `_reanchor()` → `refresh_baseline()` → 整表快照替换(丢位置、丢 activity 行、重挂全部 widget)。实测:上翻一页后 `at_tail=False` 永久保持。

#### P1-5 activity(进度)行在快照重锚后静默消失

- **位置**:`tui/textual_client.py:2673-2682` 用 `stream.mount(activity)` **绕过 `TranscriptSurface`** 直接挂进 `#transcript`;`surface.replace()`/`clear()` 走 `container.remove_children()`,把 activity 一起删掉,但 `_activity_widgets` 仍持有引用;只有 `_clear_rendered_transcript()` 才清空该 dict。
- **实测**:
  ```
  挂载 activity 后 DOM 子节点数 = 1
  surface.mounted_entry_widgets = 0      ← 簿记层完全不知道这行存在
  re-anchor 后:activity 仍在 DOM 上的 = 0
  ```
  这同时证明**widget 簿记与真实 DOM 不一致**:`mount_entries(prepend=True)` 用 `container.children[0]` 作为插入锚点(`textual_widgets.py:1927-1930`),而 `children[0]` 可能是它不认识的 activity 行。

#### P1-6 图片-only 提交在队列面板永久残留

- `submit_composer`(`textual_client.py:760-773`):`display_text = "[image] …"` 存入 `_pending_messages`;`_pop_pending_message(content)` 按**内容字符串相等**匹配,而服务端回显的 `content` 是空串(文本为空)。
- 结果:该 pending 项永不消失;transcript 里出现一条空内容的用户消息。
- 现有 197 个测试中**没有任何一个覆盖附件/图片提交**(`grep pending_images tests/` 无命中)。

#### P1-7 queue 语义是虚构的

- `TerminalSession.send_message()`(`tui/terminal.py:210-230`)不传 `delivery`;`XBotClient.send_message()`(`client.py:520`)默认 `delivery="steer"`。
- 但 `_refresh_input_mode`(`textual_client.py:2490-2502`)提示 "Turn running — type to queue a follow-up"/"(queue)",Queue 面板、以及 `tests/integration/test_tui_interaction.py` 头部注释 "typing during a running turn queues and drains in FIFO order" 都按 queue 描述。
- 服务端两种语义差别很大(`session/runtime.py:318-350`):`steer` **立刻** publish `message` 事件并作为 `NEXT_STEP` 在下一个 step 拼接;`queue` 走 `NEXT_TURN` 且 `defer_message_event`,直到新 turn claim 才 publish。
- 后果:UI 会先显示"已排队",随即被 steer 的 message 事件弹掉,队列面板闪烁;用户以为输入会在本轮结束后生效,实际是在下一个 step 就被插入。

#### P1-8 `_view_cursor` 不按 thread 复位

- `tui/terminal.py:65` 初始化,`:292` 只做 `max()` 递增,**从不复位**。查看线程 A 后查看线程 B,会用 A 的游标去订阅 B 的流;若 A 游标大于 B 的 sequence,服务端 `subscribe()` 抛 `ValueError`(`session/event_stream.py:111`),`_pump_thread_view` 的 `except Exception` 静默降级到轮询。

#### P1-9 状态写入点分散(19 处)

`client.py:209,450,457,834,837,839,841,843,845` 与 `textual_client.py:420,433,826,848,868,921,924,1423,2086,2279`。其中至少 3 处(`_copy_feedback`、`_settle_interrupted`、`_record_error`)与 `_refresh_status()` 的优先级规则相互打架。`_refresh_status` 本身还有一条容易误用的规则:状态为 `Error/Interrupted/Permission denied` 时**早退**,于是 `Error` 会一路粘到下一次 `turn_started`。

### P2 — 性能、内存、可维护性

#### P2-1 流式 Markdown 每帧全量重渲染(O(n²))

- `_stream_tick`(`textual_client.py:2295`)每 50ms 调 `refresh_streaming_assistant_widget` → `apply_streaming_message_widget` → `render_message(content)`;缓存键是 `(role, content)`(`textual_widgets.py:47`),**内容每次 delta 都变,缓存必然 miss**,注释里"memoizing the parsed render avoids re-tokenizing the whole message on every 50ms tick"这一前提不成立。
- **实测**(本机):

  | 内容长度 | 单次全量渲染 | 20 帧/秒的主线程占用 |
  |---|---|---|
  | 1.0 KB | 1.9 ms | 4% |
  | 2.1 KB | 3.1 ms | 6% |
  | 4.2 KB | 6.1 ms | 12% |
  | 8.8 KB | 11.4 ms | 23% |

  单次耗时随长度线性增长、帧率固定,总代价是 O(n²)。17.6KB 单帧 26ms,已经超过 50ms 预算的一半。

#### P2-2 渲染积压 → SSE 反压 → 游标过期(性能缺陷如何变成 P0-2)

`_collect_session_events` 用一个 `maxsize=512` 的队列把网络读取与渲染解耦;队列满时 `await pending.put()` 阻塞 reader 协程,HTTP 响应不再被读取,而服务端 `deque(maxlen=512)`(`session/event_stream.py:69`)继续淘汰旧帧。客户端于是拿到 `session_event_cursor_expired`,走 `_recover_expired_cursor`(3 次)或 `_rebuild_session_baseline`(2 次),后者正是 P0-2 的触发路径之一。**代码里的注释已经承认了这个风险**(`textual_client.py:91-95`),但没有把它与"状态丢失"联系起来。

#### P2-3 客户端侧无限增长

| 集合 | 位置 | 增长源 | 清理 |
|---|---|---|---|
| `_turn_started_at: dict[int, float]` | `textual_client.py:287` | 每个 turn 一条 | **无** |
| `_activity_widgets: dict[int, Static]` | `:261` | 每个 turn 一条 | 仅 `/clear`、`history_updated`、会话切换 |
| `_view_items: list` / `ThreadView._items` | `:306`,`widgets:1302` | 每次轮询 append | 退出视图时 |
| `_pending_messages` | `:254` | 图片-only 提交 | 匹配失败则残留 |
| `_input_history: list[str]` | `:288` | 每次提交 | 无上限 |

这与 `docs/project/tui-web-performance.md` 声称的"内存不随会话长度增长"相矛盾——该文档只覆盖了 `TuiState` 内部的窗口,没覆盖 App 侧。

#### P2-4 二次复杂度的小热点

`_message_index()`(`client.py:630`)对每条消息做线性扫描,被 `append_message` 每条消息调用一次 → 窗口内 O(n²);`_rename_tool`、`prepend_history` 的键重编号也是 O(transcript)。在 600 上限内可接受,但属于"用索引当主键"的必然代价。

#### P2-5 每 0.5 秒的固定开销

`_tick_activity`(`textual_client.py:2684-2696`)每 tick 做:`_update_pending_tool_elapsed()`(对每个 tool widget 做一次 DOM `query_one(".meta")`,上限 100 个)、`prune_finished_tasks()`、`_refresh_job_panel()`(可能重建任务行)、`_refresh_status()`。即 200 次 DOM 查询/秒 + 状态重渲染,与是否有事件无关。

#### P2-6 静默失败文化(量化)

- `tui/` 共 **66 个 `except` 子句**(其中 41 个是 `except Exception`),**25 个** 直接 `pass`/`return`/`return None`/`continue`;另有 **23 处**标注 `# noqa: BLE001` 的宽泛捕获。
- `logger.debug` 6 次、`logger.warning` 7 次、`logger.exception` 5 次——即大部分失败只留一行日志,UI 无任何反馈。
- 典型:`_fetch_older_page` 吞掉分页失败返回 `None`(`:2385`)、`_poll_thread_view` 吞掉轮询失败 `continue`(`:1145`)、`_read_view_trajectory` 的 thread 流失败静默轮询(`:1130`)、`_handle_stream_failure` 用 `_stream_failure_reported` 把同一 turn 内的第二次失败**完全吞掉**(`:1875`)。
- 与项目规范直接冲突:AGENTS.md「Do not silently recover from malformed internal state or unsupported behavior.」

---

## 3. 架构根因

### 3.1 缺少状态机,只有可变字符串

"运行中"应当是**从若干事实推导出的一个函数**,事实包括:服务端 thread 的 `turn_status`、本地是否有未闭合的 turn、是否存在未决交互、是否有后台任务。现在它被拆成:

- `state.status`(字符串,19 个写入点)
- `state.turn_active`(本地布尔,快照不可恢复)
- `state.compaction_active`
- `state.interrupt_requested`
- `state.pending_permission_payload` / `pending_user_input_payload`
- `state.tasks`(任务状态,完全不参与状态栏)

`_refresh_status()` 是唯一的推导点,但它有 4 条早退规则、被 19 个写入点绕过、还依赖调用者选对 `reset_terminal` 参数。

### 3.2 服务端权威状态未被使用

协议已经提供且 Web 已在用的事实:

- `GET /sessions/{sid}/threads` → `threads[].turn_status ∈ {idle, running}`(`session/contracts.py:342`)
- `GET .../queue` → 未决输入快照(`XBotClient.list_pending_inputs`,Web 用它做 `pending_inputs`)
- `open_session` → `pending_inputs`、`pending_interactions`、`history`、`event_cursor`

TUI 只用了 `history/pending_interactions/status_slots`,把 `turn_status` 排除在快照之外,也没有看门狗。**这是"没有依据现有 c/s api 合理设计统一 tui 状态机"的字面含义。**

### 3.3 语义状态与渲染簿记同层(In-band vs out-of-band 双通道)

同一个 `apply_event` 既变更语义、又变更窗口位置、又变更淘汰计数。渲染层要理解"刚才发生了什么",只能差分 9 个计数器。任何一条新的变更路径若忘记更新某个计数器,就产生静默漂移;P0-1 与 P1-5 都是这个结构的直接产物。

### 3.4 用列表索引当主键

`TuiTranscriptEntry.key = str(index)`(`client.py:615`、`:656`、`:970`),于是:

- 前置插入必须重编号**全部**同类型键(`client.py:591-599`);
- 前端淘汰必须重编号 + 丢弃越界项(`_trim_payloads`,`:501-525`);
- 流式目标 `_streaming_assistant_index` 必须随淘汰平移(`:522-524`);
- widget 缓存必须随淘汰左移(`reconcile_evictions`,`widgets:1709-1738`)。

opencode 的做法是用**稳定 ID**(`messageID` / `part.id`)做 upsert,以上四项全部消失。

### 3.5 三套"是否跟随尾部"的标志

`self._transcript_follow`(App)、`self.container.is_vertical_scroll_end`(widget 实时值)、`state.at_tail`(状态机闩锁)。三者语义不同、更新点分散(`textual_client.py:298,369,2361,2414,2665`;`widgets.py:1226`),且 `state.at_tail` 永不复位(P1-4)。

### 3.6 失败即降级、降级即沉默

见 P2-6。更根本的是:**代码里没有"失败必须可见"的通道**。`record_error` 在 `at_tail=False` 时返回 `""`,而调用方无从区分"已记录"与"被丢弃"。

---

## 4. 现有测试为什么几乎没有生产价值

### 4.1 事实

| 指标 | 数值 |
|---|---|
| TUI 测试行数 / 生产行数 | 8873 / 7821 |
| 测试函数 / 展开用例 | 197 / 200 |
| 全绿耗时 | 83.7 s(其中 3 个文件 196 passed) |
| `class FakeSession` 手写桩 | **40 个**(`tests/core/test_tui_client.py`) |
| `turn_status` 在全部测试中出现 | **0 次** |
| `delivery` 在交互/中断测试中出现 | **0 次** |
| 图片/附件提交测试 | **0 个** |
| 直接伪造 `turn_active`/`status`/`_pending_messages` | **45 处** |
| 私有状态/属性访问与赋值(`test_tui_interaction.py`) | **120 处** |
| 驱动真实 Textual app(`run_test`) | 109 次(有价值的骨架,但输入是假的) |
| 使用真实 HTTP 服务端的 TUI 测试 | 0 |

### 4.2 假会话为什么测不出问题(逐条对应 P0)

`tests/integration/test_tui_interaction.py:43` 的 `_ScriptedSession`:

```python
async def send_message(self, text, *, images=None):
    del images                                   # ← 附件被直接丢弃
    self.sent.append(text)
    self._message_events.put_nowait({... "message" ...})   # ← 同步立即回显
    events = self._scripts.pop(0) if self._scripts else [turn_started, assistant_message, turn_finished]
    for event in events: self._message_events.put_nowait(event)
```

- **不接受 `delivery`** → 无法区分 steer/queue → P1-7 测不出。
- **不接受 `images`** → P1-6 测不出。
- **没有 `turn_status`** → P0-2 的服务端校准路径测不出。
- **没有游标/断线/重放** → `_rebuild_session_baseline` 路径测不出(P0-2 的主要触发路径)。
- **没有 `refresh_baseline` / `switch` 返回真实快照** → P1-4、P1-5 测不出。
- `connect()` 返回 `None` → `_apply_open_session` 整段历史恢复路径不被执行。

`tests/core/test_tui_client.py:1306` `test_message_event_pops_queue_before_turn_end` 是最典型的例子:它**手工设置** `app._pending_messages = {2: "queued input"}` 与 `app.state.turn_active = True`,再手工塞一个 `message` 事件,然后断言队列被清空。它验证的正是 §P1-6 里那个按内容匹配的脆弱实现,却完全没有验证客户端是否真的以 queue 语义提交过。

### 4.3 反证:绿灯与 P0 并存

本机执行:

```
$ PYTHONPATH=XBotv2 .venv/bin/python -m pytest \
    XBotv2/tests/core/test_tui_client.py \
    XBotv2/tests/integration/test_tui_interaction.py \
    XBotv2/tests/bench/test_tui_event_throughput.py -q
196 passed in 83.69s (0:01:23)
```

而 P0-1、P0-2、P0-3、P0-4 在同一份代码上可稳定复现。这直接印证 AGENTS.md:「A green suite does not prove provider interoperability, interactive behavior, recovery, or test quality.」

### 4.4 项目自己的文档已经承认了缺口

`docs/project/tui-web-performance.md:205`:

> **open** the same cycle against a *live* event stream (the test drives state events directly rather than through the transport).

以及 `:349-353`:

> **Spinner that never stopped.** … 客户端现在每 5 s 问服务端线程实际在做什么并采纳(`thread_synced`)——**这句话只对 Web 成立,TUI 从未实现**。

也就是说:Web 侧已经修复的同一类缺陷,TUI 侧连"已记录的问题"都没有。

---

## 5. 开源实现的可借鉴结论

> 以下均来自源码阅读,链接为对应文件/PR。只列与本案缺陷直接对应的机制。

### 5.1 Codex CLI(Rust + ratatui)

**a) 单一类型化事件总线。** `codex-rs/tui/src/app_event.rs` 定义 `enum AppEvent`,widget 只发事件、不直接访问 `App` 内部;跨线程用一条 mpsc。与本项目"19 处直接写 `state.status`"形成对照。
关键变体:
- `InsertHistoryCell(Box<dyn HistoryCell>)` —— **进入 scrollback 的唯一入口**,单一漏斗。
- `SubmitThreadOp { thread_id, op }` —— 操作**显式携带 thread_id**,UI 不假设"当前线程"。
- `BeginInitialHistoryReplayBuffer` / `EndInitialHistoryReplayBuffer` —— 回放缓冲是一段**显式协议**,而非靠隐式状态推断。
- `CommitTick` / `StartCommitAnimation` / `StopCommitAnimation` —— 帧提交显式调度。

**b) 双区流式模型。** `codex-rs/tui/src/streaming/controller.rs`:
> Each stream partitions rendered markdown into a *stable region* (committed to scrollback) and a *tail region* (mutable, displayed in the active-cell slot).

不变式(该文件头注释原文):
- `emitted_stable_len <= enqueued_stable_len <= render.lines.len()`
- **committed source is append-only until `reset()`; never modified mid-stream**
- Tail starts exactly at `enqueued_stable_len`

这正是 P0-1 的反面:XBot 的 TUI 允许"已提交"的消息被后续 delta 就地改写,且改写目标可能指向历史任意位置。Codex 通过"已提交区只增不改 + 可变尾区独立"从结构上排除了这种可能。

**c) 流式内容恒为后缀。** `AppEvent::ConsolidateAgentMessage` 的语义是「替换 transcript **末尾**连续的一段 `AgentMessageCell`」。因为尾区是后缀,合并只需从尾部回退查找。XBot 的 `_streaming_assistant_index` 可以指向中间(steer 消息插入后)甚至指向历史(翻页后),没有任何前缀/后缀不变式保护它。

**d) 专门的 busy 组件。** `codex-rs/tui/src/status_indicator_widget.rs`:一个只负责"agent busy 时显示在 composer 上方的状态行"的 widget,自己拥有 spinner 计时、可选的中断提示与 inline context,并通过 `FrameRequester::schedule_frame_in(32ms)` 主动请求下一帧(而不是固定 20Hz 空转)。中断是 `app_event_tx.interrupt()`,即**通过同一条事件总线**。

**e) 测试方式。** 同一文件底部:用 `TestBackend` 渲染到内存 buffer,再用 `insta::assert_snapshot!` 对**终端缓冲区**做快照断言(含宽度截断、换行、无动画等分支)。这是"可证伪的渲染契约",与本项目"断言私有字段"形成对照。

### 5.2 opencode(Go + bubbletea,客户端/服务端分离)

**a) 服务端权威的 busy 状态是一个事件。** SDK 类型定义中:

```ts
export type SessionStatus =
  | { type: "idle" }
  | { type: "retry"; attempt: number; message: string; next: number }
  | { type: "busy" }

export type EventSessionStatus = { type: "session.status"; properties: { sessionID: string; status: SessionStatus } }
export type EventSessionIdle   = { type: "session.idle";   properties: { sessionID: string } }
```

客户端**不维护**"我在跑"这个信念,它只接收 `session.status`。注意 `retry` 变体:服务端把"正在重试第 N 次,下次在 T 时刻"也作为一等状态推给 UI——XBot 的 TUI 在这种情况下会写死 `status = "Error"` 并一直粘住(§P1-9)。

**b) 稳定 ID + 幂等 upsert,而非索引 + 淘汰计数。**

```ts
export type EventMessageUpdated     = { type: "message.updated";      properties: { info: Message } }
export type EventMessagePartUpdated = { type: "message.part.updated"; properties: { part: Part; delta?: string } }
export type EventMessageRemoved     = { type: "message.removed";      properties: { sessionID; messageID } }
export type EventMessagePartRemoved = { type: "message.part.removed"; properties: { sessionID; messageID; partID } }
```

`Part` 有稳定 `id`/`messageID`/`type`(`text`/`reasoning`/`tool`/`step-start`/`step-finish`/…)。客户端把事件当作对 `Map<id, Part>` 的 upsert/remove。**删除是显式事件**,客户端不需要通过"窗口位置 + 淘汰计数"推断删了什么——这正是 P0-3 里"内容凭空消失"的结构性免疫。

**c) 客户端自有 ID 贯穿往返。** `SessionPrompt` 请求体里就有 `messageID?: string`;`EventMessageUpdated`/`EventCommandExecuted` 都带回 `messageID`。因此"我刚提交了什么"有确定答案。XBot 的 `TerminalSession.send_message` 内部生成 `tui-…` request_id 并且**不返回给调用方**(`tui/terminal.py:218`),导致 TUI 只能用"内容字符串相等"来认领自己提交的消息(P1-6)。

**d) 权限是对象。** `Permission` 有 `id`/`callID`;`permission.updated` / `permission.replied` 成对。XBot 的 TUI 用 `_active_choice_key: str | None`(单槽)管理交互,多个并行请求只能排队等待(P0-4)。

**e) 服务端→TUI 的显式命令通道。** `tui.command.execute`、`tui.toast.show`、`tui.prompt.append`。UI 状态变更由服务端命令化,而不是靠 UI 从数据流里猜。

**f) 已知代价。** opencode 也存在 "stuck in busy forever after toolcall" 一类 issue,说明"服务端权威 + 客户端不猜"并不自动解决一切;但它把失败模式收敛为"服务端没发 status",而不是"客户端本地标志与真实状态分叉"。

### 5.3 对照表

| 关注点 | XBotv2 TUI 现状 | Codex | opencode | 建议采纳 |
|---|---|---|---|---|
| 事件入口 | 两条路径(`state.apply_event` + `_handle_stream_event`),另加直接 DOM 写入 | 单一 `AppEvent` 枚举 | 单一 `/event` SSE + 类型化 `Event` 联合 | opencode + Codex(类型化单漏斗) |
| busy 判定 | 本地 `turn_active`,快照不可恢复 | 由 turn 生命周期驱动专用组件 | 服务端 `session.status` | opencode(服务端权威)+ 看门狗 |
| transcript 主键 | 十进制索引字符串 | `HistoryCell`(尾部有序) | `messageID` / `part.id` | opencode(稳定 ID) |
| 删除表达 | 靠 `evicted_*` 计数器反推 | 不适用(scrollback) | 显式 `*.removed` 事件 | opencode |
| 流式写入 | 就地改写任意 index,无后缀不变式 | 已提交区 append-only + 可变尾区 | `message.part.updated` upsert(带 delta) | Codex(不变式)+ opencode(upsert) |
| 回放/重连 | 隐式,靠 revision + 计数器 | `Begin/EndInitialHistoryReplayBuffer` 显式 | 重连后按 ID upsert,天然幂等 | Codex(显式阶段)+ opencode(幂等) |
| 输入关联 | 服务端生成 request_id,不回传 | — | 客户端提供 `messageID` | opencode |
| 动画/帧调度 | 固定 20Hz + 0.5s 定时器 | `FrameRequester::schedule_frame_in` | bubbletea 渲染循环 | Codex(按需请求帧) |
| TUI 测试 | 40 个手写假会话 + 私有字段断言 | `TestBackend` + `insta` 缓冲快照 | — | Codex |

---

## 6. 重构建议

### 6.1 六条设计原则

1. **服务端说什么就是什么。** 客户端不维护"我认为在跑"这种可被本地事件推翻的信念;任何本地派生量都必须有服务端权威来源可以校准,且校准必须自动、周期性、有超时。
2. **单一事件漏斗 + 单一状态推导函数。** 所有输入(SSE 帧、用户提交结果、中断结果、看门狗结果、定时器)都变成同一种内部事件,交给一个纯函数 reducer。任何人不得直接写状态字段。
3. **稳定 ID,不做索引算术。** transcript 条目以服务端 id 为主键;插入/删除/更新都是按 id 的 upsert/remove。任何 `evicted_*` / `inserted_*` 计数器都是设计味道。
4. **流式内容是后缀,已提交内容只增不改。** 用不变式替代防御性判断。
5. **失败必须可见。** 禁止把错误降级成 `logger.debug` 后继续;要么呈现给用户,要么作为显式状态(如 `degraded`)暴露。
6. **渲染不得反压传输。** 消费与渲染之间必须是"可丢弃/可合并"的模型(见 6.7),而不是有界阻塞队列。

### 6.2 目标分层

```
┌───────────────────────────────────────────────────────────┐
│ 传输层  TransportSession                                  │
│  · SSE 读取(永不阻塞在渲染上)                          │
│  · 事件信封解码(sequence / session / thread / type)     │
│  · 重连 + 游标恢复;缺口检测                              │
│  · 看门狗:GET /threads → turn_status / pending_inputs     │
│  产出:TransportEvent | TransportGap | TransportDown       │
└───────────────────────────┬───────────────────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────┐
│ 归约层  reduce(state, event) -> (state, [Effect])          │
│  · ConversationState:entries: OrderedMap[id, Entry]       │
│  · ThreadState:turn_status(权威) + local_turn_open        │
│  · UiState:interactions / queue / jobs / notices          │
│  · 纯函数;无 DOM、无 IO、无 Textual                       │
└───────────────────────────┬───────────────────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────┐
│ 视图层  TranscriptView                                    │
│  · 输入:只读的 ConversationState 快照 + window 范围       │
│  · 输出:按 id diff 挂载/卸载 widget                       │
│  · 流式尾区:独立可变 widget,不参与 diff                  │
│  · 帧调度:脏标记 + 合并,由 app 请求帧                    │
└───────────────────────────────────────────────────────────┘
```

**归约层必须可以在没有 Textual 的情况下单测**,这是让测试重新变得有价值的先决条件。

### 6.3 状态机

用**一个显式的、可枚举的状态**,由若干事实推导,并且**每个事实都有权威来源**:

```python
@dataclass(frozen=True)
class StatusFacts:
    connection: Literal["connecting", "connected", "disconnected"]
    server_turn: Literal["idle", "running", "unknown"]   # 权威:threads[].turn_status
    server_turn_at: float                                # 最近一次权威读数时间
    local_turn_open: bool                                # turn_started..turn_finished
    local_turn_at: float
    interaction_pending: bool
    compaction: bool
    interrupt_inflight: bool
    jobs_running: int
    last_error: str | None

def derive(f: StatusFacts) -> Status: ...
```

规则要点:

- `running` 的判据是 `server_turn == "running" or local_turn_open`,**不是** `local_turn_open`。
- `server_turn` 由三个来源更新:(a) 快照——**需要协议补 `turn_status`**;(b) 看门狗(见下);(c) `turn_started`/`turn_finished`/`turn_cancelled` 事件(它们同时也是权威事实,可把 `server_turn` 一起置位)。
- `local_turn_open` **在快照采纳时不得被清零**,除非快照带回的权威 `turn_status == "idle"`。
- `error` 事件**不再**清 `turn_active`;它只设置 `last_error`,终态由 `turn_finished`/`turn_cancelled` 决定(与服务端 `agentloop/engine.py:579-583` 的行为一致:有 `turn_started` 就必有终态帧)。
- 子代理/后台任务:`jobs_running > 0` 出一个独立的修饰位,状态栏显示 `Running (2 tasks)` 之类,而不是让状态栏与 Tasks 面板各说各话。

**看门狗(必须实现,Web 已有先例)**:`server_turn == "running" or local_turn_open` 期间,每 5 秒调 `list_threads()`;若 `turn_status == "idle"` 而本地认为在跑 → 采纳服务端,结束本地 turn 并给出可见提示(例如 notice「服务端报告该 turn 已结束(终态帧可能丢失),已按空闲处理」)。反向也成立:本地 `idle` 而服务端 `running` → 置为 running(解决附加到运行中会话的场景)。

**协议侧最小改动(建议)**:
- `OpenSessionResponse` 增加 `turn_status` 与 `turn`(与 `threads[].turn_status` 同源)。注意 `SessionDescriptor`(`session/contracts.py:295`)带 `extra="forbid"`,因此这是一个必须显式修改模型的字段变更。
- 可选:`GET /threads/{t}` 单线程端点,免得为了一个字段拉整个列表。

### 6.4 时序与顺序

**已确认的服务端保证**(可以直接依赖):

- `SessionEventStream` 是严格 FIFO,`sequence` 单调递增(`session/event_stream.py:88-134`)。
- 事件按产生顺序 publish,`turn_started` 先于本轮所有 delta,`turn_finished`/`turn_cancelled` 在后(`agentloop/engine.py:1038`、`:1328`、`:538`)。
- `message` 事件(用户输入)对 steer 是**提交时立刻** publish(`session/runtime.py:341-345`),对 queue 是**claim 时** publish(`:620-636`)。

**客户端必须做的四件事:**

1. **信封完整校验。** 当前 `_consume_stream_event` 只看 `type` 和 `data`,完全忽略 `session_id`/`thread_id`/`sequence`/`request_id`。必须校验:(a) 事件属于当前 session/thread;(b) `sequence` 是 `last_seen + 1`;(c) 否则进入"缺口"处理(重新订阅或重建基线,**并告知用户**)。

2. **客户端自有提交 ID。** 在 UI 层生成 `client_input_id`(uuid),通过 `request_id` 传给 `TerminalSession.send_message`,并且:
   - 提交时**立刻**在 transcript 尾部插入一条 `pending` 状态的本条输入(乐观),而不是等服务器回显;
   - 服务端 `message` 事件的 `id` 与之相等时,把该条从 `pending` 升级为 `accepted`(原地替换内容,不改位置);
   - `send_message` 失败 → 该条标记 `failed` 并给出**可见**错误,而不是留在队列里假装排队。

   这一步同时修掉 P1-6(不再按内容匹配)与 P1-7 的一半矛盾。

3. **明确 delivery。** UI 必须知道自己在做 steer 还是 queue(由 `server_turn == "running"` 决定),并把该值传给服务端;Queue 面板只显示**真正** `delivery="queue"` 的条目,steer 直接进 transcript。文案同步修正。

4. **steer 的位置语义写进不变式。** steer 消息到达时,当前流式尾区必须**先提交**(finalize),再插入用户消息,新的流式尾区从其后开始。这样 transcript 永远是: `… A(已提交) / steer / B(尾区)`。当前实现让 delta 继续追加到 A 上,视觉顺序与时间顺序相反(§实测 F)。

**当前实测反例(供回归测试直接使用):**
```
messages = [('assistant', '我先看一下代码……继续输出A的剩余部分'), ('user', '别改 A,改成 B')]
```
——assistant 的续写落在 user 消息**之前**。

### 6.5 时间线条目模型(替换索引键 + 淘汰计数)

```python
@dataclass(frozen=True)
class Entry:
    id: str                      # 稳定:message_id / tool_call_id / notice_id
    kind: Literal["user", "assistant", "reasoning", "tool", "notice", "error", "activity", "compaction"]
    parent_id: str | None        # 同一 assistant turn 下的 part 归属
    seq: int                     # 本地单调,仅用于同 id 排序兜底
```

- 容器:`OrderedDict[str, Entry]`。窗口只决定**渲染哪些 id**,不决定**删哪些条目**。
- 状态层不再删条目;容量控制放在视图层("最多渲染最近 N 条")与持久层(翻页取更旧)。
- 翻页变成:窗口 `[start_id, end_id]` + `older_cursor`;`at_tail` 由**窗口末端是否等于最新 id** 推导,而不是一个闩锁。
- 视图层 diff:上一帧渲染的 id 列表 vs 这一帧的 id 列表,只增删差异 widget。**淘汰不再需要任何计数器**。

这条改动的收益面最大:它一次性消除 `TuiTranscriptEntry`、`evicted_*`(5 个)、`inserted_*`(2 个)、`_tool_transcript_keys`、`_mounted_entry_indices`、`reconcile_evictions()`、`_trim_payloads()` 的重编号逻辑、以及 P0-1/P0-3/P1-4/P1-5。

### 6.6 流式渲染(借 Codex 双区模型)

- **已提交区**:进入 transcript 的 assistant 内容渲染一次,之后**只读**。键盘滚动、翻页、重连后重建都基于它。
- **尾区**:当前正在流式的那一条,单独一个可变 widget,挂在 transcript 末尾之外(或末尾之内但由视图层标记为 volatile)。
- **提交时机**:`assistant_message` 事件到达、或 steer 消息到达、或尾区超过阈值(如 200 行)时,把尾区内容固化为已提交条目并开启新尾区。
- **渲染节流**:尾区按 30–60ms 合并更新,且**只在内容变化时**重渲染;Markdown 解析按"已提交前缀 + 未提交后缀"分段缓存,避免 O(n²)(见 §P2-1)。
- **帧调度**:引入 `request_frame()`(脏标记 → 下一帧统一渲染),取消 20Hz 无条件 tick 与 0.5s 无条件全套刷新。

### 6.7 GC 与内存

| 对象 | 现状 | 目标 |
|---|---|---|
| transcript 条目 | 状态层 600 + 视图层 100 双份上限,靠计数器同步 | 视图层单一份窗口(默认 200 条 widget);状态层按"最近 N 条 + 已加载的旧页"持有,`OrderedDict` FIFO |
| widget 缓存 | `message_widgets` / `tool_widgets` 两个 dict + 手动 trim | 由 §6.5 的 id diff 派生,淘汰即卸载,无需缓存 dict |
| `_turn_started_at` | 无界 | 只保留当前 turn(或 `deque(maxlen=8)`) |
| `_activity_widgets` | 按 turn 累积,绕过 surface | 由视图层统一管理,随尾区一起提交/卸载 |
| `_view_items` / `ThreadView._items` | 无界 append | 只保留窗口 + 游标,旧页按需取 |
| `_pending_messages` | 内容匹配,可残留 | 以 `client_input_id` 为键,服务端 ack / 失败 / 超时三态终结 |
| `_input_history` | 无界 | `deque(maxlen=500)` |
| Markdown 缓存 | 64 条全量文本,流式必然 miss | 已提交前缀按内容寻址 + 尾区分段缓存 |

**验证方式**:保留现有 `tracemalloc` 风格的测试,但断言对象从"`state.messages` 长度"改为"**驻留 widget 数 + 客户端持有对象数**",并用一个跑 500 轮的 headless 会话做长稳断言。

### 6.8 失败处理

用一条硬规则替换当前文化:**每个 `except` 必须落入以下三类之一,并在代码里显式可见**:

1. **可重试** → 有退避、有次数上限、超限后转 2 或 3;
2. **可降级** → 必须产生一个**用户可见**的状态或 notice(禁止只写 `logger.debug`);
3. **不可恢复** → 转为终态错误并终止相关流程,给出明确文案。

配套:
- 删除 `_stream_failure_reported` 这种"同一 turn 内第二次失败全丢"的门闩。
- `record_error` 不再返回 `""` 表示丢弃;改为返回一个结果对象,调用方必须处理"未渲染"。
- 为 §P2-6 的 24 个静默分支建立清单(可在 CI 里用 lint 规则冻结数量,只允许减少)。

### 6.9 测试策略(什么才算有生产价值)

**必测矩阵(每一条都要能证伪):**

A. **归约层纯函数测试(不需要 Textual)**
- steer 在流中途插入:断言最终条目顺序为 `A(已提交) → steer → B`,且 A 的内容不再变化。
- 窗口外收到 live 事件:断言条目**进入状态**、窗口标记为"落后 N 条"、回到尾部后不丢内容、且**没有任何历史条目内容被改写**。
- `error` 后 `turn_finished`:工具全部收尾。
- 快照采纳:权威 `turn_status="running"` 时 `turn_status` 保留;`idle` 时结束。

B. **传输层测试(真实 SSE + 真实 HTTP,`ASGITransport` 或 uvicorn)**
- 直接在 `tests/integration/test_http_transport.py` 的既有 HTTP 夹具上驱动 TUI 的 `TransportSession`,不使用任何 `FakeSession`。
- 场景:提交→steer 插队→turn 结束;断流后重连;游标过期后重建;请求失败。
- **必须包含一个"渲染故意变慢"的注入**(如 monkeypatch 渲染函数 sleep 200ms),断言:传输不丢帧、不发生游标过期、或过期后状态与用户可见一致。这是验证 §P2-2 因果链的唯一方式。

C. **视图层快照测试(借 Codex 做法)**
- 用 Textual 的 headless 会话导出文本/截图,对**渲染结果**做快照:长消息换行、窄终端降级、运行中状态栏、子代理运行中状态栏、翻页位置保持。
- 不要再用 `app.state.turn_active = True` 这类伪造;一律走 reducer 输入。

D. **契约测试**
- `turn_status` 出现在协议类型测试里;
- `delivery` 语义端到端测试:steer 与 queue 各一条,断言客户端请求的 `delivery` 与服务端行为一致(P1-7 回归);
- 图片-only 提交端到端(P1-6 回归)。

**硬性要求:**
- 删除或重写全部 40 个 `FakeSession`。允许一个**共享的** `ScriptedTransport`,它模拟 sequence、游标、delivery、turn_status、断线;**不允许**各测试自带手写桩。
- 禁止测试直接写 `app.state.*` / `app._x = ...`(可加一条 lint/测试守卫,项目已有 `test_tui_modules_do_not_import_core` 这类结构性测试的先例)。
- 任何新增渲染簿记计数器,必须同时给出"它为什么不能用稳定 ID 表达"的理由。

### 6.10 分阶段实施计划

> 每阶段独立可验收;每阶段结束前不得进入下一阶段。

**阶段 0 — 止血(不改架构,1 个分支,可当天完成)**
1. `append_assistant_delta` 在 `not at_tail` 时**不得**设置 `_streaming_assistant_index = len(messages)-1`(P0-1)。
2. `error` 事件调用 `_finish_pending_tools("error")`(P1-1)。
3. `_copy_feedback` 改为通过独立 notice 呈现,不经过 `status`(P1-3)。
4. `_start_interaction_response` 的 `await gather` 改为不阻塞消费循环(P0-4)。
5. 状态栏把 `jobs_running > 0` 纳入显示(P1-2)。
6. 为 1–5 各写一个**能证伪**的测试(用 §6.9 A 类纯函数测试即可)。

**阶段 1 — 状态机(核心)**
1. 引入 `StatusFacts` + `derive()`;删除 `state.status` 的全部外部写入点(改为派发内部事件)。
2. 协议补 `turn_status`(server: `OpenSessionResponse`;client: 类型);`refresh_baseline`/`open_session` 采纳。
3. 实现 5 秒看门狗;实现"本地在跑/服务端空闲"与反向两种校准,并产生可见通知。
4. `interrupt` 路径改为"看门狗优先",把 15 秒特例删除。
5. 验收:附加到运行中会话 → 状态正确;重建基线 → 状态保持;服务端提前结束 → 5 秒内自行纠正。

**阶段 2 — 传输层与顺序**
1. 抽出 `TransportSession`(从 `TerminalSession` + `_collect_session_events` 提取):负责连接、sequence 校验、缺口、重连、看门狗;产出类型化事件。
2. **渲染与传输彻底解耦**:消费端不阻塞读取端(见 6.7 与 §P2-2 的修法:读取协程只做解码入队,队列满时**丢弃可合并事件并记录 `gap`**,而不是阻塞;或改为无界 + 背压到渲染合并)。
3. 客户端自有 `client_input_id` 贯穿提交/回执(P0-3、P1-6、P1-7)。
4. 明确 delivery;修正文案与 Queue 面板语义。
5. 验收:B 类传输测试全绿,含慢渲染注入。

**阶段 3 — 时间线模型与视图**
1. `ConversationState` 换成 `OrderedDict[id, Entry]`;删除索引键与全部 `evicted_*`/`inserted_*` 计数器。
2. `TranscriptView` 改为 id diff;窗口 = id 区间;`at_tail` 由区间末端推导。
3. 流式改为双区(尾区独立)。
4. 统一"跟随尾部"为单一事实来源。
5. activity 行纳入视图层管理。
6. 验收:A 类窗口测试 + C 类快照测试全绿;P0-1/P0-3/P1-4/P1-5 的回归测试从"能过"变为"结构上不可能失败"。

**阶段 4 — 性能与 GC**
1. Markdown 分段缓存 + 尾区合并渲染。
2. 帧调度(`request_frame()`),取消 20Hz/0.5s 无条件刷新。
3. 逐项修 §P2-3 的泄漏。
4. 长稳基准:500 轮 headless 会话,断言驻留 widget 数、`tracemalloc` 峰值、每轮耗时上界。

**阶段 5 — 测试体系替换**
1. 删除 40 个 `FakeSession`,建立共享 `ScriptedTransport`。
2. 加"禁止测试写私有状态"的结构化守卫。
3. 补齐 §6.9 的 A/B/C/D 矩阵。

> 说明:阶段 1 与阶段 3 有轻微交叠(时间线模型会让状态推导更简单)。若人力允许,建议 1 与 3 在同一分支内连续完成,因为它们共享"删除本地信念、改为权威推导"这一条主线。

### 6.11 是否从头实现 TUI

**建议:重写"状态与渲染"内核(约 `client.py` 全部 + `textual_widgets.py` 的 3 个核心类 + `textual_client.py` 的事件/分页/状态部分,合计约 4000 行),保留应用外壳。**

保留(它们与缺陷无关,重写只会增加风险):

- `TextualTuiClient` / `XBotTextualApp` 的 Textual 接线、`BINDINGS`、CSS(`textual_theme.py`)、`ComposerTextArea` 的按键语义;
- `command.py` / `command_palette.py` / `completion_popup.py` / `selection.py`;
- `BoundedText`(块内窗口)与 `TranscriptScroll` 的事件形状(它们自身逻辑是自洽的,已被测试覆盖);
- `TerminalSession` 的 HTTP 方法(只把"事件收集 + 顺序保证"上移到新的 `TransportSession`)。

重写(净删除):

- `TuiState` 作为"语义 + 渲染簿记"混合体的存在方式;
- `TuiTranscriptEntry` 索引键、`evicted_*`/`inserted_*`、`reconcile_evictions`、`_trim_payloads` 的重编号;
- `TranscriptSurface` 的 `window_start/window_end/_mounted_entry_indices/mounted_entry_widgets` 四重簿记;
- `_handle_stream_event` + `state.apply_event` 的双分派;
- `_transcript_follow` / `at_tail` / `is_vertical_scroll_end` 三标志。

**不建议**换框架(去 bubbletea/ratatui):缺陷与 Textual 无关,重写成本远超收益。Textual 的 `VerticalScroll` 完全够用。

---

## 7. 验收标准(整体)

1. **状态**:附加到运行中的会话、切换会话、基线重建、终态帧丢失四种情况下,状态栏在 ≤5 秒内与服务端 `turn_status` 一致;有后台任务时状态栏与 Tasks 面板不矛盾。
2. **顺序**:transcript 中任意条目的内容**在其被提交后不再改变**(可用一个"内容哈希序列"断言);steer 出现在其时间点之后、其回复之前。
3. **分页**:在窗口外的任意时刻,实时内容不丢失、不改写历史、回到尾部后可见;窗口位置在插入/淘汰时不被重置。
4. **失败**:不存在只留日志不留 UI 的失败路径;CI 冻结静默 `except` 数量且只允许下降。
5. **性能**:20KB 流式回复期间主线程占用有明确上界(建议 ≤15%);500 轮会话后驻留 widget 数与内存峰值不随轮数增长。
6. **测试**:TUI 测试中存在**至少一条**用真实 SSE + 真实服务端跑通的端到端用例;不存在 `FakeSession` 类;测试代码不出现对 `app.state.*` / `app._*` 的赋值。
7. **文档**:`tui-web-performance.md` 的"open"项关闭;新增 TUI 状态机与时间线模型的设计文档,并替换其中已被证伪的说法(如"queue 语义"、"缓存避免重复 parse")。

---

## 8. 风险与取舍

| 风险 | 说明 | 缓解 |
|---|---|---|
| 协议改动触碰公共面 | `OpenSessionResponse.turn_status` 是新增可选字段,Web/ACP/`main.py` 均会看到 | 只增不改;旧客户端忽略即可;AGENTS.md 允许协议演进但要求"不要在协议里塞业务逻辑",此处是纯状态镜像 |
| 重写期间 TUI 不可用 | 阶段 1–4 期间主线 TUI 不能停 | 阶段 0 先止血;阶段 1–4 在 `dev-*` 分支上以"新旧并存 + 特性开关"推进,或接受短窗口冻结(需用户决策) |
| 稳定 ID 的可用性 | 并非所有条目都有服务端 id(本地 notice、错误、activity) | 本地生成 `local-uuid` 并加 `local: true` 前缀;服务端条目一律用服务端 id |
| 看门狗带来额外请求 | 每 5 秒一次 `list_threads` | 仅在"本地认为在跑"时启用;可复用 Web 的 5 秒常量;失败退避 |
| 重写引入新缺陷 | 4000 行新代码 | §6.9 的 A/B/C/D 矩阵必须在阶段 3/5 全部就位;每阶段独立验收 |
| 全面分析可能遗漏 | 本次分析基于通读 + 定点复现,未做全量交互式冒烟 | 建议在动手前,按 §2 清单在真实 `xbot` 服务上逐条手动复现一次,确认优先级 |

---

## 附录 A — 复现脚本要点(已执行)

- **A2**(P0-2):`turn_started` → `restore_history([...])` → `assistant_message_delta` → `_refresh_status()` ⇒ `status=Ready, turn_active=False`,而内容仍在流入。
- **D2**(P0-1):`restore_history` → `prepend_history` → `assistant_message_delta` + `assistant_message` ⇒ 历史消息 `A1(历史回复)` 被改写为 `这是新回复`。
- **D**(P0-3):同上 + `append_message("user", "STEER…")` ⇒ 该消息不存在于 `state.messages`,`pending_newer=2`。
- **C**(P1-1):`tool_calls_started` → `error` ⇒ `tool.status == "pending"`、`finished_at == 0`。
- **F**(steer 时序):`delta("我先看一下代码")` → `append_message("user", …)` → `delta("…继续")` ⇒ `[('assistant','我先看一下代码…继续'), ('user','别改 A,改成 B')]`。
- **P1-3**:`status="Copied 123 chars"` → `_refresh_status()` ⇒ `Ready`。
- **P1-5**:`_append_activity()` → `surface.replace()` ⇒ DOM 中 activity 消失,`_activity_widgets` 仍持引用。
- **P2-1**:`_render_message_uncached` 计时表见 §P2-1。

## 附录 B — 未验证/存疑项(不隐瞒)

1. 未在**真实运行的 `xbot` 服务 + 真实终端**上做交互冒烟。§2 的缺陷均在纯 Python 层复现,凡是依赖服务端行为(游标过期、重连、`turn_status` 时序)的因果链属于**代码级推断**,建议动手前手动验证一次。
2. `_handle_stream_event` 的节流路径(`_refresh_status` 只在 `_stream_timer is not None` 时节流)在真实高并发下的表现未实测。
3. Web 客户端的对照结论来自源码阅读(`web/src/state/*.ts`)与 `tui-web-performance.md`,未运行 Web。
4. Codex / opencode 的结论来自其仓库源码与 SDK 类型定义(Codex:`app_event.rs`、`streaming/controller.rs`、`status_indicator_widget.rs`;opencode:`sdk/js/src/gen/types.gen.ts`);未在其运行实例上验证。两个并行的深度调研子任务在时限内未产出报告,被主动中止,因此 §5 未覆盖:Codex 的 `interrupt_queue`/排队输入与丢失终态帧时的服务端对账细节、opencode 事件流是否带可续传游标。这两点建议在动手前补查(对应本报告 §6.3 看门狗与 §6.4 顺序校验的设计依据)。
5. 未评估 `evaluation/` 中是否存在依赖 TUI 内部结构的用例。
