# 历史窗口化分析：服务端驻留模型、compact 退役与 codex/opencode 对照

**引用基线。** 全文所有 `path:line` 引用指向提交 `133d813`（分支 `fix-runtime-stream-context-compat`），可用 `git show 133d813:<path>` 复核。写作时工作区**已有未提交改动**（`XBotv2/core/history.py`、`XBotv2/core/messages.py`、`XBotv2/persistence/models.py`、`XBotv2/session/contracts.py`、`XBotv2/session/manager.py`、`XBotv2/session/protocol.py`、`XBotv2/session/runtime.py`、`XBotv2/application/contracts.py`、`XBotv2/application/host.py`、`XBotv2/tui/state.py`、`XBotv2/tui/transport.py`、`XBotv2/tui/timeline.py`、`XBotv2/tui/events.py` 等），其中已包含"给 wire history item 稳定身份"的 (a) 步实现。本文以 `133d813` 为"现状"，凡涉及工作区已完成的部分单独标注，避免行号漂移。

---

## 1. 我们的服务端驻留模型（现状）

### 1.1 谁持有全量会话：一个活跃线程至少有 6 个可增长容器

`ConversationHistory` 是会话的 owner（`XBotv2/core/history.py:119`），内部是**三条并行列表**，而不是三份数据：

- `_nodes: list[HistoryNode]`（`XBotv2/core/history.py:133`）——派生的 conversation surface，每个 `HistoryNode` 是 `(node_id, message)`（`XBotv2/core/history.py:37-42`）。
- `_transcript: list[HistoryNode]`（`XBotv2/core/history.py:134`）——人类 transcript 投影，初始化为 `list(initial)`，即与 `_nodes` 引用同一批 `HistoryNode` 对象。
- `_messages: list[Message]`（`XBotv2/core/history.py:138`）——`[node.message for node in self._nodes]`，`Message` 对象与 `_nodes` 共享；`extend`（`XBotv2/core/history.py:168-170`）与 `replace_range`（`XBotv2/core/history.py:234-235`）也保持三者引用同一批对象。
- 另有 `_lineage: dict[str, tuple[str, ...]]`（`XBotv2/core/history.py:135`），记录 replacement 节点的来源链，随节点数增长。

**driver/engine 不另存一份**：`Engine.messages` 直接返回 `self.state.messages`（`XBotv2/agentloop/engine.py:243-244`），而 `MountedAgentApplication.snapshot()` 的 `messages=tuple(self.driver.messages)`（`XBotv2/application/host.py:54`）只是新建 tuple，元素仍是同一批 `Message` 对象。`manager.messages()` 对活跃 runtime 返回 `tuple(runtime.engine.messages)`（`XBotv2/session/manager.py:877-879`），同样是同一批对象。

**persistence 侧还有一组独立投影**（`XBotv2/persistence/store.py`），按 trajectory 文件路径做进程级共享（`_trajectory_use`，`XBotv2/persistence/store.py:254-272`）：

- `_TrajectoryState.records: list[TrajectoryRecord]`（`XBotv2/persistence/store.py:154`）——整个 `messages.jsonl` 的解析结果（pydantic record，非 `Message`）。
- `_SurfaceState.nodes: list[HistoryNode]`（`XBotv2/persistence/store.py:67`）与 `.revision`（`:68`）。
- `_TranscriptState.nodes` + `.lineage` + `.revision`（`XBotv2/persistence/store.py:92-94`）。

这三个容器与 `ConversationHistory` 的三条列表是**不同的 list**，且 `Message` 对象也不同：两个投影各自调用一次 `record.to_message()`（`XBotv2/persistence/store.py:72` 与 `:99`），而 `MessagePayloadRecord.to_message()` 每次构造新的 `Message`（`XBotv2/persistence/models.py:52-66`）。同一 position 的同一条消息在两个投影里是**两个 Message 对象，但 node_id 相同**（append 时 `str(record.position)`，`XBotv2/persistence/store.py:72`；replacement 时 `f"{position}:{index}"`，`XBotv2/persistence/store.py:407` 与 `:595`）。

唯一的对象共享发生在 hydration：`XBotv2/persistence/plugin.py:62` 取 `persistence.history.load_surface()`，`:68` 用 `ConversationHistory(sink=persistence.history, nodes=nodes)` 直接采纳 persistence 的这一批 `HistoryNode`。于是 `ConversationHistory.__init__` 里的 `node.message.seal()`（`XBotv2/core/history.py:136-140`）会就地 seal persistence surface 投影里的同一批 `Message`（`_sealed_node` 之前已 seal 过一次，`XBotv2/persistence/store.py:131-134`）；`Message.seal()` 是原地冻结（`XBotv2/core/messages.py:220-233`），因此这是一次跨组件的共享可变状态。hydrate 之后新 append 的消息两边各持一份对象：`MessageHistoryStore.append` 返回调用方传入的 message（`XBotv2/persistence/store.py:356-359`），而 surface 投影里是 `to_message()` 的新对象。

**额外的一次性副本**：每次 `snapshot()`/`open_session` 都会产生 `tuple(self._messages)`（`XBotv2/core/history.py:145-146`）+ `conversation_replay` 生成的 pydantic `SessionHistoryItem` 列表（`XBotv2/session/contracts.py:160`）+ `conversation_stats(snapshot.messages)` 的整表遍历（`XBotv2/session/manager.py:1296`，实现见 `XBotv2/core/timing.py:38`）。每次 attach 是 O(n) 内存、O(n) CPU、O(n) JSON。每轮模型请求还会做一次 `context_messages = list(context_build.messages)`（`XBotv2/agentloop/engine.py:651`），即 O(n) 浅拷贝，请求结束即释放。

**是否有界**：
- `_TrajectoryState` 的缓存有**进程级跨会话**上界：`_CACHE_LIMIT = 8`、`_CACHE_RECORD_BUDGET = 60_000`（`XBotv2/persistence/store.py:247-248`），`_evict_idle_states`（`:275-299`）超限时丢弃 `users == 0` 的 LRU 条目。这是跨会话的界，不是单会话的界：某个活跃会话的 trajectory 被逐出后，下一次读会重新全量 `_parse`（`XBotv2/persistence/store.py:165-172`）。
- `ConversationHistory` 的三条列表**没有任何容量上界**，只随 `SessionRuntime` 关闭而释放。释放由 idle reaper 控制：`idle_timeout = 3600.0`（`XBotv2/session/manager.py:93`），`_reap_idle` 回收 `last_activity` 超时且无 turn、无 pending input、无 SSE 订阅者的 runtime（`XBotv2/session/manager.py:155-172`）。

### 1.2 窗口化客户端能让服务端内存有界吗？不能

