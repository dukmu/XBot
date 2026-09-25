# XBotv2 数据模型重设计

本文从领域事实出发定义 XBotv2 真正需要的数据类型。它不是现有类名清单，也不以兼容现有实现为目标。现有测试只能验证迁移后的行为，不能决定类型是否继续存在。

## 1. 设计约束

1. 一个领域语义只有一个模型。协议 DTO、持久化记录和 UI 模型可以存在，但必须明确是投影或容器，不能重新发明领域语义。
2. 领域字段由拥有该语义的包定义。core 不认识插件，protocol 不拥有业务规则，客户端不反推服务端语义。
3. 不用 `dict[str, JsonValue]`、`additional_kwargs`、动态属性或 `x or default` 承载已知语义。开放字典只允许出现在真正不透明的外部扩展边界，并由边界所有者立即验证。
4. `None` 只表达领域上的“没有”，不表达“调用者忘了传”“旧版本可能没有”或“稍后再猜”。状态互斥时使用判别联合，不使用一组 optional 字段。
5. 数据在创建时完整。后续阶段通过构造新值表达状态变化，不向已有 payload 动态注入字段。
6. 跨层只传领域对象或显式端口。wire 编解码和持久化编解码是边界函数，不成为第二个业务模型。

## 2. 公共地基

### 2.1 消息

provider 的 `system/user/assistant/tool` 是请求协议角色，不是持久领域分类。持久 conversation 需要以下判别联合；provider role 只由 context compiler 生成：

- `HumanInputMessage`
  - `id: MessageId`
  - `input_id: InputId`
  - `parts: tuple[TextPart | ImagePart, ...]`
  - `artifacts: tuple[ArtifactRef, ...]`
- `RuntimeNoticeMessage`
  - `id: MessageId`
  - `notice_id: NoticeId`
  - `source: RuntimeSource`
  - `event: RuntimeEventKind`
  - `parts: tuple[TextPart | ImagePart, ...]`
  - `artifacts: tuple[ArtifactRef, ...]`
  - 它是明确的模型可见内部输入，不是 UserMessage；是否唤醒 turn、何时投递属于 inbox 调度命令，不属于消息本身。
- `AssistantMessage`
  - `id: MessageId`
  - `parts: tuple[TextPart | ReasoningPart | ToolCall, ...]`
  - `exchange: ModelExchange`，唯一包含本次请求观察、计费计数、timing、stop 与 provider extensions。
- `ToolMessage`
  - `id: MessageId`
  - `call: ToolCallRef`，字段为 `id`、`name`。
  - `outcome: ToolOutcome`
  - `timing: ToolTiming`
- `CompactionSummaryMessage`
  - `id: MessageId`
  - `summary: str`
  - 它是持久、模型可见且客户端可见的压缩结果；provider compiler 将它编译为 system message，客户端投影为 summary record。

system prompt、开发者指令和 workspace/context component 不进入 `ConversationMessage`；它们属于一次 provider request 的构建材料。`MessageId` 在消息构造时生成并终身不变；同一 thread trajectory（包括 surface、transcript 与 replacement lineage）内不得重复，fork/thread 之间不要求建立全局 id registry。`client_events` 和 `turn_complete` 不属于消息，必须移出。

内容类型只保留：

- `TextPart(text)`
- `ReasoningPart(text, provider_extensions)`
- `ImagePart(image: ImageRef)`；持久数据只有逻辑 artifact id，不含路径。
- `ToolCall(id, name, args)`，只允许出现在 AssistantMessage。

`content`、`reasoning`、`tool_calls` 可以是只读派生属性，不能再作为可写的第二套内容表示。

### 2.2 工具执行

工具调用、工具结果和执行期副作用是不同语义：

- `ToolCall(id, name, args)`：模型提出的调用。
- `StructuredToolOutput` 是下层 Protocol（`kind`）；每个 tool owner 定义并注册自己的结构化 result codec。
- `ToolOutput(parts: tuple[TextPart | ImagePart, ...], structured: StructuredToolOutput | None, artifacts)`：一套权威输出；不接受 ReasoningPart/ToolCall，图片不再另存 images，已知结构化结果不落入开放 data dict。
- `ToolOutcome` 判别联合：
  - `ToolSucceeded(output: ToolOutput)`
  - `ToolFailed(error: ToolError, output: ToolOutput)`，output 允许携带失败前已产生的有效输出。
  - `ToolDenied(reason: str)`
  - `ToolCancelled(reason: str)`
- `ToolExecution(message: ToolMessage, events: tuple[ClientEvent, ...], directive: TurnDirective)`：执行管线产物；message 组合唯一的 outcome，不复制结果字段。
- `TurnDirective = ContinueTurn | CompleteTurn`。

模型上下文和 UI 展示都从 `ToolOutcome` 投影。XML/preview 等格式是 projector 的输出，不持久化第二份 `model_content/display_content`。这样 `ToolResult` 不再先伪装成 `Message`，也不存在“成功但带 error”“失败但无 error”的非法组合。

### 2.3 历史与分页

- `ConversationHistory`
  - `surface: tuple[ConversationMessage, ...]`
  - `transcript: tuple[ConversationMessage, ...]`
  - `seen_ids: frozenset[MessageId]`
  - `lineage: Mapping[MessageId, tuple[MessageId, ...]]`，只为当前 replacement 记录其对应的原 transcript 连续区间。
  - `surface_revision: HistoryRevision`
  - `transcript_revision: HistoryRevision`
  - 只暴露 `append`、`replace_range`、`clear`、`undo` 和只读 snapshot。
- `HistoryPage[T]`
  - `items: tuple[T, ...]`
  - `older_cursor: Cursor | None`
- `TrajectoryRead`
  - `page: HistoryPage[TrajectoryEntry]`
  - `newest_position: int`
