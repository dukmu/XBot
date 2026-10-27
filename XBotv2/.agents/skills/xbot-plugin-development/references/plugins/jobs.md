# Background jobs

Jobs owns the thread registry for background execution, cancellation, waiting and job queries. It provides `JOBS` (`JobsPort`) and depends on commands and the Agent loop engine. Registry Tasks, runners and completion waiters are runtime state; job specifications, results and current states stay in memory for the registry lifetime. `JobView` is the existing public query result. This plugin does not define durable retention.

At apply time `JobsConfig.max_concurrent_subagents` defaults to 4 and sets the subagent execution limit. Job owners register their own runners using the `JOBS` service. It emits `JOB_UPDATED` and `JOB_COMPLETED`; the plugin also sends completion to the loop inbox without waking a new turn and publishes client runtime events. The `/jobs` command lists or stops jobs, and the HTTP router provides list and stop operations. It contributes no Agent tool or prompt itself.

```python
from XBotv2.jobs.api.contracts import JOBS

jobs = ctx.require(JOBS)
active = jobs.list(kind="subagent", status="running")
```

See `XBotv2/jobs/doc/design.md` and `XBotv2/jobs/api/` for the contracts.
