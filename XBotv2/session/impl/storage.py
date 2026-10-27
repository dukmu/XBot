"""Session owns authoritative session/thread rows and lazy materialization."""

from __future__ import annotations
import json
import weakref
from datetime import datetime, timezone
from XBotv2.session.api.storage import ThreadFacts
from XBotv2.persistence.api.models import StreamCorrupt

TABLES = ("session", "threads")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS session(id TEXT PRIMARY KEY,title TEXT NOT NULL,workspace TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS threads(thread_id TEXT PRIMARY KEY,parent_thread_id TEXT NOT NULL,agent_ref TEXT NOT NULL,selection TEXT NOT NULL,created_at TEXT NOT NULL)",
)


class SessionRepository:
    def __init__(self, backend):
        self.backend = backend
        self._bindings = weakref.WeakValueDictionary()

    def domain(self, session_id):
        return self.backend.database(session_id, owner="session", tables=TABLES)

    def thread(self, session_id, thread_id):
        key = (session_id, thread_id)
        value = self._bindings.get(key)
        if value is None:
            value = ThreadRepository(self, session_id, thread_id)
            self._bindings[key] = value
        return value

    def bind_thread(self, session_id, thread_id):
        value = ThreadRepository(self, session_id, thread_id)
        self._bindings[(session_id, thread_id)] = value
        return value

    async def row(self, session_id):
        if not await self.backend.database_exists(session_id):
            return None
        domain = self.domain(session_id)
        if not await domain.exists():
            raise StreamCorrupt("Authoritative session tables are missing")
        rows = await domain.transaction(
            lambda sql: sql.execute(
                "SELECT id,title,workspace,created_at,updated_at FROM session"
            ),
            write=False,
        )
        if not rows:
            raise StreamCorrupt("Authoritative session row is missing")
        return dict(
            zip(("id", "title", "workspace", "created_at", "updated_at"), rows[0])
        )

    async def thread_count(self, session_id):
        if not await self.backend.database_exists(session_id):
            return 0
        domain = self.domain(session_id)
        if not await domain.exists():
            raise StreamCorrupt("Authoritative thread table is missing")
        rows = await domain.transaction(lambda sql: sql.execute("SELECT count(*) FROM threads"), write=False)
        return rows[0][0]

    async def thread_page(
        self, session_id, after="", *, limit=100, backwards=False,
        inclusive=False, parent_thread_id=None, kind=None,
    ):
        if not await self.backend.database_exists(session_id):
            return ()
        domain = self.domain(session_id)
        if not await domain.exists():
            raise StreamCorrupt("Authoritative thread table is missing")
        op = "<" if backwards else ">"
        if inclusive:
            op += "="
        where = [f"thread_id{op}?"]
        args = [after]
        if parent_thread_id is not None:
            where.append("parent_thread_id=?")
            args.append(parent_thread_id)
        if kind == "main":
            where.append("parent_thread_id=''")
        elif kind == "subagent":
            where.append("parent_thread_id!=''")
        order = "DESC" if backwards else "ASC"
        return await domain.transaction(
            lambda sql: sql.execute(
                f"SELECT thread_id FROM threads WHERE {' AND '.join(where)} "
                f"ORDER BY thread_id {order} LIMIT ?", (*args, limit),
            ),
            write=False,
        )

    async def has_thread(self, session_id, thread_id):
        if not await self.backend.database_exists(session_id):
            return False
        domain = self.domain(session_id)
        if not await domain.exists():
            raise StreamCorrupt("Authoritative thread table is missing")
        return bool(await domain.transaction(
            lambda sql: sql.execute("SELECT 1 FROM threads WHERE thread_id=?", (thread_id,)),
            write=False,
        ))

    async def rename(self, session_id, title):
        claim = (
            self.backend.acquire(session_id)
            if not self.backend.owns(session_id)
            else None
        )
        try:
            if not await self.backend.database_exists(session_id):
                bound = [
                    v
                    for (sid, _), v in self._bindings.items()
                    if sid == session_id and v.pending is not None
                ]
                if not bound:
                    raise ValueError("Unknown session")
                await bound[0].ensure()
            await self.domain(session_id).transaction(
                lambda sql: sql.execute(
                    "UPDATE session SET title=?,updated_at=?",
                    (title, datetime.now(timezone.utc).isoformat()),
                )
            )
        finally:
            if claim is not None:
                claim.release()