- `TrajectoryEntry` 判别联合：
  - `MessageAppended(position, message)`
  - `SurfaceReplaced(position, operation, transcript_policy, source_ids, replacements)`
  - `DurableEventRecorded(position, timestamp, event)`

history replacement 必须满足：source ids 在当前 surface 连续；replacement id 从未出现；非 preserve replacement 在 transcript 中匹配完整 lineage；preserve replacement 只把一条 summary 映射到原连续区间。持久化先验证 prospective fold，再原子追加，再发布内存状态；崩溃只能留下旧状态或完整新 record。

`ConversationPage`、`ThreadMessagesResponse.next_cursor`、`history_cursor` 等平行定义全部由 `HistoryPage[T]` 取代。attach、history mutation 和 `/messages` 都携带同一个 page。字节输出、目录列表等不共享这个类型，因为它们的 EOF/cursor 语义不同。

### 2.4 事件

内部不再存在 `ClientEvent(type: str, data: dict)`。

- `ClientEvent` 是只读 Protocol，只要求具体事件声明 `kind`；普通事件不自带关联 id。
- 每个插件定义自己的具体事件 dataclass/Pydantic model，字段直接位于事件上，没有 `data` 字典。
- `EventScope = SessionScope | TurnScope(turn_id)`；`SessionEventFrame(sequence, scope, event)` 是唯一运行时 envelope。input id、turn id 和 interaction id 是三个不同 value type。
- interaction payload 自有 `interaction_id`，用于 waiter/response；它不与 frame 的 turn scope 复用。
- `ServerEvent(protocol_version, session_id, thread_id, sequence, scope, kind, payload)` 只存在于 HTTP/SSE 边界；`payload` 由事件 registry 序列化。
- 客户端使用同一 registry 把 `kind + payload` 解成具体 payload。未知插件事件可保留为 `OpaqueServerEvent`，但服务端内部不得使用 opaque 事件处理已知语义。

engine 只产出 `LoopTurnEnded(outcome)`。session/application 组合层取得 status slots 与 stats 后，一次构造完整的 `SessionTurnFinished` 或 `SessionTurnCancelled`；engine 不反向依赖 session 数据，也不先发半成品再注入。

### 2.5 可见会话记录

历史读取和 live 完成事件共享一个 transport-neutral read model `ConversationRecord`，由 session application projection 层拥有，protocol 只编码：

- `HumanInputRecord(id, content, images, artifacts)`
- `RuntimeNoticeRecord(id, source, event, content, images, artifacts)`
- `AssistantRecord(id, content, reasoning, tool_calls, timing, stop)`
- `ToolRecord(id, call, outcome, timing)`
- `CompactionSummaryRecord(id, summary)`

投影函数只有一个：`project_message(ConversationMessage) -> ConversationRecord`。`SessionHistoryItem`、`MessageData`、`AssistantMessageData`、旧 wire `agentloop.protocol.ToolResultData` 四套重复模型删除。流式 delta 和 tool-call-started 是生命周期事件，不是假装成完整记录，继续使用各自模型。

### 2.6 共享值类型的完整字段

以下共享类型不得留给实施阶段重新发明：

- `ProviderExtensions(provider: str, payload: Mapping[str, JsonValue])`：只由对应 provider adapter 生产和读取；其他层只能原样携带。
- `ToolError(code: str, message: str, retryable: bool, details: Mapping[str, JsonValue])`：details 只属于外部工具错误扩展，业务层不以 key 分支。
- `ModelStop = Completed | LengthLimited(limit_kind) | ToolCallsRequested`；cancel/failure 是 stream terminal event，不能伪装成已完成 response 的 stop reason。
- `GenerationSettings(mode: GenerationMode, temperature: float | None, max_output_tokens: int)`；temperature 的 None 明确表示 provider default。
- `ProviderMessage = ProviderSystem(parts: tuple[TextPart, ...]) | ProviderUser(parts: tuple[TextPart | ResolvedImagePart, ...]) | ProviderAssistant(parts: tuple[TextPart | ReasoningPart | ToolCall, ...]) | ProviderTool(call_id, parts: tuple[TextPart | ResolvedImagePart, ...])`；`ResolvedImagePart(ref: ImageRef, absolute_path)` 仅存在于请求构建期，context compiler 使用 ArtifactStore 将 ImageRef 单向解析为它。
- `ToolSchema(name, description, parameters)`；parameters 是 JSON Schema 这一外部标准的原始对象，只有 tool registry/provider adapter 读取。
- `HistoryRevision(value: str)`、`Cursor(value: str)`、`MessageId/InputId/TurnId/InteractionId/NoticeId/ToolCallId` 均为不可混用的 value type。
- `TransactionRef(kind, id)`、`TransactionOutcome = Committed | Aborted(reason) | TransactionFailed(error)`、`TransactionStarted(transaction: TransactionRef)` 与 `TransactionEnded(transaction: TransactionRef, outcome: TransactionOutcome)`；具体持久事件由 owner 注册 codec，不从 payload dict 抽取 id。
- `ResolvedModelSelection(route: ModelRoute, generation: GenerationSettings, context_window: int)` 与 `AgentExecutionLimits(max_turns, max_tool_calls, timeout_seconds)`。
- `ResolvedRuntimeSelection(agent_name, prompt, limits: AgentExecutionLimits, enabled_tools, model: ResolvedModelSelection)` 是 thread 唯一 effective runtime config；model token limits 只在 model selection 中，`limits` 只表达 agent 执行限制。它们属于 core contract，由 llm/agents service 构造。

共享类型清单同时定义 owner：message/history/tool/artifact/usage/provider-neutral request 属于 core；LoopEvent 属于 agentloop；ConversationRecord 属于 session application projection；具体 feature event、interaction、job spec/result 属于各插件；wire envelope 属于 protocol；磁盘 envelope 属于 persistence。

