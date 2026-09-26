# TUI 重写规范(先于实现)

> **已归档的施工草案，不是当前实现规范。** 本文记录早期重写分支的计划和
> 基线；不要把其中的分支、路径、禁令或待办项当作当前状态。当前 TUI 已由
> `XBotv2/tui/` 实现，行为摘要见 [clients.md](../clients.md)，细节以当前
> 源码和 `XBotv2/tests/tui/` 为准。

本文件是 `dev-tui-rewrite` 分支的施工契约。所有实现与测试必须服从本文件的模块边界与不变式。
分析依据见 `tui-remediation-analysis.md`。**旧 TUI 的这三条必须消失**:本地"运行中"信念、索引键 + 淘汰计数、静默降级。

## 0.0 基线:干净提交

- worktree:`/home/shefrin/repo/XBot/.worktrees/tui-rewrite`
- 分支:`dev-tui-rewrite`
- **基线提交:`59244f4`**(分支 tip,工作树干净、零本地改动;不叠加任何未提交内容)
- 本分支**不产生 commit**

## 0.1 范围:整包删除,零移植

"从头写"在本分支的判定标准是**可机械检查的**:

- `XBotv2/tui/` 下**不存在**任何旧文件;旧实现不重命名、不搬迁、不包装、不兼容。
- 新代码**不得**出现"移植自旧 xx"的注释或形状;每个模块的设计理由必须能独立陈述。
- 旧测试**全部删除**;新测试不得复用旧断言。

已删除(基线提交上执行,全部为文件删除,无 commit):

| 删除 | 行数(基线 59244f4) | 原因 |
|---|---|---|
| `tui/client.py` | 1064 | 语义状态与渲染簿记混合体,索引键 + 淘汰计数 |
| `tui/textual_client.py` | 2824 | 事件循环/分页/状态写入点耦合 |
| `tui/textual_widgets.py` | 1905 | 窗口四重簿记 + 计数器反推 |
| `tui/terminal.py` | 455 | 由新 `transport.py` 取代 |
| `tui/command.py` / `command_palette.py` / `completion_popup.py` / `selection.py` | 547 | 全部重写 |
| `tui/textual_theme.py` | 308 | CSS 全部重写 |
| `tui/session_config.py` / `trace.py` | 60 | 全部重写 |
| `tests/core/test_command.py` | (基线版) | 整文件删除,覆盖迁入 `tests/tui/test_commands.py` |
| `tests/core/test_tui_client.py` | (基线版) | 整文件删除 |
| `tests/integration/test_tui_interaction.py` | (基线版) | 整文件删除 |
| `tests/integration/test_tui_interrupt_and_usage.py` | (基线版) | 整文件删除 |
| `tests/bench/test_tui_event_throughput.py` | (基线版) | 整文件删除 |

合计删除 16 个文件(11 个模块 + 5 个测试文件)。删除后台账可用
`git status --short | grep -c '^ D'` 复核(应为 16)。

> 注意:缺陷分析针对的是主工作区**未提交**的较新 TUI(7963 行);基线提交上的 TUI 是
> 较早的版本(7646 行)。两者都被删除,因此差异不影响重写;但分析文档中的行号引用属于
> 较新版本,核对旧代码时需以行为为准而非行号。

**连带消费者**(旧包被外部 import,必须一并改到新模块,属于本次工作范围):

- `tests/integration/test_http_transport.py`:`TerminalSession`(70)、`tui.command`(866)、`XBotTextualApp`(1823/5065/5180)
- `tests/core/test_client.py`:`TerminalSession`(9)
- `tests/core/test_command.py`:`CommandRegistry`(7)
- `XBotv2/main.py`:`TextualTuiClient`(274)

在上述文件改到新模块之前,仓级测试套件预期为红;新 TUI 套件(`XBotv2/tests/tui/`)必须始终绿。



## 0. 不变式(每一条都要有可证伪的测试)

| # | 不变式 | 消除的历史缺陷 |
|---|---|---|
| I1 | 服务端 `turn_status` 是"是否在跑"的唯一权威;客户端本地标志只用于"终态帧还没到"的过渡 | running 显示 Ready |
| I2 | 已提交的 timeline 条目内容,除非用**同一 id** upsert,否则永不改变 | 翻页时流式内容覆盖历史 |
| I3 | 窗口位置只由"渲染哪些 id"决定;条目进入状态与窗口位置无关,永不丢弃 | live 消息静默消失 |
| I4 | 任何错误要么可见,要么是显式状态;禁止"只写日志继续" | 静默失败文化 |
| I5 | 渲染不得反压传输(允许合并/丢弃可合并帧并记录 gap,不允许阻塞读取) | 渲染积压 → 游标过期 → 状态丢失 |
| I6 | 流式内容永远是 timeline 的**后缀**;用户插话(steer)必须先提交当前后缀 | steer 顺序反转 |
| I7 | 状态是纯函数 `derive(facts)` 的结果;没有任何模块直接写状态字段 | 19 处状态写入点 |
| I8 | 组件依赖单向:`app → view → state`;`state`/`status`/`timeline` 不 import textual | 语义与渲染耦合 |

## 1. 模块边界

规划时的树与最终落地的树有出入(计划里的 `selection.py`/`palette.py`/`completion.py`/`blocks.py`/`thread.py`
落在别处、或由别的设计取代)。**以下是实际落地**:

```
XBotv2/tui/
  status.py       StatusFacts + Status + derive()。纯函数,无 IO 无 DOM。
  timeline.py     Entry 类型 + Timeline(OrderedDict by id)+ TimelineWindow。纯数据。
  events.py       内部 UiEvent 联合。所有输入(网络/用户/定时器/看门狗)统一成它,
                  并携带产生方的 pydantic 模型(复用规则 §6.0)。
  protocol.py     wire 信封:解析 + 校验 + 类型化。**唯一校验 payload 的模块**
                  (`model_validate` 只出现在这里;`events` 按设计持有 payload 模型,
                  `transport` 为本地失败构造 error payload)。
  state.py        SessionState + reduce(state, event) -> state。纯函数。
  transport.py    TransportSession:SSE 读取、sequence 缺口、重连、看门狗、提交/中断/切换。
  controller.py   装配 transport + state + view;唯一的事件分发点;构建视图模型。
  commands.py     命令注册表 + 客户端命令目录(内置命令只登记能执行的)。
  attachments.py  本地图片 → wire `ImageInput`(纯判定 + 编码)。
  theme.py        screen 级 layout CSS(widget 自己的样式留在 widget 的 DEFAULT_CSS)。
  app.py          TuiApp:Textual 接线、按键、布局、命令分发;业务判断为 0。
  view/
    plan.py         纯窗口计划(plan_window/older_anchor/newer_anchor)
    entries.py      Entry → widget;header/body/reasoning 为纯函数;BlockVisibility
    transcript.py   TranscriptView:按 id diff 挂载 + 独立流式尾区 + 锚点
    status_bar.py   状态行渲染(纯函数 + 一个 Static 包装)
    jobs.py         任务面板(按 job_id 对账)
    queue.py        排队输入面板(按 message_id 对账)
    composer.py     输入区(ComposerModel + 纯 hint/placeholder/delivery)
    completion.py   补全弹窗
    palette.py      命令面板
    selection.py    模态选择器
```

**依赖方向**:`app → controller → (transport, state, view)`;`controller → view`
的**模型**(`ComposerModel`/`StatusLine`,控制器负责构建);`view → timeline/state/status`(只读);
`state/status/timeline/events/protocol/commands` 不 import textual、不 import 任何上层。

I7/I8 由 `tests/tui/test_layering.py` 机械强制:AST 检查 import 方向(含"TUI 包内一律绝对 import")、
**子进程探针**确认 import 状态内核不会拉入 textual、"任何模块都不得 `except: pass`"、
以及"`facts`/`timeline`/`stream_entry_id` 等 reducer 拥有的字段只允许 `state.py` 赋值"。
每一条都用注入违规代码的变异确认过会失败(11 项,全部被杀)。

**逐个不变式的可证伪性**(每个 I 一次定向变异,全部被现有测试杀死):

| 不变式 | 变异 | 抓住它的测试 |
|---|---|---|
| I1 | `derive()` 不再看 `server_turn`,只看本地 `turn_open` | `test_status` / `test_state` / `test_transport` |
| I2 | `Timeline.upsert` 每次生成新 key 而不是按 id 覆盖 | `test_timeline` / `test_state` / `test_view_transcript` |
| I3 | `Timeline.window` 顺手淘汰窗口外的条目 | `test_timeline` / `test_view_plan` / `test_view_transcript` |
| I4 | 看门狗失败只累加计数、不发可见错误 | `test_transport` / `test_app` |
| I5 | 跳过"陈旧/重复帧"的判断被去掉 | `test_transport` |
| I6 | `UserMessagePublished` 不再先 `_close_stream` | `test_state` |
| I7 | controller 直接写 `self.state.facts` | `test_layering` |
| I8 | `view/jobs.py` import controller | `test_layering` |

I7 的变异第一次**存活**了:守卫只认 `state.facts` 这种形状,而最可能的真实违规是
`self.state.facts = ...`。把守卫扩到属性链(`_attribute_chain`)后立刻被杀死——又一次
"变异存活先怀疑守卫"。

## 2. status.py 契约

