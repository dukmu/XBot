# XBotv2 完全插件化实施计划

## 目标

将 XBotv2 的 Agent runtime、server、HTTP route、session host、命令、工具、
配置、客户端通知与可选能力统一到 XCore 的 plugin、service、event 和 fiber
生命周期中。

完成态必须满足：

1. `xcore.yaml` 是插件组合关系的唯一真源，Python 不维护平行插件或路由清单；
2. 插件可以显式公开并互相导入 types、invariant、commands、events 和 service
   Protocol，具体运行时逻辑通过 XCore event 或声明过 `inject` 的 service；
3. 不跨插件导入 concrete service、manager、registry、plugin 或 router；
4. 不通过整个 XCore Context、SessionRuntime、service bag、`getattr()` 或无类型函数
   参数隐藏运行时依赖；
5. server 只是 ASGI/FastAPI carrier；各业务插件的 `protocol.py` 同时拥有 wire
   model 和 route contribution，并通过 typed operation 调用能力；
6. 配置文件只保存可序列化配置，运行时 paths、factory、parent service 和 launch
   facts 通过 typed composition-owned services 提供；
7. 插件卸载时，其 services、listeners、routes、commands、tools 和后台任务全部由
   对应 fiber 清理；
8. `protocol/` 只拥有 wire DTO 与编解码，不拥有插件或命令业务逻辑；
9. outbound event 由生产插件拥有并校验，不存在 server 业务事件总表；
10. public declaration graph 和 required-service graph 均无环。

`TYPE_CHECKING` 只是在 Python 局部循环引用时可选的实现工具，不是架构规则或
验收条件。

## 边界规则

### 1. Public export

插件包可公开以下声明类别：

- `types`：稳定数据类型、request/result、配置和快照类型；
- `invariant`：纯校验、规范化、状态转换和不变条件；
- `commands`：human command 声明及其 typed handler factory；
- `events`：事件名称及 typed payload；
- `services`：窄 service Protocol，不包含实现类；
- `protocol`：该插件拥有的 C/S wire model、route mapping 和 route contribution；
- 迁移期 `contracts.py` 可以保留，但只能包含上述声明。

公开符号必须由模块 `__all__` 显式列出。package `__init__.py` 只能 re-export
公开声明，不能 re-export provider、registry、manager 或 concrete service。
架构门禁不能只按文件名放行，还要确认导入符号属于该模块的公开声明。

同一插件包内部可以正常导入自己的实现模块。跨插件允许：

```python
from XBotv2.jobs import JobsPort
from XBotv2.llm import ProviderCatalog
from XBotv2.agents import AgentConfigured
from XBotv2.permissions import PermissionsPort, build_permissions_commands
```

插件根 `__init__.py` 是唯一的跨插件 Python import 面；它只从
`types/invariants/commands/events/services/contracts/protocol` 重导出显式 `__all__`
中的声明。
声明文件仍可供插件内部直接导入，但外部消费者不依赖其目录布局。

跨插件禁止：

```python
from XBotv2.jobs.registry import JobRegistry
from XBotv2.llm.service import LlmService
from XBotv2.session.manager import SessionManager
from XBotv2.permissions.plugin import PermissionsComponent
```

### 2. Runtime interaction

- service 用于稳定、单所有者、有返回值的 API；消费方声明 required/optional
  `inject`，并按 public Protocol 使用；
- event 用于生命周期、多播通知、扩展点和 typed operation dispatch；
- HTTP/session operations 是 event 上的 typed request/result，不携带 runtime/context；
- optional service 可以在已声明 optional inject 后通过 `ctx.get(..., strict=False)`
  解析，禁止未声明 lookup；
- 禁止 `ctx.services.get()`、`getattr(ctx, "service")`、service `__getattr__` 代理、
  `.registry` 泄漏和 manager 私有字段；
- Tool 只能接收模型参数或标准 typed invocation context，不通过任意
  `injected: dict[str, Any]` 和隐藏 keyword 参数注入相邻插件实现。

### 3. Configuration

- Loader 拥有 plugin tree、profile、overlay、reload 和 fiber bookkeeping；
- plugin config 只能由 XCore 调用 `apply(ctx, config)` 提供；插件不得扫描
  `xcore.yaml`、硬编码自己的 entry id 或重新实现 layer merge；
- Settings 拥有全局/workspace/session domain config 与持久化 policy；
- `/reload` 属于 Loader，因为它重载 plugin tree 和 workspace overlay；
- paths、session launch options、application factory、parent permissions 和 client
  event sink 是 composition-owned services，不写入 YAML config。