## 3. 分包/插件类型需求

### 3.1 core

需要：

- 上述消息、内容、工具、artifact、usage、timing、request observation、历史、分页和 trajectory 类型。
- `ArtifactRef(id, media_type, name, kind, size, sha256)`：持久化逻辑标识；`ResolvedArtifact(ref, absolute_path)`：仅请求构建期存在。
- `ImageRef(artifact_id, media_type, size)` 与仅运行时的 `ResolvedImagePart(ref, absolute_path)`；持久化不保存机器相关绝对路径。
- `TokenCounters(input, output, cache_read, cache_create, prompt_cache_write)`：只含可相加计数。
- `ObservedContext = ProviderMeasured(tokens) | MeasurementUnavailable(reason)`；估算不是 observed 的 fallback 值。
- `RequestObservation(selection: ResolvedModelSelection, purpose, estimated_input_tokens, observed_context)`，其中 purpose 为 `TurnRequest(turn_id) | AuxiliaryRequest(owner, operation_id)`；route/context window 不再复制成另一组字段。
- `UsageDelta(counters)` 只含本次可相加计数。
- `UsageSnapshot(total_counters, requests, latest_turn_observation)`；初始 latest 为真正的 None。auxiliary request 可以累计 counters，但绝不替换 latest turn observation。
- `ModelRoute(provider, model)`；`GenerationMode = Standard | Reasoning(effort)`；显示用 `model_mode` 只能派生。
- `ModelExchange(observation: RequestObservation, usage: UsageDelta, timing: ModelTiming, stop: ModelStop, provider_extensions: ProviderExtensions)`；这是 AssistantMessage 唯一请求侧信息。
- `ModelTiming(total_ms, first_delta_ms: int | None)`、`ToolTiming(duration_ms)`；first_delta 为 None 只表示确实没有观察到任何 delta，不用 0 冒充测量；`decode_ms` 需要时由 `total_ms - first_delta_ms` 派生。
- `Operation[Request, Response]`：字段仅为 `name`、`request_type`、`response_type`、`exclusive_policy`。
- `RuntimePaths`、`SessionPaths`、`ThreadPaths`：只负责路径解析。

设计：

- 删除 `Message.additional_kwargs`、泛化 `data`、`client_events`、`turn_complete`。
- request observation、runtime provenance、tool display content 不再塞入 metadata。
- `Message` 不再需要 `seal()`；领域值从创建起不可变，改写通过新值和 history replacement 表达。
- artifact externalization 始终完整保存原文；消息只持逻辑 ArtifactRef，provider request 通过当前 ArtifactStore 与 RuntimeVariables 解析绝对路径。Agent authored tool arguments 永不 externalize 或 truncate。

### 3.2 agentloop

需要：

- `InboxItem(id, target, input)`，其中 `input = HumanInput(content, images, artifacts) | RuntimeInput(source, event, content, images, artifacts)`；`target` 只决定 next-turn/next-step。
- `InboxChange` 判别联合：`Inserted`、`Edited`、`Removed`、`Retargeted`、`Claimed`、`Consumed`、`Discarded`；每个变体只携带其必需字段。
- core-owned `ResolvedRuntimeSelection`；loop 直接消费它，并另收本 turn 的 `user_identity`、memory 与 workspace context，不复制 route/generation/context window。
- provider-neutral `ModelRequest` 只在 core provider contract 定义一次：`messages: tuple[ProviderMessage, ...]`、`tools: tuple[ToolSchema, ...]`、`selection: ResolvedModelSelection`。
- `LoopEvent` 判别联合：`LoopTurnStarted`、assistant delta、assistant completed、tool calls started、tool call delta、tool completed、usage、`LoopTurnEnded(outcome)`、error。finished/cancelled 是 `outcome` 的变体，不再另设三个可能冲突的终态事件。
- 每个 hook boundary 都有独立的输入与返回联合，不共享万能 context：
  - `OnTurnInput(input, history)` → `AcceptInput | RejectInput(error) | CompleteTurn(result)`。
  - `BeforeContextBuild(request)` → `KeepContextRequest | ReplaceContextRequest(request) | CompleteTurn(result)`。
  - `AfterContextBuild(context)` → `KeepContext | ReplaceContext(context) | CompleteTurn(result)`。
  - `BeforeModelRequest(request)` → `KeepRequest | ReplaceRequest(request) | CompleteTurn(result)`。
  - `AfterModelResponse(request, response)` → `KeepResponse | ReplaceResponse(response) | CompleteTurn(result)`。
  - `OnModelFailure(request, error)` → `PropagateFailure | RetryRequest(request) | CompleteTurn(result)`。
  - `BeforeToolCall(call)` → `KeepToolCall | ReplaceToolCall(call) | CompleteTurn(result)`。
  - `AfterToolExecution(execution)` → `KeepExecution | ReplaceExecution(execution) | CompleteTurn(result)`。
  - `ObserveInbox(change)` 没有返回值；observer 不能改变投递或伪造处理结果。

设计：

- 删除含 20 个 optional 字段的 `EventContext`；上述每个 input 只含该 hook 保证存在的字段。
- engine 返回 `AsyncIterator[LoopEvent]`，不返回 dict。
- assistant id 就是 `AssistantMessage.id`；删除 `xbot_message_id` 注入。
- tool pipeline 返回 `ToolExecution`，不再 deepcopy 一个带运行时字段的 Message。

### 3.3 persistence

需要：