1. 分页原语是**读路径**。`MessageHistoryStore.page` / `page_transcript` / `page_trajectory`（`XBotv2/persistence/store.py:454`、`:465`、`:481`）只是在已经全量驻留的投影上切片（`page_messages`，`XBotv2/core/history.py:372-391`）。数据留在服务端投影里，客户端窗口大小不改变服务端占用。
2. compact 只退役 surface，不退役 transcript（§2 有逐条证据）。transcript 投影（`XBotv2/persistence/store.py:88-129`）与 `ConversationHistory._transcript` 保留从会话开头起的每一条消息。
3. trajectory 是 append-only 记录文件，`_TrajectoryState.records` 持有全部记录；`page_trajectory` 用 `len(records)` 定位尾部与 `before` 锚点（`XBotv2/persistence/store.py:498-517`），它需要 `records` 全量存在。

要让服务端内存有界，必须改**驻留策略**（把 surface/transcript/records 换成外存索引或真正按需读取），这与客户端窗口化是两件独立的事；本次改动不做这件事，因此服务端内存曲线不变。

### 1.3 identity 现状

- 核心层**已经有稳定的 node identity**：append 的节点 id 是 record position 的字符串，replacement 的节点 id 是 `f"{position}:{index}"`（`XBotv2/persistence/store.py:72`、`:407`、`:595`）；无 sink 的内存态用 `memory:{uuid4().hex}`（`XBotv2/core/history.py:129-132`、`221-223`）。
- **wire 层在 `133d813` 上没有 identity**：`SessionHistoryItem`（`XBotv2/session/contracts.py:94-110`）没有 id 字段，且 `input_id` 还是 `Field(exclude=True)`（`:102`），所以序列化后完全不可用于合并。
- 工作区的未提交改动已补上这一步：`SessionHistoryItem.id`、`message_identity()`（`XBotv2/core/messages.py`）、`ConversationPage.node_ids`、`page_nodes()`（替换 `page_messages`）、`OpenedSession.node_ids`、`HistoryMutation.node_ids`、`SurfaceReplaceRecord.node_id(index)`，以及 TUI 侧按 `item.id` upsert（`XBotv2/tui/state.py`）。这些尚未提交，本文其余部分仍按 `133d813` 描述，必要时注明。

---

## 2. compact / 退役机制

### 2.1 真实代码路径

- 手动 `/compact`：`XBotv2/compact/commands.py` 的 `run_compact_command` → `CompactService._compact_command`（`XBotv2/compact/service.py:119`）→ `_compact_current_history`（`:125`）。
- 自动：同一条 `_compact`（`XBotv2/compact/service.py:493`），reason 为 threshold / context-overflow；提案由 `build_compaction_proposal`（`XBotv2/compact/compactor.py:112`）构造，切点由 `compact_prefix_end`（`XBotv2/compact/history.py:19`）计算——优先保留最近 `keep_recent_turns` 个 user 边界，退化为 assistant 边界，并回退到 tool-call 配对完整的切点（`XBotv2/compact/history.py:72`）。
- source 集合：`source_node_ids = self.state.history.node_ids()`（`XBotv2/compact/service.py:517`），取前 `prefix_end` 个（`proposal["prefix_end"]`，`XBotv2/compact/compactor.py:315`）。
- 提交：`self.state.replace_message_range(0, prefix_end, list(replacement), operation=f"compact:{compaction_id}", preserve_transcript=True)`（`XBotv2/compact/service.py:388-394`）→ `LoopState.replace_message_range`（`XBotv2/agentloop/contracts.py:157-172`）→ `ConversationHistory.replace_range`（`XBotv2/core/history.py:185`）。

### 2.2 到底从哪些集合退役了什么

| 集合 | preserve 替换后 |
| --- | --- |
| `ConversationHistory._nodes` | `self._nodes[start:end] = nodes`（`XBotv2/core/history.py:234`）：退役 `prefix_end` 个 surface 节点，换成 1 个 summary 节点 |
| `ConversationHistory._messages` | `self._messages[start:end] = [node.message ...]`（`:235`）：同步退役 |
| `ConversationHistory._transcript` | **不动**。preserve 分支只写 `self._lineage[nodes[0].node_id] = origins`（`XBotv2/core/history.py:226-227`），不触碰 `_transcript`，也不改 `_transcript_revision` |
| `ConversationHistory._surface_revision` | 重置为新 uuid（`:236`），surface 游标作废 |
| `ConversationHistory._lineage` | 新增一条 `summary_node_id → 原始 origins`（`:227`） |
| `_SurfaceState.nodes`（persistence） | `_replace_nodes` 就地替换（`XBotv2/persistence/store.py:73-82`、`:600-622`）；revision = max(revision, record.position)（`:82`） |
| `_TranscriptState.nodes` | **不动**。`record.transcript == "preserve"` 时只写 `self.lineage[replacements[0].node_id] = sources` 然后 `return`（`XBotv2/persistence/store.py:108-114`）；revision 不变（只有非 preserve 替换才推进，`:125`） |
| `_TrajectoryState.records` | **只增**：压缩前后的记录都在（`XBotv2/persistence/store.py:206-214`），并追加一条 `compaction/summary` event 记录（`XBotv2/compact/service.py:398-406`） |
| 磁盘 `messages.jsonl` | 追加一条 `SurfaceReplaceRecord`（`XBotv2/persistence/store.py:383-398`），无删除路径 |

### 2.3 退役节点与存活节点的 identity

- 未被压缩的尾部节点：node_id 不变（append 时为 position 字符串，`XBotv2/persistence/store.py:72`）。
- summary 节点：新 identity `f"{record.position}:{index}"`（`XBotv2/persistence/store.py:407`，命名规则集中在 `MessageHistoryStore.replace_surface`），`ConversationHistory` 通过 sink 返回值拿到同一批 id（`XBotv2/core/history.py:227-239`）。
- 被退役节点的 node_id 从 surface 消失，但**没有消失于内存**：仍存在于 `ConversationHistory._lineage`（`XBotv2/core/history.py:135`、`:227`）与 `_TranscriptState.lineage`（`XBotv2/persistence/store.py:93`、`:113`），供后续非 preserve 替换把 transcript span 反查回原始 origins（`XBotv2/core/history.py:202-211`、`XBotv2/persistence/store.py:102-107`）。
- 压缩之后 surface 与 transcript 的 node_id 集合不再相同：transcript 保存原始 `str(position)`，surface 保存 `{position}:0`。**同一条"消息"在两套投影里 identity 不同**——wire 层必须区分这两类，`SessionTrajectoryMessage.message_id` 与 replacement 的 `source_node_ids` 正是这一分裂的体现（`XBotv2/session/contracts.py:113-134`）。

### 2.4 trajectory / transcript 是 append-only 吗

- trajectory 记录文件：**是**。没有删除记录的代码路径；例外只有崩溃后截断不完整尾行（`_drop_incomplete_tail`，`XBotv2/persistence/store.py:198`）与写入失败回滚（`os.ftruncate`，`:551-554`）。
- transcript：**不是**。`clear` / `undo` / `regenerate` / `after-tools` / `replace` 都走 `preserve_transcript=False`，从 `_TranscriptState.nodes` 删掉一段（`XBotv2/persistence/store.py:115-122`），并在 `ConversationHistory` 里 `transcript[start:start+len(origins)] = nodes`（`XBotv2/core/history.py:230`）；`_TranscriptState.revision` 仅在这些时刻前进（`XBotv2/persistence/store.py:125`）。对应调用方：`XBotv2/session/session.py:131`（clear）、`:138`（undo）、`:165`（regenerate）、`XBotv2/agentloop/engine.py:970`（after-tools）。
- 普通 append 与 compaction 对 transcript 而言都是只增。

