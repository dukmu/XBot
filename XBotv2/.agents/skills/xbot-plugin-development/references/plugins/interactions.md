# `interactions`

Owns model-facing user-input requests and non-blocking client notices. Permission
approval is a separate capability owned by `permissions`.

- **Import/profile:** `XBotv2.interactions`, Agent profile.
- **Source:** `interactions/plugin.py`, `contracts.py`, `protocol.py`, and
  `tools.py`.
- **Injects/provides:** `tools`, `client_events`, `session_launch` →
  `interactions` (`InteractionsService`).
- **Events:** typed `UserInputRequest`, `UserInputRecorded`, and `ClientNotice`
  values use the installed client-event/interaction ports.
- **Tools:** `ask_user` is registered only for interactive sessions;
  `send_message` sends a non-blocking notice.
- **Responses:**
  `POST /sessions/{session_id}/threads/{thread_id}/interactions/user-input`.

## Public input contract

```python
class UserInputOption(WireModel):
    label: str
    description: str

class UserInputRequest(WireModel):
    kind: Literal["user_input_required"]
    interaction_id: str
    source: str
    tool_call_id: str = ""
    question: str
    options: tuple[UserInputOption, ...] = ()
    timeout_seconds: float | None = None
    resume_supported: bool = False
```

Requests from `ask_user` require at least two options. The Tool parameters are
`question`, `options` (each with `label` and `description`), and optional
`timeout_seconds`. A keyword-only `tool_call: ToolCall` is supplied by the core
and used to correlate the request; it is not exposed in the provider schema.

The client response body is `{request_id, answer}`. `answer` is JSON-compatible.
The route returns a typed `InteractionResponse` that confirms the response was
recorded and lists remaining pending IDs. The request's `interaction_id` and
response `request_id` address the same interaction; neither is a turn ID.

## Lifecycle and persistence boundary

`request_user_input()` constructs a unique interaction ID and publishes a
typed request through the live `ClientEventsPort`. Without a live interaction
sink, it resolves as an unsupported/cancelled result rather than leaving the
turn blocked indefinitely. A configured timeout produces a typed timeout
resolution. Closing the session cancels waiters.

Live pending requests are included in the open/resume snapshot's
`pending_interactions`; they are not reconstructed from an expired event cursor
or persisted as prior user answers. Clients can rebuild an unanswered dialog
from this snapshot. See [client-runtime.md](../client-runtime.md) for the client
boundary and permission reference for approval interactions.

## `send_message`

`send_message(message, level="info")` emits a non-blocking `ClientNotice` with
level `info`, `warning`, or `error`. It does not wait for delivery and should
not be used as the final assistant response; the normal assistant message is
the canonical transcript reply.

## Plugin guidance

- Use `interactions.request_user_input()` when the Agent needs an answer; do not
  implement a second prompt transport.
- Keep client notices, user answers, and permission decisions as distinct
  contracts.
- `ask_user` is absent from non-interactive sessions; do not assume it is
  always present in the Tool catalog.
- User input supplies data to a waiting operation. It does not approve or
  authorize a Tool call.