class ThreadRepository:
    def __init__(self, session, session_id, thread_id):
        self.session = session
        self.session_id = session_id
        self.thread_id = thread_id
        self.pending = None
        self._ensured = False
        self._domain = session.domain(session_id)

    async def load(self):
        if not await self.session.backend.database_exists(self.session_id):
            return self.pending
        domain = self.session.domain(self.session_id)
        if not await domain.exists():
            raise StreamCorrupt("Authoritative session metadata is missing")
        rows = await domain.transaction(
            lambda sql: sql.execute(
                "SELECT t.parent_thread_id,t.agent_ref,t.selection,s.workspace,s.title FROM threads t CROSS JOIN session s WHERE t.thread_id=?",
                (self.thread_id,),
            ),
            write=False,
        )
        if not rows:
            return self.pending
        parent, agent, selection, workspace, title = rows[0]
        return ThreadFacts(
            agent_ref=agent,
            parent_thread_id=parent,
            workspace_root=workspace,
            title=title,
            **json.loads(selection),
        )

    async def persisted(self):
        return await self.session.has_thread(self.session_id, self.thread_id)

    async def session_title(self):
        row = await self.session.row(self.session_id)
        return row["title"] if row else ""

    async def remember(self, facts):
        self._ensured = False
        self.pending = facts
        if await self.session.backend.database_exists(self.session_id):
            if await self.session.has_thread(self.session_id, self.thread_id):
                await self._save(facts)
                self._ensured = True

    async def _save(self, facts):
        data = json.dumps(
            {
                key: getattr(facts, key)
                for key in ("provider_override", "model_override", "effort_override")
            },
            separators=(",", ":"),
        )
        now = datetime.now(timezone.utc).isoformat()
        await self._domain.transaction(
            lambda sql: sql.execute(
                "INSERT INTO threads VALUES(?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET parent_thread_id=excluded.parent_thread_id,agent_ref=excluded.agent_ref,selection=excluded.selection WHERE threads.parent_thread_id!=excluded.parent_thread_id OR threads.agent_ref!=excluded.agent_ref OR threads.selection!=excluded.selection",
                (self.thread_id, facts.parent_thread_id, facts.agent_ref, data, now),
            )
        )

    async def ensure(self):
        if self._ensured:
            return
        facts = self.pending or await self.load()
        if facts is None:
            raise RuntimeError(
                "Thread selection must be resolved before its first durable modification"
            )
        if not self.session.backend.owns(self.session_id):
            raise RuntimeError(
                "Session identity modification requires its actual owner"
            )
        domain = self.session.domain(self.session_id)

        def create(sql):
            for statement in SCHEMA:
                sql.execute(statement)
            now = datetime.now(timezone.utc).isoformat()
            sql.execute(
                "INSERT OR IGNORE INTO session VALUES(?,?,?,?,?)",
                (
                    self.session_id,
                    facts.title or self.session_id,
                    facts.workspace_root,
                    now,
                    now,
                ),
            )

        await domain.transaction(create)
        ancestors = set()
        parent = facts.parent_thread_id
        while parent:
            if parent in ancestors or parent == self.thread_id:
                raise ValueError("Cyclic thread parents")
            ancestors.add(parent)
            store = self.session.thread(self.session_id, parent)
            saved = store.pending or await store.load()
            if saved is None:
                raise StreamCorrupt("Thread parent metadata is missing")
            await store._save(saved)
            parent = saved.parent_thread_id
        await self._save(facts)
        self._ensured = True