- canonical `TrajectoryEntry` 本身就是 append/replace/event 的唯一 mutation 模型。
- `StoredTrajectoryRecord(schema_version, entry: TrajectoryEntry)` 是磁盘 envelope，不重复 entry 字段。
- `InboxSnapshot(version, items: tuple[InboxItem, ...])`。
- `ThreadLifecycleEvent` 判别联合：`ThreadStarted(thread, parent, agent, at)`、`ThreadCompleted(thread, at)`、`ThreadFailed(thread, at, error)`、`ThreadCancelled(thread, at, reason)`。
- `ThreadMetadata(runtime_selection, parent_thread, workspace_root, title)`；`runtime_selection` 是该 thread 唯一持久化的有效 agent/model 选择。
- 插件 state/artifact 端口。

设计：

- 删除逐字段复制的 `MessagePayloadRecord`；canonical message 使用一个版本化 codec 直接序列化。持久化 record 只包裹 message，不继承或复制 message 字段。
- 新 writer 永远不写空 id。默认不永久保留旧 schema 兼容路径；若产品明确要求保留已有数据，提供一次性 migration command 或有截止版本的 legacy codec，迁移完成后删除。无论采用哪种，legacy 只在该边界出现。
- `source_node_ids` 只作为旧磁盘字段名在 decoder 中存在；领域名统一为 `source_ids`。
- trajectory fold 同时验证 position 连续、source 当前、message id 在当前 thread trajectory 内唯一；错误立即终止加载。

### 3.4 application

需要：

- `ApplicationSnapshot(metadata, messages, usage, status_slots)`；有效运行时选择只从 `metadata.runtime_selection` 读取。
- `ApplicationInitialized`、`RuntimeEvent` 等少量组合事件。
- ports：driver、history commands、history reader、usage、client interaction router、child applications。

设计：

- snapshot 只出现一次；session 在其外增加 session/thread/event/pending 状态，不复制有效 agent/model 字段。
- `InteractionRouter` 保存 typed `InteractionRequest` 并直接读取其 `interaction_id`；普通 ClientEvent 不参与 waiter。
- parent/child application 共享端口，不共享可变服务对象或 dict carrier。

### 3.5 session

需要：

- `SessionKey(session_id, thread_id)`。
- `SessionRuntimeState(key, metadata, status, turn_count, event_cursor)`；workspace 与有效运行时选择都从 metadata 读取。
- `OpenSessionCommand`、`OpenThreadCommand`、`SendMessageCommand`、`RegenerateCommand`、`PendingInputCommand`。
- `OpenedThread(key, metadata, usage, status_slots, event_cursor, history: HistoryPage[ConversationRecord], pending_inputs, pending_interactions)`；不再内嵌另一份 ApplicationSnapshot、runtime 副本或 raw messages。
- `HistoryMutation(removed_turns, history: HistoryPage[ConversationRecord], stats)`。
- `SessionSummary`、`ThreadSummary` 两个不同列表视图。
- typed session events：message published、history replaced、agent configured、queue replaced、input delivery。

设计：

- 删除 `OpenedSession` 内 raw Message 与 protocol response 的平行建模；manager 直接返回领域 `OpenedThread`，HTTP 只加 envelope。
- pending interaction 使用 composition registry 中已注册的具体 `InteractionRequest`，不使用 `type + data`。
- attach、history mutation、messages endpoint 共用 `HistoryPage[ConversationRecord]`。
- runtime 不检查/修改 `event.data`；只匹配事件类型并构造新事件。

### 3.6 protocol/server

需要：

- 通用 `ResourceResponse[T]`、`HistoryPage[T]`、`ServerEvent[P]`、`ErrorResponse`；不存在没有实际用途的 RequestEnvelope。
- HTTP 特有的 base64 upload 输入类型。
- `RouteContribution(owner, router, exception_handlers)` 和 `ServerInfo/Options/Status`。

设计：

- protocol 只描述 wire，不包含命令、插件或 history 业务逻辑。
- wire DTO 通过组合 canonical projection，禁止重新声明其所有字段。
- `_format_sse` 接受 typed `SessionEventFrame`，不接受任意 dict，也不使用 `event.get(...) or ...`。

### 3.7 llm

需要：

- `ProviderConfig(protocol, endpoint, credential, default_model, models, headers)`。
- `ModelConfig(name, context_window, max_output_tokens, generation_modes, modalities, provider_options)`。
- `LlmConfig(default_provider, providers)`。
- core-owned `ModelRoute(provider, model)`、`GenerationMode` 与唯一 `ResolvedModelSelection`；llm 从配置解析并只返回该 selection。modalities/tool support 等 capability 由 `route` 查询 `ProviderCatalog`，不再复制进另一个 effective model 类型。
- `ModelDescriptor`、`ProviderDescriptor`、`ProviderCatalog`。
- core-owned `ModelRequest`。
- `ModelStreamEvent = TextDelta(text) | ReasoningDelta(text) | ToolCallDelta(call_id, name_delta, arguments_delta) | ModelCompleted(response) | ModelFailed(error) | ModelCancelled(reason)`。
- `ModelResponse(parts, usage: UsageDelta, observed_context: ObservedContext, stop: ModelStop, provider_extensions: ProviderExtensions)`；provider 不负责 wall-clock timing。
- `ProviderError(code, message, retryable, category, provider_details)`；context overflow 是 category 变体，不靠字符串匹配。

设计：

- selection request/response 组合 `ModelRoute + GenerationMode`，不重复 provider/model/mode 字段。
- provider 私有 streaming chunk 形状只留在 adapter；归一化后交出 typed `ModelStreamEvent`。每个被正常消费至结束的 stream 必须且只能产生一个 terminal event；terminal 后继续产出是 contract violation。adapter 在仍能运行并观察到取消时产出 `ModelCancelled`；若调用方直接 cancel task/关闭 iterator，provider 不可能继续 yield，engine 负责把该 out-of-band cancellation 收口为 cancelled `LoopTurnEnded`。
- engine 记录开始、TTFT 与完成时间，并把 `ModelResponse`、本次 `RequestObservation` 与测得的 `ModelTiming` 组合为 `ModelExchange`。adapter 不得在 extensions 再携带 usage、stop 或 error。
- 在 `ModelCompleted` 前取消或失败时，不创建/持久化 `AssistantMessage`；已经发送给 UI 的增量只是 ephemeral stream state，以 cancelled/failed lifecycle event 收口。只有 completed response 可成为持久消息。
- `extra_body` 是明确的 provider passthrough 边界，不能被 core/session 解读。