### 2.5 "服务端应当从会话开头就全量驻留内存，compact 负责压缩和退役"是否与实现相符

**前半句相符，后半句只对 surface 成立。** 不相符之处逐条列出：

1. transcript 不退役任何被压缩的消息。`XBotv2/persistence/store.py:108-114`（preserve 直接 return）与 `XBotv2/core/history.py:226-227`（只写 lineage）。压缩后 `_transcript` 仍持有全部原始 `Message` 对象。
2. trajectory records 与磁盘不退役（`XBotv2/persistence/store.py:206-214`、`:383-398`）。
3. 内存中的 `_lineage`（`XBotv2/core/history.py:135`）与 `_TranscriptState.lineage`（`XBotv2/persistence/store.py:93`）随 replacement 单调增长，本身没有退役机制。
4. surface 尺寸不会持续变小：每次 compact 用 1 个 summary 节点替换 prefix，summary 又会被下一次 compact 并入新的 prefix；退役量取决于 `keep_recent_turns`（`XBotv2/compact/service.py:92`）而不是"总内存上界"。
5. content 层面也不退役：被压缩消息的完整内容仍以 `MessagePayloadRecord` 存在于 `records` 与磁盘（`XBotv2/persistence/store.py:383-391`），summary 文本还会重新附在 wire 上（`XBotv2/session/contracts.py:133`、`:231-246`）。
6. `SessionStats`/`turn_count` 依赖 summary 的 `response_metadata` 折叠保留（`XBotv2/core/timing.py:38-48`；`XBotv2/persistence/plugin.py:69-73`），说明设计上把 summary 当作"压缩后 surface 的唯一表征"，但 transcript 仍保留原文，两者语义已经分叉。

---

## 3. 服务端分页原语的现状与 gap

### 3.1 已存在的原语（HTTP 路径与 query 参数，`XBotv2/session/protocol.py`）

| 方法与路径 | operation_id | 请求 | 响应 |
| --- | --- | --- | --- |
| `POST /sessions` (`:393`) | `open_session` | `OpenSessionRequest{session_id, thread_id, workspace_root, mode, agent, history_limit}`，`history_limit` 为 `int|None`，`ge=1, le=500`（`:66-72`） | `OpenSessionResponse{..., history: list[SessionHistoryItem], history_cursor: str|None, pending_inputs, pending_interactions}`（`:94-99`） |
| `POST /sessions/{session_id}/threads` (`:503-507`) | `open_thread` | `OpenThreadRequest{thread_id, parent_thread_id, workspace_root, mode, agent, history_limit}`（`:102-108`） | 同上 |
| `GET /sessions/{sid}/threads/{tid}/messages` (`:544-548`) | `list_messages` | `cursor: str|None`、`limit: int|None`（`ge=1, le=500`）（`:551-552`） | `ThreadMessagesResponse{messages, next_cursor}`（`:111-115`） |
| `GET /sessions/{sid}/threads/{tid}/trajectory` (`:567-569`) | `list_trajectory` | `cursor: str|None`、`before: int|None`（`ge=1`）、`limit` 默认 160（`ge=1, le=500`）（`:574-576`） | `ThreadTrajectoryResponse{items, next_cursor, newest_position}`（`:118-125`） |
| `POST .../history/undo` (`:629-633`) | `undo_thread_history` | `UndoRequest{count, history_limit}`（`:128-130`） | `HistoryMutationResponse{..., history_cursor}`（`:137-146`） |
| `GET .../events` SSE | `stream_events` | `after: int|None` | 事件 `history_updated` 携带 `history` + `history_cursor`（`HistoryUpdatedData`，`:169-174`） |

分页装配点：`_requested_history_page`（`XBotv2/session/protocol.py:312-325`，`limit is None` 时返回 `None`）、`_open_session_response`（`:281-292`，`history = page.messages if page is not None else value.history`）、`list_messages` 调 `sessions.message_page`（`:554-559`）、`list_trajectory` 调 `sessions.trajectory_page`（`:578-584`）。

### 3.2 已存在的原语（core / persistence / SDK）

- `ConversationHistory.page`（surface，`XBotv2/core/history.py:284`）、`.page_transcript`（transcript，`:298`）、`ConversationPageReader.page` 协议（`:82-88`）。
- `page_messages(messages, revision, limit, cursor, out_of_range)`（`XBotv2/core/history.py:372`）：`limit < 1` 报错；`messages` 为空且给了 cursor 抛 `HistoryCursorInvalid`；`end = len(messages)` 或解码 cursor；`start = max(0, end - limit)`；`next_cursor` 在 `start == 0` 时为 `None`（`:390`）。
- cursor 编码 `json.dumps([1, revision, offset])` → urlsafe base64 去 padding（`XBotv2/core/history.py:394-396`）；解码校验 list 长度 3、版本 1、revision 相等、offset 为非 bool 整数，否则抛 `HistoryCursorInvalid`（`:399-420`，异常类定义在 `:17`）。
- `HistoryCursorInvalid` → `OperationError("invalid_cursor", ...)`（`XBotv2/session/manager.py:900-903`、`:923-924`）→ HTTP 400（`XBotv2/server/http.py:208-228`，`invalid_cursor` 落到 `else: status = 400`）。
- `HistoryPort` 暴露 `page` / `page_transcript` / `page_trajectory`（`XBotv2/persistence/contracts.py:105-125`）。
- `TrajectoryPage.newest_position`（`XBotv2/core/history.py:74-79`）与 `SessionTrajectoryPage.newest_position`（`XBotv2/session/contracts.py:152-156`）。
- Python SDK：`XBotClient.open_session(..., history_limit=None)`（`XBotv2/client.py:190-211`）、`open_thread(..., history_limit=None)`（`:296-318`）、`list_messages(cursor, limit)`（`:408-424`）、`list_trajectory(cursor, before, limit=160)`（`:427-448`）。
- `SessionsPort` 协议（`XBotv2/session/contracts.py:528`）：`messages`（`:541`）、`message_page`（`:542-549`）、`trajectory_page`（`:550-558`）；实现分别在 `XBotv2/session/manager.py:872`、`:883`、`:909`。

### 3.3 TUI 今天实际调用什么

`XBotv2/tui/transport.py` 只调用：`hello`、`open_session`（`:190`、`:252`、`:443`）、`list_threads`（watchdog，`:468`）、`list_sessions`、`list_commands`、`list_providers`、`list_agents`、`select_*`、`run_command`、`stream_events`（`:369-373`）、`send_message`、`interrupt`。

