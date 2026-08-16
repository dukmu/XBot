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
| `AgentDefinition` | dataclass |  |
| `AgentMode` | type alias |  |
| `AgentRuntime` | protocol |  |
| `AgentSession` | protocol |  |
| `AgentSessionResult` | dataclass |  |
| `ArtifactRef` | dataclass |  |
| `ChildEngineFactory` | type alias |  |
| `ClientEvent` | dataclass |  |
| `Command` | dataclass |  |
| `CommandResult` | dataclass |  |
| `ContentPart` | type alias |  |
| `ContextComponent` | dataclass |  |
| `EventContext` | dataclass |  |
| `Events` | class |  |
| `ImageContent` | dataclass |  |
| `ImagePart` | dataclass |  |
| `InputModality` | type alias |  |
| `JsonValue` | type alias |  |
| `MESSAGE_FORMAT_KEY` | constant |  |
| `Message` | dataclass |  |
| `ModelChunk` | dataclass |  |
| `ModelResponse` | dataclass |  |
| `BaseProvider` | abstract class | Provider-neutral configuration and Tool binding contract for model adapters. |
| `ProviderRetryExhaustedError` | exception | A provider request failed after all configured retries were consumed. |
| `ProviderCapabilities` | dataclass |  |
| `PromptFragmentStage` | type alias |  |
| `ReasoningPart` | dataclass |  |
| `RuntimePaths` | class |  |
| `RuntimeVariables` | class |  |
| `SessionInfo` | dataclass |  |
| `SessionPaths` | class |  |
| `SHORT_CIRCUIT_EVENTS` | frozenset |  |
| `SubagentAgentError` | exception |  |
| `SubagentTurnError` | exception |  |
| `TextPart` | dataclass |  |
| `ThreadPaths` | class |  |
| `Tool` | class |  |
| `ToolAction` | enum |  |
| `ToolCall` | dataclass |  |
| `ToolCallDelta` | dataclass |  |
| `ToolCallPart` | dataclass |  |
| `ToolDecision` | dataclass |  |
| `ToolError` | exception |  |
| `ToolResult` | dataclass |  |
| `calibrated_context_tokens` | function |  |
| `context_token_limit` | function |  |
| `estimate_messages_tokens` | function |  |
| `estimate_request_tokens` | function |  |
| `prompt_container` | function |  |
| `prompt_element` | function |  |

## Exported Symbols (XBotv2.jobs)

The background job subsystem contract: `Job`, `JobContext`, `JobRunner`,
`JobRegistry`, `JobStatus`, `JobKind`, `JobResult`, `JobSummary`, `JobError`,
`JobId`, `JobNotFound`, `JobRegistryClosed`, `CancelResult`, `WaitResult`,
`OutputStore`, `TextOutputStore`, `StreamOutputStore`, `OutputChunk`,
`CombinedShellOutput`, `TERMINAL_STATES`, `MAX_SUMMARY_CHARS`, `job_summary`,
`normalize_error`.