### 3.8 agents

需要：

- `AgentDefinition(name, description, mode, prompt, model_policy, limits, permission_policy, tool_policy, hidden)`。
- `AgentModelPolicy(route: ModelRoute | InheritRoute, generation: GenerationMode | InheritGeneration, temperature, max_output_tokens, context_window)`。
- `AgentToolPolicy(enabled: tuple[str, ...] | AllTools, disabled: tuple[str, ...])`。
- `AgentCatalog(active, agents)`。
- `AgentConfigured(runtime_selection: ResolvedRuntimeSelection, session_key)`。

设计：

- provider/model/temperature/context limit 组合成 `AgentModelPolicy`。
- tools/disabled_tools 组合成 `AgentToolPolicy`。
- `AgentDefinition` 只是可继承的策略输入，不是第二份 effective config。agents service 将 definition、默认值与配置来源一次解析为完整 `ResolvedRuntimeSelection`，再原子替换 `ThreadMetadata.runtime_selection`；loop、application、session 与 attach 全部读取这一对象，不维护 active agent、route 或 context window 的副本。
- protocol `AgentInfo` 是 `AgentDefinition` 的公开投影，不能再独立维护相同字段表。

### 3.9 commands

需要：

- `CommandDefinition(name, description, kind, usage, examples, parameters, effects, exclusive, handler)`。
- `CommandInvocation(definition, raw_args)`。
- `CommandOutcome(status, message, effects)`。

设计：

- `Command` 与 `CommandDescription` 合并为 definition + `public_view()`，handler 是服务组合关系，不产生第二个字段平行类。
- HTTP 只收 `raw`；解析一次后传 typed invocation。不得同时接受 `command/raw/kind` 再猜哪一套生效。

### 3.10 compact

需要：

- `CompactConfig`。
- `CompactionSelection(expected_revision, source_ids)`。
- `CompactionPlan(id, reason, selection, summary: CompactionSummaryMessage, metrics)`；replacement 恒为 `(summary,)`，prefix position 由当前 revision 下的 source ids 派生。
- `CompactionMetrics`。
- `CompactionStarted`、`CompactionCompleted`、`CompactionFailed`。
- `BeforeCompact`、`AfterCompact` hook context。

设计：

- 删除 `TypedDict CompactionProposal`；plan 是完整不可变模型，构造时必须已有 source ids，不能稍后注入。
- metrics 只定义一次；内部事件与 wire event 复用它。
- compaction durable transaction 复用 core 的 `TransactionStarted/TransactionEnded`，其 ref 为 `TransactionRef(kind="compaction", id=compaction_id)`；插件生命周期事件 `CompactionStarted/Completed/Failed` 只用于 client observation，不再建立第二套 durable transaction event。
- summary text 只存在于 CompactionSummaryMessage；completed event 和 trajectory read 都引用/投影该 message，不再保存第二份 summary string。

### 3.11 interactions

需要：

- stable lower-level `InteractionRequest` Protocol：`kind`、`interaction_id`、`resume_supported`。它不枚举上层插件类型。
- interactions 插件拥有 `UserInputRequest(interaction_id, source, tool_call_id, question, options, timeout, resume_supported)`。
- `UserInputOption(label, description)`。
- `UserInputResolution = Answered(answer) | InputTimedOut(reason) | InputCancelled(reason)`，实现 `InteractionResolution`。
- `InteractionResolution` 是 owner-defined Protocol（`kind`）；`InteractionReceipt(interaction_id, resolution: InteractionResolution, pending_ids)`。composition registry 按 request kind 注册唯一 resolution type，并在构造 receipt 时验证配对；不能让任意 request 接受任意 resolution。
- `ClientNotice(message, level, source, tool_call_id)`。

设计：

- waiter、pending snapshot、live event 使用同一个 request 类型；不再转成 `PendingInteractionData(type, data)`。
- permission resolution 由 permissions 拥有；composition 层的 registry 只负责将具体 request 路由到 waiter/codec，不建立包含所有插件的封闭 union。

### 3.12 permissions

需要：

- `PermissionRule(tool_pattern, param_patterns, path_scope, decision)`。
- `PermissionPolicy(rules: tuple[PermissionRule, ...], default_decision)`；rule order 只用于同优先级的确定性 tie-break，不用三个 bucket 重复表达 decision。
- `PermissionSubject = ToolPermission(tool_call) | NamedPermission(tool, params)`。
- `PermissionRequest(interaction_id, source, subject, reason, resume_supported)`，实现 stable InteractionRequest Protocol。
- `Approval = Allowed(scope) | Denied(reason)`，实现 `InteractionResolution`。
- `PermissionDecisionRecorded(request, approval, rule)`。

设计：

- 删除 `tool_call | permission` 两个 optional 字段；subject 是判别联合。
- guard、approval service、client interaction 共享上述类型，不经 dict 拼装或补 `tool_call_id`。
- 关联 tool call 发生在构造 request 时；删除 `_correlate_client_event` 的动态 data 改写。
- 单层求值优先级固定为：matching deny > 尚未消费的 scoped one-shot grant > matching allow > matching ask > default。one-shot grant 只在真正执行时消费，且永远不能覆盖 deny。
- 多层策略按安全 meet 合成：任一层 deny 即 deny；无 deny 但任一层 ask 即 ask；只有所有适用层都 allow 才 allow。子 Agent、session patch 与 workspace policy 都不能放宽父层限制。

### 3.13 jobs

