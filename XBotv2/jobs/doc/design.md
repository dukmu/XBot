# Background jobs

Jobs owns the thread registry for background execution, cancellation, waiting and job queries. It provides `JOBS` (`JobsPort`) and depends on commands and the Agent loop engine. Registry tasks, runners, completion waiters and retained job results are runtime state. `JobView` is the existing public query result. This plugin does not define durable retention.

Job owners register their own runners using the `JOBS` service. It emits `JOB_UPDATED` and `JOB_COMPLETED`; the plugin also sends completion to the loop inbox without waking a new turn and publishes client runtime events. The `/jobs` command lists or stops jobs, and the HTTP router provides list and stop operations. It contributes no Agent tool or prompt itself.

Active jobs remain available for their lifetime. The registry retains at most
100 completed jobs, except while a wait temporarily holds a result; an evicted
job is no longer available for lookup. A wait defaults to any target, captures
the current active set when its target list is empty, and reports `empty`,
`ready`, `timeout`, or `steered`. Pending next-step inputs interrupt a wait;
completion notices for its own targets do not. Cancelling a wait leaves its
targets running. Job state is runtime-only and is not restored after shutdown.
Completed jobs retain only a bounded label and their owner-provided lightweight
result summary or references. Owners must store large results as artifacts and
return references rather than retaining their contents in the job result.

```python
from XBotv2.jobs.api.contracts import JOBS

jobs = ctx.require(JOBS)
active = jobs.list(kind="shell", status="running")
```

A runner may raise `JobExecutionError` with a lightweight terminal result.
`FailedRunning` retains that result for explicit owner reads. Shell uses it
for its output artifact reference and exit code, including timeout output;
completion notices and job summaries still carry no output body.
