# API Inventory

This inventory records the current supported Python extension surface. It is
kept in sync with `XBotv2.core.__all__` and `tests/core/test_public_api.py`.
Updating the list is allowed, but it must be deliberate, documented, and tested.

## Import Rule

Plugins and external extensions import the shared contracts from:

```python
from XBotv2.core import ...
```

Job contracts are imported from core, while the jobs package exposes runtime
implementations only:

```python
from XBotv2.core.jobs import ...
from XBotv2.jobs import JobRegistry, JobRunner
```

Feature plugins must not import these contracts from `XBotv2.jobs`; that would
make a plugin masquerade as their owner.

## Exported Symbols (XBotv2.core)

| Symbol | Kind | Purpose |
|---|---|---|
| `AgentDefinition` | dataclass |  |
| `AgentMode` | type alias |  |
| `AgentSession` | protocol |  |
| `AgentSessionResult` | dataclass |  |
| `ArtifactRef` | dataclass |  |
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

## Exported Symbols (`XBotv2.core.jobs`)

The background job subsystem contract: `Job`, `JobStatus`, `JobKind`,
`JobResult`, `JobSummary`, `JobError`,
`JobId`, `JobNotFound`, `JobRegistryClosed`, `CancelResult`, `WaitResult`,
`TERMINAL_STATES`, and `MAX_SUMMARY_CHARS`.

## Exported Symbols (`XBotv2.jobs`)

Runtime implementations only: `JobRegistry`, `JobContext`, `JobRunner`,
`OutputStore`, `TextOutputStore`, `StreamOutputStore`, `OutputChunk`,
`CombinedShellOutput`, `job_summary`, and `normalize_error`.
