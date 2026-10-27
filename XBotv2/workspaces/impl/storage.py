"""Process-owned workspace preferences; session metadata stays in its DB."""

from XBotv2.workspaces.api.contracts import WorkspaceRecord, WorkspaceSnapshot


class WorkspaceStorage:
    def __init__(self, state):
        self.domain = state.domain(
            "workspaces",
            ("workspaces_items", "workspaces_members", "workspaces_archived"),
        )

    async def load(self):
        if not await self.domain.exists():
            return WorkspaceSnapshot()

        def read(sql):
            records = sql.execute(
                "SELECT id,path,title,created_at,updated_at FROM workspaces_items ORDER BY ordinal"
            )
            items = []
            for identity, path, title, created, updated in records:
                members = sql.execute(
                    "SELECT session_id FROM workspaces_members WHERE workspace_id=? ORDER BY ordinal",
                    (identity,),
                )
                items.append(
                    WorkspaceRecord(
                        id=identity,
                        path=path,
                        title=title,
                        created_at=created,
                        updated_at=updated,
                        session_ids=tuple(row[0] for row in members),
                    )
                )
            archived = sql.execute(
                "SELECT session_id FROM workspaces_archived ORDER BY ordinal"
            )
            return WorkspaceSnapshot(
                items=tuple(items),
                archived_session_ids=tuple(row[0] for row in archived),
            )

        return await self.domain.transaction(read, write=False)

    async def save(self, snapshot):
        def write(sql):
            sql.execute(
                "CREATE TABLE IF NOT EXISTS workspaces_items(id TEXT PRIMARY KEY,path TEXT,title TEXT,created_at TEXT,updated_at TEXT,ordinal INTEGER)"
            )
            sql.execute(
                "CREATE TABLE IF NOT EXISTS workspaces_members(workspace_id TEXT,session_id TEXT,ordinal INTEGER,PRIMARY KEY(workspace_id,session_id))"
            )
            sql.execute(
                "CREATE TABLE IF NOT EXISTS workspaces_archived(session_id TEXT PRIMARY KEY,ordinal INTEGER)"
            )
            ids = {item.id for item in snapshot.items}
            for (identity,) in sql.execute("SELECT id FROM workspaces_items"):
                if identity not in ids:
                    sql.execute("DELETE FROM workspaces_items WHERE id=?", (identity,))
                    sql.execute(
                        "DELETE FROM workspaces_members WHERE workspace_id=?",
                        (identity,),
                    )
            for ordinal, item in enumerate(snapshot.items):
                sql.execute(
                    "INSERT INTO workspaces_items VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,title=excluded.title,created_at=excluded.created_at,updated_at=excluded.updated_at,ordinal=excluded.ordinal WHERE path!=excluded.path OR title!=excluded.title OR updated_at!=excluded.updated_at OR ordinal!=excluded.ordinal",
                    (
                        item.id,
                        item.path,
                        item.title,
                        item.created_at,
                        item.updated_at,
                        ordinal,
                    ),
                )
                old = {
                    row[0]
                    for row in sql.execute(
                        "SELECT session_id FROM workspaces_members WHERE workspace_id=?",
                        (item.id,),
                    )
                }
                for identity in old - set(item.session_ids):
                    sql.execute(
                        "DELETE FROM workspaces_members WHERE workspace_id=? AND session_id=?",
                        (item.id, identity),
                    )
                for position, identity in enumerate(item.session_ids):
                    sql.execute(
                        "INSERT INTO workspaces_members VALUES(?,?,?) ON CONFLICT(workspace_id,session_id) DO UPDATE SET ordinal=excluded.ordinal WHERE ordinal!=excluded.ordinal",
                        (item.id, identity, position),
                    )
            old = {
                row[0]
                for row in sql.execute("SELECT session_id FROM workspaces_archived")
            }
            for identity in old - set(snapshot.archived_session_ids):
                sql.execute(
                    "DELETE FROM workspaces_archived WHERE session_id=?", (identity,)
                )
            for ordinal, identity in enumerate(snapshot.archived_session_ids):
                sql.execute(
                    "INSERT INTO workspaces_archived VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET ordinal=excluded.ordinal WHERE ordinal!=excluded.ordinal",
                    (identity, ordinal),
                )

        await self.domain.transaction(
            write, size=len(snapshot.model_dump_json().encode())
        )