### 4. Plugin-owned protocol and outbound events

- route contribution 是 server carrier 拥有的 public event contract；server 不反向
  导入任何业务插件；
- 每个业务插件的 `protocol.py` 拥有该能力的 request/response/event wire models、
  FastAPI mapping 和 route contribution plugin；
- `protocol.py` 只从其他插件根导入公开声明，通过 root/session typed operation 或
  声明的 service Protocol 调用能力，不读取 child services；
- `http_transport/` 被删除，不再存在平行 adapter ownership；
- 中央 `XBotv2.protocol` 只拥有 hello/health/error、SSE envelope/framing 和版本，
  不拥有业务 request/response，也不维护所有业务 event payload 的总表；
- 能力发布 producer-owned typed outbound event；通用 session stream bridge 只负责
  排序、重放、背压和 SSE 编码，不解释 jobs/agents/history 等能力语义。

## 当前状态与阻塞

已完成并保留为后续迁移基线：

1. architecture checker 已检查 public declaration symbol、package-root re-export、
   inject/service access 一致性和 required-service graph；
2. Agent catalog、runtime/controller、loop factory 和 subagent integration 已拆分，
   Agent profile required-service graph 无环；
3. Tools public service 已移除 `.registry`/`__getattr__`，主要消费者改用窄 Protocol；
4. command owner 已用 typed factory 捕获声明依赖，`CommandContext` 已删除；
5. Loader 拥有 reload，application 提供 typed launch ports，LLM 不再反向扫描 tree；
6. config 拥有 policy persistence，permissions/sandbox 只更新各自 policy；
7. capability HTTP routes 已迁入各 owner 的 `protocol.py` 并派发 typed
   operations；server 只拥有 route contribution 与 ASGI carrier；
8. 平行 `ServerEvents` registry 已删除，MCP 已改为显式 service 注入；
9. package root 只 re-export 已声明的 contracts/commands/service Protocol。
10. SessionRuntime 已改为持有窄 `AgentApplicationPort`，不再保存 XCore Context 或
    service bag；状态槽通过 `application/status-slots/collect` typed event 聚合，Goal
    已迁移为事件贡献者。

tool policy 已完成收敛：`BEFORE_TOOL_CALL` 只允许重写 ToolCall/args，schema
validation 和全部 guard 必须在执行前通过；`ToolDecision`/`ToolAction`、事件拒绝、
stop 和伪造结果入口已删除。

当前 architecture scanner 已达到 **0 项**。业务协议所有权迁移已完成：

1. Session 根公开 `SessionHostPort`、host request/result、summary、stream event 和
   公开错误；
2. `SessionManager` 实现 transport-neutral host API，内部拥有 runtime、persistence、
   parent permission、history lock、media、interaction waiter 和 stream lifecycle；
3. Session/Thread summary 不再使用 protocol Pydantic DTO；
4. Session route implementation 与 wire DTO 已合入 `session/protocol.py`；
5. agents/agentloop/commands/config/jobs/llm/session 各自拥有 `protocol.py`，usage、
   interactions、permission_request 拥有各自 wire event/request models；
6. `xcore.yaml` 直接激活 `<plugin>.protocol`，集中 `http_transport` 已删除；
7. server 不再提供 `web_server` 兼容 service，也不把 manager/paths/services 写入
   FastAPI `app.state`。

扫描零违规只是当前门禁的证据，不代表最终完成。下一步依次收敛
producer-owned typed outbound events、ACP 对 concrete SessionManager/runtime 的依赖，
再执行全仓配置硬编码、Context/Any 逃逸、直接声明模块导入和 inject 一致性审计。

扫描器之外仍需在最终审计处理：policy update 的 inactive-session/active-jobs
语义、ACP 的 concrete composition import、producer-owned outbound event 完整迁移，
以及真实 provider/interactive smoke。不得为通过旧测试恢复 app.state、`.registry`
或 SessionRuntime service bag。

## 目标插件目录

### A. Composition-owned ports

| Service | 提供方 | 消费方 | Public declaration |
|---|---|---|---|
| `runtime_paths` | application launcher | config/session/persistence host | `RuntimePaths` |
| `session_launch` | Agent application launcher | config/session/agent runtime | immutable `SessionLaunch` |
| `server_options` | server launcher | session host/server protocol | `ServerOptions` |
| `agent_application_factory` | application composition | session host | `AgentApplicationFactory` |
| `child_applications` | Agent application composition | subagent adapter | `ChildApplicationsPort` |
| `client_events` | client/application adapter | approval/interactions/outbound bridge | `ClientEventsPort` |
| `parent_permissions` | parent composition | child permissions | optional `PermissionsPort` |

