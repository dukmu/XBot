# Subagents

Subagents provides continuous child threads through the standard tool registry. The session owns each child runtime, its inbox and conversation archive. Inputs use the ordinary AgentLoop submission path; child tasks are not jobs.

`spawn_subagent` returns a stable thread ID and acceptance status. `send_message` delivers a steer without waking an idle child; `followup_task` starts another task on the same thread. `list_subagents`, `wait_subagent`, `read_subagent`, `cancel_subagent`, and `close_subagent` use thread IDs. Wait defaults to any active direct child and can return ready, empty, timeout, or steered. Reading pages the latest completed response from the archive using a character cursor. An unfinished archived task without a matching live execution or pending input is reported as interrupted. Cancelling retains the thread; closing releases its live slot.

`SubagentsConfig.max_live_threads` defaults to `None`, allowing unlimited live child threads. When set to a positive integer, it limits all open child threads in a session, including idle and nested children. Reopening a child consumes a slot again. `timeout_seconds` defaults to 600 and applies to the spawn tool.

`list_subagents` accepts `limit` (1–100, default 100) and an optional opaque
`cursor`. Its JSON text result contains `items` with each direct child's thread
ID, state, and agent, plus `previous_cursor` and `next_cursor`. Pass either
cursor to fetch another page; null marks that end. The order is thread ID, and
pages reflect live changes rather than a fixed snapshot.