```python
class Connection(Enum): CONNECTING, CONNECTED, DISCONNECTED
class ServerTurn(Enum): UNKNOWN, IDLE, RUNNING
class Interaction(Enum): NONE, PERMISSION, USER_INPUT
class Interrupt(Enum): NONE, REQUESTED

@dataclass(frozen=True)
class StatusFacts:
    connection: Connection = Connection.CONNECTING
    server_turn: ServerTurn = ServerTurn.UNKNOWN
    server_turn_age: float = 0.0     # 距最近一次权威读数的秒数
    turn_open: bool = False          # turn_started..turn_finished
    interaction: Interaction = Interaction.NONE
    compaction: bool = False
    interrupt: Interrupt = Interrupt.NONE
    jobs_running: int = 0
    last_error: str | None = None

class Status(Enum):
    CONNECTING, DISCONNECTED, READY, RUNNING, WAITING_USER,
    APPROVAL_REQUIRED, COMPACTING, INTERRUPTING, ERROR

def derive(facts: StatusFacts) -> Status
def status_label(status: Status, facts: StatusFacts) -> str   # 人类可读,含任务数
```

**判定顺序**(先命中先返回):

1. `connection is CONNECTING` → `CONNECTING`
2. `connection is DISCONNECTED` → `DISCONNECTED`
3. `interrupt is REQUESTED` → `INTERRUPTING`
4. `interaction is PERMISSION` → `APPROVAL_REQUIRED`
5. `interaction is USER_INPUT` → `WAITING_USER`
6. `compaction` → `COMPACTING`
7. `server_turn is RUNNING or turn_open` → `RUNNING`
8. `last_error is not None` → `ERROR`
9. → `READY`

第 7 条是 I1 的落点:**服务端说在跑,就一定显示在跑**,与本地 `turn_open` 无关。
第 7 条后半 `or turn_open` 让"终态帧尚未到达"时不回到 Ready。
`last_error` 必须在 `turn_started` 或新的权威读数处被清空(否则会粘住,即旧实现的 P1-9)。

**`turn_open` 的清零责任在 reducer,不在 derive**:只有"权威 IDLE"(快照或看门狗)或 `turn_finished`/`turn_cancelled` 才清零。

`status_label` 必须在 `jobs_running > 0` 时体现任务数(消除"状态栏与 Tasks 面板各说各话")。
复制反馈、一次性提示等瞬时消息**不进 status**,走独立 notice 通道。

## 3. timeline.py 契约

```python
class EntryKind(Enum): USER, ASSISTANT, REASONING, TOOL, NOTICE, ERROR, ACTIVITY
class Delivery(Enum): PENDING, ACCEPTED, FAILED
class ToolStatus(Enum): PENDING, RUNNING, COMPLETED, FAILED, DENIED, CANCELLED

@dataclass(frozen=True)
class Entry:                 # 所有子类共同字段
    id: str                  # 稳定主键:服务端 message_id / tool_call_id,或 local:<uuid>
    seq: int                 # 本地单调序号,仅用于同 id 冲突与稳定排序
    kind: EntryKind

@dataclass(frozen=True)
class UserEntry(Entry):      content: str; delivery: Delivery
@dataclass(frozen=True)
class AssistantEntry(Entry): content: str; reasoning: str; streaming: bool
@dataclass(frozen=True)
class ToolEntry(Entry):      name: str; args: Mapping[str, Any]; status: ToolStatus
                             result: str; started_at: float; finished_at: float
@dataclass(frozen=True)
class NoticeEntry(Entry):    notice_kind: str; text: str; detail: str
@dataclass(frozen=True)
class ErrorEntry(Entry):     message: str
```

`Timeline`:
- `upsert(entry)`:同 id → **原位替换**(顺序不变);新 id → 追加。
- `remove(entry_id) -> bool`。
- `ids() -> tuple[str, ...]`、`get(id)`、`__len__`。
- `tail_id() -> str | None`。
- `window(size: int, end: str | None = None) -> TimelineWindow`:`end=None` 表示尾部窗口。
- `TimelineWindow`:`.ids`、`.start_index`、`.end_index`、`.newer_count`、`.at_tail`(属性)、`.total`。
  (计划里叫 `pending_newer`,落地为 `newer_count`:它数的是"窗口下方还有多少条",不是"丢了多少条"。)
- 容量:**没有容量 API**。计划里的 `Timeline(max_entries)` / `trim_front(n)` 从未实现出调用者,已在第 14 轮
  连同其测试一起删除——不保留"看起来有界"的死代码。淘汰这件事的结论与理由见 §5.16.1
  (历史窗口化必须以 trajectory 面的稳定身份为地基,而不是给 timeline 加一个上限)。
- **禁止**:`TuiTranscriptEntry` 式索引键、`evicted_*` / `inserted_*` 计数器。

## 4. events.py 契约

```python
@dataclass(frozen=True)
class Connected:        session: SessionSnapshot
@dataclass(frozen=True)
class Disconnected:     reason: str
@dataclass(frozen=True)
class Frame:            event: WireEvent                 # 已校验的信封
@dataclass(frozen=True)
class StreamGap:        expected: int; received: int     # 必须产生用户可见提示
@dataclass(frozen=True)
class SubmitAccepted:   input_id: str; message_id: str; content: str
@dataclass(frozen=True)
class SubmitFailed:     input_id: str; error: str
@dataclass(frozen=True)
class InterruptAsked:   ()
@dataclass(frozen=True)
class InterruptSettled: cancelled: bool
@dataclass(frozen=True)
class WatchdogReport:   thread: ThreadStatus             # turn_status 权威读数
@dataclass(frozen=True)
class JobsReplaced:     jobs: tuple[Job, ...]
@dataclass(frozen=True)
class Ticked:           now: float
UiEvent = Union[...]
```

`UserInput`(用户提交)与 `Interaction` 应答也走这里,保证**只有一条入口**。

## 5. 测试规范

- 目录 `XBotv2/tests/tui/`。
- **一个**共享 `ScriptedTransport`(conftest),它必须能表达:sequence 连续/缺口、delivery=queue|steer、`turn_status` 变化、断线与重连、慢渲染。**禁止**测试自带手写 session 桩。
- 禁止在测试里直接写 `state.*` / `app._*`;一律通过 `UiEvent` 驱动。
- 每一条不变式 I1–I8 至少一个**能证伪**的测试(去掉实现后必须失败)。
- 视图层用 Textual headless + 导出文本断言,不断言私有字段。

## 6. 实施顺序(每步先红后绿)

1. ✅ `status.py` + `tests/tui/test_status.py`(28 用例,已证伪)
2. ✅ `timeline.py` + `tests/tui/test_timeline.py`(18 用例,已证伪)
3. ✅ `events.py` + `state.py` + `tests/tui/test_state.py`(42 用例,旧行为变异可证伪)
4. ✅ `protocol.py` + `tests/tui/test_protocol.py`(信封校验 / 帧表 / 契约 fixture 交叉校验)
5. ✅ `tests/tui/test_reuse.py`:把"复用主代码模型"变成机器可检查的约束
6. ✅ `transport.py` + `tests/tui/test_transport.py`(33 用例,6 处旧行为变异各自可证伪)
7. ✅ `view/plan.py` + `view/entries.py` + `view/transcript.py` + 3 个测试文件(90 用例,变异验证)
8. ◑ `view/status_bar.py` + `view/jobs.py` + `view/composer.py` ✅;`view/thread.py` 待做
9. ✅ `controller.py` + `app.py` + `tests/tui/test_controller.py` + `tests/tui/test_app.py`(真实 Textual pilot)
10. ✅ `commands.py` + `theme.py` + `view/completion.py` + `view/selection.py` + `view/palette.py`,并接入 app(ctrl+p、斜杠命令本地分发)
11. ✅ 改连带消费者:`main.py`(新 `run_tui` 入口)、`tests/core/test_client.py`(改用真实
    `XBotClient` + `TransportSession`)、删除 `tests/core/test_command.py`(覆盖面迁入
    `tests/tui/test_commands.py`)、`tests/integration/test_http_transport.py`
    (`TerminalSession` 门面 → 真实 `XBotClient`,3 个 TUI 测试移除)
12. ◑ **真实服务端端到端已验证**(`tests/tui/test_app_real_server.py`);仓级全量回归 + 更新
    `docs/project/tui-web-performance.md` 中已被证伪的说法仍待做

### 5.5 第 6 步落定的传输语义

- **`SessionBackend` 是结构化端口**:`XBotClient` 天然满足,生产直接传入;测试用
  `tests/tui/factories.py` 里**唯一**的 `ScriptedBackend`(可脚本化 sequence、重订阅游标、
  thread 读、失败),不再出现每个测试自带桩的情况。
- **顺序**:`sequence <= cursor` 的帧**不重复应用**(恢复时服务端会重放,重复应用会翻倍计数);
  `sequence > cursor + 1` 发 `StreamGapDetected` **并且仍然应用该帧**;`sequence == 0` 的本地帧照常应用。
- **恢复分层**:游标过期先"回退到最旧保留帧前一位"重订阅(`cursor_recoveries` 次),
  用尽后重建基线(`open_session(mode="resume")` → 采纳快照 + 以 `event_cursor` 重订阅,`baseline_rebuilds` 次);
  再失败才走退避重连;预算用尽 → `ErrorFrame` + `ConnectionChanged(DISCONNECTED)` 并返回。**不静默断开。**
- **看门狗是"是否在跑"的唯一校准源**。`connect()` **必须**读一次线程(否则附加到运行中的会话会显示
  Ready,这正是被报告的缺陷);之后按 `watchdog_seconds` 轮询。线程消失、读取失败都产生 `ErrorFrame`,
  失败只在**连续失败的第一拍**报告一次,恢复后再失败会重新报告。
- **看门狗只发 `WatchdogRead` + `StatusSlotsUpdated`**,**不发 `UsageUpdated`**:线程读数里的 usage 是
  会话**总量**,而 `usage` 帧是**增量**;把总量喂进累加路径会翻倍。此约束有专门测试与变异验证。
- **提交必须声明 `delivery`**:旧 TUI 从不发送该字段,于是 UI 告诉用户"已排队"而服务端按 steer 处理。
  现在由调用方决定并显式传递。
- **客户端自有 input id** 贯穿提交:失败产生可见的 `UserInputFailed` 而非静默留下 pending。

