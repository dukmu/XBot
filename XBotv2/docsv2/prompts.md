# Prompt Assembly

XBotv2 keeps provider protocol roles authoritative: `system`, `user`,
`assistant`, and `tool`. XML structures synthetic and runtime-owned message
content inside those roles; it never changes a message's protocol authority.
Natural human input and ordinary assistant prose remain unwrapped because their
roles already identify both source and semantics.

## System Context

`ContextBuilder` emits one leading `<xbot_context version="1">` system message.
Its sections have a fixed logical order:

1. `core_instructions`: built into XBotv2 and present for every primary Agent
   and subagent.
2. `runtime_environment`: the actual human identity, sandbox description, and
   available runtime capabilities.
3. `developer_instructions`: explicit compatible `system_prompt` and
   `instructions` configuration.
4. `agent_identity` and `agent_instructions`: the active `.agents/<name>.md`
   definition.
5. `plugin_instruction`: startup-loaded workspace and plugin instructions.
6. `memory`: advisory persistent context.
7. `runtime_state`: small state that is included only when it exists.

All section content and attributes are XML-escaped. A file or plugin fragment
containing a closing tag therefore remains text and cannot create a higher
priority section. `ContextComponent.source`, `plugin_name`, `stage`, and
`source_path` remain available to context Hooks before rendering.

The legacy fragment stages remain ordering zones for API compatibility. A
plugin cannot gain core authority by registering `system_prefix`; every plugin
fragment is rendered as `plugin_instruction` with its owner and declared stage.

## Stable And Dynamic Inputs

Core, Agent, workspace, and startup plugin instructions form a deterministic
prefix. Clocks and turn counters are excluded. Runtime mailbox notifications
are transient `<runtime_event>` inputs and are not persisted as human history.
An active Goal is injected only when its idle continuation is delivered, not on
every provider call.

Slash-invoked Skills use `<skill_invocation>` with separate
`skill_instructions` and `user_arguments` children. Model-invoked Skills remain
normal Tools. General Mailbox delivery uses `<runtime_event>` with explicit
source, event, instruction, and encoded payload fields. Compact checkpoints use
`<conversation_summary>` and preserve their structured marker across resume.

## Tool Results

Tool results retain the standard `tool` role and `tool_call_id`, while their
content is normalized before history persistence:

```xml
<tool_result name="filesystem_read" status="success">
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
Their lifecycles remain distinct:

- Tool-result caching runs after Tool execution and before history persistence.
- Context caching creates provider-only copies of oversized human input,
  assistant text, reasoning, and Tool arguments without changing history.

Tool-result caching stores raw Tool output before assembling its outer envelope.
Provider-only caching may store a complete structured message as one atomic
string, but its preview is escaped inside a fresh cache envelope, so it never
splices partial markup into the request. The complete leading system context is
never externalized as one unit; component-level system budgets must preserve
the core instructions.

## Provider Conversion

OpenAI-compatible providers receive the system context as the first system
message. Anthropic providers move the same text to the top-level `system`
parameter. Both retain structured Tool content in the provider's native Tool
result role. Provider conversion does not reinterpret XML priority and does not
move Tool or file content into the system context.

## Extension Rules

- Prefer protocol roles and Tool schemas before adding another XML envelope.
- Register durable plugin instructions during setup and identify their source.
- Put external content in Tool results. If it must enter a synthetic prompt,
  render it with `prompt_element` so content and attributes are escaped.
- Do not persist runtime events as human messages.
- Do not add dynamic values to the stable context without a demonstrated need.
