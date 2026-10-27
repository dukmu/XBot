"""Workspace-owned schema and queries for preferences and membership."""

import base64
import json

from XBotv2.workspaces.api.contracts import (
    WorkspaceView, WorkspaceNotFound, WorkspaceSessionMoveInvalid,
)


def _cursor(row, direction):
    return base64.urlsafe_b64encode(json.dumps([row[0], row[1], direction]).encode()).decode()


def _decode(cursor):
    if cursor is None:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if (
            not isinstance(value, list) or len(value) != 3
            or not isinstance(value[0], (int, float)) or not isinstance(value[1], str)
            or value[2] not in ("next", "previous", "next_inclusive", "previous_inclusive")
        ):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid page cursor") from exc


class WorkspaceStorage:
    def __init__(self, state):
        self.domain = state.domain(
            "workspaces", ("workspaces_items", "workspaces_members", "workspaces_archived"),
        )

    async def transaction(self, callback, *, write=False):
        if not write and not await self.domain.exists():
            return None
        def run(sql):
            if write:
                sql.execute(
                    "CREATE TABLE IF NOT EXISTS workspaces_items("
                    "id TEXT PRIMARY KEY,path TEXT,title TEXT,created_at TEXT,"
                    "updated_at TEXT,ordinal INTEGER)"
                )
                sql.execute(
                    "CREATE TABLE IF NOT EXISTS workspaces_members("
                    "workspace_id TEXT,session_id TEXT,ordinal INTEGER,"
                    "PRIMARY KEY(workspace_id,session_id))"
                )
                sql.execute(
                    "CREATE TABLE IF NOT EXISTS workspaces_archived("
                    "session_id TEXT PRIMARY KEY,ordinal INTEGER)"
                )
            return callback(sql)
        return await self.domain.transaction(run, write=write)

    async def page(self, kind, limit, cursor=None, workspace_id=None):
        position = _decode(cursor)
        def read(sql):
            if kind == "workspace":
                table, ident, where, args = "workspaces_items", "id", "1=1", []
                cols = (
                    "ordinal,id,path,title,created_at,updated_at,"
                    "(SELECT count(*) FROM workspaces_members m "
                    "WHERE m.workspace_id=workspaces_items.id AND NOT EXISTS "
                    "(SELECT 1 FROM workspaces_archived a WHERE a.session_id=m.session_id))"
                )
            elif kind == "member":
                if not sql.execute("SELECT 1 FROM workspaces_items WHERE id=?", (workspace_id,)):
                    raise WorkspaceNotFound(workspace_id)
                table, ident = "workspaces_members", "session_id"
                where = (
                    "workspace_id=? AND NOT EXISTS (SELECT 1 FROM workspaces_archived a "
                    "WHERE a.session_id=workspaces_members.session_id)"
                )
                args = [workspace_id]
                cols = "ordinal,session_id"
            else:
                table, ident, where, args = "workspaces_archived", "session_id", "1=1", []
                cols = "ordinal,session_id"
            backwards = bool(position and position[2].startswith("previous"))
            if position:
                op = "<" if backwards else ">"
                tie_op = op + "=" if position[2].endswith("_inclusive") else op
                where += f" AND (ordinal{op}? OR (ordinal=? AND {ident}{tie_op}?))"
                args.extend((position[0], position[0], position[1]))
            direction = "DESC" if backwards else "ASC"
            rows = sql.execute(
                f"SELECT {cols} FROM {table} WHERE {where} "
                f"ORDER BY ordinal {direction},{ident} {direction} LIMIT ?",
                (*args, limit + 1),
            )
            more = len(rows) > limit
            rows = rows[:limit]
            if backwards:
                rows = tuple(reversed(rows))
            previous = _cursor(rows[0], "previous") if rows and (more if backwards else position is not None) else None
            next_cursor = _cursor(rows[-1], "next") if rows and (position is not None if backwards else more) else None
            if not rows and position:
                if backwards:
                    next_cursor = _cursor(position, "next_inclusive")
                else:
                    previous = _cursor(position, "previous_inclusive")
            return rows, next_cursor, previous

        result = await self.transaction(read)
        if result is None:
            if kind == "member":
                raise WorkspaceNotFound(workspace_id)
            return (), None, None
        return result

    @staticmethod
    def view(sql, identity):
        rows = sql.execute(
            "SELECT id,path,title,created_at,updated_at,"
            "(SELECT count(*) FROM workspaces_members WHERE workspace_id=? "
            "AND NOT EXISTS (SELECT 1 FROM workspaces_archived a "
            "WHERE a.session_id=workspaces_members.session_id)) "
            "FROM workspaces_items WHERE id=?", (identity, identity),
        )
        if not rows:
            raise WorkspaceNotFound(identity)
        return WorkspaceView(
            workspace_id=rows[0][0], path=rows[0][1], title=rows[0][2],
            created_at=rows[0][3], updated_at=rows[0][4], session_count=rows[0][5],
        )

    @staticmethod
    def move(sql, table, identity, before, workspace_id=None):
        ident = "id" if table == "workspaces_items" else "session_id"
        scope = "1=1" if workspace_id is None else "workspace_id=?"
        args = () if workspace_id is None else (workspace_id,)
        source = sql.execute(f"SELECT ordinal FROM {table} WHERE {scope} AND {ident}=?", (*args, identity))
        anchor = (
            sql.execute(f"SELECT ordinal FROM {table} WHERE {scope} AND {ident}=?", (*args, before))
            if before is not None else ()
        )
        if not source or (before is not None and not anchor):
            if workspace_id is None:
                raise WorkspaceNotFound(identity if not source else before)
            raise WorkspaceSessionMoveInvalid("Session or anchor is not in workspace")
        if before == identity:
            return False
        source_pos = source[0][0]
        if before is None:
            rows = sql.execute(f"SELECT max(ordinal) FROM {table} WHERE {scope}", args)
            target = rows[0][0]
        else:
            target = anchor[0][0]
            if source_pos < target:
                target -= 1
        if target == source_pos:
            return False
        if source_pos < target:
            sql.execute(
                f"UPDATE {table} SET ordinal=ordinal-1 WHERE {scope} AND ordinal>? AND ordinal<=?",
                (*args, source_pos, target),
            )
        else:
            sql.execute(
                f"UPDATE {table} SET ordinal=ordinal+1 WHERE {scope} AND ordinal>=? AND ordinal<?",
                (*args, target, source_pos),
            )
        sql.execute(f"UPDATE {table} SET ordinal=? WHERE {scope} AND {ident}=?", (target, *args, identity))
        return True