这些对象在 root `prepare()` 中通过 `ctx.set()` 提供，并由消费者 `inject`；YAML
只选择插件和传入普通配置。

### B. Session infrastructure

| Plugin | 职责 | 目标 inject | Public export | Runtime surface |
|---|---|---|---|---|
| `loader` | tree/profile/overlay/reload/fiber | none | tree/reload types, invariants, `LoaderPort`, reload command/event | `loader`; `/reload` |
| `config` | domain settings 与 policy persistence | `runtime_paths`, `session_launch` | config/policy types, merge invariants, `SettingsPort`, policy operations/events | `settings` |
| `commands` | slash-command registry/dispatch | none | command types/invariants, `CommandsPort`, list/execute operations | `commands` |
| `llm` | provider adapters/catalog 与 active model port | optional `commands` | provider/model types/invariants, `LlmCatalogPort`, `ModelPort`, selection operations, commands | `llm`, `model`; `/provider`, `/model`, `/effort` |
| `tools` | Tool registration/schema/guard/execute | none | tool types/invariants, `ToolsPort`, list operation | `tools` |
| `agentloop.factory` | 构造 provider-neutral loop driver | none | loop factory types, `AgentLoopFactoryPort` | `agent_loop_factory` |
| `agents.catalog` | Agent definitions/profile registry | none | Agent types/profile invariants, `AgentCatalogPort` | `agent_catalog` |
| `session` | identity、loop state、storage、history | `runtime_paths`, `session_launch`, `commands` | session/history types/invariants, `SessionPort`, operations, commands | `session`, `loop_state`, storage/path views; `/status`, `/clear`, `/undo`, `/fork` |
| `persistence` | hydrate/persist session state | `loop_state`, `thread_paths` | record types/invariants, `PersistencePort`, reader Protocol | `persistence`, private store |
| `usage` | token/turn accounting | `thread_paths`, `loop_state` | `UsageSnapshot`, invariants, `UsagePort` | `usage` |
| `agents.runtime` | resolve active Agent/model and create/reconfigure loop | `agent_catalog`, `agent_loop_factory`, `settings`, `llm`, `model`, `tools`, `loop_state`, `loader`, `commands` | runtime types/invariants, `AgentRuntimePort`, operations/events, command | `agent_runtime`, `engine`; `/agent` |
| `context_builder` | provider-neutral context assembly | none | component types/invariants, `ContextBuilderPort`, events | `context_builder` |
| `prompts` | fiber-owned prompt fragments | `context_builder` | fragment types/invariants, `PromptsPort` | `prompts` |
| `jobs` | background lifecycle/limits/output | `commands` | job types/invariants, `JobsPort`, `JobRunner`, operations/events, commands | `jobs`; `/tasks`, `/task` |
| `sandbox` | path/network/shell isolation guard | `tools`, storage/path facts, `commands` | sandbox types/invariants, `SandboxPort`, command | `sandbox`; `/sandbox` |
| `permission_request` | one-shot approval coordination | `client_events` | approval types/invariants, `ApprovalPort` | `approval` |
| `permissions` | permission intersection/tool guard | `tools`, `approval`, `variables`, `commands`; optional `parent_permissions` | permission types/invariants, `PermissionsPort`, command | `permissions`; `/permission` |
| `interactions` | generic user-input coordination | `tools`, `client_events` | interaction types/invariants, `InteractionsPort` | `interactions` |
| `content_cache` | model request content externalization | `storage` | cache types/invariants | model-request event adapter |

### C. Optional capability plugins