### 5.6 第 7 步落定的视图语义

- **窗口是纯函数**。`view/plan.py` 的 `plan_window(ids, mounted, anchor=?, limit)` 返回
  "挂载哪些 / 卸载哪些",**从不读也不改 timeline**。旧实现的 `window_start`/`window_end` 整数对、
  `_mounted_entry_indices`、三套淘汰计数器全部消失。
- **翻页 = 换锚点**。`anchor` 是"窗口结束于哪个 id",`None` 表示尾部;
  `older_anchor`/`newer_anchor` 是独立的纯函数,可单测。锚点不存在时
  **抛 `UnknownAnchor` 而不是静默回退到尾部**(静默回退正是"读者被莫名挪走"的成因)。
- **挂载集必须连续**。真出现空洞(两个已挂载 id 之间缺一个)就整体替换挂载集;
  静默保留空洞会让读者看到不完整的 transcript。
- **窗口只决定渲染什么,不决定保留什么**(I3)。`plan_window` 的签名里没有 timeline 引用,
  这一点由构造保证,并由测试盯住。
- **entry 只被自己的条目写入**(I2)。`_refresh_changed` 用 `entry_id` 取 widget;
  测试特意让被更新的 entry **不在最后一位**,以钉死"更新写到最后一个 widget"这个旧缺陷。
- **"是否跟随尾部"不存储**,而是问容器(`container.is_vertical_scroll_end`)。
  但**意图必须在挂载前采样**:一旦内容高于视口,"是否在末尾"按定义就是 false,
  挂载后再问会永久停止跟随——这是本轮测试抓到的一个真 bug。
- **`Static` 在 Textual 8 用 `.content`**;entry 容器必须显式 `height: auto`,
  否则 `Vertical` 默认占满视口、transcript 根本无法滚动,所有滚动断言都会变成假绿。

**本轮被测试抓到的两个真 bug(不是实现者事后发现的)**:
1. 跟随意图在挂载后采样 → 长 transcript 永久失去自动跟随;
2. 两处变异最初**未被捕获**(无条件跟随、更新写错 widget),说明测试太弱;
   补强后(滚动后加 pause、让被更新 entry 不在末位)两个变异均被抓住。

### 5.7 第 8 步(部分)落定的面板语义

- **状态行是纯函数** `render_status_line(StatusLine, width)`。状态词**只**来自
  `status.derive`;视图不做任何状态判断。`status_line_for(state, ...)` 从 session state 组装模型,
  视图不新增事实。
- **状态行永不超过给定宽度**(参数化测试 11 种宽度)。细节按优先级让位:队列 → activity →
  tokens → ctx-free → session → agent → provider/model → status slots → cwd;
  被裁剪的只有状态词本身,且用 `...` 标注。
- **任务行显示服务端原始状态词**(`pending/running/completed/failed/stopped`),字形只做视觉标记。
  第一版我写了 `_MARKERS` 把 `completed` 映射成 `done`——那正是被批评的重复词表,已删除。
- **任务面板按 `job_id` 复用行**:旧面板每拍重建导致展开行闪烁;现在只在 `_order` 变化时
  `move_child`,行文本原地更新。
- **composer 的文案是纯函数**;运行时文案明确写 **steer**,并有一条专门测试防止退回"queue"这个谎话
  (旧 TUI 从不发送 `delivery`,UI 却告诉用户已排队)。阻塞式提问(审批/问答)优先于运行提示。
- 只读(thread 视图)时 `composer_enabled` 为假,回车不提交。

### 5.8 第 9 步落定的装配语义

- **controller 是唯一装配点**:它自己构造 `TransportSession` 并把 `emit=self.dispatch` 接上,
  所以"事件从哪来、进哪去"只有一个地方可看。
- **渲染合并**:`dispatch(event)` 是**同步**的,只做 reduce + 置脏,永不渲染;
  `flush()` 只在脏时渲染四个视图;`flush_loop(interval)` 是一个帧循环。
  N 个事件 → 1 次渲染,这是"渲染不得反压传输"的落点。
- **视图只被告知,不被询问**:`status_line_for` / `ComposerModel` 由 controller 构造,
  视图不重新推导任何事实(`ViewPort` 协议只有 7 个方法)。
- **进度不进 transcript**:`turn_started_at` 存在 state 里,`activity()` 用注入的时钟算时长,
  由状态行显示。
- **`app.py` 只回答"屏幕上是什么、按键做什么"**;`TextualViewAdapter` 是唯一同时了解两边的类,
  它不推导任何东西。`pageup/pagedown` 是 `priority=True` 绑定——否则焦点在 composer(TextArea) 时失效。
- **生命周期**:`on_unmount` 取消 read/render/watch 三个任务;`background_tasks` 是可观测的公开属性,
  测试断言应用退出后不留任务。

**设计被推翻的一次记录**:我先尝试把"进度"做成 timeline 里的 `ActivityEntry`(理由是旧实现的
activity 行绕过渲染层、被快照重建摘除)。结果它污染每一轮的 transcript 并打乱 13 个既有断言——
这说明设计错了。改为"起始时间进 state + 状态行显示",并**删除** `ActivityEntry` 与 `EntryKind.ACTIVITY`
两个概念。

**本轮端到端测试抓到的真 bug**:服务端回执先于本地记录到达时,
`UserInputSubmitted` 会把已 `ACCEPTED` 的条目**降级回 PENDING**(transcript 上永远显示"发送中")。
已在 reducer 中禁止降级,并补了单元测试 + 应用级测试。

**flaky 自查**:全套件连跑 6 次稳定通过。期间修掉了一个真实的竞态:
视图在 `call_after_refresh` 里排入的"钉到尾部"滚动会与测试的手动滚动竞争,
测试现在先等所有排程滚动落地再手动滚动(`settle(pilot, rounds)`)。

### 5.9 第 10 步(部分)落定的 chrome 语义

- **目录条目复用仓库的 `CommandDescription`**:客户端命令与服务端命令是同一种东西
  (`name/slash/kind/description/usage/examples/parameters/exclusive`),没有本地副本。
- **解析不猜**:未知斜杠返回 `ParsedCommand(description=None)`,调用方必须显式处理,
  而不是回落到某个看起来合理的命令。
- **`merge` 是替换而非累加**:服务端目录是它自己能力的唯一事实来源;
  客户端命令与客户端别名永不被服务端覆盖。
- **`search` 与 `complete` 分开**:`search`(调色板)匹配名称+描述+用法;
  `complete`(补全弹窗)**只匹配名称**。第一版只用一个 search,导致输入 `/st` 时
  "copy" 也出现在候选里(它的描述含 "latest")——这是噪音,已拆开并加测试。
- **补全状态是纯函数**:`completion_for/move/dismiss/accepted_text` 无 widget 可测;
  `CompletionPresenter` 只负责把状态镜像到 popup。高亮环绕、有参数即隐藏、Esc 只隐藏不清空。
- **接受之后不立刻重开**:`Tab` 会把文本写回 composer,触发一次 change 事件;
  presenter 记住刚接受的文本,直到文本再次变化才允许重开(否则弹窗与用户抢输入)。

**两次"变异无效"的记录**:本轮 7 个变异里最初有 2 个是**无效替换**(等于没改代码),
第一次跑显示"未被捕获"。我重做了这两个变异,确认对应测试确实会失败——
"变异没抓住"必须先怀疑变异脚本,再怀疑测试。

### 5.10 第 10 步落定的 chrome

- **只宣传能执行的命令**。`BUILTIN_COMMANDS` 中的 client command 必须有实际 handler；当前包含
  session/thread/provider/model/agent、显示控制、附件、交互回应、清屏、复制和退出等已实现操作。
  `/approve <id> [once|session]`、`/deny <id>` 与 `/answer <id> <text>` 通过标准 HTTP client
  permission/user-input endpoints 响应阻塞请求，ID 来自可见 interaction entry。不能把审批或答案
  当作普通 Agent 消息发送，也不能以“尚未实现”的占位命令冒充能力。
- **调色板不是新概念**:`CommandPalette` 就是"在命令搜索结果上的 `SelectionScreen`",
  没有第二套列表模型。`Option` / `SelectionModel` 同时服务会话选择与命令面板。
- **过滤保持高亮**:`with_options(..., keep=...)` 在候选中保留当前高亮项,否则按索引收敛——
  输入时选择不能从用户脚下被挪走(有变异验证)。
- **`/clear-screen` 清的是本地窗口,不是服务端历史**:旧实现调用 `state.reset_history()` 清状态,
  因此"清完立刻按 state 重画"等于没清。现在用客户端事件 `TranscriptCleared` 清空 timeline,
  下一次快照仍以服务端为准(有 reducer 级 + app 级测试)。
- **`ENABLE_COMMAND_PALETTE = False`**:Textual 自带 ctrl+p 面板会抢走绑定;这是测试抓到的
  (第一次按 ctrl+p 打开的是 Textual 的面板,不是客户端的)。
- **斜杠命令本地分发,永不发给服务端**:`_submit` 先 parse;未知命令产生可见提示;
  已宣传但未接线会产生一条"目录 bug"提示(而不是静默)。

**本轮测试抓到的两个真问题**:
1. ctrl+p 打开的是 Textual 内置命令面板(不是客户端的),`CommandPalette` 名字被抢;
2. `/clear-screen` 清完 DOM 后 `flush` 立刻按 state 重画,命令等于无效——暴露出"清屏"的语义
   必须是清状态(或等价的水位),而不是清 DOM。

### 5.11 真实服务端端到端(第 12 步)