- **三处 `open_session` 都没有传 `history_limit`**（`XBotv2/tui/transport.py:190-196`、`:252-258`、`:443-449`），于是 `_open_session_response` 走 `value.history` 分支，即全量消息（`XBotv2/session/protocol.py:285`）。
- **从不调用 `list_messages` / `list_trajectory`**：`SessionBackend` Protocol 里没有这两个方法（`XBotv2/tui/transport.py:58-97`）。
- 收到 `history_updated` 时用整份 `payload.history` 重建 timeline（`XBotv2/tui/state.py:373-374` → `_rebuild_from_items`，`:637-650` 会 `state.timeline = Timeline()` 后逐条 `upsert`），**payload 里的 `history_cursor` 从未被读取**（`133d813` 的 `XBotv2/tui/state.py` 全文 `grep -c history_cursor` = 0；`XBotv2/tui/protocol.py:157` 只做 `_carries(HistoryReplaced)`，把 payload 原样塞进事件）。

对照：Web 客户端**已经**是有界初始 + 向上分页 + 有界保留：

- `history_limit: options.historyLimit ?? 160`（`XBotv2/web/src/api/client.ts:160`）、`openThread` 固定 160（`:218`）、undo 也带 160（`:353`）。
- `MAX_TRAJECTORY_WINDOW = 240`（`XBotv2/web/src/state/runtime.ts:31`）；超窗时按 `atTail` 决定保留尾部还是头部，并把窗口最旧 position 记为 `windowAnchor`（`:260-266`）；`boundTranscriptEntries` 再裁渲染条目（`:268`）。
- `loadEarlier` 用 `before: anchor ?? undefined`、无锚点时才回退到 `cursor`（`XBotv2/web/src/state/useXBot.ts:838-863`），锚点为空时先 `loadLatest()` 重新锚定（`:849`）；`loadViewEarlier` 用 `olderCursor` 翻页并 prepend（`:546-568`）；`olderCursor` 由 `windowAnchor` 优先推导（`:1228-1231`）。
- HTTP 层已有集成测试覆盖"有界初始 + 向上翻页"：`XBotv2/tests/integration/test_http_transport.py:411-440`（`history_limit: 2` → 断言 `history_cursor` → 用 `next_cursor` 取更早一页 → 断言 `next_cursor is None`）。

### 3.4 阻塞窗口化客户端的具体 gap

1. **wire 无 identity（工作区已修）**。`133d813` 上 `SessionHistoryItem` 没有 id（`XBotv2/session/contracts.py:94-110`），`input_id` 还是 `exclude=True`（`:102`）；`conversation_replay` 不产出任何可用于合并的键（`:160-190`）。工作区改动加了 `SessionHistoryItem.id`、`message_identity()`、`ConversationPage.node_ids`、`OpenedSession.node_ids`、`HistoryMutation.node_ids`，TUI 也改成按 `item.id` upsert。**该 gap 正在被关闭，尚未提交。**
2. **transcript 与 surface 语义分叉**。`message_page(limit=N)` 读 transcript（`XBotv2/session/manager.py:897-899`），`message_page(limit=None)` 读 live `engine.messages`（surface，`:891-896`），`_opened_session` 无 limit 时也读 surface（`:1297`）。因此**同一个 `POST /sessions` 带不带 `history_limit` 返回两套不同视图**：压缩后 surface 只有 summary，transcript 还是原文。
3. **surface 在协议上不可分页**。`MessageHistoryStore.page`（`XBotv2/persistence/store.py:454`）与 `ConversationHistory.page`（`XBotv2/core/history.py:284`）**没有任何调用者**：`history_pages` 是 `_TranscriptPages` 恒调 `page_transcript` 的适配器（`XBotv2/application/host.py:85-92`）。所以"模型当前看到的 surface"没有 wire 读路径。
4. **游标存活期分两类，且语义不对称**。trajectory 的 revision 是 `f"{scope}:trajectory"`，与内容无关（`XBotv2/persistence/store.py:500`），永不失效；surface/transcript 用 `_revision(generation, projection)`（`:520-521`），其中 surface 的 generation 只在 `SurfaceReplaceRecord` 时推进（`:82`），transcript 只在**非 preserve** 替换时推进（`:125`）。结论：**compaction 会使 surface 游标失效，但不会使 transcript/trajectory 游标失效**；`clear`/`undo`/`regenerate`/`after-tools` 会使 transcript 游标失效。
5. **没有显式 "has older" 信号**，只能从 `next_cursor is None` 反推（`XBotv2/core/history.py:390`；`XBotv2/persistence/store.py:516`）。也没有总数；`newest_position` 只说明尾部位置（`XBotv2/core/history.py:77-79`），不说明窗口头之后还剩多少。
6. **`message_page` 不支持 `before` 重锚**。`SessionsPort.message_page` 只有 `cursor`/`limit`（`XBotv2/session/contracts.py:542-549`，实现 `XBotv2/session/manager.py:883-889`），而 `page_trajectory` 有 `before`，其 docstring 明确说这是为"已经驱逐了头部窗口"的客户端准备的（`XBotv2/persistence/store.py:488-497`）。因此 transcript 分页一旦丢弃最前一页，就只能放弃 cursor 重新拉最新页——Web 客户端正是这样绕开的（`XBotv2/web/src/state/useXBot.ts:843-853`）。
7. **分页读恒走磁盘，且需要 persistence 已装载**。`manager.message_page` / `trajectory_page` 都先 `_persisted_thread`（`XBotv2/session/manager.py:897`、`:918`），它要求 thread 目录存在（`:624-627`，`has_thread` 见 `XBotv2/core/paths.py:82-84`）。目录由 `ThreadPersistence.create` 在 open 时创建（`XBotv2/persistence/store.py:899`、`:913`），所以常态可用；但 `no_plugins` 下 persistence 不装载（`XBotv2/application/app.py:99-110`），此时带 `history_limit` 的 open_session 会以 `SessionNotFound` 失败。且 `history_pages` 在无 persistence 时退化为 live `ConversationHistory`（`XBotv2/application/host.py:73-77`），与 manager 的磁盘读路径不一致。
8. **分页粒度是消息节点，不是 turn**。没有 turn 边界 API（codex 有 turn 页 + item 页两级，见 §4）。
9. **`history_updated` 是全量替换 + 硬编码 160**。`SessionRuntime._on_history_changed` 取 `history_pages.page(limit=160)`，不带 cursor（`XBotv2/session/runtime.py:137`；regenerate 路径见 `:651`），于是 TUI 用最近 160 条 transcript 覆盖整个 timeline 并丢掉 cursor（`XBotv2/tui/state.py:373-374`、`:637-650`）。触发这条路径的操作包括 compact（`XBotv2/compact/service.py:421-427`）、clear/undo/regenerate（`XBotv2/session/session.py:131`、`:138`、`:165`）。**结果：客户端会认为历史就只有那 160 条。**
10. **没有"重新锚定"语义**。`invalid_cursor` 只是 400（`XBotv2/server/http.py:208-228`），客户端只能整份重建 baseline（Web 的做法见 `XBotv2/web/src/state/useXBot.ts:871-877`）。

---

## 4. codex 的机制