需要：

- `JobId`、owner-defined `JobSpec` Protocol（`kind`、`label`）和 owner-defined result。
- `JobIdentity(id, owner, parent, name, created_at)`；identity 在整个生命周期中不变。
- `Job(identity, spec, state)`。
- `JobState` 判别联合：
  - `Queued`
  - `Running(started_at)`
  - `Succeeded(started_at, finished_at, result)`
  - `FailedBeforeStart(finished_at, error)`
  - `FailedRunning(started_at, finished_at, error)`
  - `CancelledBeforeStart(finished_at, reason)`
  - `CancelledRunning(started_at, finished_at, reason)`
- `JobView(id, kind, label, state, elapsed_ms, summary)` 是唯一通用公开 projection；owner 可注册额外 typed detail codec。
- `OutputPage(data, next_cursor, eof, truncated)`。
- job updated/completed typed events。

设计：

- jobs 不导入 coretools 或 subagents。`ShellJobSpec/Result` 由 coretools 注册，`AgentJobSpec/Result` 由 subagents 注册。
- 删除 metadata dict 中的 `command/cwd/agent/thread_id` 语义；它们属于 owner spec。
- `JobSummary`、`JobSnapshot` 删除；wait/list/event 复用 JobView。
- completion notice 不重复 `type` 与 `kind`。

### 3.14 subagents

需要：

- `SubagentConfig(timeout_seconds)`。
- `SubagentRequest(agent, prompt, parent_thread, interactive)`。
- `ChildApplicationResult(final_response, usage)` 是同步与异步子应用共享的唯一结果。
- `SubagentResult(thread_id, child: ChildApplicationResult)`。
- `AgentJobSpec(agent, thread_id, prompt)` 与 `AgentJobResult(child: ChildApplicationResult)`，注册给 jobs。

设计：

- 不另造一套 job 状态；异步 subagent 是 `AgentJob`，同步调用只包装 child application result。
- parent permissions 和 client interaction ports 是 launcher 调用依赖，不是 SubagentRequest 字段。

### 3.15 goal

需要：

- `GoalState` 判别联合：
  - `NoGoal`
  - `ActiveGoal(condition, started_at, progress, stats, checkin_policy)`
  - `PausedGoal(condition, started_at, paused_at, reason, progress, stats, checkin_policy)`
  - `AchievedGoal(condition, started_at, finished_at, reason, progress, stats)`
  - `FailedGoal(condition, started_at, finished_at, reason, progress, stats)`
- `GoalProgress(turns_evaluated, retries, tool_less_turns, idle_checkins, stalled)`。
- `GoalStats(tool_calls, input_tokens, output_tokens, todo_items, todo_completed)`。
- `GoalSnapshot(state: GoalState)`。
- `GoalVerdict = NotMet(reason) | Met(reason) | Impossible(reason)`。
- `GoalChanged(snapshot)` typed event。

设计：

- persistence schema migration只在 goal store；service 不读取旧字段别名 `objective/condition`、`summary/reason`。
- 状态变体决定时间、reason、progress 是否存在；不再持久化 status + 0.0 哨兵组合。snapshot 直接包裹 GoalState，不复制 progress/stats。

### 3.16 todolist

需要：

- `Task(id, subject, description, active_form, owner, status)`。
- `DependencyEdge(prerequisite, dependent)`；TaskList 持有唯一 edge 集合，`blocks/blocked_by` 都派生。
- `TaskList(version, next_id, tasks, edges)`。
- `TaskChanged(snapshot)` typed event。

设计：

- Python 字段统一 snake_case；camelCase 只在 wire alias。
- deleted 是命令，不是 `TaskStatusInput` 的伪状态。
- service 拒绝自环和循环；删除任务原子清除相关 edges。若 metadata 没有已声明消费者则删除，不把开放 dict 搬入新模型。
- 事件直接携带 `TaskList`，不通过 `projection()` dict。

### 3.17 config

需要：

- `UserContext`、`RuntimeConfig`、`PluginConfigEntry`。
- `PermissionPolicy`、`SandboxConfig` 的引用，不复制成 dict。
- `PluginConfigDescriptor`、`PluginConfigCatalog`。
- `ConfigPatch` 判别联合：plugin config patch、permission patch、sandbox patch。
- 每个可 patch 字段使用 `Keep | Set(value) | Clear`；Clear 表示移除当前层覆盖，恢复下一低优先级来源，不表示写入 None。

设计：

- 每个插件拥有自己的 config model；总配置只持有验证后的 config union/registry，不长期保存任意 dict。
- `PolicySnapshot` 组合 permissions 与 sandbox 的 canonical models。
- revision 是明确的并发控制值，不通过缺省推断。
- 标量配置合成顺序固定为 defaults → global → workspace → session → explicit runtime override；AgentDefinition 不是第二份 effective config，而是 agents service 解析 `ResolvedRuntimeSelection` 时的策略输入。
- 安全策略不使用 last-write-wins：permission 使用 3.12 的 meet；sandbox capability 取所有适用层的交集——network 只有各层都允许时才允许，path/resource access 只能缩窄，子 Agent 和 session patch 不能增加父层未授予的 capability。用户批准只产生有 scope 的 one-shot permission，不修改 sandbox policy。

### 3.18 loader

需要：

- `PluginRef(id, name)`。
- `PluginEntry(ref, config, enabled, isolation, profiles)`。
- `PluginPatch` 使用显式 `Keep | Set[T] | Clear` 变体，不使用 `_Unset` 与 `None` 双重语义。
- `PluginTree` 和 `PluginOverlay`。

设计：

- loader 只处理组合与来源，不解释插件 config 内容。
- `disabled` 改为正向 `enabled`，避免默认与 patch 反义。

### 3.19 sandbox

需要：

