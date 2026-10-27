# Interactions

Interactions owns live user-input requests, typed resolutions, and client notices. The thread-scoped service holds pending Futures and cancels them on session close; pending requests are runtime state, not persisted answers. It depends on the tool registry and session launch context, supplies `interactions` and `client_events`, and registers the user-input route with the client event router. It has no plugin configuration or slash commands.

The `notify_user` tool returns a non-blocking progress message in its tool output; it is not the final assistant response. `ask_user` is registered only in interactive sessions and requires at least two options. Permission approval has its own owner. Requests and recorded results are published through the client event path; a timeout resolves the Future, and a request without a live sink and without a timeout returns an unsupported cancellation rather than waiting indefinitely.

```python
from XBotv2.interactions.api.contracts import INTERACTIONS
from XBotv2.interactions.api.models import UserInputOption

answer = await ctx.require(INTERACTIONS).request_user_input(
    "Continue?", options=(UserInputOption(label="Yes", description="Continue"),
                          UserInputOption(label="No", description="Stop")),
    timeout_seconds=60,
)
```

Clients respond through `/sessions/{session_id}/threads/{thread_id}/interactions/user-input`. Shared models and service contracts live in `api`; waiters, registration, routing and tools live in `impl`.
