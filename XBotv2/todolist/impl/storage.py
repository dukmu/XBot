"""The task owner stores queryable task rows, edges and small counters."""

from XBotv2.todolist.api.models import TaskList, Task, DependencyEdge


class TaskStorage:
    def __init__(self, state, thread_id, thread_repository):
        self.domain = state.domain(
            "todolist", ("todolist_tasks", "todolist_edges", "todolist_counters")
        )
        self.thread = thread_id
        self.thread_repository = thread_repository

    async def load(self):
        if not await self.domain.exists():
            return TaskList()

        def read(sql):
            counters = sql.execute(
                "SELECT version,next_id FROM todolist_counters WHERE thread_id=?",
                (self.thread,),
            )
            tasks = sql.execute(
                "SELECT task_id,subject,description,active_form,owner,status FROM todolist_tasks WHERE thread_id=? ORDER BY ordinal",
                (self.thread,),
            )
            edges = sql.execute(
                "SELECT prerequisite,dependent FROM todolist_edges WHERE thread_id=? ORDER BY prerequisite,dependent",
                (self.thread,),
            )
            return TaskList(
                version=counters[0][0] if counters else 0,
                next_id=counters[0][1] if counters else 1,
                tasks=tuple(
                    Task(
                        id=row[0],
                        subject=row[1],
                        description=row[2],
                        active_form=row[3],
                        owner=row[4],
                        status=row[5],
                    )
                    for row in tasks
                ),
                edges=tuple(
                    DependencyEdge(prerequisite=row[0], dependent=row[1])
                    for row in edges
                ),
            )

        return await self.domain.transaction(read, write=False)

    async def save(self, tasks):
        await self.thread_repository.ensure()

        def write(sql):
            sql.execute(
                "CREATE TABLE IF NOT EXISTS todolist_tasks(thread_id TEXT,task_id TEXT,ordinal INTEGER,subject TEXT,description TEXT,active_form TEXT,owner TEXT,status TEXT,PRIMARY KEY(thread_id,task_id))"
            )
            sql.execute(
                "CREATE TABLE IF NOT EXISTS todolist_edges(thread_id TEXT,prerequisite TEXT,dependent TEXT,PRIMARY KEY(thread_id,prerequisite,dependent))"
            )
            sql.execute(
                "CREATE TABLE IF NOT EXISTS todolist_counters(thread_id TEXT PRIMARY KEY,version INTEGER,next_id INTEGER)"
            )
            sql.execute(
                "INSERT INTO todolist_counters VALUES(?,?,?) ON CONFLICT(thread_id) DO UPDATE SET version=excluded.version,next_id=excluded.next_id",
                (self.thread, tasks.version, tasks.next_id),
            )
            ids = {task.id for task in tasks.tasks}
            for row in sql.execute(
                "SELECT task_id FROM todolist_tasks WHERE thread_id=?", (self.thread,)
            ):
                if row[0] not in ids:
                    sql.execute(
                        "DELETE FROM todolist_tasks WHERE thread_id=? AND task_id=?",
                        (self.thread, row[0]),
                    )
            for ordinal, task in enumerate(tasks.tasks):
                sql.execute(
                    "INSERT INTO todolist_tasks VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(thread_id,task_id) DO UPDATE SET ordinal=excluded.ordinal,subject=excluded.subject,description=excluded.description,active_form=excluded.active_form,owner=excluded.owner,status=excluded.status WHERE ordinal!=excluded.ordinal OR subject!=excluded.subject OR description!=excluded.description OR active_form!=excluded.active_form OR owner!=excluded.owner OR status!=excluded.status",
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
            old = set(
                sql.execute(
                    "SELECT prerequisite,dependent FROM todolist_edges WHERE thread_id=?",
                    (self.thread,),
                )
            )
            new = {(edge.prerequisite, edge.dependent) for edge in tasks.edges}
            for edge in old - new:
                sql.execute(
                    "DELETE FROM todolist_edges WHERE thread_id=? AND prerequisite=? AND dependent=?",
                    (self.thread, *edge),
                )
            for edge in new - old:
                sql.execute(
                    "INSERT INTO todolist_edges VALUES(?,?,?)", (self.thread, *edge)
                )

        size = sum(len(task.model_dump_json().encode()) for task in tasks.tasks)
        await self.domain.transaction(write, size=size)