- `SandboxPolicy(enabled, network, workspace_access, external_access, resources)`。
- `ResourceRule(path, access)`。
- `MountSpec(source, target, access, kind, masked)`。

设计：

- config、effective policy、backend mount 是同一 policy 的不同阶段；使用显式编译函数，不维护重复规则类型和手工字段转换。
- path access 使用 enum，不用字符串默认值。

### 3.20 browser

需要：

- `BrowserConfig(search, network, session)`。
- `SearchPolicy(backend, region, safesearch)`。
- `NetworkPolicy(timeout, max_bytes, private_access)`。
- `BrowserSessionPolicy(headless, timeout)`。

设计：

- 删除与 `BrowserNetworkConfig` 平行的 `NetworkOptions`，运行器直接接收 canonical `NetworkPolicy`。

### 3.21 content_cache

需要：

- `ContentCachePolicy(threshold_chars, preview_chars, tail_chars)`。
- `ExternalizedContent(preview, original: ArtifactRef, original_chars)`。

设计：

- ContentCache 是唯一 externalization owner：先把完整 UTF-8 原文写入 ArtifactStore，再返回 ref 与 preview；失败则不改消息。cache 只返回新 message/tool execution，不原地改 Message。
- tool result 与长用户输入共用同一 externalization result，不各自发明路径字段。

### 3.22 context_builder、prompts、workspace_instructions、token_manager

需要：

- `ContextComponent` 判别联合：
  - `InlinePromptComponent(stage, source, text)`
  - `FilePromptComponent(stage, source, logical_path, text)`
  - `HistoryComponent(message: ConversationMessage)`
- `ContextBuildRequest(history, runtime_selection, user_identity, memory, sandbox_summary, runtime_paths, turn)`；agent identity、prompt/instructions 与 model limits 只从同一个 `ResolvedRuntimeSelection` 读取。
- `BuiltContext(components)`；它不再同时存 messages。`ProviderMessage` 由单向 compiler 从 components 生成。
- `TokenBudget(context_window, output_reservation, used)`；`remaining = context_window - output_reservation - used` 是唯一只读派生公式并允许为负，`over_budget` 与 `excess` 继续从 remaining 派生，不另存 totals。负值是 compact 的正常输入，不是构造错误。

设计：

- stage 是必需判别值，不靠 `plugin_name/source_path` 是否为空判断来源。
- token_manager 接收明确的 `ProviderMeasured` 或 estimator 产生的 `EstimatedContext(tokens, method)`，计算 typed budget，不向 response metadata 注水；`MeasurementUnavailable` 不会自动退化为旧 anchor 或 truthy fallback。

### 3.23 usage

需要：

- core 的 `TokenCounters`、`UsageDelta`、`UsageSnapshot` 是唯一 usage 模型。
- `UsageUpdated(snapshot: UsageSnapshot)` typed event。

设计：

- provider metadata 先转换为 `UsageDelta` 与 `ObservedContext`；累加器只相加 `TokenCounters` 并追加 `RequestObservation`。只有 `TurnRequest` 更新 `latest_turn_observation`，auxiliary request 不覆盖它；compact、goal、session 都不直接读取任意 usage dict。

### 3.24 caption

需要：

- `CaptionConfig(auto, allow_access, max_chars, output_tokens)`。
- `CaptionRequest(messages, current_title)`。
- `CaptionResult(title)`。

设计：无独立消息模型；只消费 canonical history snapshot。

### 3.25 coretools

需要：

- `CoreToolsConfig(enabled_tools, hooks, workspace_tools, result_cache_policy)`。
- `HookSpec(stage, target)`、`WorkspaceToolSpec(target)`。
- `ShellJobSpec(command, cwd, escalation)`、`ShellJobResult(output_ref, exit_code)`，注册给 jobs。
- 各工具自己的 typed input/output。

设计：

- shell/filesystem/browser 的结构化结果先成为各自结果模型，再由统一 adapter 转成 `ToolOutcome`。
- `data.get("ok")`、`data.get("error")` 只允许在外部进程响应 decoder 内出现；decoder 之后必须是判别联合 `Success | Failure`。

### 3.26 skills

需要：

- `SkillDefinition(name, description, path, instructions, tool_policy, invocation_policy, scope)`。
- `SkillToolPolicy(allowed, denied)`。
- `SkillInvocationPolicy(model_allowed, user_allowed)`。

设计：

- frontmatter dict 只存在于 parser 输入；registry 保存验证后的 canonical skill。
- sandbox runner 是端口，不是 skill 数据字段。

### 3.27 mcp_plugin

需要：

- `McpServerConfig(enabled, required, transport)`。

设计：

- 外部 MCP payload 在 adapter 中验证并直接映射为 core `ToolSucceeded` 或 `ToolFailed`；MCP 不再拥有与 `ToolOutcome` 平行的 outcome 类型。
- `is_error + content + data` 的 SDK 组合只存在于 decoder 输入，decoder 之后只能是 canonical `ToolOutcome`。

### 3.28 workspaces

需要：

- `Workspace(id, path, title, session_ids, created_at, updated_at)`。
- `WorkspaceCatalog(workspaces, archived_session_ids, revision)`。
- `DirectoryEntry`、`DirectoryPage`。
- typed catalog changes：workspace added/updated/removed/reordered、session reordered、archive changed。

设计：

- 删除 `WorkspaceRecord` 与 `WorkspaceView` 的字段复制；canonical Workspace 通过序列化策略隐藏内部字段。
- 删除与 session 包同名的 `SessionSummary` Protocol，改为 `SessionMembership` 接口，仅含 `session_id/workspace_root`。
- `WorkspaceSnapshot` 是 persistence envelope，字段为 version + catalog，不重写 workspace 字段。

### 3.29 server

需要：

- `ServerOptions(provider, workspace_root, plugin_policy)`。
- `ServerStatus(started_at, session_count, thread_count, workspace_root)`。
- `RouteContribution(owner, router, exception_handlers)`。

