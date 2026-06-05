# XBotv2 Code Review Report — TUI & Engine

## Scope
- `XBotv2/xbotv2/tui/` (12 files, 3,042 lines)
- `XBotv2/xbotv2/core/` (engine.py: 1,196 lines, bootstrap.py: 304 lines)
- `XBotv2/xbotv2/protocol/` (dispatcher.py: 358 lines, http_server.py: 378 lines)
- `XBotv2/xbotv2/tools/runtime.py` (803 lines)

---

## 1. 架构概况

```
┌─────────────────────────────────────────────────────┐
│ TUI (textual_client.py, 1265 行, 71 方法)            │
│   ├─ XBotTextualApp         主 App                  │
│   ├─ ComposerTextArea       输入区                  │
│   ├─ TranscriptScroll       转录区                  │
│   ├─ CommandPalette          Ctrl+P 模态            │
│   ├─ CompletionPopup         Tab 补全面板            │
│   └─ 状态机: TuiState (client.py, 540 行)           │
├─────────────────────────────────────────────────────┤
│ Transport (transport.py 88 行 + transport_http.py 246 行) │
│   └─ HttpTransport ←→ SSE over HTTP                 │
├─────────────────────────────────────────────────────┤
│ TerminalSession (terminal.py, 199 行)               │
│   └─ 薄封装: session 持有 transport，拦截 live 交互  │
├─────────────────────────────────────────────────────┤
│ Dispatcher (dispatcher.py, 358 行)                  │
│   ├─ SessionManager: 会话生命周期                   │
│   ├─ SessionContext: 每轮 turn 协调                 │
│   ├─ _drain_engine_into_bus: 引擎→事件总线          │
│   └─ live_interaction_sink: 实时交互桥接            │
├─────────────────────────────────────────────────────┤
│ Engine (engine.py, 1196 行)                        │
│   ├─ run_turn / _run_turn_impl: ReAct 循环          │
│   ├─ Hook 系统: 28 个 hook stage                   │
│   ├─ 工具链调度 → runtime.py (803 行)               │
│   └─ 消息持久化 → persistence/store.py              │
├─────────────────────────────────────────────────────┤
│ HTTP Server (http_server.py, 378 行)               │
│   ├─ 8 个 endpoint (1 SSE + 7 REST)                │
│   └─ sse_stream(): SSE 帧生成器                    │
└─────────────────────────────────────────────────────┘
```

**层级数量**: TUI → Session → Transport → HTTP → Dispatcher → Engine → Tools → LLM
**总代码量**: ~7,000 行（不含协议帧定义和持久化层）

---

## 2. 重点发现 (按优先级)

### P0 — 会引发线上事故

#### 2.1 LLM 调用无超时保护 — `engine.py:602`

```python
response = await llm_with_tools.ainvoke(context_messages)
```

无 `asyncio.wait_for` 包裹。若 LLM provider 挂死（网络分区、限流卡顿），整个引擎永久堵塞，且无法通过 ESC 取消（the `CancelledError` 根本进不去，因为卡在 LLM 的 await 上）。

**建议**: 加 `asyncio.wait_for(..., timeout=_)`（如 120s），超时 yield error 事件，关闭该 turn。

#### 2.2 重复的 `turn_cancelled` 事件 — `engine.py:208` + `dispatcher.py:295`

Engine 在 CancelledError handler 里 yield 了一次 `turn_cancelled`，Dispatcher 的 drain 在 except CancelledError 里又注入了一个 `turn_cancelled`。SSE 消费者收到**两个**同名事件，可能导致 TUI 状态机重复触发。

**建议**: 要么只在 Engine 侧产出，Dispatcher 只转发不合成；要么只在 Dispatcher 侧合成，Engine 不产出。

#### 2.3 `InteractionDisconnected` 路径未清理孤儿 tool_calls — `engine.py:225`

```python
except InteractionDisconnected:
    await self._save_messages()  # ← 未调 _backtrack_orphan_tool_calls()
```

`CancelledError` handler (L206) 已加了 `_backtrack_orphan_tool_calls()`，但 `InteractionDisconnected` handler **没有**。如果断连发生在 AIMessage(tool_calls) 已 append 但 tool_messages 未 extend 之间，orphan AIMessage 写入磁盘，下次 turn LLM 400。这就是用户报告的 error。

**建议**: `InteractionDisconnected` 也调 `_backtrack_orphan_tool_calls()`，或把清理逻辑提到 `run_turn` 的 `finally` 块一次到位。

#### 2.4 `request_interrupt()` 存在 TOCTOU 竞态 — `dispatcher.py:84-91`