`tests/tui/test_app_real_server.py` 起一个**真实的 uvicorn 服务端**(`start_server_application`
+ `MockLLM`,临时端口,无 socket 桩),用真实 `XBotClient` 驱动真实 `TuiApp`。7 个用例覆盖:
连接并就绪、权威线程读数、一个真实 turn 上屏、turn 结束后为 Ready、transcript 顺序、
真实序列无 gap/无拒绝帧、连续两个 turn 不互相污染。连跑 8 次稳定。

**为什么必须起真实服务器**:`httpx.ASGITransport` 会把整个响应体缓冲完才返回
(源码里是 `body_parts` 列表),**无限 SSE 流永远不会返回**。仓库现有集成测试因此从不走客户端
SSE,只从 manager 内部订阅——这是仓库测试策略的一个真实盲区,本次记录在案。

**真实服务端抓到的问题(脚本化测试不可能发现)**:

1. **契约不一致**:`turn_finished` 的线上 payload 被 `session/runtime.py` 在引擎校验之后
   追加了 `session_stats`,而 `TurnData` 是 `extra="forbid"` 且未声明该字段。客户端按产生方模型
   校验payload → 合法的终态帧被判为 malformed,状态卡在 Error。
   修法:在 `TurnData` 声明 `session_stats: dict[str, JsonValue]`,让传输模型描述真正上线的字段;
   并在 `test_protocol.py` 补两个(终态/取消)测试,不用服务器也能挡住回归。
2. **渲染竞态**:`flush_loop` 与 `submit()` 的 flush 会**并发**调用 `TranscriptView.render`,
   两者基于同一份 `_mounted` 计算计划,各自挂载同一 entry 的 widget,后者覆盖 `_widgets`,
   前者永久留在 DOM 里 → **重复行**。旧实现有 `render_lock`,我在重写时丢了。
   修法:渲染窗口在视图层串行化(`asyncio.Lock`),并加两个并发回归测试
   (去掉锁会失败,已验证)。
3. **一次更新被永久丢弃**:entry 在挂载的同一帧内变化时,widget 子节点尚未 compose,
   `update_entry_widget` 找不到 `.meta` 就什么都没写,而调用方仍把新值记为"已渲染"
   → `(sending…)` 永久残留。修法:更新返回"是否真的写入";未写入则不记账,下一帧重试。

**教训**:脚本化后端能证明一致性与时序规则,但**证明不了客户端与真实服务端对契约的理解一致**。
这三条都只有真实服务端能暴露。

### 5.12 第 11 步:连带消费者

- **`main.py`** 改指 `XBotv2.tui.app.run_tui`(新建的真实入口,负责构造 `XBotClient` + `TuiApp`
  并在结束时关闭客户端)。
- **`tests/core/test_client.py`**:`TerminalSession.refresh_baseline` 已随门面删除;该用例改为
  "游标过期后经**真实 `XBotClient`**(`httpx.MockTransport`)+ 真实 `TransportSession` 重建基线",
  并断言下一次订阅确实从采纳的游标恢复。
- **`tests/core/test_command.py` 删除**:旧 `CommandSpec`/`register_prompt_commands` 等概念已不存在;
  有价值的覆盖面(prompt 类命令、palette 按描述查询、无匹配)迁入 `tests/tui/test_commands.py`。
- **`tests/integration/test_http_transport.py`**:移除 `TerminalSession` 导入、poking 旧模块的
  autouse fixture,并把 `_real_terminal_session` 改为 `_real_client`(产出
  `(client, session_id, thread_id)`,只使用生产 API)。6 个真实套接字用例全部通过。
  同时**删除 3 个 TUI 测试**(它们属于 TUI 套件,而不是传输套件)。

**因此被移除、且新客户端尚未实现的能力(显式缺口,不是静默丢弃)**:

1. **`/session <id>` 切换 + 继续对话**:需要会话目录 API 与"切换时重建 transport"的语义。
2. **queue 投递**:新客户端一律 `delivery="steer"`(并在 UI 上如实说明);queue 面板与
   `queue_updated` 已接入 reducer,但"以 queue 语义提交"尚未暴露给用户。
3. **忙时提交重试**:旧客户端会把输入重投一次;新客户端的选择是"失败即可见
   (`UserInputFailed`)",不自动重投。

以上三项要么在后续步骤实现,要么在文档中保留为已知缺口。

### 5.13 TDD 纪律与偏离记录

本分支的规则:**没有先失败的测试,就不写生产代码**;每一条测试都必须能通过"把实现改回旧行为"
的变异验证,否则它只是装饰。

已按此执行的步骤:status / timeline / state / protocol / transport / plan / entries / transcript /
status_bar / jobs / composer / selection / palette / commands / controller / app —— 每一步
都记录了红→绿以及针对性的变异结果(spec §5.x)。

**本轮自查发现的三处偏离,以及处置**:

1. **`run_tui` 是先写生产代码、后补测试**——真正的 TDD 缺口。已按正确顺序重做:先写
   `tests/tui/test_launcher.py`(5 个用例),跑出 5 个失败(`client_factory` 尚不存在),
   再实现 seam(`client_factory` 注入 + `TuiApp.transport_config` / `.workspace` 只读属性),
   转绿;并用两个变异验证(不关闭客户端、丢弃 `mode`)确认测试会失败。
   同时把测试里的 factory 调用约定统一为 `**kwargs`,让**测试定义契约**而不是实现。
2. **先删 `tests/core/test_command.py`、后迁移覆盖面**——顺序错了。已把有价值的覆盖
   (prompt 类命令、palette 按描述查询、无匹配)迁入 `tests/tui/test_commands.py`,并补了 3 个用例。
3. **消费者测试改写**(`test_client.py` / `test_http_transport.py`)是对**已存在的生产代码**
   写测试,不属于 TDD 范畴;但其中一条断言是我写错的(`session.cursor == 0`),被测试自己抓住——
   这是正常的红→绿循环,不是偏离。

**遗留的纪律要求**:后续实现 `session`/`thread` 等命令时,先写用例(含"命令未实现时必须可见")
再实现;任何新增的 view/回调都必须有对应的 pilot 测试。

### 5.14 会话切换(缺口 1 已补齐)

`/session [id]` 恢复可用:**不带参数**列出服务端会话并弹出选择器,带参数直接切换。

- **`TransportSession.switch(session_id, thread_id="", mode="resume")`** 是这一步的核心:
  开新会话 → 重建 `FrameTranslator` → `_adopt`(发出 `SnapshotAdopted`、重放未决交互、
  把游标设为新快照的 `event_cursor`)→ 通知读循环 → 读一次线程。
  `thread_id=""` 表示"这个会话的主线程",由 `_main_thread()` 查询。
- **读循环必须能被"叫醒"**:旧订阅可能完全没有帧,等着它自己返回是不可能的。
  `_consume_once` 用 `asyncio.wait({读帧, 重启信号})` 竞争;`switch` 置位重启信号,
  `stop()` 也置位(否则停止会一直等服务器说话——这是实现的第一个 bug,被测试抓到)。
- **切换不是重连**:重启路径不消耗重连预算、不报 `DISCONNECTED`。有专门测试把重连预算设为
  空来钉死这一点(把重启当重连会立刻报失败)。变异验证:去掉重启处理 → 该测试失败。
- **失败不改动任何东西**:`open_session` 抛错时客户端仍在原会话,UI 给出可见提示。
- **过期帧不会串台**:切换后 translator 用新会话/线程重建,旧线程的迟到帧被拒并报告。
- 命令目录重新加入了 `session`(只宣传能执行的命令这一原则不变)。

真实服务端端到端(`test_app_real_server.py`)新增:在 TUI 里完成一个 turn → 通过同一 client
另建一个会话并跑完一个 turn → `/session other-e2e` 切换 → 断言 transcript 被替换
(旧会话的用户消息消失、新会话的出现)→ 在新会话里继续对话并拿到第二个回复。

**旧客户端的每一项能力现在都有结论**:`session`(§5.14)、`thread`(§5.17)、
`thinking`/`details`(§5.18)、队列面板(§5.19)已补齐;`attach`(§5.15)补齐;
"queue 投递""忙时重试"经查**不是丢失的能力**(§5.19 末),不实现为功能。
§5.16 记录的是两条**有证据的"不做"决定**(历史窗口化需要 trajectory 面的稳定身份、`/attach` 不预判模型模态),
不是待办清单。

### 5.15 图片附件(缺口 2 已补齐)

`/attach <path>` 把本地图片挂到**下一条**消息上,`/attach clear` 撤销。先红后绿:
先写 `tests/tui/test_attachments.py`(7 例)与 composer/controller/app 的新用例,跑出
`ModuleNotFoundError: XBotv2.tui.attachments` 与 8 个失败,再实现。

- **归属**:读取文件与编码是 IO,放在 `tui/attachments.py`;挂起状态属于"正在编辑的下一条输入",
  因此由 `TuiController` 持有(`attachments` / `attach` / `clear_attachments`),**不进 `SessionState`**
  —— 它不是服务端事实,reducer 里没有它的位置。
- **线上形状复用仓库模型**:`load_image` 返回 `XBotv2.session.contracts.ImageInput`;
  `TransportSession.submit(..., images: Sequence[ImageInput])` 与 `XBotClient.send_message`
  的签名一致(此前是手抄的 `Sequence[dict[str, str]]`,已改掉)。
- **图片-only 提交合法**:`MessageRequest` 允许只有图片,所以 `composer_can_submit(model, text)`
  与输入框的 `may_submit_empty` 让空文本 + 附件也能提交;文本和附件都没有时 Enter 仍然什么都不做。
- **失败必须可见且不改动待发列表**:路径不存在 / 不是图片时只发 `LocalNotice`,不改变已挂起的附件。
- **附件的生命周期止于提交**:提交后消费掉(成功或失败都消费),避免"下一条消息偷偷带上旧图片"。
- 命令目录重新加入 `attach`(只宣传能执行的命令这一原则不变)。