设计：server 只组合 routes 和 transports，不拥有 session、command、provider 数据模型。

### 3.30 ACP、TUI、Web 客户端

客户端需要自己的 UI/view state，但不应重新定义服务端记录语义。

- ACP：消费 typed `ConversationRecord` 与 typed events，映射成 ACP update；删除 `event.get/data.get` 解析器。
- TUI：
  - `TimelineEntry` 是 UI projection，允许存在。
  - `HistoryLoadState = Complete | Available(cursor) | Loading(cursor) | Failed(cursor, error)`。
  - `HeldPage(cursor, ids)` 和 `TimelineWindow(entries, anchor)`。
  - live record 与 history record 共用一个 `entry_from_record`。
  - UI 事件命名必须表达动作，如 `ToolRecordReceived`，不与 core `ToolResult` 重名。
- Web：使用与 Python client 同源生成或共享的 wire schema；不得维护一套手写、漂移的 event union。

## 4. 必须删除的重复与动态语义

1. `HistoryNode`、平行 `node_ids/messages`、`surface/messages` 双列表。
2. `SessionHistoryItem`、`MessageData`、`AssistantMessageData`、旧 wire `agentloop.protocol.ToolResultData` 四套可见记录模型。
3. `ClientEvent.data: dict`、`validated_client_event`、事件模型 registry 后再 dump 回 dict 的往返。
4. `EventContext` 的 optional 字段集合。
5. `Message.additional_kwargs` 中的 runtime provenance、display content、format、message id。
6. `Message.client_events`、`Message.turn_complete`。
7. `CompactionProposal` 的 `source_ids` 后注入，以及 `source_ids/prefix_end`、`replacement/summary` 双份事实。
8. `Job.metadata` 中已知的 command/cwd/agent/thread_id。
9. `PendingInteractionData(type, data)`。
10. `PermissionRequestData.tool_call | permission` 双 optional。
11. `WorkspaceRecord/WorkspaceView`、`Command/CommandDescription`、`JobSummary/JobSnapshot` 的手写字段复制。
12. `ToolResult` 重名 UI 事件、`SessionSummary` 重名 workspace Protocol。
13. provider role 与领域 message kind 的混用；运行时通知不得再伪装成人类 user message。
14. `UsageData.context_tokens`、request metadata 与 provider response 中多套 current-context 竞争来源。
15. `Task.blocks/blockedBy` 正反依赖双存。

## 5. 依赖方向

```text
core value types + provider-neutral contracts
  ↑
agentloop / jobs / interaction base protocols
  ↑
feature plugins + persistence codecs
  ↑
application composition + registries
  ↑
session orchestration
  ↑
protocol + server transports
  ↑
ACP / TUI / Web clients
```

反向依赖禁止：agentloop 不导入 session wire 模型；core 不枚举插件事件；jobs 不导入 coretools/subagents；interaction base 不导入 permissions；protocol 不执行业务；客户端不生成服务端身份。插件通过 application composition 注册 typed event/interaction/job codecs，不通过下层封闭 union 认识所有上层插件。

## 6. 实施切片

迁移不能按“先建新模型、以后再迁生产者/消费者”分层发布，那会制造两套模型。下面 1–5 是一个**不可单独发布的基础迁移单元**内的工作组，不是可依次合并的兼容阶段：允许开发分支在组间暂时不绿，但禁止加入 adapter、双写或默认 fallback；只有五组全部完成、旧定义归零并通过整体验证后，基础迁移单元才可提交为完成。这样消息不会依赖“以后再做”的 usage/event 类型，事件也不会在两种 envelope 间长期共存。

1. **消息/工具/历史工作组**：canonical ConversationMessage 与 ToolOutcome → agentloop 生产者 → context/provider projection → history owner → persistence codec/fold → compact rewrite → session read model → ACP/TUI/Web records → 删除旧 Message 字段、HistoryNode、四套 record payload。磁盘数据是否迁移在开始前作一次明确产品决策。
2. **provider/usage/token 工作组**：ModelStreamEvent、ModelResponse、RequestObservation、UsageDelta/UsageSnapshot 与 timing → provider adapters → loop exchange construction → accumulator/token manager → compact/session/client display → 删除 context token 回退链、metadata 注水与第二份 request measurement。
3. **事件/interaction/permission 工作组**：typed LoopEvent 和全部 plugin events → application registry → session frame → SSE codec → clients；stable interaction request → waiter/pending snapshot → permissions/user-input → HTTP response → clients；删除 ClientEvent.data、validated_client_event、type+data 与 tool_call/permission optional 组合。
4. **job/subagent/shell 工作组**：JobIdentity + JobState 与 registration → coretools/subagents specs/results → registry → events/list/wait/clients → 删除 metadata dict、JobSummary/JobSnapshot 与重复 child result。
5. **配置/agent/运行时工作组**：AgentDefinition 与各层 config → 唯一 ResolvedRuntimeSelection → ThreadMetadata → loop/application/session/attach → permissions/sandbox 单调合成 → 删除 RuntimeDescriptor、AgentSelection 与 route/model/context 的副本。

基础迁移完成后，goal、todolist、workspaces、commands、loader、browser、skills/MCP 等独立插件可各自作为完整 vertical slice 迁移；每个 slice 必须从 parser/store 到 event/client 完成闭环，不得只换一层模型。

基础迁移单元及后续每个插件 slice 的完成条件相同：该语义旧模型引用归零；已知语义的 `.get(...) or ...` 与动态 payload 写入归零；所有状态非法组合不可构造；生产者、持久化、transport 和全部消费者使用同一权威模型或单向投影；schema、文档、Python/ACP/TUI/Web 同步；相关验证证明实际路径，而不是仅让既有断言变绿。