| Plugin | 职责 | 目标 inject | Public export / runtime surface |
|---|---|---|---|
| `coretools` | filesystem/shell/base Tools 和 result cache hook | `tools`, `storage`, `sandbox`, `jobs`, `workspace_root` | tool declarations/invariants；Tool registrations |
| `agents.builtins` | built-in/data-root Agent definitions | `agent_catalog`, `data_root`, `variables` | definitions；catalog registrations |
| `agents.subagents` | subagent jobs/tools/prompt | `agent_catalog`, `session`, `jobs`, `tools`, `prompts`, `child_applications` | subagent types/invariants；spawn/list/wait/read/cancel Tools |
| `browser` | search/fetch/browser Tools | `tools`, `sandbox`, `variables` | browser/network types/invariants；Tools |
| `compact` | compaction policy/proposal/commit | `tools`, `commands`, `model` | compact types/history invariants/events/command；Tool + `/compact` |
| `goal` | persisted goal state machine | `tools`, `commands`; XCore built-in `state` | goal types/transition invariants/command/events；Tools + `/goal` |
| `todolist` | persisted todo state | `tools`; XCore built-in `state` | todo types/transition invariants；Tool |
| `skills` | Skill discovery、prompt command、scope guard | `tools`, `commands`, `sandbox` | Skill types/path/scope invariants；dynamic commands/Tools |
| `mcp_plugin` | MCP lifecycle、Tool bridge、sampling/elicitation | `tools`, `session`; optional `model`, `interactions` | MCP types/handshake invariants；dynamic Tools |
| `token_manager` | request budget projection | none | token snapshot/accounting invariants；model event listeners |
| `workspace_instructions` | AGENTS.md、workspace Agent overlay/plugin patch | `loader`, `variables`, `workspace_root`; optional `agent_catalog` | instruction/overlay types/invariants；context/reload listeners |

### D. Server and plugin protocols

| Plugin | 目标 inject | Public/runtime surface |
|---|---|---|
| `persistence.host` | `runtime_paths` | typed `StateReaderFactory` |
| `session.host` | `state_reader_factory`, `runtime_paths`, `agent_application_factory`, `server_options` | `SessionHostPort`; root-to-child typed operation routing |
| `server` | none | server route contribution event、ASGI app、generic error mapping |
| `server.protocol` | `server`, `server_info` | hello/health wire models 与 routes |
| `session.protocol` | `server`, `session_host`, `server_options` | session/thread/message/history/SSE wire models 与 routes |
| `agents.protocol` | `server` | Agent list/select/reload wire models 与 routes |
| `llm.protocol` | `server`, `llm` | provider list/select/effort wire models 与 routes |
| `config.protocol` | `server` | reload/persisted/effective policy wire models 与 routes |
| `jobs.protocol` | `server` | task event/list/stop wire models 与 routes |
| `agentloop.protocol` | `server` | Tool/turn/assistant wire models 与 Tool list route |
| `commands.protocol` | `server` | command wire models 与 list/execute routes |
| `usage.protocol` | none | usage event wire model |
| `permission_request.protocol` | none | approval request/response/event wire models |
| `interactions.protocol` | none | user-input request/response/event wire models |

Protocol plugin 的 required services 只用于激活门控，具体调用仍走 typed XCore
operation。缺失 operation handler 返回明确 `capability_unavailable`，不 fallback 到
manager 或 service lookup。

## Command ownership

| Owner | Commands |
|---|---|
| `loader` | `/reload` |
| `session` | `/status`, `/clear`, `/undo`, `/fork` |
| `agents.runtime` | `/agent` |
| `llm` | `/provider`, `/model`, `/effort` |
| `jobs` | `/tasks`, `/task` |
| `permissions` | `/permission` |
| `sandbox` | `/sandbox` |
| `compact` | `/compact` |
| `goal` | `/goal` |
| `skills` | dynamic prompt commands |

Command owner 负责注册与卸载。需要其他能力时，handler factory 捕获 owner plugin
声明的 service Protocol，或派发 typed operation；不得传入万能 CommandContext。

## 实施阶段

### Phase 0: 固化边界与门禁

1. 以本文件为职责真源，更新 architecture checker 和测试；
2. 允许显式 public exports，禁止具体实现 import；
3. 检查 package re-export 只指向 public declaration；
4. 检查 plugin 的直接 service access 与 required/optional inject 一致；
5. 检查 required-service provider graph 无环；
6. 检查 capability route 只位于 owner `protocol.py`，server 不导入业务插件；
7. 修正旧测试仍发送 `server/route` 而实现监听 `http/route` 的失败。

验收：architecture checker 不误报合法声明导入，并能报告 Agent/Session 环、
manager/router import、未声明 service 和实现 re-export。

### Phase 1: 解除 Agent 组合环

1. 将 `AgentsService` 拆为 catalog、runtime/controller 和 subagent integration；
2. `agentloop.factory` 提供 `agent_loop_factory` service，不再依赖 agents；
3. Session 不再拥有 Agent catalog 或 child application；
4. Agent runtime 消费 `loop_state` 与 factory，创建并提供 engine；
5. application 只提供 typed launch/composition ports 并装载 tree，不手工拼装 Engine；
6. 更新 `xcore.yaml` entries 与 subagent disabled profile。

验收：Agent profile required-service graph 无环；正常、resume、subagent startup
均通过；Agent catalog 不导入 Session/LLM 实现。

### Phase 2: Public services 与命令边界

