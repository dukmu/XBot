import asyncio
import json
import pytest

from XBotv2.core.api.paths import RuntimePaths
from XBotv2.session.api.contracts import OpenThread, SESSIONS
from XBotv2.team.api.service import TEAM
from XBotv2.team.impl.storage import stream_id
from XBotv2.team.impl.service import scoped_id
from XBotv2.tests.runtime import thread_scope
from XBotv2.persistence.impl.stream import append_file, encoded


@pytest.mark.asyncio
async def test_board_real_loader_and_fork(temp_data_dir, temp_workspace):
    from XBotv2.llm.impl.mock import MockLLM
    from XBotv2.agentloop.api.contracts import AGENT_LOOP, TOOLS
    from XBotv2.core.api.tools import ToolCall
    from XBotv2.permissions.api.models import PermissionPolicy
    scope = await thread_scope(paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id='team-session', thread_id='main', workspace_root=temp_workspace,
        llm_override=MockLLM(responses=[{'content': 'received'}]),
        extra_plugins=[{'id': 'caption', 'config': {'auto': False}}, {'id': 'team', 'config': {'enabled': True}}])
    try:
        sessions = scope.require(SESSIONS)
        team = scope.require(TEAM)
        await team.add_member('group', 'main')
        child = await sessions.open_thread(OpenThread(session_id='team-session', thread_id='child',
            parent_thread_id='main', workspace_root=str(temp_workspace), provider_name=None, no_plugins=False, mode='new', model_override=MockLLM(responses=[{'content': 'received'}])))
        await team.add_member('group', 'child')
        await sessions.close_thread('team-session', 'child')
        first = await team.publish('group', 'one', 'main', 'hello', ['main', 'child'])
        assert first['deliveries']['main']['status'] == 'accepted'
        assert first['deliveries']['child']['status'] == 'failed'
        runtime = scope.require(AGENT_LOOP)
        if runtime.wakeup_task is not None:
            await runtime.wakeup_task
        child = await sessions.open_thread(OpenThread(session_id='team-session', thread_id='child',
            parent_thread_id='main', workspace_root=str(temp_workspace), provider_name=None,
            no_plugins=False, mode='resume', model_override=MockLLM(responses=[{'content': 'received'}])))
        scope.permissions.replace_policies((PermissionPolicy(default_decision='allow'),))
        retry = (await scope.require(TOOLS).execute_all([ToolCall(id='retry', name='team_retry',
            args={'team': 'group', 'message_id': 'one'})]))[0].message.outcome
        assert retry.kind == 'succeeded'
        assert "'child': {'status': 'accepted'}" in retry.output.parts[0].text
        assert await team.count('group') == 1

        child_runtime = child.require(AGENT_LOOP)
        if child_runtime.wakeup_task is not None:
            await child_runtime.wakeup_task
        notices = []
        for target_scope, target in ((scope, 'main'), (child, 'child')):
            identity = 'team-' + scoped_id('group', 'one', target)
            matches = [m for m in target_scope.loop_state.history.snapshot() if m.id == identity]
            assert len(matches) == 1
            notices.append(matches[0])
        assert notices[0].artifacts == notices[1].artifacts == retry.output.artifacts

        with pytest.raises(ValueError, match='conflicts'):
            await team.publish('group', 'one', 'main', 'different', ['main', 'child'])
        assert [(i, r and r['id']) async for i, r in team.get('group', ['one', 'missing', 'one'])] == [
            ('one', 'one'), ('missing', None), ('one', 'one')]
        store = team.storage('group', 'main')
        async with store.mutation():
            head, offset, _ = await store.board.head()
            rows = [encoded({'seq': head + n, 'id': f'bulk-{n}', 'kind': 'message',
                'data': {'team': 'group', 'sender': 'main', 'content': str(n), 'mentions': []}})
                for n in range(1, 606)]
            await asyncio.to_thread(append_file, store.board.paths.rollout_file,
                b''.join(rows), offset, store.board.paths.session.root)
            await store.board.recover()
        assert await team.count('group') == 606
        import httpx
        from fastapi import FastAPI
        from XBotv2.team.impl.http import build_router
        app = FastAPI()
        app.include_router(build_router(sessions))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            base = '/sessions/team-session/threads/main/teams/group'
            response = await client.get(base + '/latest', params={'n': 2})
            assert [json.loads(line)['id'] for line in response.text.splitlines()] == ['bulk-604', 'bulk-605']
            assert (await client.get(base + '/after/missing')).status_code == 404
            assert (await client.put(base + '/members/unknown')).status_code == 400
            body = {'request_id': 'http', 'content': 'original', 'mentions': []}
            assert (await client.post(base + '/messages', json=body)).status_code == 200
            assert (await client.post(base + '/messages', json={**body, 'content': 'changed'})).status_code == 409

        iterator = team.after('group', 'one')
        first_row = await anext(iterator)
        await team.publish('group', 'later', 'main', 'later', [])
        rows = [first_row] + [row async for row in iterator]
        assert len(rows) == 606 and rows[-1]['data']['content'] == 'original'
        assert [r['id'] async for r in team.latest('group', 2)][-1] == 'later'
        ref = await store.backend.artifacts('team-session', 'main').reference_records(stream_id('group', 'board'), after_seq=0, through_seq=1)
        runtime = scope.require(AGENT_LOOP)
        if runtime.wakeup_task is not None:
            await runtime.wakeup_task
        fork = await sessions.fork_session('team-session')
        # Fork includes team-owned streams and projections even while its runtime is closed.
        fork_store = type(store)(store.backend, fork, 'group', store.repository)
        assert await fork_store.count() == 608
        await sessions.close_session('team-session')
        await sessions.delete_session('team-session')
    finally:
        await scope.root.close()
    from XBotv2.main import open_runtime
    from XBotv2.loader.api.contracts import PluginOverlay
    from XBotv2.persistence.api.contracts import PERSISTENCE
    cold = await open_runtime(paths=RuntimePaths.from_data_dir(temp_data_dir),
        overrides=PluginOverlay.parse([{'id': 'team', 'disabled': True}]))
    try:
        records = [row async for row in cold.require(PERSISTENCE).artifacts(fork, 'main').read_records(ref)]
        assert records[0]['data']['content'] == 'hello'
    finally:
        await cold.close()


