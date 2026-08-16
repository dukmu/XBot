# API Inventory

This inventory records the current supported Python extension surface. It is
kept in sync with `XBotv2.core.__all__` and `tests/core/test_public_api.py`.
Updating the list is allowed, but it must be deliberate, documented, and tested.

## Import Rule

Plugins and external extensions import the shared contracts from:

```python
from XBotv2.core import ...
```

The job-system contract is a plugin package of its own:

```python
from XBotv2.jobs import ...
```

Modules under `XBotv2.core` may hold the implementation of these types, but
new plugin examples should use the aggregate package unless they need a local
type-only import.

## Exported Symbols (XBotv2.core)

| Symbol | Kind | Purpose |
|---|---|---|
| `AgentDefinition` | dataclass | Declarative configuration for one primary agent or subagent. |
| `AgentMode` | type alias | primary / subagent / all. |
| `AgentRuntime` | protocol | Core execution capability exposed to Agent plugins. |
| `AgentSession` | protocol | One spawned child session owned by a Session. |
| `AgentSessionResult` | dataclass | Outcome of one completed child agent session. |
| `ArtifactRef` | dataclass | Reference to a stored artifact. |
| `ChildEngineFactory` | type alias | Child engine bootstrap factory used by the session plugin. |
| `ClientEvent` | dataclass | Client-visible runtime event envelope. |
| `Command` | dataclass | Human-facing slash command contract. |
| `CommandResult` | dataclass | Slash command execution result. |
| `ContentPart` | type alias | Union of model message content parts. |
| `ContextComponent` | dataclass | One source-tagged context section before escaped provider rendering. |
| `EventContext` | dataclass | Payload object passed to runtime event listeners (replaces the hook context). |
| `Events` | class | Runtime event names dispatched on the XCore context. |
| `ImageContent` | dataclass | Image content block for provider requests. |
| `ImagePart` | dataclass | Image message part. |
| `InputModality` | type alias | Provider input modalities. |
| `JsonValue` | type alias | JSON-compatible value. |
| `MESSAGE_FORMAT_KEY` | constant | Persisted message-format key. |
| `Message` | dataclass | One model conversation message. |
| `ModelChunk` | dataclass | Streamed model response chunk. |
| `ModelResponse` | dataclass | Complete model response. |
| `ProviderCapabilities` | dataclass | Provider capability flags. |
| `PromptFragmentStage` | type alias | Prompt fragment stages. |
| `ReasoningPart` | dataclass | Reasoning content part. |
| `RuntimePaths` | class | Runtime filesystem layout (config/sessions/memory/logs). |
| `RuntimeVariables` | class | Read-only runtime variable expansion. |
| `SessionInfo` | dataclass | Session identity and provider info. |
| `SessionPaths` | class | Session-scoped filesystem layout. |
| `SHORT_CIRCUIT_EVENTS` | frozenset | Events dispatched with ctx.serial. |
| `SubagentAgentError` | exception | Invalid subagent spawn request. |
| `SubagentTurnError` | exception | Child turn finished without a usable assistant response. |
| `TextPart` | dataclass | Text message part. |
| `ThreadPaths` | class | Thread-scoped filesystem layout. |
| `Tool` | class | Tool contract with provider schema generation. |
| `ToolAction` | enum | Tool decision actions (allow / continue / deny / stop). |
| `ToolCall` | dataclass | Parsed tool invocation. |
| `ToolCallDelta` | dataclass | Streamed tool-call delta. |
| `ToolCallPart` | dataclass | Tool-call message part. |
| `ToolDecision` | dataclass | Tool permission decision. |
| `ToolError` | exception | Tool execution failure. |
| `ToolResult` | dataclass | Tool execution result. |
| `calibrated_context_tokens` | function | Calibrated context token estimate. |
| `context_token_limit` | function | Context window token limit. |
| `estimate_messages_tokens` | function | Token estimate for a message list. |
| `estimate_request_tokens` | function | Token estimate for a request. |
| `prompt_container` | function | Render a prompt container element. |
| `prompt_element` | function | Render a prompt element. |

## Exported Symbols (XBotv2.jobs)

The background job subsystem contract: `Job`, `JobContext`, `JobRunner`,
`JobRegistry`, `JobStatus`, `JobKind`, `JobResult`, `JobSummary`, `JobError`,
`JobId`, `JobNotFound`, `JobRegistryClosed`, `CancelResult`, `WaitResult`,
`OutputStore`, `TextOutputStore`, `StreamOutputStore`, `OutputChunk`,
`CombinedShellOutput`, `TERMINAL_STATES`, `MAX_SUMMARY_CHARS`, `job_summary`,
`normalize_error`.
