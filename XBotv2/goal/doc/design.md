# Goal

An optional same-session objective driven by the working Agent. The plugin requires `commands`, `engine`, `state`, `tools`, and `usage`; `jobs` is optional. It accepts no plugin configuration. Enable it in the plugin configuration, then use `/goal finish the API`, `/goal`, `/goal pause`, `/goal resume`, or `/goal clear`.

The Agent tools `create_goal`, `get_goal`, and `update_goal` manage the exact goal ID and revision. An active, armed goal queues a same-session runtime continuation after a turn; running background jobs defer it until completion. Continuation text includes the objective and is sent as runtime input, affecting the model context. The turn input, tool, turn end, error, session resume/close, job completion and status hooks maintain the state; changes emit `GoalChanged` through the application runtime event.

`api/models.py` defines the state union, stats and snapshot; `api/events.py` defines the event. The state and usage baseline persist in the `goal` namespace under `snapshot`; continuation authority is process-local and is disarmed on interruption, error, close and resume. Stats account for rounds, tool calls and usage. There is no independent evaluator or goal budget.