```python
def request_interrupt(self) -> bool:
    if self.turn_task is None or self.turn_task.done():  # T1
        return False
    self.turn_task.cancel()  # T2 — turn_task 可能在 T1→T2 间被设为 None
```

`run_turn_stream` 的 finally 块在 L342 做 `ctx.turn_task = None`。如果这个赋值发生在 T1 通过、T2 之前，则 `self.turn_task.cancel()` 是 `AttributeError`。

**建议**: 用局部变量快照：
```python
task = self.turn_task
if task is None or task.done():
    return False
task.cancel()
return True
```

#### 2.5 工具执行超时后线程泄漏 — `runtime.py:778-791`

```python
return await asyncio.wait_for(_runner(), timeout=60.0)
```

`_runner()` 内部用 `asyncio.to_thread` 执行同步工具。`wait_for` 超时后抛出 `TimeoutError`，但后台线程**继续运行**——Python 线程无法强制 kill。工具跑路（比如 shell 里卡了 `sleep 999`）线程永远不会退出，积累。

**建议**: `to_thread` 不能直接超时。正确的做法是让工具本身支持取消（通过 `subprocess.run(timeout=N)` 的 `timeout` 参数），或者接受线程泄漏并加监控（当前策略）。

---

### P1 — 影响可维护性/正确性，但不会立刻爆炸

#### 2.6 TUI 主文件过于庞大 — `textual_client.py` (1,265 行, 71 方法)

这是整个代码库最大的单文件。内容包含：App 生命周期、worker 调度、slash command dispatch、活动行管理、内联选择渲染、输入历史、状态栏刷新、CSS 定义、widget 工厂函数。所有内容在一个 class 里。

**建议**: 拆分：
- `widgets.py` — 所有 `_*_widget()` / `_entry_widget()` / `_render_text()` / `_spinner()`
- `activity.py` — `_tick_activity()` / `_update_activity()` / `_append_activity()` / `_finalize_activity()` / 计时器
- `choices.py` — `_choice_mode_active()` / `select_*_choice()` / `confirm_active_choice()` / `_request_widget()` / `_notice_widget()` 等
- `composer.py` — `ComposerTextArea` + `action_clear_input` + `history_*` + `_resize_composer`
- `transcript.py` — `TranscriptScroll` + `_render_new_transcript_entries` + `_widget_for_entry`
- 主 App 类保留 wire-up（`compose` / `on_mount` / `submit_composer` / BINDINGS）

#### 2.7 CSS 重复定义 — `textual_client.py:96-146`

同一文件里 `#transcript`、`.entry`、`.meta`、`.body` 各定义了**两次**，且第一次和第二次的规则不完全相同（第二次缺少 `height: auto; width: 1fr`）。

**建议**: 删除重复块，合并为一份。同时把硬编码的色值（`#0f1115`, `#171a21`, `#d6dae2`, `#7aa2f7`）提取为文件级常量（已有 `tokens.py` 占位但未实际使用？如果 `tokens.py` 已是命名颜色，就用它）。

#### 2.8 配置参数在 4 个类中重复 — `data_dir/personality_id/provider_name/session_id/thread_id/no_plugins`

`TextualTuiClient.__init__`、`XBotTextualApp.__init__`、`TerminalSession.__init__`、`CursesTuiClient.__init__` 四个构造函数都有完全相同的参数签名（7 个参数）。每个类各存一份副本。

**建议**: 定义 `SessionConfig` dataclass，一次构建，传给所有类。减少参数传递和脑负荷。

#### 2.9 无保护的 `query_one` 调用 — 8 处

8 个 DOM 查询没有 try/except 也没有 is_mounted guard（见前文 TUI 报告 §3.2）。teardown 时或 widget 被 remove 后会抛异常。

**建议**: 统一包装一个 `_safe_query` 方法：
```python
def _safe_query(self, selector, expect_type=None):
    if not self.is_mounted:
        return None
    try:
        return self.query_one(selector, expect_type) if expect_type else self.query_one(selector)
    except NoMatches:
        return None
```

#### 2.10 错误格式不统一 — REST vs SSE vs Engine

| 来源 | 形状 |
|------|------|
| REST 错误 (HttpServerError) | `{"code": str, "message": str}` 顶层 |
| SSE 错误 (sse_stream catch) | `{"type": "error", "data": {"code": "engine_busy", "message": str}}` |
| Engine 错误 (run_turn) | `{"type": "error", "data": {"code": "BadRequestError", "message": str}}` |
| Dispatcher 错误 (drain) | `{"type": "error", "data": {"code": "turn_failed", "message": str}}` |

