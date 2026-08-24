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

Plugin-owned declarations are imported from the owning package root. Direct
cross-plugin imports from declaration submodules are not part of the public
API:

```python
from XBotv2.jobs import LIST_TASKS, TaskSnapshot
from XBotv2.config import RuntimeConfig
from XBotv2.llm import LlmCatalogPort, ModelConfig, ProviderConfig
```

Package roots may re-export explicit declaration modules only: `types`,
`invariants`, `commands`, `events`, `services`, `protocol`, and transitional
`contracts`.
They must not export concrete registries, services, managers, routers, or
plugin implementations.

## Exported Symbols (XBotv2.core)

| Symbol | Kind | Purpose |
|---|---|---|
| `ArtifactRef` | dataclass |  |
| `ArtifactKind` | enum | Logical artifact category independent of filesystem layout. |
| `ArtifactStorePort` | protocol | Typed artifact write, read, existence, and model-reference contract. |
| `ClientEvent` | dataclass |  |
| `ContentPart` | type alias |  |
| `ConversationHistory` | class | Single owner of the effective conversation history. |
| `EmptyRequest` | dataclass | Explicit payload for typed query operations without arguments. |
| `Operation` | dataclass | Typed XCore operation name and request/response contract. |
| `OperationContext` | protocol | Narrow event-dispatch surface used by typed operations. |
| `ImageContent` | dataclass |  |
| `ImagePart` | dataclass |  |
| `InputModality` | type alias |  |
| `JsonObject` | type alias | JSON object carried by generic core envelopes. |
| `JsonValue` | type alias |  |
| `HistorySink` | protocol | Durable append/replace boundary used by ConversationHistory. |
| `MESSAGE_FORMAT_KEY` | constant |  |
| `Message` | dataclass |  |
| `ModelChunk` | dataclass |  |
| `ModelResponse` | dataclass |  |
| `BaseProvider` | abstract class | Provider-neutral configuration and Tool binding contract for model adapters. |
| `ProviderRetryExhaustedError` | exception | A provider request failed after all configured retries were consumed. |
| `ProviderCapabilities` | dataclass |  |
| `ReasoningPart` | dataclass |  |
| `RuntimePaths` | class |  |
| `RuntimeVariables` | class |  |
| `SessionPaths` | class |  |
| `TextPart` | dataclass |  |
| `ThreadPaths` | class |  |
| `Tool` | class |  |
| `ToolCall` | dataclass |  |
| `ToolCallDelta` | dataclass |  |
| `ToolCallPart` | dataclass |  |
| `ToolError` | exception |  |
| `ToolResult` | dataclass |  |
| `calibrated_context_tokens` | function |  |
| `context_token_limit` | function |  |
| `estimate_messages_tokens` | function |  |
| `dispatch_operation` | function | Dispatch and validate a typed XCore operation. |
| `estimate_request_tokens` | function |  |
| `prompt_container` | function |  |
| `prompt_element` | function |  |
| `json_object` | function | Validate and copy a JSON object. |
| `json_value` | function | Validate and copy a JSON value. |

## Agents Declarations (`XBotv2.agents`)

The Agents plugin owns Agent definitions, creation options, catalog and
selection operations, child-session protocols, and subagent errors. These
declarations are exported from `XBotv2.agents`; concrete catalogs, loaders,
services, and subagent runners remain internal.

## Agentloop Declarations (`XBotv2.agentloop`)

The Agentloop plugin owns runtime lifecycle event names, `EventContext`, the
narrow `EventPort`, `SHORT_CIRCUIT_EVENTS`, `LoopState`, `LoopSettings`, and
the typed loop factory input. It also exports its Tool and loop service
contracts. Concrete loop drivers, Tool registries, and services remain
internal. The LLM root exports the `ModelPort` consumed by that factory.

## Context Builder Declarations (`XBotv2.context_builder`)

The Context Builder root exports `ContextComponent` and
`PromptFragmentStage`. Context contributors use those declarations without
importing the concrete builder or plugin implementation.

## Jobs Declarations (`XBotv2.jobs`)

The Jobs plugin exports its domain and service contracts: `Job`, `JobStatus`,
`JobKind`, `JobResult`, `JobSummary`, `JobError`, `JobsPort`, `JobRunner`,
`JobRunnerContext`, and output reader protocols, plus
`JobId`, `JobNotFound`, `JobRegistryClosed`, `CancelResult`, `WaitResult`,
`TERMINAL_STATES`, and `MAX_SUMMARY_CHARS`.

## Exported Symbols (`XBotv2.commands`)

The Commands plugin root exports `Command`, `CommandResult`, handler helpers,
and the typed list/execute operation declarations. Wire request and response
models remain owned by `commands.protocol`; concrete registry and plugin
implementations are not exported.

The package root also exports typed operations and DTOs from `contracts.py`
plus command declarations from `commands.py`. `JobRegistry` and concrete
output stores remain internal implementations.

The same rule applies to the `llm`, `session`, `permissions`, and `sandbox`
package roots. Packages without an explicit declaration module do not expose
their concrete implementation through `__init__.py`.

The Session root exports `SessionInfo` and its other domain declarations;
Session wire DTOs remain lazily exported from the same root. Display-history
projection is internal to the Session plugin.

The Loader root exports plugin-tree declarations.
Concrete loading and XCore mounting live in `loader.runtime` and are used only
by application composition.