1. 为 loader/settings/tools/jobs/agents/session/persistence/permissions/sandbox/
   interactions/approval/prompts 定义窄 Protocol；
2. 删除 service `__getattr__`、公开 `.registry` 和跨插件 registry 访问；
3. ToolsPort 补齐 namespace registration、restriction、inspection 等真实需求；
4. JobsPort 补齐 create/wait/read/cancel/busy 等 domain adapter 需求；
5. 用 typed handler factory/operation 替代万能 CommandContext；
6. Loader 删除 command proxy、plugin object lookup 和 `status_slots` 反射惯例，状态
   聚合改为 typed event contribution。

验收：生产代码无 `ctx.services`、跨插件 `.registry`、service `__getattr__` 或
SessionExecutionContext；命令执行不携带 runtime scope。

### Phase 3: 配置与 policy 真源

1. composition root 提供 typed runtime paths/session launch/parent ports；
2. 删除 YAML config 中 Python 对象插值；
3. 删除 LLM 对 `DEFAULT_TREE` 和字面量 entry id 的扫描；
4. Loader 校验并重载 plugin config，失败保持 last-good fiber；
5. `/reload` 迁到 Loader，workspace overlay 通过 LoaderPort 应用；
6. Settings 统一拥有 policy persistence；permissions/sandbox 订阅 typed policy changed
   event，各自只更新自身状态。

验收：插件不读取其他插件配置、不解析全局 tree、不重建 launch facts；改变 entry
id 不影响插件实现。

### Phase 4: Plugin-owned C/S protocol

1. 将业务 wire models 和 routes 迁入各 owner 的 `protocol.py`；
2. route contribution contract 迁到 server public events/types；
3. protocol plugins 只做 wire DTO 与 typed operation 转换；
4. SessionHost 提供 typed open/query/close/dispatch operations；
5. persistence host 提供 typed reader，删除 router 对 paths/private store 的访问；
6. 删除集中 `http_transport`、`web_server` compatibility service 和旧 router modules。

验收：server package 不导入 capability 实现；protocol plugin 不导入
manager/service implementation；route mount/unmount/collision 由 fiber 测试证明。

### Phase 5: Outbound events

1. 为 message/history/jobs/agents/compact/approval/interactions 定义 producer-owned
   typed events；
2. 发布前验证 payload，未知类型不静默透传；
3. 建立 capability-neutral session stream bridge；
4. 删除 `server/events.py`、`server/event_registry.py` 和 server profile entry；
5. 删除 SessionRuntime 对 jobs/history/agent/completion 的业务投影；
6. 保留 reconnect/resume/mailbox/history 各自独立语义。

验收：新增 capability event 不修改 server/session central inventory；SSE 顺序、断线、
重连、completion 不自动新开 turn 等行为有测试。

### Phase 6: Optional capabilities 与客户端

1. skills/MCP 补齐 required/optional inject 并只消费 public Protocol；
2. MCP sampling 使用 active ModelPort，elicitation 使用 InteractionsPort；
3. coretools/browser/skills 删除任意 injected kwargs，改用 typed binding；
4. compact/goal/todolist 将 state/invariant 声明从实现中分离；
5. ACP 改为 typed application/session operations，不导入 manager/persistence/config
   实现；
6. TUI/Web 保持 wire client 边界，删除残留 protocol business helpers。

验收：可选插件能独立装卸；ACP/TUI/HTTP 对同一 operation 保持一致结果与错误语义。

### Phase 7: 文档与完整验证

1. 更新 architecture、plugins、public API、protocol 与 development log；
2. 运行 architecture checker、compile、XCore tests、XBotv2 core/integration/full suite；
3. 对 provider streaming、Tool permission、SSE reconnect、subagent lifecycle 和
   interactive client 执行相应 smoke tests；
4. 检查 OpenAPI、route inventory、public exports、plugin profiles 和实际启动状态；
5. 记录未运行的真实 provider/evaluation 及原因，不用单个 green suite 代替。

## 最终审计命令

```bash
.venv/bin/python scripts/check_architecture.py --scope all
python -m py_compile <all changed Python files>
PYTHONPATH=XBotv2 .venv/bin/pytest XCore/tests -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/core -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests/integration -q
PYTHONPATH=XBotv2 .venv/bin/pytest XBotv2/tests -q
```

完成不能仅依据测试数量。还必须逐项确认：public export graph、required-service
graph、plugin profiles、HTTP route owner、outbound event owner、配置真源和客户端边界
均与本计划一致，且不存在保留中的兼容旁路。