code 命名不统一：`engine_busy` / `stream_failed` / `turn_failed` / `BadRequestError`。TUI 要处理四种风格。

**建议**: 统一 error 格式：
```python
{"type": "error", "data": {"code": "ENGINE_ERROR", "sub_code": "...", "message": "..."}}
```
所有 error code 走常量枚举。

#### 2.11 `live_interaction_sink` 访问 engine 私有属性 — `dispatcher.py:213,215`

Sink 直接读 `ctx.engine._permission_waiter` / `ctx.engine._user_input_waiter`。Engine 已有公开方法 `submit_user_input` / `submit_permission_response`，sink 用私有属性是为了实现"同时监听 disconnect 和 waiter"的 race 逻辑。然而这样破坏封装——waiter 是 Engine 内部细节。

**建议**: Engine 提供 `create_interaction_future(request_id, timeout)` 返回一个 `asyncio.Task`，sink 不需要知道内部 waiter 结构。

#### 2.12 `SessionInfo` 重复定义 — `core/state.py:13` 和 `hooks/types.py:114`

两个完全相同的 dataclass，各存一份。如果一边加字段另一边不同步，hook 上下文和引擎核心的 session 信息就不一致。

**建议**: 删除 `hooks/types.py` 里的定义，统一 import `from xbotv2.core.state import SessionInfo`。

#### 2.13 Engine `_run_turn_impl` 有 19 个早期退出点

`_run_turn_impl` 里 `break`、`return`、hook 触发 `turn_complete=True` 等 exit 点共计 19 处。控制流图非常复杂（每一个 hook short-circuit 都是一个新的 exit branch），导致测试覆盖不全，行为隐晦。

**建议**: 不急于改——这是 ReAct 循环 + Hook 系统的固有复杂度。但应在 `turn_finished` yield 之前加 assert 验证消息状态一致性（至少没有 orphan tool_calls）。

---

### P2 — 代码清爽度，建议修复

#### 2.14 死代码 — 9 个符号

| 符号 | 位置 | 说明 |
|------|------|------|
| `_status_badge()` | textual_client.py:1044 | 从未被调用 |
| `render_transcript_entry()` | textual_state.py:43 | 从未被调用（TUI 用 `_widget_for_entry` 代替） |
| `apply_frame()` | client.py:102 | 从未被调用（只用 `apply_event`） |
| `MODE_BADGE` | mode.py:30 | 从未 import |
| `ProtocolEncoder` 中 `status`/`session_ready`/`hello_ok`/`shutdown_ok` 四种 frame | frames.py | SSE 路径不使用 ProtocolEncoder |

**建议**: 删除。死代码占用认知负荷，且 grep 时会误导。

#### 2.15 `_notice_label()` 和 `_notice_title()` 语义重复

- `client.py:_notice_label()` — 给 CursesTuiClient 用
- `textual_client.py:_notice_title()` — 给 XBotTextualApp 用

两者都做"notice kind → 可读标签"的映射，但 Curses 和 Textual 两套文字的文案不完全一致。

**建议**: 合并到 `command.py` 或新建 `notice_ui.py`，统一映射表，两个客户端共享。

#### 2.16 CSS 色值硬编码在 3 个文件里

`textual_client.py`、`command_palette.py`、`completion_popup.py` 各自硬编码 `#171a21`, `#0f1115`, `#d6dae2`, `#7aa2f7` 等色值。

**建议**: 检查 `tokens.py` 是否已有这些颜色常量。如有，直接引用；如没有，补充。

#### 2.17 completion/palette 的渲染逻辑重复

`completion_popup.py:_rebuild_rows()` 和 `command_palette.py:_refresh_results()` 做几乎一样的事：对 matches 遍历、创建 Static、标 active class。只有"重建所有 vs 重新标 class"的细微差异。

**建议**: 提取公共组件 `_render_match_list(container, matches, selected)`。

#### 2.18 8 个 Protocol 方法，但 interrupt 的 docstring 描述与实现不一致

`transport.py:81-85` 说 interrupt 返回后 "the in-flight send_message async iterator will close on the next event boundary"，实际上 interrupt 只负责发 POST，cancel 是服务器侧做的，闭流也由服务器侧完成。如果服务器没及时响应，客户端可能永远等不到流关闭。

**建议**: 修正 docstring 或加客户端超时。

#### 2.19 配置文件无 validation wrapper — `bootstrap.py`

