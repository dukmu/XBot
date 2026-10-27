"""Agent-facing team operations registered through the normal tool registry."""
from XBotv2.core.api.tools import ToolInvocation, failed_text, succeeded_text
from XBotv2.team.impl.service import scoped_id
from XBotv2.team.impl.storage import stream_id


class TeamTools:
    def __init__(self, service, artifacts, thread_id):
        self.service, self.artifacts, self.thread_id = service, artifacts, thread_id

    async def team_members(self, team: str):
        """List the current member thread IDs of a team."""
        return succeeded_text(', '.join(await self.service.members(team)) or 'No members.')

    async def team_add_member(self, team: str, thread: str):
        """Add an existing thread to a team."""
        try:
            changed = await self.service.add_member(team, thread)
            return succeeded_text('Member added.' if changed else 'Already a member.')
        except ValueError as exc:
            return failed_text('team_invalid', str(exc))

    async def team_remove_member(self, team: str, thread: str):
        """Remove a thread from a team."""
        changed = await self.service.remove_member(team, thread)
        return succeeded_text('Member removed.' if changed else 'Not a member.')

    async def team_count(self, team: str):
        """Count committed team board messages."""
        return succeeded_text(str(await self.service.count(team)))

    async def _range(self, team, start, cut):
        if cut <= start:
            return succeeded_text('No messages.')
        ref = await self.artifacts.reference_records(stream_id(team, 'board'), after_seq=start, through_seq=cut, name='team-board.jsonl')
        return succeeded_text(f'Fixed team board selection: {ref.id} ({ref.size} bytes).', artifacts=(ref,))

    async def team_latest(self, team: str, n: int):
        """Attach the latest n team messages in chronological order."""
        try:
            storage = self.service.storage(team)
            await storage.prepare_read()
            start, cut = await storage.bounds(latest=n)
            return await self._range(team, start, cut)
        except ValueError as exc:
            return failed_text('team_invalid', str(exc))

    async def team_after(self, team: str, message_id: str, n: int | None = None):
        """Attach messages following the ID, bounded by the current durable head."""
        try:
            storage = self.service.storage(team)
            await storage.prepare_read()
            start, cut = await storage.bounds(after_id=message_id, limit=n)
            return await self._range(team, start, cut)
        except ValueError as exc:
            return failed_text('team_invalid', str(exc))

    async def team_get(self, team: str, ids: list[str]):
        """Attach specified messages in caller order, reporting unknown IDs."""
        found, missing = [], []
        async for identity, record in self.service.get(team, ids):
            (found if record else missing).append(identity)
        if not found:
            return succeeded_text(f'Missing IDs: {missing}' if missing else 'No messages.')
        ref = await self.artifacts.reference_records(stream_id(team, 'board'), record_ids=found, name='team-board.jsonl')
        return succeeded_text(f'Selected records: {ref.id}. Missing IDs: {missing}', artifacts=(ref,))

    async def team_publish(self, team: str, content: str, mentions: list[str], *, invocation: ToolInvocation):
        """Publish text to the team board and notify mentioned members."""
        try:
            identity = 'tool-' + scoped_id(self.thread_id, str(invocation.assistant_message_id), str(invocation.call_id))
            result = await self.service.publish(team, identity, self.thread_id, content, mentions)
            return await self._published(team, result)
        except ValueError as exc:
            return failed_text('team_invalid', str(exc))

    async def team_retry(self, team: str, message_id: str):
        """Retry delivery of an existing team message to its original mentions."""
        try:
            return await self._published(team, await self.service.retry(team, message_id))
        except ValueError as exc:
            return failed_text('team_invalid', str(exc))

    async def _published(self, team, result):
        record = result["record"]
        ref = await self.artifacts.reference_records(stream_id(team, "board"), after_seq=record["seq"] - 1, through_seq=record["seq"], name="team-message.jsonl")
        return succeeded_text(f"Published {record['id']}. Deliveries: {result['deliveries']}", artifacts=(ref,))
