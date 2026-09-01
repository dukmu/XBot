# Prompt Assembly

XBotv2 keeps provider protocol roles authoritative: `system`, `user`,
`assistant`, and `tool`. XML structures synthetic and runtime-owned message
content inside those roles; it never changes a message's protocol authority.
Natural human input and ordinary assistant prose remain unwrapped because their
roles already identify both source and semantics.

## System Context

`ContextBuilder` emits one leading `<xbot_context>` system message.
Its sections have a fixed logical order:

1. `core_instructions`: built into XBotv2 and present for every primary Agent
   and subagent.
2. `runtime_environment`: the actual human identity, sandbox description, and
   model-visible workspace/session paths.
3. `developer_instructions`: explicit compatible `system_prompt` and
   `instructions` configuration.
4. `agent_identity` and `agent_instructions`: the active `.agents/<name>.md`
   definition.
5. `workspace_instructions`: the dynamically loaded workspace `AGENTS.md`.
6. `plugin_instruction`: other plugin-owned instructions.
7. `memory`: advisory persistent context.
8. `runtime_state`: small state that is included only when it exists.

All section content and attributes are XML-escaped. A file or plugin fragment
containing a closing tag therefore remains text and cannot create a higher
priority section. `ContextComponent.source`, `plugin_name`, `stage`, and
`source_path` remain available to context Hooks before rendering.

The legacy fragment stages remain ordering zones for API compatibility. A
plugin cannot gain core authority by registering `system_prefix`; every plugin
fragment is rendered as `plugin_instruction` with its owner and declared stage.

## Stable And Dynamic Inputs

Core, Agent, workspace, and startup plugin instructions form a deterministic
prefix. Clocks and turn counters are excluded. Runtime inbox notifications
are persisted `<runtime_event>` inputs with explicit non-human metadata. An
active Goal schedules such an input only after a turn ends, not on every
provider call or Tool result.

Runtime paths are stable for the thread. The workspace is shown explicitly;
cached artifacts use the read-only `session/artifacts/...` virtual namespace.
Internal configuration and plugin-state directories are not exposed merely
because Core has immutable runtime variables for them.

Slash-invoked Skills use `<skill_invocation>` with separate
`skill_instructions` and `user_arguments` children. Model-invoked Skills remain
normal Tools. General Mailbox delivery uses `<runtime_event>` with explicit
source, event, and encoded payload fields. Compacted surface summaries use
`<historical_context source="compaction">` around `<conversation_summary>` and
preserve their structured marker across resume.

## Tool Results

Tool results retain the standard `tool` role and `tool_call_id`, while their
content is normalized before history persistence:

```xml
<tool_result name="read" status="success">
  <data encoding="json">...</data>
</tool_result>
```

Text, structured data, errors, and artifacts use separate children. When a
Tool's textual content is the JSON serialization of its data, only the `data`
child is emitted to avoid duplication. Live client events retain the original
display content; resume derives the same display text from the structured
history instead of exposing XML in the TUI.

## Cached Content

Both cache layers use `<cached_content>` with a relative `session/...` path,
size metadata, escaped beginning/ending previews, and bounded-read guidance.
The read instruction identifies the path as an XBot model path that must be
passed unchanged to `filesystem_read`; models must not search for or derive an
absolute host path. Their lifecycles remain distinct:

- Tool-result caching runs after Tool execution and before history persistence.
- Context caching runs at the start of the pre-model request chain and replaces only the
  current oversized user message in the request copy. It retains the complete
  persisted user message and reuses the same cached request copy across ReAct
  iterations. It never scans or caches historical user messages, assistant
  text, reasoning, system context, or Tool results.

Tool-result caching stores raw Tool output before assembling its outer envelope.
The user-input preview is escaped inside a fresh cache envelope, so it never
splices partial markup into the request. `inline_limit_chars` reports the
actual inline preview length; `cache_threshold_chars` reports the size that
triggered externalization.

The `content_cache` plugin has three independently configurable character
limits: `cache_threshold_chars` (48,000 by default), `preview_chars` (12,000),
and `tail_chars` (2,000 of the preview). Configure them in the `content_cache`
entry of `xcore.yaml` or a `plugins.yaml` overlay. The preview may not exceed
the threshold, and its tail may not exceed the complete preview.

## Provider Conversion

OpenAI-compatible providers receive the system context as the first system
message. Anthropic providers move the same text to the top-level `system`
parameter. Both retain structured Tool content in the provider's native Tool
result role. Provider conversion does not reinterpret XML priority and does not
move Tool or file content into the system context.

Assistant responses retain their provider-native replay state. Anthropic keeps
the ordered `thinking`, `redacted_thinking`, `text`, and `tool_use` blocks,
including thinking signatures. OpenAI-compatible Chat Completions keeps the
native assistant message and any reasoning field returned by that provider.
Tool-result continuation replays this state without parsing reasoning text or
reconstructing provider blocks from display content.

## Extension Rules

- Prefer protocol roles and Tool schemas before adding another XML envelope.
- Register durable plugin instructions during setup and identify their source.
- Put external content in Tool results. If it must enter a synthetic prompt,
  render it with `prompt_element` so content and attributes are escaped.
- Persist delivered runtime events as source-labelled, non-human messages.
- Do not add dynamic values to the stable context without a demonstrated need.
