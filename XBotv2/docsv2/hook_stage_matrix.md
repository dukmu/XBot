# Hook Stage Matrix

This matrix documents the current `HookStage` enum without removing, renaming,
or marking any stage as experimental. It is the working contract for tightening
`HookContext` and plugin capabilities.

## Columns

- **Category**: observer ignores returns; transform may return a dict; guard may
  return `HookDecision` or a documented dict.
- **Short**: `default` means `HookManager` uses `SHORT_CIRCUIT_STAGES`;
  `caller` means the call site explicitly requests short-circuiting.
- **Strict**: failures are collected and raised as `ExceptionGroup` after every
  callback runs.
- **Payload**: primary `HookContext` fields expected by plugins.
- **Return**: accepted return value today.
- **Caller**: primary runtime area that invokes the stage.

Every hook invoked inside `Engine.run_turn` also receives the turn's
`request_id`, including stop, error, client-event, and persistence stages. The
session init/start/resume/close stages are not owned by one message request and
receive an empty value.

| Stage | Category | Short | Strict | Payload | Return | Caller |
|---|---|---:|---:|---|---|---|
| `on_session_init` | observer | no | yes | `session`, `config`, `tools`, `sandbox`, `plugin_store`, `plugin_runtime` | ignored | bootstrap after plugin setup |
| `on_session_start` | observer | no | no | `session`, `config` | ignored | new engine session |
| `on_session_resume` | observer | no | no | `session`, persisted `state` | ignored | resumed engine session |
| `on_session_close` | observer | no | yes | `session`, pending interactions already cancelled | ignored | engine close |
| `on_turn_start` | observer | no | no | `session` with current `turn_count`, `user_input` | ignored | start of accepted user turn |
| `on_turn_end` | observer | no | no | `session`, final turn state | ignored | before stop hooks |
| `on_stop` | observer | no | yes | `stop_reason` | ignored | normal turn stop |
| `on_stop_failure` | observer | no | no | `stop_reason`, `error`, optional `user_input` | ignored | turn failure path |
| `before_user_message_accept` | transform | caller | no | `user_input` | `{user_input}`, `{event, turn_complete}`, or rejection | before message enters history |
| `after_user_message_accept` | observer | no | no | `user_input` | ignored | after user message append |
| `before_context` | transform | default | no | `state.messages`, `session` | compaction dict or event dict | before context preparation |
| `pre_compact` | transform | default | no | `compact_reason`, `state.messages` | `{messages}`, `{compact_reason}`, or rejection | before history replacement |
| `post_compact` | observer | no | no | `compact_reason`, message count state | ignored | after history replacement |
| `before_context_build` | transform | default | no | `state.messages`, context build inputs | `{messages}`, `{context_kwargs}`, or event dict | before context builder |
| `after_context_components_build` | observer | no | no | `context_components: list[ContextComponent]` | replace `ctx.context_components`; return ignored | after immutable source-tagged components, before provider conversion |
| `after_context` | transform | default | no | `context_messages` | `{context_messages}`, `{messages}`, or event dict | after provider messages exist |
| `after_context_build` | observer | no | no | `context_messages` | ignored | after final context build |
| `before_agent` | guard | default | no | `context_messages`, `session` | event/messages dict, `HookDecision`, or stop | before model/tool agent step |
| `before_tool_schema_bind` | transform | default | no | `context_messages`, `model_request` | `{tools}`, `{messages}`, or event dict | before provider tool binding |
| `after_tool_schema_bind` | observer | no | no | `context_messages`, `model_request` | ignored | after provider tool binding |
| `before_model_request` | transform | default | no | `context_messages`, `model_request` | `{messages}`, `{tools}`, `{llm}`, or event dict | before LLM call |
| `after_model_response` | observer | no | no | `model_request`, `model_response`, `agent_response` | ignored | after full model response |
| `on_model_request_error` | observer | no | no | `model_request`, `error` | ignored | LLM error path |
| `after_agent` | transform | default | no | `agent_response` | `{messages}`, `{event, turn_complete}`, or stop | after assistant response |
| `before_tools` | guard | default | no | `tool_calls`, `agent_response` | any value stops tool batch | before tool execution batch |
| `after_tools` | transform | default | no | `tool_results` | `{tool_results}`, `{event, turn_complete}`, or stop | after tool batch result build |
| `on_user_message` | observer | no | no | `user_input` | ignored | after user message accept |
| `on_assistant_message` | observer | no | no | `agent_response` | ignored | after assistant message append |
| `on_tool_message` | observer | no | no | `tool_results` | ignored | after tool messages append |
| `on_tool_calls_parsed` | observer | no | no | `tool_calls`, `agent_response` | ignored | before tool batch starts |
| `on_permission_request` | observer | no | no | `tool_call`, `permission_decision`, `error` | ignored | permission ask path |
| `on_permission_denied` | observer | no | no | `tool_call`, `permission_decision`, `error` | ignored | permission denial path |
| `before_tool_call` | guard | default | no | `tool_call` | `HookDecision`, `{tool_call}`, `{args}`, `{tool_result}`, or `{deny_reason}` | before one tool call |
| `after_tool_call` | observer | no | no | `tool_call`, `tool_result`, optional `error` | ignored | after one tool call |
| `on_tool_call_failure` | observer | no | no | `tool_call`, `tool_result`, `error` | ignored | tool exception path |
| `post_tool_batch` | observer | no | no | `tool_calls`, `tool_results` | ignored | after tool runtime batch |
| `on_tool_denied` | observer | no | no | `tool_call`, `tool_result`, `error` | ignored | denied tool call result |
| `on_client_event` | observer | no | no | `client_event`, optional `tool_result` | ignored | before client-visible event dispatch |
| `before_state_persist` | observer | no | yes | changed `state.messages`, `session` | ignored | before one changed-message checkpoint |
| `after_state_persist` | observer | no | yes | persisted `state.messages`, `session` | ignored | after one successful checkpoint |
| `on_error` | observer | no | no | `error`, optional `user_input` | ignored | generic engine error path |

## Tightening Rules

- Keep the stage values above intact while improving payload types and tests.
- Reuse existing `HookContext` fields and public types before adding another
  payload type. Add one only for a repeated gap shared by independent consumers.
- Do not let plugins reach `HookManager`, `ContextBuilder`, or engine internals.
- Runtime tool registration from hooks must use `ctx.plugin_runtime` so the
  plugin loader records ownership.
- Persistence stages do not run for an unchanged message snapshot. Mutations by
  `before_state_persist` are included in that checkpoint; later mutations form
  a new checkpoint.
- Model-request Hooks inspect `model_request`; transform stages use their
  documented return dictionary rather than mutating it in place.
- When a built-in plugin needs new data, first reuse an existing public field.
  Extend the API only when the same missing contract affects multiple
  independent consumers, then cover it in this matrix.
