"""Team-owned projections over separate member and board record streams."""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager

TABLES = ("team_members", "team_messages")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS team_members (team TEXT NOT NULL, thread TEXT NOT NULL, PRIMARY KEY(team,thread))",
    "CREATE TABLE IF NOT EXISTS team_messages (team TEXT NOT NULL, message_id TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY(team,message_id), UNIQUE(team,seq))",
)


def stream_id(team: str, kind: str) -> str:
    if not team:
        raise ValueError("Team identity must not be empty")
    encoded = base64.urlsafe_b64encode(team.encode("utf-8")).decode("ascii").rstrip("=")
    return f"__team-{kind}-{encoded}"


def project(sql, stream, records):
    for record, _, _ in records:
        data = record["data"]
        if record["kind"] == "member_added":
            sql.execute("INSERT INTO team_members VALUES(?,?)", (data["team"], data["thread"]))
        elif record["kind"] == "member_removed":
            sql.execute("DELETE FROM team_members WHERE team=? AND thread=?", (data["team"], data["thread"]))
        elif record["kind"] == "message":
            sql.execute("INSERT INTO team_messages VALUES(?,?,?)", (data["team"], record["id"], record["seq"]))
        else:
            raise ValueError(f"Unknown team record kind: {record['kind']}")


class TeamStorage:
    def __init__(self, backend, session_id, team, repository):
        self.backend, self.session_id, self.team = backend, session_id, team
        self.repository = repository
        self.domain = backend.database(session_id, owner="team", tables=TABLES)
        self.members_stream = backend.rollout(session_id, stream_id(team, "members"), domain=self.domain, project=project)
        self.board = backend.rollout(session_id, stream_id(team, "board"), domain=self.domain, project=project)

    async def _initialize(self):
        await self.domain.transaction(lambda sql: [sql.execute(s) for s in SCHEMA])
        await self.members_stream.initialize()
        await self.members_stream.recover()
        await self.board.initialize()
        await self.board.recover()

    @asynccontextmanager
    async def mutation(self):
        await self.repository.ensure()
        async with self.members_stream.lease(write=True):
            async with self.board.lease(write=True):
                await self._initialize()
                yield

    async def prepare_read(self):
        if not await self.domain.exists():
            return
        async with self.members_stream.lease():
            async with self.board.lease():
                await self.members_stream.recover()
                await self.board.recover()

    async def members(self):
        if not await self.domain.exists():
            return ()
        result, after = [], ""
        while True:
            rows = await self.domain.transaction(lambda sql: sql.execute(
                "SELECT thread FROM team_members WHERE team=? AND thread>? ORDER BY thread LIMIT 256", (self.team, after)), write=False)
            if not rows:
                return tuple(result)
            result.extend(row[0] for row in rows)
            after = rows[-1][0]

    async def change_member(self, thread, add):
        async with self.mutation():
            exists = thread in await self.members()
            if exists == add:
                return False
            await self.members_stream.append("member_added" if add else "member_removed", {"team": self.team, "thread": thread})
            return True

    async def publish(self, identity, sender, content, mentions):
        async with self.mutation():
            row = await self.domain.transaction(lambda sql: sql.execute(
                "SELECT seq FROM team_messages WHERE team=? AND message_id=?", (self.team, identity)), write=False)
            data = {"team": self.team, "sender": sender, "content": content, "mentions": list(mentions)}
            if row:
                record = await self.board.record(identity)
                if record["data"] != data:
                    raise ValueError("Publish identity conflicts with an existing message")
                return record
            members = set(await self.members())
            if sender not in members or not set(mentions) <= members:
                raise ValueError("Publisher and mentions must be current team members")
            return await self.board.append("message", data, identity=identity)

    async def count(self):
        if not await self.domain.exists():
            return 0
        rows = await self.domain.transaction(lambda sql: sql.execute(
            "SELECT COUNT(*) FROM team_messages WHERE team=?", (self.team,)), write=False)
        return rows[0][0]

    async def bounds(self, after_id=None, latest=None, limit=None):
        if latest is not None and latest < 0 or limit is not None and limit < 0:
            raise ValueError("Count must be nonnegative")
        if not await self.domain.exists():
            if after_id is not None:
                raise ValueError(f"Unknown message: {after_id}")
            return 0, 0
        def read(sql):
            rows = sql.execute("SELECT COALESCE(MAX(seq),0) FROM team_messages WHERE team=?", (self.team,))
            cut = rows[0][0]
            if after_id is not None:
                rows = sql.execute("SELECT seq FROM team_messages WHERE team=? AND message_id=?", (self.team, after_id))
                if not rows:
                    raise ValueError(f"Unknown message: {after_id}")
                start = rows[0][0]
            else:
                start = max(0, cut - latest) if latest is not None else 0
            return start, min(cut, start + limit) if limit is not None else cut
        return await self.domain.transaction(read, write=False)

    async def read(self, start, cut):
        cursor = start
        while cursor < cut:
            rows, cursor_next, _ = await self.board.read_range(after_seq=cursor, through_seq=cut, max_bytes=self.backend.record_bytes)
            if not rows:
                raise ValueError("Missing team board record")
            for row in rows:
                yield row
            cursor = cursor_next

    async def get(self, ids):
        if not await self.domain.exists():
            for identity in ids:
                yield identity, None
            return
        for identity in ids:
            rows = await self.domain.transaction(lambda sql: sql.execute(
                "SELECT seq FROM team_messages WHERE team=? AND message_id=?", (self.team, identity)), write=False)
            yield identity, await self.board.record(identity) if rows else None
