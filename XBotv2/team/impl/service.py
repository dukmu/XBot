"""Team operations and delivery to active thread inboxes."""
from __future__ import annotations
import base64
import json
from collections.abc import Sequence
from XBotv2.agentloop.api.contracts import AGENT_LOOP, InboxItem, RuntimeInput
from XBotv2.core.api.domain import InboxTarget, MessageId
from XBotv2.team.impl.storage import TeamStorage, stream_id


def scoped_id(*values: str) -> str:
    data = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


class TeamService:
    def __init__(self, backend, sessions, session_id, enabled):
        self.backend, self.sessions = backend, sessions
        self.session_id, self.enabled = session_id, enabled

    def require_enabled(self):
        if not self.enabled:
            raise ValueError("Team is disabled")

    def storage(self, team, thread=None):
        self.require_enabled()
        repository = self.sessions.thread_repository(self.session_id, thread) if thread else None
        return TeamStorage(self.backend, self.session_id, team, repository)

    async def members(self, team):
        storage = self.storage(team)
        await storage.prepare_read()
        return await storage.members()

    async def count(self, team):
        storage = self.storage(team)
        await storage.prepare_read()
        return await storage.count()

    async def add_member(self, team, thread):
        if await self.sessions.thread_repository(self.session_id, thread).load() is None:
            raise ValueError(f"Unknown thread: {thread}")
        return await self.storage(team, thread).change_member(thread, True)

    async def remove_member(self, team, thread):
        if thread not in await self.members(team):
            return False
        return await self.storage(team, thread).change_member(thread, False)

    async def latest(self, team, n):
        storage = self.storage(team)
        await storage.prepare_read()
        start, cut = await storage.bounds(latest=n)
        async for record in storage.read(start, cut):
            yield record

    async def after(self, team, message_id, n=None):
        storage = self.storage(team)
        await storage.prepare_read()
        start, cut = await storage.bounds(after_id=message_id, limit=n)
        async for record in storage.read(start, cut):
            yield record

    async def get(self, team, ids):
        storage = self.storage(team)
        await storage.prepare_read()
        async for item in storage.get(ids):
            yield item

    async def publish(self, team, identity, sender, content, mentions: Sequence[str]):
        if not identity or not content:
            raise ValueError("Publish identity and content must not be empty")
        if len(set(mentions)) != len(mentions):
            raise ValueError("Mentions must be unique")
        storage = self.storage(team, sender)
        record = await storage.publish(identity, sender, content, mentions)
        return await self._deliver(team, record)

    async def retry(self, team, message_id):
        async for _, record in self.get(team, [message_id]):
            if record is None:
                raise ValueError(f"Unknown message: {message_id}")
            return await self._deliver(team, record)

    async def _deliver(self, team, record):
        identity = record['id']
        sender = record['data']['sender']
        mentions = record['data']['mentions']
        ref = await self.backend.artifacts(self.session_id, sender).reference_records(
            stream_id(team, "board"), after_seq=record['seq'] - 1, through_seq=record['seq'], name="team-message.jsonl")
        deliveries = {}
        for target in mentions:
            input_id = MessageId('team-' + scoped_id(team, identity, target))
            try:
                scope = await self.sessions.locate(self.session_id, target)
                await scope.require(AGENT_LOOP).submit_input(InboxItem(
                    id=input_id, target=InboxTarget.NEXT_STEP,
                    input=RuntimeInput(source='team', event='message',
                        content=f"Team {team}: message {identity} from {sender}; see attached record.", artifacts=(ref,))), wake=True)
                deliveries[target] = {"status": "accepted"}
            except Exception as exc:
                deliveries[target] = {"status": "failed", "error": str(exc)}
        return {"published": True, "record": record, "deliveries": deliveries}