变异验证(12 项,全部被测试杀死):`image_media_type` 放行任意文件、不 base64 编码、
`composer_can_submit` 忽略附件、`may_submit_empty` 置假、placeholder 不报附件、
controller 丢掉 `images`、提交后不消费附件、`/attach clear` 失效、相对路径不按工作区解析、
缺文件被吞、非图片被放行、`attach` 不登记进目录。真实服务端用例另做一次变异(transport 丢掉 `images`)
确认它确实穿过 HTTP,而不是只在 controller 里成立。

**本轮发现(端到端)**:`handle` 图片的能力由**模型配置**声明,`MockLLM(model_override=...)`
绕过了插件配置,所以真实服务端用例必须在 override 上写明 `input_modalities=["text","image"]`,
否则服务端在 `providers._validate_message_capabilities` 处抛
`ValueError: Provider model 'mock' does not support image input`、turn 失败。
这条路径是**服务端正确行为**,客户端把该失败作为 `error` 帧渲染(I4);
客户端目前不读取模型的模态能力,所以不会在 `/attach` 时就预判拒绝——记录为已知限制,不做无依据的静默处理。

### 5.16 历史窗口:稳定身份、分页与驻留(已实现)

**问题。** 附加会话时不传 `history_limit`(`transport.connect` → `open_session`),服务端返回**完整**
`value.history`(`session/protocol.py:_open_session_response`,page 为 `None` 时取整段),客户端把整段放进
`Timeline` 且没有淘汰路径。新会话(resume)、`--session`、thread 切换、重连基线重建,**四条路径都是全量**;
渲染是 O(窗口)(`TranscriptView.limit`),**状态与内存是 O(n)**。

**第一步:身份属于节点,不属于消息身上的字段(已实现)。**

先前一版实现是错的,记在这里以免复发:它用 `input_id or xbot_message_id or tool_call_id` 从**别的字段**推断
身份,再回退到 node_id,再回退到空串,客户端再回退到本地 id。这有两个后果:同一服务器消息在不同投影下可能
得到不同的 id;而"没有身份"这件事被**静默降级**成"本地 id",于是预取一页与窗口重叠的历史会**重复行**——
正是最初被报告的跨页错乱。

现在的规则只有一条,且由结构保证:

- 身份是 **transcript 节点的 identity**(`HistoryNode.node_id`,即 trajectory record 的 position:
  append 为 `str(position)`,replacement 为 `f"{position}:{index}"`,`persistence/store.py` 的
  `MessageRecord.node_id()` / `SurfaceReplaceRecord.node_id(index)` 是**唯一**的命名处)。
- 消息与身份**一起**传递:`ConversationPage.nodes`、`AgentApplicationSnapshot.surface`、
  `HistoryMutation.nodes`、`TrajectoryMessage.node`、`TrajectorySurfaceReplace.nodes` 都是 `HistoryNode`,
  `messages` / `node_ids` 只是派生属性——**不存在两份需要对齐的列表**,也就没有对齐校验这种兜底。
- `SessionHistoryItem.id` **必填且非空**(`Field(min_length=1)`)。没有身份的记录无法进入窗口,
  在 wire 校验处就报错,而不是在客户端猜。
- 客户端 `_entry_from_history_item` 直接用 `item.id`,**没有 `or` 分支**。

**第二步:分页与驻留(已实现)。**

- **有界起步**:`TransportConfig.history_limit`(默认 50)随 attach 一起发;服务端返回最新 N 条 +
  `history_cursor`。四条路径共用 `TransportSession._attach_request`,不会有一条漏掉窗口。
- **向上翻页**:`OlderHistoryLoaded`/`HistoryFailed`/`HistoryRequested` 三个事件 +
  `list_messages(cursor, limit)`;controller 的 `page_older` 先在窗口内移动,移动不了才向服务端要更旧一页。
- **显式状态**:`OlderHistory = HistoryComplete | HistoryAvailable(cursor) | HistoryLoading(cursor) |
  HistoryFailed(cursor, message)`。游标只存在于**能取页**的变体里,因此没有"这个 None 是什么意思"的判断;
  失败保持可见且可重试。视图在窗口上方渲染同一状态(`view/transcript.py:older_history_label`),
  没有可说的内容时该行**不挂载**。
- **驻留**:`SessionState.retention_limit`(默认 2000,来自 `TransportConfig`)。超限时 controller 的
  `_release_held_pages` 释放**读者自己加载的整页**(LIFO),并**把该页之前那个游标放回**
  (`HeldPage(cursor, ids)`)。这是关键:一页正好是两个游标之间的跨度,所以释放后 `history_cursor` 恰好指向
  新窗口前方那一页,**被释放的部分仍然取得回来**——不像 opencode 今日 TUI 的 `slice(-100)` 那样永久丢失。
  释放只在读者**跟随尾部**时发生(读者正在看的那一页不会在他脚下消失),attach 返回的窗口永不释放。
- `Timeline.prepend` 把新页插到最前并保持页内顺序,已持有的 id **原地更新而不移动**(读取位置不跳),
  预置条目的 `seq` 重编号到现有最小值之下,`seq` 与插入顺序不再可能互相矛盾。

**与 Web 端的关系。** Web 端(`XBotv2/web/src/state/useXBot.ts:838-863`)已经在做**同类**窗口:它按
trajectory 的**绝对 position** 作锚点、`before=` 取更旧页,并在裁剪时**同步推进锚点**
("otherwise the next older page would start before the dropped records and leave a gap")。两者的差别是:
Web 是"任意裁剪 + 数字锚点 + `before`",TUI 是"整页释放 + 放回游标"。两者都保证未持有的部分仍可取得;
把两者统一到哪一种(给 `SessionHistoryItem` 加 position 并把 `/messages` 加上 `before`,或让 Web 也用
整页释放)是**下一步的独立决定**,不在本次改动内。

**验证。** 除逐层单测外,有两条端到端证据:HTTP 层 `test_every_read_of_a_message_names_the_same_node`
(带 limit 的附加 / `/messages?limit&cursor` / `/trajectory` 对同一条消息给出同一个 id)与真实服务端 +
真实 Textual 的 `test_app_real_server.py::test_a_real_window_pages_back_and_names_the_same_nodes`
(有界重附加 → 报"还有更早" → `page_older()` → 记录 id 与 SDK 读到的逐一相等)。视口不变量另有两条:
`test_a_prepended_older_page_does_not_move_the_readers_window`(页插在读者上方,窗口与锚点不动,再翻一次能到达)
与 `test_releasing_the_front_keeps_the_tail_window_on_screen`。**变异校验**:对本次新增行为做了 11 处破坏性
变异(attach 漏掉 window、恢复重建不带 window、prepend 变 append、prepend 忘记去重、释放页丢掉游标、
驻留忽略"读者在不在尾部"、历史条目退回本地 id、所有记录共用一个 id、有界附加忽略 page、状态行永不挂载、
翻页不发请求),11 处全部被上述测试捕获(其中"有界附加忽略 page"由既有的
`test_message_pages_artifact_download_and_regenerate_are_authoritative` 捕获)。

**服务端驻留与 compact:不改,证据见 `docs/project/history-windowing-analysis.md`。** 要点:
会话全量驻留内存的是**读路径**,窗口化客户端不会让服务端内存变小;`compact` 只退役 **surface**
(`preserve_transcript=True` 时 transcript 与 trajectory 只增不减);一个活跃线程至少 6 个可增长容器、
2 组 `Message` 对象;分页原语(attach `history_limit`、`/messages`、`/trajectory`、`[1,revision,offset]`
游标、`newest_position`)都已存在。另一处已知不一致:messages 读取在 `limit=None` 时读 surface、
在 `limit=N` 时读 transcript(压缩后是两套内容),本次未改动,已记录在分析文档 §3。

### 5.17 线程视图(缺口 3 已补齐)

`/thread [thread-id]` 查看会话里的线程;不带参数列出并选择,`/thread main` 回到主线程。
子代理线程**只读**。

**这一步的核心决策是"不做第二个视图"**。旧实现有:独立 `ThreadView` 组件、独立
`_view_items`/`_view_agent`/`_view_older_cursor` 状态、独立 `_pump_thread_view` 读循环、
以及"流不可用就退化为轮询"的分支——四套并行的东西,正是"相互耦合的 TUI 设计"这一指控的来源。
新实现把它降为**一次重新附加**:`TuiController.switch_thread(thread_id)` →
`TransportSession.switch(session_id, thread_id, mode="resume")`,已经存在的快照/timeline/
reducer/渲染/分页/状态机**原样作用于子线程**。线程视图没有自己的状态、自己的读循环、自己的降级路径。

- **只读是服务端的事实,不是客户端的开关**。`ThreadSummary.kind` 是权威;为此把
  `WatchdogRead(turn_status=ServerTurn, turn=...)` 换成 `ThreadRead(payload=ThreadSummary)`
  ——一次权威读取同时给出 turn 答案与线程种类,并且**不再重述任何 wire 字段**。
  `SessionState.thread_kind` 直接保存服务端那个词,`read_only` 只做一次比较;
  `tests/tui/test_reuse.py` 把客户端的两个词与 `ThreadSummary.kind` 的 `Literal` **相等**绑定。
  这一改动也让 transport 里的 `_TURN_STATUS_BY_WIRE`/`_turn_status` 与 `turn` 可选字段一并消失
  (`ServerTurn(payload.turn_status)` 就够了)。
- **只读只约束"发消息",不约束"敲命令"**。第一版我把闸门放在输入组件里(`ComposerInput.allowed`),
  结果在子代理视图里 `/thread main` 自己也发不出去——**app 级测试当场抓住**(§5.13 的红→绿)。
  现在组件把每一行都交给 app,由 app 区分命令与消息:命令照常执行,消息被拒时给出可见提示
  (`composer_can_submit` 仍在 controller/app 一侧)。`allowed` 概念随之删除。