@pytest.mark.asyncio
async def test_tools_use_execution_identity_and_record_refs(temp_data_dir, temp_workspace):
    from XBotv2.agentloop.api.contracts import HumanInput, InboxItem
    from XBotv2.core.api.domain import InboxTarget
    from XBotv2.core.api.messages import ToolMessage
    from XBotv2.llm.impl.mock import MockLLM
    from XBotv2.permissions.api.models import PermissionPolicy
    from XBotv2.tests.runtime import engine_of

    llm = MockLLM(responses=[
        {'tool_calls': [{'id': 'same', 'name': 'team_publish', 'args': {'team': 'group', 'content': 'first text', 'mentions': []}}]},
        {'tool_calls': [{'id': 'same', 'name': 'team_publish', 'args': {'team': 'group', 'content': 'second text', 'mentions': []}}]},
        {'content': 'done'},
    ])
    scope = await thread_scope(paths=RuntimePaths.from_data_dir(temp_data_dir), session_id='team-tools',
        thread_id='main', workspace_root=temp_workspace, llm_override=llm,
        extra_plugins=[{'id': 'caption', 'config': {'auto': False}}, {'id': 'team', 'config': {'enabled': True}}])
    try:
        scope.permissions.replace_policies((PermissionPolicy(default_decision='allow'),))
        team = scope.require(TEAM)
        await team.add_member('group', 'main')
        engine = engine_of(scope)
        item = InboxItem(target=InboxTarget.NEXT_TURN, input=HumanInput(content='publish'))
        events = [event async for event in engine.run_turn(item)]
        assert await team.count('group') == 2, (llm.call_count, [e.kind for e in events], [(m.kind, getattr(m, 'parts', ())) for m in scope.loop_state.history.snapshot() if m.kind == 'assistant'])
        records = [row async for row in team.latest('group', 2)]
        assert [row['data']['content'] for row in records] == ['first text', 'second text']
        outputs = [m.outcome for m in scope.loop_state.history.snapshot() if isinstance(m, ToolMessage)]
        assert len(outputs) == 2
        assert all(len(output.output.artifacts) == 1 for output in outputs)
        assert all('first text' not in str(output.output.parts) and 'second text' not in str(output.output.parts) for output in outputs)
    finally:
        await scope.root.close()


@pytest.mark.asyncio
async def test_disabled_team_has_no_tools_or_http_access(temp_data_dir, temp_workspace):
    import httpx
    from fastapi import FastAPI
    from XBotv2.agentloop.api.contracts import TOOLS
    from XBotv2.llm.impl.mock import MockLLM
    from XBotv2.team.impl.http import build_router

    scope = await thread_scope(paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id='team-disabled', thread_id='main', workspace_root=temp_workspace,
        llm_override=MockLLM(responses=[{'content': 'done'}]))
    try:
        assert not any(name.startswith('team_') for name in scope.require(TOOLS).registered_names())
        with pytest.raises(ValueError, match='disabled'):
            await scope.require(TEAM).add_member('group', 'main')
        app = FastAPI()
        app.include_router(build_router(scope.require(SESSIONS)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/sessions/team-disabled/threads/main/teams/group/messages',
                json={'request_id': 'request', 'content': 'message'})
            assert response.status_code == 403
        assert not (temp_data_dir / 'sessions' / 'team-disabled').exists()
    finally:
        await scope.root.close()
