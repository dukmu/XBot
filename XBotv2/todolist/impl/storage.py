"""Task-owned SQL state, with bounded active snapshots and historical reads."""

from XBotv2.todolist.api.models import (
    TaskList,
    Task,
    DependencyEdge,
    TaskValidationError,
)


class TaskStorage:
    def __init__(self, state, thread_id, thread_repository):
        self.domain = state.domain(
            "todolist", ("todolist_tasks", "todolist_edges", "todolist_counters")
        )
        self.thread = thread_id
        self.thread_repository = thread_repository

    @staticmethod
    def _task(row):
        return Task(
            id=row[0],
            subject=row[1],
            description=row[2],
            active_form=row[3],
            owner=row[4],
            status=row[5],
        )

    @staticmethod
    def _pages(sql, query, params):
        offset = 0
        while True:
            rows = sql.execute(query + " LIMIT 400 OFFSET ?", (*params, offset))
            yield from rows
            if len(rows) < 400:
                break
            offset += len(rows)

    async def load(self):
        if not await self.domain.exists():
            return TaskList()

        def read(sql):
            counters = sql.execute(
                "SELECT version,next_id,active_count,completed_count FROM todolist_counters WHERE thread_id=?",
                (self.thread,),
            )
            count = counters[0][3] if counters else 0
            tasks = tuple(
                self._task(row)
                for row in self._pages(
                    sql,
                    "SELECT task_id,subject,description,active_form,owner,status FROM todolist_tasks WHERE thread_id=? AND status IN ('pending','in_progress') ORDER BY ordinal",
                    (self.thread,),
                )
            )
            rows = tuple(
                self._pages(
                    sql,
                    "SELECT e.prerequisite,e.dependent,p.status FROM todolist_tasks d CROSS JOIN todolist_edges AS e INDEXED BY todolist_edges_dependent CROSS JOIN todolist_tasks p WHERE d.thread_id=? AND d.status IN ('pending','in_progress') AND e.thread_id=d.thread_id AND e.dependent=d.task_id AND p.thread_id=e.thread_id AND p.task_id=e.prerequisite ORDER BY e.prerequisite,e.dependent",
                    (self.thread,),
                )
            )
            return TaskList(
                version=counters[0][0] if counters else 0,
                next_id=counters[0][1] if counters else 1,
                tasks=tasks,
                edges=tuple(
                    DependencyEdge(prerequisite=a, dependent=b) for a, b, _ in rows
                ),
                completed_prerequisites=tuple(
                    sorted({a for a, _, status in rows if status == "completed"})
                ),
                completed_count=count,
            )

        return await self.domain.transaction(read, write=False)

    async def get(self, task_id):
        if not await self.domain.exists():
            return None

        def read(sql):
            rows = sql.execute(
                "SELECT task_id,subject,description,active_form,owner,status FROM todolist_tasks WHERE thread_id=? AND task_id=?",
                (self.thread, task_id),
            )
            if not rows:
                return None
            edges = tuple(
                DependencyEdge(prerequisite=a, dependent=b)
                for a, b in self._pages(
                    sql,
                    "SELECT prerequisite,dependent FROM todolist_edges WHERE thread_id=? AND (prerequisite=? OR dependent=?) ORDER BY prerequisite,dependent",
                    (self.thread, task_id, task_id),
                )
            )
            return self._task(rows[0]), edges

        return await self.domain.transaction(read, write=False)

    async def completed_page(self, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 400:
            raise ValueError(
                "after must be nonnegative and limit must be between 1 and 400"
            )
        if not await self.domain.exists():
            return ()

        def read(sql):
            return tuple(
                self._task(row)
                for row in sql.execute(
                    "SELECT task_id,subject,description,active_form,owner,status FROM todolist_tasks WHERE thread_id=? AND status='completed' AND CAST(task_id AS INTEGER)>? ORDER BY CAST(task_id AS INTEGER) LIMIT ?",
                    (self.thread, after, limit),
                )
            )

        return await self.domain.transaction(read, write=False)

    async def change(
        self, *, task=None, delete=None, additions=(), removals=(), max_tasks
    ):
        await self.thread_repository.ensure()

        def write(sql):
            sql.execute(
                "CREATE TABLE IF NOT EXISTS todolist_tasks(thread_id TEXT,task_id TEXT,ordinal INTEGER,subject TEXT,description TEXT,active_form TEXT,owner TEXT,status TEXT,PRIMARY KEY(thread_id,task_id))"
            )
            sql.execute(
                "CREATE TABLE IF NOT EXISTS todolist_edges(thread_id TEXT,prerequisite TEXT,dependent TEXT,PRIMARY KEY(thread_id,prerequisite,dependent))"
            )
            sql.execute(
                "CREATE TABLE IF NOT EXISTS todolist_counters(thread_id TEXT PRIMARY KEY,version INTEGER,next_id INTEGER,active_count INTEGER NOT NULL DEFAULT 0,completed_count INTEGER NOT NULL DEFAULT 0)"
            )
            sql.execute(
                "CREATE INDEX IF NOT EXISTS todolist_tasks_status_id ON todolist_tasks(thread_id,status,CAST(task_id AS INTEGER))"
            )
            sql.execute(
                "CREATE INDEX IF NOT EXISTS todolist_edges_dependent ON todolist_edges(thread_id,dependent)"
            )
            sql.execute(
                "INSERT OR IGNORE INTO todolist_counters(thread_id,version,next_id,active_count,completed_count) VALUES(?,0,1,0,0)",
                (self.thread,),
            )
            version, next_id, active_count, completed_count = sql.execute(
                "SELECT version,next_id,active_count,completed_count FROM todolist_counters WHERE thread_id=?",
                (self.thread,),
            )[0]
            if delete is not None:
                status = sql.execute(
                    "SELECT status FROM todolist_tasks WHERE thread_id=? AND task_id=?",
                    (self.thread, delete),
                )[0][0]
                if status == "completed":
                    completed_count -= 1
                else:
                    active_count -= 1
                sql.execute(
                    "DELETE FROM todolist_edges WHERE thread_id=? AND (prerequisite=? OR dependent=?)",
                    (self.thread, delete, delete),
                )
                sql.execute(
                    "DELETE FROM todolist_tasks WHERE thread_id=? AND task_id=?",
                    (self.thread, delete),
                )
            else:
                existing = sql.execute(
                    "SELECT status FROM todolist_tasks WHERE thread_id=? AND task_id=?",
                    (self.thread, task.id),
                )
                if task.status != "completed" and (
                    not existing or existing[0][0] == "completed"
                ):
                    if active_count >= max_tasks:
                        raise TaskValidationError(
                            "task_limit",
                            f"A task list must not exceed {max_tasks} active tasks",
                        )
                if not existing:
                    if task.status == "completed":
                        completed_count += 1
                    else:
                        active_count += 1
                    if task.id != str(next_id):
                        raise TaskValidationError(
                            "invalid_task", "Task identity changed during creation"
                        )
                    next_id += 1
                elif existing[0][0] != task.status:
                    if existing[0][0] == "completed":
                        completed_count -= 1
                        active_count += 1
                    elif task.status == "completed":
                        active_count -= 1
                        completed_count += 1
                ordinal = int(task.id)
                sql.execute(
                    "INSERT INTO todolist_tasks VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(thread_id,task_id) DO UPDATE SET subject=excluded.subject,description=excluded.description,active_form=excluded.active_form,owner=excluded.owner,status=excluded.status",
                    (
                        self.thread,
                        task.id,
                        ordinal,
                        task.subject,
                        task.description,
                        task.active_form,
                        task.owner,
                        task.status,
                    ),
                )
                for edge in removals:
                    sql.execute(
                        "DELETE FROM todolist_edges WHERE thread_id=? AND prerequisite=? AND dependent=?",
                        (self.thread, edge.prerequisite, edge.dependent),
                    )
                for edge in additions:
                    if edge.prerequisite == edge.dependent:
                        raise TaskValidationError(
                            "invalid_task_dependency", "A task cannot depend on itself"
                        )
                    for target in (edge.prerequisite, edge.dependent):
                        if not sql.execute(
                            "SELECT 1 FROM todolist_tasks WHERE thread_id=? AND task_id=?",
                            (self.thread, target),
                        ):
                            raise TaskValidationError(
                                "task_not_found", f"Unknown task: #{target}"
                            )
                    cycle = sql.execute(
                        "WITH RECURSIVE reach(id) AS (SELECT dependent FROM todolist_edges WHERE thread_id=? AND prerequisite=? UNION SELECT e.dependent FROM todolist_edges e JOIN reach r ON e.prerequisite=r.id WHERE e.thread_id=?) SELECT 1 FROM reach WHERE id=? LIMIT 1",
                        (self.thread, edge.dependent, self.thread, edge.prerequisite),
                    )
                    if cycle:
                        raise TaskValidationError(
                            "invalid_task_dependency",
                            "Task dependencies must be acyclic",
                        )
                    sql.execute(
                        "INSERT OR IGNORE INTO todolist_edges VALUES(?,?,?)",
                        (self.thread, edge.prerequisite, edge.dependent),
                    )
                blocked = sql.execute(
                    "SELECT d.task_id,p.task_id FROM todolist_tasks d CROSS JOIN todolist_edges AS e INDEXED BY todolist_edges_dependent CROSS JOIN todolist_tasks p WHERE d.thread_id=? AND d.status='in_progress' AND e.thread_id=d.thread_id AND e.dependent=d.task_id AND p.thread_id=e.thread_id AND p.task_id=e.prerequisite AND p.status!='completed' LIMIT 1",
                    (self.thread,),
                )
                if blocked:
                    raise TaskValidationError(
                        "blocked",
                        f"Task #{blocked[0][0]} is blocked by #{blocked[0][1]}",
                    )
            sql.execute(
                "UPDATE todolist_counters SET version=?,next_id=?,active_count=?,completed_count=? WHERE thread_id=?",
                (version + 1, next_id, active_count, completed_count, self.thread),
            )

        size = len(task.model_dump_json().encode("utf-8")) if task else 0
        size += len(delete.encode("utf-8")) if delete else 0
        size += sum(
            len(edge.model_dump_json().encode("utf-8"))
            for edge in (*additions, *removals)
        )
        await self.domain.transaction(write, size=size)
