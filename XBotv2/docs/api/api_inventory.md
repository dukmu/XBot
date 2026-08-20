# API Inventory

This inventory records the shared core extension surface and the plugin-owned
declaration rule. It is kept in sync by `tests/core/test_public_api.py`.
Updating an export is allowed, but it must be deliberate, documented, and
tested.

## Import Rule

Plugins and external extensions import shared contracts from:

```python
from XBotv2.core import ...
```

Plugin-owned declarations are imported from the owning package root:

```python
from XBotv2.jobs import LIST_TASKS, TaskSnapshot
from XBotv2.llm import LlmCatalogPort, ProviderCatalog
```

Package roots may re-export explicit declaration modules only: `types`,
`invariants`, `commands`, `events`, `services`, and transitional `contracts`.
They must not export concrete registries, services, managers, routers, or
plugin implementations.

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
| `EmptyRequest` | dataclass | Explicit payload for typed query operations without arguments. |
| `Events` | class |  |
| `Operation` | dataclass | Typed XCore operation name and request/response contract. |
| `OperationContext` | protocol | Narrow event-dispatch surface used by typed operations. |
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
| `dispatch_operation` | function | Dispatch and validate a typed XCore operation. |
| `estimate_request_tokens` | function |  |
| `prompt_container` | function |  |
| `prompt_element` | function |  |

## Exported Symbols (`XBotv2.core.jobs`)

The background job subsystem contract: `Job`, `JobStatus`, `JobKind`,
`JobResult`, `JobSummary`, `JobError`,
`JobId`, `JobNotFound`, `JobRegistryClosed`, `CancelResult`, `WaitResult`,
`TERMINAL_STATES`, and `MAX_SUMMARY_CHARS`.

## Exported Symbols (`XBotv2.jobs`)

The package root exports the typed operations and DTOs from `contracts.py` plus
the command declarations from `commands.py`. `JobRegistry`, output stores, and
runner implementations are deliberately excluded.

The same rule applies to the `llm`, `session`, `permissions`, and `sandbox`
package roots. Packages without an explicit declaration module do not expose
their concrete implementation through `__init__.py`.
