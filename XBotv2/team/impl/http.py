"""Streaming HTTP access to the shared board."""
import json
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from XBotv2.team.api.models import PublishRequest
from XBotv2.team.api.service import TEAM
from XBotv2.team.impl.service import scoped_id


def build_router(sessions):
    router = APIRouter()
    prefix = '/sessions/{session_id}/threads/{thread_id}/teams/{team}'

    async def service(session_id, thread_id):
        instance = (await sessions.locate(session_id, thread_id)).require(TEAM)
        if not instance.enabled:
            raise HTTPException(status_code=403, detail="Team is disabled")
        return instance

    @router.get(prefix + '/members')
    async def members(session_id: str, thread_id: str, team: str):
        return await (await service(session_id, thread_id)).members(team)

    @router.put(prefix + '/members/{member}')
    async def add_member(session_id: str, thread_id: str, team: str, member: str):
        try:
            return {'changed': await (await service(session_id, thread_id)).add_member(team, member)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete(prefix + '/members/{member}')
    async def remove_member(session_id: str, thread_id: str, team: str, member: str):
        return {'changed': await (await service(session_id, thread_id)).remove_member(team, member)}

    @router.get(prefix + '/count')
    async def count(session_id: str, thread_id: str, team: str):
        return {'count': await (await service(session_id, thread_id)).count(team)}

    def stream(records):
        async def generate():
            async for item in records:
                yield json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n'
        return StreamingResponse(generate(), media_type='application/x-ndjson')

    @router.get(prefix + '/latest')
    async def latest(session_id: str, thread_id: str, team: str, n: int = Query(ge=0)):
        storage = (await service(session_id, thread_id)).storage(team)
        await storage.prepare_read()
        start, cut = await storage.bounds(latest=n)
        return stream(storage.read(start, cut))

    @router.get(prefix + '/after/{message_id}')
    async def after(session_id: str, thread_id: str, team: str, message_id: str, n: int | None = Query(default=None, ge=0)):
        storage = (await service(session_id, thread_id)).storage(team)
        await storage.prepare_read()
        try:
            start, cut = await storage.bounds(after_id=message_id, limit=n)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return stream(storage.read(start, cut))

    @router.post(prefix + '/get')
    async def get(session_id: str, thread_id: str, team: str, ids: list[str]):
        return stream((await service(session_id, thread_id)).get(team, ids))

    @router.post(prefix + '/messages')
    async def publish(session_id: str, thread_id: str, team: str, request: PublishRequest):
        try:
            return await (await service(session_id, thread_id)).publish(team,
                'http-' + scoped_id(thread_id, request.request_id), thread_id, request.content, request.mentions)
        except ValueError as exc:
            status = 409 if 'conflicts' in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    return router