来源（均为实际抓取）：[`app_server_session/history.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/app_server_session/history.rs)、[`app/history_pagination.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/app/history_pagination.rs)、[`pager_overlay.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/pager_overlay.rs)、[`pager_overlay/transcript.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/pager_overlay/transcript.rs)、[`transcript_view.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/transcript_view.rs)、[`transcript_view/footer.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/transcript_view/footer.rs)、[`transcript_view/search.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/transcript_view/search.rs)、[`app/event_dispatch.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/app/event_dispatch.rs)、[`app_event.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/app_event.rs)、[`app.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/app.rs)、[`app/native_history.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/app/native_history.rs)、[`resize_reflow_cap.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/resize_reflow_cap.rs)、[`insert_history.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/insert_history.rs)、[`rollout/reverse_jsonl_scanner.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/rollout/src/reverse_jsonl_scanner.rs)、[`rollout/state_db.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/rollout/src/state_db.rs)、[`thread-store/lib.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/thread-store/src/lib.rs)、[`app-server/request_processors/thread_processor.rs`](https://raw.githubusercontent.com/openai/codex/main/codex-rs/app-server/src/request_processors/thread_processor.rs)、[PR #36950](https://github.com/openai/codex/pull/36950)、[commit dbcd837](https://github.com/openai/codex/commit/dbcd837c20cd891f3de4bb3976c7f643d0b2fb93)、[commit 449f099](https://github.com/openai/codex/commit/449f099f1cad470fa4f63dcc198f8d6cb8944cde)、[issue #21635](https://github.com/openai/codex/issues/21635)、[issue #22068](https://github.com/openai/codex/issues/22068)。

**有界初始水合。** 常量 `INITIAL_HISTORY_TURN_LIMIT = 5`、`HISTORY_ITEM_PAGE_LIMIT = 100`、`HISTORY_ITEM_SCAN_LIMIT = 4 * 100 = 400`（`history.rs`）。`hydrate_initial_thread_history` 先取 5 个 turn 的首页（`thread_turns_page(thread_id, turn_cursor, INITIAL_HISTORY_TURN_LIMIT)`），`thread.turns = page.data.into_iter().rev().collect()`，再按 `HistoryLoadBudget` 循环取 item 页直到预算耗尽或 `next_item_cursor` 为空。`HistoryLoadBudget::new` 分档：owned-transcript 模式下 `rows = Some(terminal_height.max(1) * 3)`、`items = Some(HISTORY_ITEM_SCAN_LIMIT)`；否则 `rows = resize_reflow_max_rows(settings.terminal_resize_reflow())`，`items` 为 `None`（`Complete`/`ThroughTurn`/`Initial` 且无行上限）或 `max_rows + HISTORY_ITEM_SCAN_LIMIT`（`Initial` 且行数有上限）或 `Some(HISTORY_ITEM_PAGE_LIMIT)`（`Initial` 且无 config）。`next_page_size` 用剩余 rows/items 算下一页大小，并保证首页之后 `limit` 不会被隐藏条目压到 1。`HistoryHydrationScope::ThroughTurn(anchor)` 会在锚点 turn 之后的更旧项已加载时提前停止。

**游标方向。** `thread_items_page_params` 与 `thread_turns_page` 都设 `sort_direction: Some(SortDirection::Desc)`，即从新到旧。`advancing_cursor(current, next, seen)` 把当前游标记入 `seen_cursors`，只有当 `next` 未出现过时才接受它——这是防服务端游标回环的守卫。分页状态在 `ThreadHistoryPagination{next_turn_cursor, next_item_cursor, seen_turn_cursors, seen_item_cursors, loading_older}`。

**prepend 不打扰视口。** `App::handle_older_history_page` 先判 `is_older_history_page_pending`，再取 thread 的 store 与 `turns`，`apply_older_history_page` → `merge_thread_item_page`（按 `turn.id`/`item.id()` 去重：`!turn.items.iter().any(|item| item.id() == entry.item.id())`，新页用 `turn.items.insert(0, item)`，`items.reverse()`），随后 `project_older_history_cells` → `prepend_older_transcript_cells`。插入位置是 `SessionInfoCell`/`SessionHeaderHistoryCell` 之后（overlay 走 `TranscriptOverlay::prepend`，其注释为 `Insert old history after the session header; stable viewport anchors keep their content.`），`replace`/`insert` 之前会按插入偏移修正 `highlight_cell`/`pending_highlight`，插入后调 `history_loaded(&self.transcript_cells, inserted.clone())`（实现在 `transcript_view/search.rs`）。`merge_older_turns` 把新 turn 前插，已存在的 turn 只把缺的 item `splice(0..0, items)`，不重复。视口锚点用 `Position::Reading(anchor)` / `TranscriptBookmark`，`prepend_snapshot_history` 只把"显式插入的旧页"并入冻结历史。

**"还有更早"如何表示与渲染。** `has_older_history(thread_id)` = `next_item_cursor.is_some()`；TUI 侧保存在 `scrollback_has_older_history: bool`（`app.rs`），并映射到 `TranscriptHistoryState`：`Idle | LoadingOlder | LoadingBeginning | Partial | Failed | Complete`（`pager_overlay.rs`，其中 `has_unloaded_history()` 覆盖 `LoadingOlder | LoadingBeginning | Partial | Failed`）。渲染在 footer（`transcript_view/footer.rs`）：`Partial` → `"Earlier messages available.  "`；`LoadingOlder`/`LoadingBeginning` → shimmer `"Loading earlier messages…"`（`is_loading_history()`）；`Failed` → `"Retry history: "` + `ctrl+home` 提示，选中状态下改为 `"Retry: ctrl+home"`。滚动触发在 `TranscriptOverlay::should_load_older`（`jump_top` 直接触发，或 `needs_history` 且按了 scroll_up/page_up/half_page_up）；`finish_owned_history_page` 在状态从 `LoadingBeginning` 推进后决定是否继续取页。

**失败处理。** `OlderThreadHistoryLoaded{result: Err}` 分支（`app/event_dispatch.rs`）调用 `cancel_older_history_page`，若仍是当前线程则把 `self.transcript_view.history = TranscriptHistoryState::Failed`（overlay 同步 `set_history_state(Failed)`）并 `schedule_frame`，同时 `tracing::warn!`，**不自动重试**。`handle_older_history_page` 在两种情况下静默放弃该页：线程已切走，或 `tui.is_owned_screen() && !scrollback_has_older_history`。

**内存里仍在累积什么。** `transcript_cells: Vec<Arc<dyn HistoryCell>>`（`app.rs`）只被回退/回滚路径截断（例如 `self.transcript_cells.truncate(index)` 于 `app/event_dispatch.rs`），分页 prepend 只做 `splice`，**没有按行数或条数的淘汰**。所以："初始水合有界"成立，"客户端总内存有界"不成立——用户一直往上翻就会一直增长。终端 scrollback 侧另有行数预算（`resize_reflow_cap.rs`：VS Code 1000、Windows Terminal 9001、WezTerm 3500、Alacritty 10000，`max_rows = 0` 表示关闭），它是 resize reflow 与初次 replay 的上限，不是内存上限。

**rollout 文件与内存 cells 的关系。** durable history 不在 TUI 进程，也不在 app-server 的内存里：app-server 通过 `codex_thread_store`（`LocalThreadStore`，`supports_paginated_history_lists()`）分页读取（`thread_processor.rs` 的 `thread_turns_list_response_inner` / `thread_items_list_response_inner` → `thread_store.list_turns/list_items`）；`rollout/state_db.rs` 用 `codex_state::StateRuntime`（SQLite）做投影与 backfill；rollout JSONL 仍是源，`ReverseJsonlScanner` 从文件尾按 64 KiB 块反向读记录（`reverse_jsonl_scanner.rs`）。TUI 只持有它请求过的页渲染成的 cells；`insert_history.rs` 把定稿行写进终端 scrollback（文件头注释：`Codex uses the terminal scrollback itself for finalized chat history`），`app/resize_reflow.rs` 在 resize 时从 `transcript_cells` 重排——这正是它必须保留 cells 的原因。

**已承认的失败模式。** PR #36950（合并为 commit `dbcd837`，标题 `Paginate TUI transcript history`）声称覆盖多页加载、页边界 prompt 重建、legacy 服务器回退、有界 retry、history-state 渲染；commit `449f099`（#36951，`Harden paginated history handling in the TUI`）声称补足被行上限截断的 scrollback、保留 hydration 失败时已创建的 fork。**issue 断言（不是代码结论）**：#21635 `TUI resume main view shows partial transcript when terminal_resize_reflow_max_rows is capped`，报告人明确说 `thread/resume` 返回全量、Ctrl+T 可见全文，只有主视图被截；评论 `oxysoft` 归因于 overlay/reflow 是 O(n) 渲染（每 cell 重新解析 markdown、无高度缓存、无虚拟化），评论 `Josephur`/`Loong0x00` 报告"durable EOF 正确、模型上下文正确，但初始可见 replay 落在旧内容"，`Loong0x00` 说 0.149 用 bounded paginated hydration + 终端相关的初始 replay 行预算（其环境 1000 行），下一页才到那条 1.16 MB 的旧 `commandExecution` 输出。#22068 由维护者 `etraut-openai` 回复：resume 路径改为向终端 scrollback 回放最后 10K 行，可用 `tui.terminal_resize_reflow_max_rows = 0` 关闭。

---

## 5. opencode 的机制

来源（均为实际抓取）：[issue #6548](https://github.com/anomalyco/opencode/issues/6548)、[`api.github.com/.../issues/6548`](https://api.github.com/repos/anomalyco/opencode/issues/6548)、[`.../issues/6548/comments`](https://api.github.com/repos/anomalyco/opencode/issues/6548/comments)、[PR #6138](https://github.com/anomalyco/opencode/pull/6138)、[PR #6656](https://github.com/anomalyco/opencode/pull/6656)、[PR #8535](https://github.com/anomalyco/opencode/pull/8535)、[`packages/tui/src/context/sync.tsx`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/src/context/sync.tsx)、[`packages/app/src/context/server-session.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/app/src/context/server-session.ts)、[`packages/app/src/context/directory-sync.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/app/src/context/directory-sync.ts)、[`packages/server/src/handlers/message.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/server/src/handlers/message.ts)、[`.../httpapi/handlers/session.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts)、[`.../httpapi/groups/session.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/server/routes/instance/httpapi/groups/session.ts)、[`packages/opencode/src/session/message-v2.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/session/message-v2.ts)、[`packages/core/src/database/database.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/core/src/database/database.ts)、[`packages/core/src/database/schema.gen.ts`](https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/core/src/database/schema.gen.ts)。

**issue 的诉求（issue 文本，不是代码）。** 长会话可含数千条消息，全量加载慢且吃内存；要"有界消息窗口 + 按需加载旧/新 + 仍支持跳到最旧/最新"；分页契约要下沉到 server/API/SDK；revert/restore 在边界消息不在窗口内时仍要正确。明确把 virtualized rendering 列为"以后单独做的优化"。

**评论里的提案与自我批评（评论，不是代码）。** 评论 `CasualDeveloper` 给出 Phase 1 清单：`session/index.ts` 加 `before`、`server/server.ts` 加 `before` query、TUI `sync.tsx` 加 `hasMore`/`oldestId`/`loading` 与 `loadMore`、`tui/routes/session/index.tsx` 在滚动到顶时触发；Phase 2 才做虚拟化（并指出 opentui 的 `viewportCulling` 只跳过绘制，仍挂载全部 Solid 组件与 Yoga 布局节点）。API 形态三选一（`paginated` 参数 / RFC 8288 `Link` 头保持 body 为数组 / SDK major bump），倾向 `Link` 头，理由是 GitHub 的做法且对外完全向后兼容。同一作者 2026-01-04 的自评列出 PR #6656 的失败模式：`sync.tsx` 硬编码 `limit: 100`（初次加载与 `loadMore`）、移除了 100 条裁剪；`loadMore` **静默失败**（catch 只把 `loading` 置 false）；`includes('rel="next"')` 解析脆弱（不处理无引号、多值 Link）；`before` 未在 query schema 校验；深翻页 N+1（每页 `Storage.list` + 逐条 `MessageV2.get`）；以及"用户翻页时 CPU/内存仍会增长，只有虚拟化能解决 #6172"。2026-01-14 更新称 #8535 提供双向游标（`before`/`after`/`oldest`）+ RFC 8288 `Link` 头 + TUI 边界加载与错误归一化，并列出测试命令；2026-02-18 称已适配"new sqlite-backed message storage"。2026-06-24 评论 `D1ChangGeng` 称 v1.17.9 的 web/desktop `packages/app` 已有 cursor-based `loadMore`（`directory-sync.ts`），TUI 没有，这是 #28257（undo）与 #30587（fork-from-history）的上游原因。

**PR 状态（GitHub API 实测）。** #6138 `feat(tui): add session_list_limit for session picker`——state `closed`、未合并，body 自称"intentionally scoped to the picker render list, not broader session loading or pagination"。#6656 `session: paginate message loading`——`closed`、未合并，body 明确"not true virtualization yet (we still render all loaded messages)"。#8535 `feat(session): bi-directional cursor-based pagination (#6548)`——`closed`、未合并，body 说明 v1 现状是"the TUI loads a single `limit: 100` page and stops"，并称 v2 会以 `message.list(limit/order/cursor)` 从设计上解决，同时在 "Scope: v1" 里声明虚拟化不在本 PR。

**TUI 今天实际做什么（dev 代码）。** `packages/tui/src/context/sync.tsx` 里唯一一次历史请求是 `sdk.client.session.messages({ sessionID, limit: 100 })`（第 603 行）；没有 `loadMore`/`hasMore`/`oldestId`/`before`/`Link`（对该文件 grep 计数为 0）。更关键的是它把结果裁到最近 100 条：`const removed = infos.slice(0, -100)`（625）、`const visible = infos.slice(-100)`（626），并对 `removed` 执行 `delete draft.part[message.id]`（655）。所以 **TUI 是有界的，但第 100 条以外的旧消息永久取不回来**——这与 #8535 声称的 #28257/#30587 症状一致，且与我们 TUI 的问题方向相反（我们是无界但可重取）。

**Web/desktop `packages/app`。** `packages/app/src/context/server-session.ts` 有 `initialMessagePageSize = 20`（30）与 `historyMessagePageSize = 200`（31），状态为 `meta.limit` / `meta.cursor` / `meta.complete` / `meta.loading` / `data.message`（243-244）。`history.more(sessionID)` = 已有数据 && limit 已知 && `!meta.complete` && 有 `meta.cursor`；`history.loadMore(sessionID, count)` 在 `meta.loading || meta.complete || !meta.cursor` 时直接 return，否则 `loadMessages(sessionID, count, meta.cursor[sessionID], "prepend")`（1396-1405）。`fetchMessages(sessionID, limit, before)` 分两路：v2 用 `messageApi.list(cursor ? {sessionID, limit, cursor} : {sessionID, limit, order: "desc"})`，并用 `pages.at(-1)?.cursor.next && needsOlderTurnRoot(...)` 连续取页直到不再需要，返回 `sourceMode: before ? "older" : "latest"`、`cursor: response.cursor.next`、`complete: response.data.length === 0`（537-566）；v1 用 `client.session.messages({sessionID, limit, before})`，游标取自响应头 **`x-next-cursor`**（579）。合并时 `page.sourceMode === "older" ? [...page.source, ...current] : [...current, ...page.source]`（685），即 prepend。

> 需要指出评论与代码的一处不一致：`packages/app/src/context/directory-sync.ts` 里游标的是**session 列表**（`setStore("limit", value => value + count)` + `more` memo，126-135），message 分页在 `server-session.ts`。评论把 message 分页归到 `directory-sync.ts` 与 dev 代码不符。

**服务端契约。**
- v2（`packages/server/src/handlers/message.ts`）：query `limit`（默认 `DefaultMessagesLimit = 50`）、`order`（`asc`/`desc`）、`cursor`；`cursor` 与 `order` 不能同时传（`InvalidCursorError`）；cursor 为 `base64url(JSON({id, order, direction}))`，`direction ∈ {previous, next}`；响应 `{data, cursor: {previous, next}}`。
- v1（`packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts` 106-145）：`before` 必须与 `limit` 同时出现，否则 400；要能 `MessageV2.cursor.decode`，否则 400；**`limit` 未给或为 0 时直接返回全量** `session.messages()`；给了 limit 走 `MessageV2.page`；有更旧页时写 `Link: <url?limit=..&before=..>; rel="next"` 与 `X-Next-Cursor`，并 `Access-Control-Expose-Headers: Link, X-Next-Cursor`。query schema `MessagesQuery`（`.../httpapi/groups/session.ts` 43-47）里 `before` 是裸 `Schema.String`，未做 cursor 形状校验——与评论指出的边界校验缺失一致。
- 存储：`packages/opencode/src/session/message-v2.ts` 的 `page()` 直接查 SQLite，`orderBy(desc(time_created), desc(id)).limit(limit + 1)`，用多取一条判断 `more`，`cursor = base64url({id, time})`，`items.reverse()` 后返回（wire 上时间升序、页内从旧到新）。
- SQLite 迁移：`packages/core/src/database/database.ts` 用 `@opencode-ai/effect-drizzle-sqlite`，设 `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`cache_size=-64000`、`foreign_keys=ON` 并 `DatabaseMigration.apply(db)`；`packages/core/src/database/schema.gen.ts` 有 `message`/`part`/`session_message` 表与索引 `message_session_time_created_id_idx (session_id, time_created, id)`——正是 `page()` 的排序键。
- **服务端仍会全量物化**：同一文件里的 `stream(sessionID)` 用 `size = 50` 循环 `page()` 直到 `!more`，把每页倒序 push 进一个 `result` 数组后整体返回。也就是说 opencode 的"分页"只约束客户端，服务端/模型侧仍有一处 O(n)。

**明确推迟与已承认失败模式。** 推迟：虚拟化（issue body、PR #6656 body、#6548 评论 Phase 2、"Scope: v1" 段落）。已承认：#6656 评论列出的五条（静默失败、Link 解析脆弱、无 cursor 边界校验、深翻页 N+1、翻页仍增长内存），以及 TUI 单页 + 100 条裁剪导致 undo/fork-from-history 不完整。另：#6548 在 2026-09-08 被 bot 以"60 天无活动"自动关闭，三个候选 PR 全部 closed 未合并；但 v1 handler 的 `Link`/`X-Next-Cursor` 与 v2 的 `message.list` 游标确实已经落在 dev 上。

---

## 6. 差异表与结论

下表中 codex 与 opencode 两列的事实来源 URL 见 §4、§5 的来源清单（均为实际抓取）；opencode 侧文件行号指抓取时刻的 `dev` 分支，codex 侧指抓取时刻的 `main` 分支。

| 轴 | XBotv2（`133d813` + 工作区未提交的身份改动） | codex | opencode |
| --- | --- | --- | --- |
| 身份来源 | 服务端 node_id：append 为 `str(record.position)`（`XBotv2/persistence/store.py:72`），replacement 为 `f"{position}:{index}"`（`:407`、`:595`）。wire 上 `SessionHistoryItem.id` 由 `message_identity()`（`input_id` → `xbot_message_id` → `tool_call_id`）兜底到 node_id——**工作区改动**；`133d813` 上 wire 无身份（`XBotv2/session/contracts.py:94-110`） | app-server 的 `ThreadItem.id` / `Turn.id`（`merge_older_turns` 按 `turn.id`，item 按 `item.id()`） | v1 `message.id` / `part.id`，SQLite 排序键 `(time_created, id)`；v2 `cursor = base64url({id, order, direction})` |
| 起步是否有界 | **否**。TUI 不带 `history_limit`（`XBotv2/tui/transport.py:190/252/443`）→ 全量 `history`；Web 带 160（`XBotv2/web/src/api/client.ts:160`） | **是**。5 turns + `HistoryLoadBudget` 内的 item 页（`INITIAL_HISTORY_TURN_LIMIT=5`） | TUI 单页 100 且裁到 100（`sync.tsx:603/625/626`）；app 首屏 20 条（`server-session.ts:30`） |
| 分页游标形式 | 不透明 base64 `[1, revision, offset]`（`XBotv2/core/history.py:394-396`），offset 是"从头数"的位置；`before: int` 只存在于 trajectory（`XBotv2/session/protocol.py:575`） | 服务端不透明 `next_cursor` / `backwards_cursor`，两级（turn 页 + item 页） | v1 `before = base64url({id, time})`；v2 `cursor = base64url({id, order, direction})`，双向 |
| 去重依据 | node_id ← `ConversationPage.node_ids` / `SessionHistoryItem.id`（工作区）；`133d813` 上无 | `item.id()` / `turn.id`，只在不存在时插入 | `message.id` / `part.id`（`tracker` + `search`/`Binary.search`） |
| prepend / 锚点处理 | Web：`before=windowAnchor` + `expectedAnchor` 不匹配即丢页（`XBotv2/web/src/state/useXBot.ts:858-877`）；TUI 无 prepend（整份替换 `XBotv2/tui/state.py:637-650`） | 插在 session header 之后；插入前修正 highlight/backtrack 索引，插入后 `history_loaded(inserted)` 冻结锚点 | app：`trajectory_prepend` + `sourceMode` 分叉（`server-session.ts:685`）；TUI 无 |
| "还有更早"如何表达 | 只能从 `next_cursor is None` 反推（`XBotv2/core/history.py:390`；`XBotv2/persistence/store.py:516`）；`newest_position` 只给尾部位置 | `has_older_history()` = `next_item_cursor.is_some()`；渲染为 `TranscriptHistoryState::Partial` + `"Earlier messages available."`，加载中 shimmer，失败 retry | v1 `Link: rel="next"` + `X-Next-Cursor`；v2 `cursor.previous/next`；app `history.more()`；TUI 无 |
| 分页失败如何呈现 | `invalid_cursor` → HTTP 400（`XBotv2/server/http.py:208-228`）；Web 收到后重建 session baseline（`useXBot.ts:871-877`）；TUI 把 `rejected_frame` 当可见错误但游标照常推进（`XBotv2/tui/transport.py:413-419`） | `TranscriptHistoryState::Failed` + footer `"Retry history: ctrl+home"`，不自动重试；线程已切走则静默 cancel | TUI 提案被指"静默失败"；app 只有 `complete`/`loading`，无专门错误 UI |
| 内存增长曲线 | 服务端 O(n) 常驻（surface + transcript + records + 磁盘 `messages.jsonl`）；TUI 客户端 O(n) 且永久保留 timeline | 服务端 SQLite/rollout 按需读；TUI cells 随"用户向上翻"增长，初始 O(1) | 服务端仍有一处 O(n)（`MessageV2.stream` 全量循环）；TUI 恒定 ≤100 条；app 随翻页增长 |
| 退役/压缩机制 | compact 只退役 surface（`preserve_transcript=True`，`XBotv2/compact/service.py:388-394`；`XBotv2/persistence/store.py:108-114`），transcript/records/磁盘不退役 | turn/item 分页 + SQLite 投影；rollout 是 append-only 源，app-server 无需在内存保留全部 | 服务端 SQLite 持久化 + compaction（`packages/opencode/src/session/compaction.ts` 存在，**未读其内容，未验证**）；客户端窗口与 compaction 解耦 |
| 客户端是否会以为历史完整 | **会**。TUI 用最近 160 条 transcript 覆盖 timeline 且丢掉 `history_cursor`（`XBotv2/session/runtime.py:137`；`XBotv2/tui/state.py:373-374`、`:637-650`）；Web 用 `atTail`/`windowAnchor`/`pendingNewer` 显式区分窗口与尾（`XBotv2/web/src/state/runtime.ts:133-138`） | **不会**：`Partial`/`Complete`/`Failed` 是显式状态，`Complete` 仅在 `!has_older_history` 时置位 | TUI：**会且更糟**（裁到 100 后旧页永久不可达）；app：`complete`/`cursor` 有显式语义 |

### 结论

1. **服务端已有可用分页原语，但身份在 `133d813` 上不完整，缺的是 wire 身份与"窗口头锚点"。** 要给 TUI 做窗口化，最小依赖面是 `trajectory`：它 append-only、position 单调、游标与内容无关（`XBotv2/persistence/store.py:500`）、自带 `newest_position`（`XBotv2/core/history.py:77-79`）与 `before` 重锚（`XBotv2/persistence/store.py:488-497`），且 Web 客户端已在生产路径上这样用（`XBotv2/web/src/state/useXBot.ts:858-863`）。**（推荐）**
2. **窗口化客户端必须同时改 `history_updated` 的形状。** 今天是"最近 160 条 transcript 全量替换 + 一个被客户端丢弃的 cursor"（`XBotv2/session/runtime.py:137`；`XBotv2/tui/state.py:373-374`），窗口化客户端会把它当成完整历史。至少需要：带身份（工作区已加 `item.id`）、显式的"还有更早"信号、以及"该帧只是窗口而非全量"的语义区分。**（推荐）**
3. **不要假设窗口化会让服务端内存有界。** compact 对 transcript 与 trajectory 完全不退役（`XBotv2/persistence/store.py:108-114`、`:206-214`），`page_*` 是只读切片（`:454`、`:465`、`:481`），`_TrajectoryState.records` 必须全量在内存才能定位 `newest_position`/`before`（`:498-517`）。服务端内存当前的界只有 idle reaper（`XBotv2/session/manager.py:93`、`:155-172`）与进程级 LRU 缓存（`XBotv2/persistence/store.py:247-248`）。
4. **任务给出的假设中，被代码直接反驳的有四处：**
   - "compact 负责压缩和退役"——只对 surface 成立；transcript、trajectory records、磁盘都不退役。
   - "客户端的 history 就是会话历史"——`message_page(limit=None)` 读 surface，`message_page(limit=N)` 读 transcript（`XBotv2/session/manager.py:891-899`），压缩后这是两套不同内容；而 surface 在协议上根本不可分页（`MessageHistoryStore.page` 无调用者）。
   - "the client never passes a page limit"——只对 TUI 成立；Web 固定 `history_limit: 160`（`XBotv2/web/src/api/client.ts:160`、`:218`、`:353`），HTTP 层还有对应集成测试（`XBotv2/tests/integration/test_http_transport.py:411-440`）。
   - "no route for older pages of an active session"——`/messages` 与 `/trajectory` 都通过磁盘读活跃会话（`XBotv2/session/manager.py:897`、`:918`），活跃 runtime 的每次写入都经 sink 落到 `messages.jsonl`。真正的缺口是：persistence 未装载时（`XBotv2/application/app.py:99-110`）这两条路径不可用，以及 surface 不可分页。
5. **codex 与 opencode 都证明"有界初始水合"和"客户端总内存有界"是两件事**：codex 的 `transcript_cells` 只增不减（`app.rs`；截断只发生在回滚路径 `app/event_dispatch.rs`），opencode 的 TUI 恒定 ≤100 条但旧页永久丢失（`sync.tsx:603/625/626`），opencode 的 app 随翻页增长。要总内存有界必须有客户端驱逐策略 + 用 position/id 重锚，而"保留窗口最旧 position 作为 `before`"是这批源码里唯一落地的做法——我们已有等价的 `before`（`XBotv2/persistence/store.py:488-497`）。**（推荐）**
6. **未验证项**：codex 的 snapshot 文件 `codex_tui__pager_overlay__tests__transcript_overlay_paginated_history_states.snap` 内容（未抓取）；codex issue #21635/#22068 中的性能与体积数据（属 issue 断言，未独立复现）；opencode PR #8535 与实际 commit 内容（PR closed 未合并，只读到 body 与评论）；opencode v2 `SessionV2.messages` 内部是否还有第二处全量物化（未读 `packages/core/src/session` 实现）；opencode `packages/opencode/src/session/compaction.ts` 的具体退役行为（未读）。这些位置一律以上文标注的"issue/PR 声称"为准，不当作代码结论。
