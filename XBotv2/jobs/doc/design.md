# Background jobs

Jobs owns the thread registry for background execution, cancellation, waiting and job queries. It provides `JOBS` (`JobsPort`) and depends on commands and the Agent loop engine. Registry tasks, runners, completion waiters and retained job results are runtime state. `JobView` is the existing public query result. This plugin does not define durable retention.

Job owners register their own runners using the `JOBS` service. It emits `JOB_UPDATED` and `JOB_COMPLETED`; the plugin also sends completion to the loop inbox without waking a new turn and publishes client runtime events. The `/jobs` command lists or stops jobs, and the HTTP router provides list and stop operations. It contributes no Agent tool or prompt itself.

Active jobs remain available for their lifetime. The process-wide registry uses
`completed_limit` (default 100) and `completed_bytes` (default 8 MiB) for its
completed-job LRU. These defaults are starting values pending workload
measurement. A wait temporarily pins its targets and can exceed either budget; an evicted
job is no longer available for lookup. A wait defaults to any target, captures
the current active set when its target list is empty, and reports `empty`,
`ready`, `timeout`, or `steered`. Pending next-step inputs interrupt a wait;
completion notices for its own targets do not. Cancelling a wait leaves its
targets running. Job state is runtime-only and is not restored after shutdown.
Completed jobs retain only a bounded label and their owner-provided lightweight
result summary or references. Each non-null result implements `retained_bytes()`
to estimate all memory it retains; owners must include referenced metadata and
store large contents as artifacts. The registry charges identity, label, state,
full error strings and fixed per-entry bookkeeping separately. This is a
conservative estimate, not an exact measurement of allocator overhead; artifact
file sizes are not resident memory. Invalid result estimates fail the job.
Set the two values under the `jobs` entry in the global `config/plugins.yaml`;
they take effect when the process starts and are shared by all sessions. A
workspace, session or thread override that differs from the running process
budget fails at thread mount instead of silently changing or ignoring it.

```python
from XBotv2.jobs.api.contracts import JOBS

jobs = ctx.require(JOBS)
active = jobs.list(kind="shell", status="running")
```

A runner may raise `JobExecutionError` with a lightweight terminal result.
`FailedRunning` retains that result for explicit owner reads. Shell uses it
for its output artifact reference and exit code, including timeout output;
completion notices and job summaries still carry no output body.