`load_agent_config` / `load_provider_config` / `load_session_policy` 任何一步 YAML 畸形就直接 crash。无结构化错误报告，无部分降级。

**建议**: 用 `try/except Exception as e` 包裹每个 config load，将错误转为 `BootstrapError` 带上 `path:line` 信息再抛出。

#### 2.20 Workspace 创建无用目录 — `workspace.py`

`SessionWorkspace.ensure()` 无条件创建 `files/` 和 `tmp/` 目录，即使 session 全程不用 filesystem 工具。浪费文件系统条目，不影响功能。

**建议**: 惰性创建——在第一次 filesystem 工具调用时才 mkdir。

---

## 3. 精简路线图 (优先级排序)

### Phase 1 — 修 Bug（不改变架构，只修代码）

| # | 项 | 文件 | 行 | 工作量 |
|---|-----|------|----|--------|
| 1 | LLM 调用加超时 | engine.py:602 | 1 | 10 min |
| 2 | `InteractionDisconnected` 加 orphan 清理 | engine.py:225 | 1 | 5 min |
| 3 | `request_interrupt` TOCTOU 竞态 | dispatcher.py:84-91 | 3 | 5 min |
| 4 | 删除重复 CSS 块 | textual_client.py:96-146 | ~30 行删除 | 5 min |
| 5 | 补 8 处无保护 query_one | textual_client.py:8处 | 8 | 15 min |
| 6 | `turn_cancelled` 去重 | engine.py:208 / dispatcher.py:295 | 3 | 15 min |

### Phase 2 — 清理死代码 + 统一接口

| # | 项 | 行数影响 | 工作量 |
|---|-----|----------|--------|
| 7 | 删除 9 个死代码符号 | -90 行 | 15 min |
| 8 | `SessionConfig` dataclass 消灭参数重复 | -30 行 net | 30 min |
| 9 | 合并 `_notice_label` / `_notice_title` | -20 行 | 15 min |
| 10 | 统一 error code 命名 | ±20 行 | 30 min |
| 11 | CSS 色值引用 tokens.py | ±15 行 | 15 min |
| 12 | 合并 `SessionInfo` 定义 | -15 行 (hooks 侧) | 10 min |

### Phase 3 — 架构精简（可选，影响测试较多）

| # | 项 | 行数影响 | 工作量 |
|---|-----|----------|--------|
| 13 | 拆分 `textual_client.py` 为 5 个模块 | 只移行，不改变 | 2h |
| 14 | 提取公共 match-list 组件 | -40 行 | 30 min |
| 15 | `live_interaction_sink` 用公开 API 替代私有访问 | ±30 行 | 1h |
| 16 | Engine `_run_turn_impl` exit points 文档化 + assert | +10 行 | 30 min |
| 17 | 惰性创建 workspace 子目录 | -5 行 bootstrap | 10 min |

---

## 4. 统计数据

| 指标 | 值 |
|------|-----|
| TUI 总行数 | 3,042 |
| 最大文件 | textual_client.py (1,265) |
| Engine 总行数 | 1,196 |
| 协议层总行数 | 358 (dispatcher) + 378 (http_server) |
| 工具运行时 | 803 |
| Hook stage 数量 | 28 |
| 引擎早期退出点 | 19 |
| TUI widget query | 20 处 (8 处无保护) |
| run_worker 调用 | 4 处 |
| _save_messages 调用 | 6 处 |
| 死代码符号 | 9 处 |
| CSS 重复块 | 2 个 (文件内) |
| 色值硬编码文件 | 3 个 |
| 参数重复的类 | 4 个类, 相同 7 参数 |
| 测试总数 | 345 |

---

## 5. 结论

**TUI 侧**最大的问题是 `textual_client.py` 过长且内部职责混杂——本来可以拆成 5 个模块的代码全部挤在一个类里，后续每加一个功能（completion/palette/interrupt）就往里堆 100 行。CSS 色值硬编码在 3 个文件里，一旦要改主题就三处同步。

**Engine 侧**最大的问题是 LLM 调用无超时——这个在生产环境会直接卡死。其次 `InteractionDisconnected` 路径缺 orphan tool_calls 清理，正是用户报告的那个 400 error。Dispatcher 的 TOCTOU 竞态概率低但后果是 AttributeError，加了局部变量快照一行就修掉。

整体架构分层合理（TUI → Transport → Dispatcher → Engine），跨层事件流清晰。Phase 1 的 6 个 bug 都只需改 1-3 行且不碰测试。Phase 2 的清理去掉 ~200 行死/重复代码。Phase 3 是审美优化，建议在功能稳定后做。