- **状态行最高优先级的细节是"你在看哪个线程"**:子代理视图显示 `subagent:<thread_id>`
  (窄终端下最后才让位);主线程不显示。
- **选择器显示线程 id**(用户要手打的正是它)与 `kind / turn_status / agent / N msg`。
- 命令目录加入 `thread`(只宣传能执行的命令这一原则不变)。

变异验证(14 项,全部被测试杀死):`read_only` 恒为假、忽略服务端 `kind`、忽略读取里的
`turn_status`、丢掉 `subagent:` 标记、controller 忽略 `read_only`、`switch_thread` 丢掉线程 id、
线程列表不读服务端、`main` 不再特殊(见下)、选择器回调丢弃选择、被拒消息被吞、`thread` 不登记目录、
选择器不显示线程 id、选择器丢掉 kind。

**"`main` 不再特殊"这一项第一次变异存活了**:因为它只被 app 测试覆盖,而 `ScriptedBackend`
无视传入的 `thread_id`、直接返回预置快照,于是"把 `main` 当成线程 id 传下去"也能通过。
补上 `backend.opened[-1]["thread_id"] == THREAD` 的断言(即"`/thread main` 必须向服务端要
**它自己的**主线程")后该变异立刻被杀死。这正是 §5.13 要求的:变异存活时先怀疑测试。

**真实服务端**(`test_app_real_server.py`):`/thread` 弹出的选择器确实列出服务端返回的线程
(含 `main` 种类);`/thread main` 重新附加后客户端照常可用——再发一条消息、断言服务端**存下来的**
用户消息就是那条。**未覆盖**:真实**子代理**线程的端到端(需要一个会产生子线程的 agent/tool,
本 fixture 是 `no_plugins=True`),该路径由脚本化用例覆盖;这一点如实记录。

### 5.18 折叠块(缺口 4 已补齐)

`/thinking [on|off|toggle]` 与 `/details [on|off|toggle]` 控制模型推理与工具载荷是否显示;
默认都显示(与重写后的现状一致)。

- **这是"读者偏好",不是会话事实**:`BlockVisibility(reasoning, details)` 是 `view/entries.py` 里的
  冻结值对象,只到视图层——不进 `SessionState`、不进 reducer、更不上行到服务端。
- **参数不猜**:纯函数 `block_choice(argument, current)` 只认 `on`/`off`/`toggle`/空,
  其余返回 `None`,由调用方给出 usage 行。这样打错一个字不会静默翻转显示。
- **显示偏好必须让视图的差异记账失效**。`TranscriptView` 靠"上次渲染的 entry 值"跳过没变的行,
  而偏好**不在 entry 值里**:改动偏好后若不清 `_rendered`,每个挂载中的行都会被判定为"没变",
  开关看起来毫无作用。`set_visibility` 因此显式作废该记账并重绘当前窗口
  (窗口有界,重绘代价就是这一屏);`test_the_same_visibility_is_not_a_change` 同时钉住另一侧:
  偏好没变就**不**作废、不重绘。
- **更新而非重建**:收起/展开走的是既有的 add/remove 子块路径,同一个 entry 的 widget 不被重建
  (测试断言 widget 身份不变)。
- 命令目录加入 `thinking`/`details`。

变异验证(9 项,9 项被杀死):推理恒显、工具载荷恒显、参数被忽略恒为 on、错参数被接受、
改偏好不作废记账、挂载时不带偏好、更新时不带偏好、**根本不应用偏好**、命令不登记目录。

**一项变异存活,处理方式记录在此**:最初我在命令处理里加了一句 `await controller.flush()`,
"开关不重绘"的变异(把该句变成永不执行)存活——说明这句话是**多余的**:渲染循环本来就按脏标记
重绘。删掉它(少一个概念),并把变异换成"根本不调用 `set_visibility`",立刻被杀死。
这正是 §5.13 的另一半:变异存活时,先分辨是测试不够,还是代码多余。

### 5.19 排队输入面板(缺口 5 已补齐)

左下的 Queue 折叠面板列出**正在等待的输入**(`PendingInputData`:内容、等待位置、附带图片/文件数);
数量已经在状态行(`queued:N`),面板回答的是"等的是什么"。

- **reducer 早就在跟踪队列**,缺的只是显示:`QueueReplaced` → `state.queue`,status line 已用。
  这一步补上 `view/queue.py`(纯函数 `queue_row` + 按 message id 对账的 `QueuePanel`),
  与 job 面板同一套做法:行只创建一次、原地更新,不整表重建(旧客户端正是每次 tick 重建才闪)。
- **一行一条、按宽度裁剪**。队列里可能是多段文字,面板若随内容长高就会把正在等的对话挤出屏幕;
  行内换行被压成空格,并断言每行不超过给定宽度。
- **共用容器的可见性必须有唯一 owner**。`#panels` 现在装着 Tasks 与 Queue 两个折叠块:
  原先 `render_jobs` 里那句 `set_class(bool(jobs), "visible")` 会让两个面板互相覆盖
  (空队列会把正在跑的任务面板藏掉)。现在 `_refresh_panels` 一处决定:每个面板按自己的行数
  显隐,容器在**任一**面板有内容时可见。第三条 app 测试("队列为空时任务面板仍在")就是钉这个的。
- 队列是服务端事实,客户端只显示:不新增 wire 形状,复用 `PendingInputData`。

变异验证(8 项,全部被杀死):行丢文本、行丢图片计数、行不裁剪、多行不压平、
出队的行不删除、行每次重建、面板可见性忽略队列、控制器渲染空队列。

**两条"缺口"的结论(有证据,不实现)**:

- **queue 投递**:`MessageRequest.delivery` 支持 `queue`,但旧客户端也从不指定它
  (`git show 59244f4:XBotv2/tui/textual_client.py` 里 `send_message(text, images=...)` 只有这两个参数),
  新客户端同样固定 `delivery="steer"` 并在 composer 提示"会在运行中被 steer"。
  也就是说这不是回归,而是**服务端支持、两代客户端都没暴露的可选模式**,记录为可选增强。
- **忙时重试**:发消息本身永远不会 `thread_busy`——服务端要么起一个 turn,要么 steer,要么 followup
  (`session/runtime.py:send_message`);`thread_busy`(retryable)只出现在**需要独占**的操作上
  (历史操作、压缩等,`session/manager.py` / `runtime.require_idle`)。旧客户端没有重试,
  新客户端按 I4 把失败作为可见条目报出来。因此没有"忙时重试"这一丢失能力。

### 5.20 真实终端冒烟测试(tmux,第 14 轮)

自动化用例跑的是 Textual 的 `run_test`(真实 widget、真实按键、真实 HTTP/SSE),但没有一个真实终端。
本轮用 tmux 在**真终端**里跑了真实服务端 + 真实 TUI,并且**发现了一个只有在真实启动路径上才会出现的缺陷**。

**发现的缺陷(已修)**:`xbot tui` 默认不带 `--session` → `hello()` 回答里没有会话 id →
`open_session` 时服务端才创建并分配 id,而 `TransportSession.connect()` **没有采纳** `opened` 的身份
(只有 `switch()` 采纳了)。后果:看门狗去读 `list_threads("")` → 404;`FrameTranslator` 仍用空 id 构造 →
**自己刚建的那个会话的每一帧都被判为 foreign** → 界面停在 **Disconnected** 并显示两条 404。
修复:抽出唯一一处身份采纳 `_attached_to(opened, session_id=..., thread_id=...)`(含按新身份重建 translator),
`connect()` 与 `switch()` 共用。4 个新用例(脚本化 3 个 + 真实服务端 1 个 `session_id=""` 的
`mode="new"` 附加必须 Ready 且能对话),2 项变异被杀。

**真终端观察到的画面**(tmux,100×28,真实 uvicorn):

```
SCREEN 1  Ready   session:20260922-021447-b794  agent:default  default/test
SCREEN 2  You / hello there / Assistant replying… / the reply
          Running  turn:1 1.3s  session:…  ← 用户报告的"明明 running 却显示 Ready"已不复现
          Turn running — your message is sent as a steer
SCREEN 3  Assistant / the reply arrived after a slow turn
          Ready  turn:1  ← 终态回到 Ready,且没有 malformed payload
SCREEN 4  /help → 9 条客户端命令(含 /thread /thinking /details /attach)
SCREEN 5  /attach /nope.png → "No such file: /nope.png"
SCREEN 6  /thread  → 选择器:▸ <session>  agent  main idle default 2 msg
SCREEN A  You hello there / Assistant reply 1 / You steer me instead / Assistant reply 2
          ← I6:插话落在已提交的流式后缀**之后**,顺序没有被反转
SCREEN C  /thinking off → "Reasoning hidden"
```

**冒烟脚本本身的坑(记录以免重犯)**:`.venv` 里 XBotv2 是**指向主检出**的 editable 安装,
所以从非 worktree 根目录运行脚本时会 import 主检出的代码。第一次冒烟就是这样:服务端来自主检出
(它的 `AssistantMessageData` 已声明 `stop_reason`),客户端来自本 worktree(未声明)→ 客户端在 turn 结束时
报 `malformed payload: stop_reason Extra inputs are not permitted` 并进入 Error。
**这不是产品缺陷**:客户端拒绝一个自己无法完整表达的帧、并把它报出来,正是 I4 的要求;
两边同版本(worktree 服务端 + worktree 客户端)时该错误不再出现(SCREEN 3)。
顺带确认了一件事:主检出的协议已经比基线 `59244f4` 新——合并本分支时 `stop_reason` 会随之而来,
TUI 不需要为此改动,但**跨版本**运行(新服务端 + 旧客户端)会以可见错误暴露契约差异,而不是静默丢字段。

冒烟脚本不属于交付物,验证完成后已删除(`.smoke/`)。

### 5.21 启动失败必须是"看得见的失败",不是 traceback(第 14 轮,真实终端发现)

用真实终端验证手动启动时,服务端因为配置里缺 `MINIMAX_API_TOKEN` 而 500,
`connect()` 把异常抛给 `TuiApp._boot` → **Textual 直接崩出 alt-screen 打了一整屏 Python traceback**。
这条路径对用户很关键:配置写错时,用户看到的应该是"为什么连不上",而不是要往上翻的堆栈。

修复:`_boot` 捕获 `connect()` 的失败并 `return`——transport 已经发出可见 `error` 帧与 `DISCONNECTED`,
controller 的 `connect()` 也在 `finally` 里 flush 过,所以理由已经在屏幕上;并且**不启动**读流/看门狗任务
(没有流可读)。用户仍可正常 `/exit`。

用例与变异:新用例断言"应用仍在运行 + 理由可见 + 状态 Disconnected + `background_tasks == ()` +
仍能 `/exit`";两项变异被杀(让异常逃逸、失败后照常启动任务)。
第一次实现里我多写了一句 `await self.controller.flush()`,**变异存活**(去掉它测试仍过),
说明它是多余的——controller `connect()` 的 `finally` 已经 flush,遂删除(与 §5.18 同一处理方式)。

### 5.22 `/status` 归客户端 + 统一选择器(第 14 轮,用户要求)

**`/status` 现在由客户端渲染**(`view/status_bar.py:status_report`),不再往返服务端。
理由:服务端的 `/status` 是把 session/thread 事实拼成文本给"没有这些事实的客户端"用的,
而 TUI **全都有**——本轮把 reducer 里那个"只学一个字段"的写法换成保留服务端整份
`ThreadSummary`(`SessionState.thread`,`thread_kind` 变成它的属性,`read_only` 不变),
于是报告能直接读:`ID/Thread/Workspace/Agent`、派生的 `State`(与页脚同一个 `derive`,含
`controller.activity()` 的 turn 计时)、`History/Queued/Tasks/Prompts`、`Provider/Model/Context`。
真实终端里 `/status` 的输出已核对(见 §5.20 的冒烟记录)。

**选择器只有一套**。用户的要求是"统一选择器,而不是各自实现",落地为:

- `view/pickers.py`:**纯**的行构造(`session_options`/`thread_options`/`provider_options`/
  `model_options`/`effort_options`/`agent_options`/`filter_options`),输入全是服务端已经发来的模型
  (`SessionSummary`/`ThreadSummary`/`ProviderCatalog`/`AgentListResponse`);
- `TuiApp._choose(title, load, apply)`:唯一 push `SelectionScreen` 的地方(有一个机械守卫:
  `test_there_is_exactly_one_place_that_offers_a_selection` 断言 `app.py` 里
  `SelectionScreen(` 只出现一次),`_chosen` 统一 `run_worker(apply(value))`;
- 每个命令只声明"行从哪来、选中后干什么":`/session` `/thread` **重构到同一条路径上**
  (原来的 `_session_chosen`/`_thread_chosen` 两个各自实现的回调已删除);
- 新选择器:`/provider` `/model` `/effort` `/agent`。**无参数的形态归客户端(选择器),
  带参数的形态一律转发给服务端命令**(`/model use x`、`/jobs stop <id>` …),服务端仍是唯一权威;
- 选择器自带 **filter 框**(与 palette 同一套),所以几十个模型也可用;
- 选中后调**类型化端点**(`select_provider`/`select_effort`/`select_agent`),失败可见,成功后
  重新拉一次命令目录(换 provider/agent 会改变可用命令)。

**`/jobs` 也归客户端列表**(`JobSnapshot` 已经在流里,面板早就在画它),`/jobs stop <id>` 转发服务端。
**破坏性命令不加确认**(用户明确要求),所以 `/clear` `/undo` `/fork` `/compact` 直接执行。

**顺带修掉一个真 bug(palette 的选中项)**。写"服务端命令按顺序排在客户端命令之后"的测试时,
palette 测试开始返回 `/model` 而不是 `/status`。追下去发现是 `SelectionModel.with_options` 的
"粘住旧高亮"策略:高亮会跟着**任何仍然存活**的旧值走(`/help` 的描述里有 "s" 就一直被选中),
旧值消失时又复用它的**行号**,于是 Enter 选中的是用户从没看过的那一行——一个会执行错命令的 bug。
现在改为:新列表高亮它的**第一行**(列表本身就按 rank 排序),并把这条规则写进测试
(`test_a_new_list_highlights_its_best_row`)。

**测试与变异**:新增 `test_view_pickers.py`(12)、`test_backend.py`(4)、状态报告 5 个、
app 层 20 个选择器/报告用例、真实服务端 3 个(报告、provider、model)。10 项变异全部被杀:
选择器不弹、列表失败被吞、选择失败被吞、有参数却弹选择器、`/status` 又跑去服务端、
model 选择器忽略当前 provider、应用选择时丢掉 provider、选择器没有 filter 框、
`/status` 又变成会被折叠的普通通知、高亮又按旧行号走。

**测试环境的坑(已查明,记录)**:真实服务端用例里 provider 目录会出现 minimax/deepseek/… 条目,
一开始我误以为是"读了本机的全局配置"。真实机制是插件树的**分层加载**:
打包的基树 `XBotv2/xcore.yaml`(由 `config/seed.py` 播种进数据目录)+ 全局覆盖
`config/plugins.yaml` + 工作区覆盖 `<workspace>/.xbot/plugins.yaml` + 会话覆盖
`sessions/<id>/config.yaml`(`config/plugin_catalog.py:_overlay_path`),逐层深合并;
`no_plugins` 关的是**能力插件**,基树里的 llm provider 仍在。因此真实用例按"当前 provider/model"
过滤后再选,而不是假定第一行。另:`provider_options` 的 detail 曾写作 `default <model>`,
导致按 "default" 过滤会匹配**所有**行,已改为 `starts at <model>`。

### 5.23 命令面理顺:公开、发现、执行(第 14 轮,用户要求)

用户的要求是"理顺 command 服务插件 / server runtime 加载 / HTTP 资源与协议,包括公开、发现和执行,
并让 TUI 与 WebUI 用同一套方式发现服务端命令",并且不许拿"之前就这样"当理由。结论与落地如下。

**这一层到底是什么。** 它是**斜杠词汇表**:一行文本(名字 + 参数),由装在**这个会话/线程**里的插件注册,
会话自身注册内建(`session/plugin.py` → `build_session_commands`),能力插件在自己的 `apply()` 里通过
`ctx.commands.register(...)` 注册(registration 是 fiber 效果,插件卸载即撤销)。所以"有哪些命令"是
**内容**,按线程变化;"资源长什么样"是**形状**,才是契约。之前把它当成不能公开的东西,是把这两件事混成一件。

**公开:它是一个正常的类型化资源。**
- 两条路由去掉 `include_in_schema=False`,`test_public_api` 的断言从"schema 里不许有 /commands"
  改成"schema 里有这条路径,且 request/response 是类型化模型"(路径集合的断言同步加上它);
- SDK(`XBotClient`)加 `list_commands` / `run_command`,`tests/integration` 里那条
  `assert not hasattr(sdk, "run_command")` 换成"两个方法都在"——那条断言既没写理由,也只禁了
  `run_command` 而不管 `list_commands`,是靠**标识符**而不是原则在挡;
- TUI 侧因此删掉了上一轮为绕开它而写的 `tui/backend.py`(`TuiBackend` 子类钻私有 `_request`):
  现在 `run_tui` 直接用 SDK,`test_backend.py` 一并删除,相关契约并入 `tests/core/test_client.py`。

**发现与执行:请求体就是用户敲的那一行。**
- `CommandRequest` 从 `{command, args, raw, kind}` 收敛为 `{raw}`。**服务端自己解析**
  (它拥有目录):取第一个词当名字,其余原样交给命令;命令解析不了自己的参数(例如引号不闭合)时,
  由 `guard_command` 变成 `status="error"` + `message` 的**结果**,而不是 500/400——
  这样每个客户端都只需展示同一种东西。
- `kind` 不再由客户端上传:目录已经写明谁执行,再让每个客户端重述一遍,就是每个客户端各写一份同样的规则。
- `CommandEffect` 增加 `policy`(权限/沙箱命令改的是会话策略,原词表表达不了),
  `Command` / `CommandDescription` 增加 **`effects`**:目录**执行前**就能告诉客户端这条命令会动什么。
  各注册点都补了声明(status 空、clear/undo 历史+线程+会话、fork 会话、jobs 任务、
  permission/sandbox 策略+命令、provider/model/effort 线程、agent 线程+agents+命令、compact 历史+线程、
  goal 线程)。真实服务端用例断言目录里 `clear.effects == [history, thread, sessions]`、
  `permission.effects == [policy, commands]`。

**统一客户端过程(两种客户端同一套,写进 `docs/http-api.md`)。**
1. 本地命令(纯 UI 能力)优先;
2. 挂载时 `GET …/commands`,并在**任何可能改变集合**的切换后重取(会话/线程/agent/provider);
3. 命中本地命令 → 本地执行;目录说 `kind="prompt"` → 送到**消息端点**(它是提示词模板,不是命令);
   其余 → 把整行 `POST` 到命令资源;
4. 显示 `message`,按 `effects` 刷新可能变化的面板。
TUI 原本把 `prompt` 类命令也 POST 到命令资源,于是显示服务端的拒绝话术——这是本轮修掉的真 bug
(`test_a_prompt_command_is_submitted_as_a_message`);Web 端本来就是这么做的
(`useXBot.ts`: `kind === "prompt" → sendMessage(raw)`),只是它的请求体还带着 `{command, raw, kind}`,
已改成 `{raw}`。

**没有做的两件事(说明理由,不是"之前如此")**:没有把 `prompt` 的执行也搬进命令资源——那需要让命令面
反向调用会话的输入路径,而提示词模板本来就属于消息端点;也没有按命令逐个补类型化路由——目录本身公开之后,
客户端直接用它就够了。

**验证**:`test_public_api`(schema 含该资源)、`test_client`(GET 目录含 effects、POST 只发 `{raw}`)、
`integration`(真实服务端目录/执行/引号错误/目录 effects)、TUI(app 层:服务端命令发一行、prompt 走消息、
参数形态转发)、真实服务端 12 例;仓级全量绿。Web 端改了 `client.ts`/`useXBot.ts`/`types.ts`,
但本机没有 `node_modules`(装依赖属于改动环境,未获授权),因此 **vitest 未运行**,这一点如实记录。

### 6.0 复用规则(不可协商)

第一次实现时我只是"复用了字段",仍然手抄了整套事件词表和分派表。现已改为:

1. **帧形状的事件必须携带产生方的 pydantic 模型**(`payload` 字段),不得重述其字段。
2. **只允许客户端本地概念自带字段**:`ConnectionChanged` / `StatusSlotsUpdated` /
   `WatchdogRead` / `StreamGapDetected` / `InterruptAsked` / `InterruptSettled` /
   `UserInputSubmitted` / `UserInputFailed`。当前 28 个 `UiEvent` 成员中,
   20 个携带协议模型、8 个是上述本地概念。
3. **帧表只登记仓库自有的 payload 模型**,不得在 `XBotv2/tui/` 下声明任何 wire 形状。
4. **客户端词表与线上 `Literal` 绑定**:`ServerTurn` 必须覆盖
   `ThreadSummary.turn_status`;`ToolStatus` 必须覆盖 `ToolResultData.status`
   (直接沿用 `success/error/denied/cancelled`,不再自造 `completed/failed` 与映射表)。
5. 上述 1–4 由 `tests/tui/test_reuse.py` 强制;把事件改回"重抄字段"、或把帧表指向本地模型、
   或让 `ServerTurn` 与 wire 不一致,测试都会失败(已实测)。

`protocol.py` 的帧表是**声明式**的:`type -> (payload 模型, 事件构造)`。校验由模型完成,
所以表里没有一个 payload 字段被重写。仓库没有公开的 server 事件登记表,因此分派表必须由客户端声明;
`test_protocol.py` 用仓库自己的 SSE 契约 fixture(`tests/fixtures/sse/server_event_contracts.jsonl`)
做**完整性 oracle**:fixture 里的每个帧类型必须"被翻译"或"被显式忽略",否则测试失败。

### 6.0.2 契约发现已修复:`XBotClient` 会被代理环境变量直接打挂(第 14 轮)

`httpx.AsyncClient(trust_env=True)` 会把 `NO_PROXY` 的每一项当 URL 解析;`[::1]` 这种常见写法
解析出的端口是 `:1]`,于是**构造 client 就抛异常**:

```
$ python -c "import httpx; httpx.AsyncClient()"
httpx.InvalidURL: Invalid port: ':1]'
$ python -c "import httpx; httpx.AsyncClient(trust_env=False); print('ok')"
ok
```

本机环境就是 `no_proxy=localhost,127.0.0.1,::1,[::1]`,因此**所有走 `httpx` 的入口都起不来**:
`XBotClient` 的每个调用方,以及仓级测试里直接构造 `httpx.AsyncClient` 的真实 HTTP 用例
(`tests/integration/test_http_transport.py` 在宿主环境跑出 7–15 个失败)。这不是 TUI 引入的。

**修法(在责任边界,而非调用方)**:

- `XBotv2/client.py`:新增纯函数 `uses_proxy_environment(base_url, *, uds_path=None)` ——
  **loopback 与 Unix socket 目标不查代理环境**,其余目标保持 httpx 默认(企业代理用户不受影响);
  `XBotClient.__init__` 增加显式 `trust_env` 参数覆盖该判断。理由:目标是本机时穿过代理没有意义,
  而"构造就失败"是不可接受的失败方式。
  证据:`tests/core/test_client.py` 5 个用例(loopback/localhost/`::1` 不查、远端仍查、UDS 不查、
  在敌意 `NO_PROXY` 下构造本地 client 不抛、显式 `trust_env=False` 生效);4 项变异全部被杀。
- `XBotv2/tests/conftest.py`:session 级 autouse fixture 规范化**环境里**的代理变量 ——
  仓级测试里直接构造 `httpx.AsyncClient` 的地方不属于产品代码,套件不该由开发机的代理配置决定成败。
  `XBotClient` 自身的行为仍由上面那组用例负责,不靠这个 fixture 掩盖。
- **同一根因在服务端 provider 侧仍然存在(有意不改)**:`XBotv2/llm/openai.py:74` 构造 `AsyncOpenAI(**kwargs)`,
  OpenAI SDK 自己建 httpx client 且 `trust_env=True` → 在敌意 `NO_PROXY` 环境下 provider 构造就抛,
  表现为 `session_open_failed: Invalid port: ':1]'`(会话根本打不开)。
  **不在本次改**:云端 provider 可能正需要代理,无条件关掉 `trust_env` 会破坏那类用户;
  这是客户端/环境策略问题,已作为"可见失败"暴露(见 §5.21),并给出可复现的绕法:
  `NO_PROXY=localhost,127.0.0.1,::1`(去掉方括号)或 `env -u no_proxy -u NO_PROXY`。
  已实测:该绕法 + worktree `PYTHONPATH` 下 `xbot tui` 正常进入 `Ready  session:…  agent:default`。
- TUI 的真实服务端 fixture **不再**清理环境变量:它在 `xbot tui` 的真实条件下跑,于是"本地 client
  在敌意代理环境下可用"是被端到端证明的,而不是被绕过的。

### 6.0.1 契约发现(需上报协议 owner)`permission_denied` 出现在仓库的 SSE 契约 fixture 中,但**当前服务端没有任何生产者**
(`grep -rn '"permission_denied"' XBotv2/` 只命中 `main.py` 的消费者分支),且其 payload
(`{request_id, source, tool_call, decision: "deny", reason, resume_supported}`)
不满足任何现有模型:`PermissionRequestData` 把 `decision` 钉死为 `Literal["ask"]`,
`InteractionRecordedData` 要求帧里没有的 `status`。客户端因此把它列入
`IGNORED_FRAMES` 并写明理由,**不修改共享协议模型、也不编造形状**。服务端真正发布的
拒绝答复走 `permission_response_recorded`。

### 6.1 第 3 步落定的语义决策(实现依据)

- **`turn_started` 同时置 `server_turn=RUNNING`**:它本身就是服务端帧,是权威读数。
- **快照只在 `turn_status is not None` 时覆盖 turn 事实**;没有答案的快照既不置 RUNNING 也不置 IDLE。
- **`error` 帧绝不改动 `turn_open`/`server_turn`**,只写 `last_error` + 一条 ErrorEntry,并把未收尾工具置 FAILED。
- **`turn_finished`/`turn_cancelled`/已确认的中断**才把未收尾工具置 CANCELLED(修复旧的「error 后工具永远 pending」)。
- **中断只覆盖"终态帧未到"的窗口**:`InterruptSettled(cancelled=False)` 表示服务端说"本来就没在跑",
  此时 `turn_open` 反而被确认为 true(直到终态帧或看门狗把它收敛);`cancelled=True` 才结束 turn。
- **提交输入不使用状态表达**:`UserInputSubmitted` 只置 `submission_in_flight`(供 composer 显示"发送中")
  与一条 `Delivery.PENDING` 的 UserEntry。status 不猜测 turn——queue 语义下线程本来就是 idle。
- **用户消息落地前先提交流式后缀**(I6):`UserMessagePublished` 先 `_close_stream`,再按 id upsert;
  不存在消息时按 id 追加。**不按内容匹配**(旧实现在图片-only 提交时会永久残留队列项)。
- **`TurnCancelled` 追加一条可见 notice**(不再依赖会粘住的 `Interrupted` 状态字符串)。
- **gap、提交失败、未知历史角色都有可见条目**(I4);reducer 遇到未知事件或未知协议状态枚举
  **抛 `TypeError`/`ValueError`**,不静默忽略。
- **history/snapshot 是基线替换**,不是窗口移动:重建 timeline 并清空 `stream_entry_id`。

## 7. 完成判据

- I1–I8 各有可证伪测试且全绿;**每个 I 都有一次定向变异证明它不是装饰**(见 §1 的表);
  I7/I8 另由 `tests/tui/test_layering.py` 机械强制(import 方向 + 子进程 textual 探针 +
  禁止 `except: pass` + reducer 字段唯一写入者),11 项注入违规全部被杀;
- `XBotv2/tui/` 下没有 `except: pass`(由上面的守卫机械检查);其余 `except` 分支逐条核对过:
  要么重新抛出、要么转成可见事件/错误,没有"只写日志继续";
- 旧 5 个测试文件删除(`git status --short | grep -c '^ D'` 应为 16,其中 11 个模块 + 5 个测试);
  新测试**不含手写 session 桩**:唯一的服务端替身是 `tests/tui/factories.py` 的 `ScriptedBackend`
  (全套件共用,它建模的是 transport 真正依赖的东西:顺序帧、重订阅游标、权威线程列表)。
  仅有的另外两个轻量替身都**不建模服务端**:`test_controller.py` 的 `FakeSleep`/`FakeClock`
  (时间)与 `test_launcher.py` 的 `FakeBackend`(只有 `close()`,用来断言 launcher 会关闭客户端);
  `RecordingView` 记录视图调用,不假装是视图;
- `XBotv2/main.py` 的 TUI 入口走新 `app.py`;
- 未产生任何 commit(按目标要求)。
