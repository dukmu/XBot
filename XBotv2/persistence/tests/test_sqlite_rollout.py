"""Real owner/API acceptance for the single SQLite + rollout format."""
import asyncio
import json
import sqlite3
import weakref
from pathlib import Path
import pytest
from XBotv2.tests.runtime import thread_scope
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.messages import CompactionSummaryMessage
from XBotv2.agentloop.api.contracts import AGENT_LOOP,InboxItem,HumanInput
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.errors import OperationError
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.persistence.api.models import CommitUncertain,StreamCorrupt
from XBotv2.session.api.contracts import SESSIONS,OpenSession

async def idle(loop):
    async with asyncio.timeout(5):
        while loop.busy:
            await asyncio.sleep(.005)

def rows(path,query):
    with sqlite3.connect(path) as connection:
        return connection.execute(query).fetchall()

@pytest.fixture
async def opened(tmp_path):
    paths=RuntimePaths.from_data_dir(tmp_path/'data')
    model=MockLLM([{'content':'reply'} for _ in range(8)])
    scope=await thread_scope(paths=paths,session_id='session',llm_override=model,extra_plugins=[{'id':'caption','config':{'auto':False}}])
    try:
        yield paths,scope,model
    finally:
        await scope.root.close()

async def test_empty_rename_and_single_body_idempotency(opened):
    paths,scope,model=opened
    manager=scope.root.require(SESSIONS); loop=scope.require(AGENT_LOOP)
    assert not paths.session('session').root.exists()
    await manager.rename_session('session','仅标题')
    db=paths.session('session').database_file
    assert rows(db,'SELECT title FROM session')==[('仅标题',)]
    assert not paths.session('session').thread('main').rollout_file.exists()
    await loop.send_message('中文唯一正文','original')
    await idle(loop)
    text=paths.session('session').thread('main').rollout_file.read_text()
    assert text.count('中文唯一正文')==1
    assert rows(db,'SELECT COUNT(*) FROM inbox')==[(0,)]
    assert rows(db,'SELECT COUNT(*) FROM input_ids')==[(1,)]
    await loop.send_message('中文唯一正文','original')
    await idle(loop)
    assert model.call_count==1
    assert paths.session('session').thread('main').rollout_file.read_text()==text
    with pytest.raises(OperationError,match='different content'):
        await loop.send_message('different','original')
    assert not tuple(paths.session('session').root.rglob('*.json'))
    await manager.close_session('session')
    reopened=await manager.open(OpenSession('session','main',None,None,'resume',False,model_override=model))
    assert reopened.require(LOOP_STATE).metadata.value.title=='仅标题'
    assert [m.id for m in reopened.require(LOOP_STATE).history][:1]==['original']

@pytest.mark.parametrize("retry_first",[True,False])
async def test_sql_failure_replays_before_next_accept_and_retry(opened,monkeypatch,retry_first):
    paths,scope,model=opened
    import XBotv2.agentloop.impl.storage_projection as stream
    original=stream.project
    failed=False
    def fail_after_log(c,thread,batch):
        nonlocal failed
        record,offset,length=batch[0]
        path=paths.session('session').thread('main').rollout_file
        if not failed and record['kind']=='input_accepted' and path.exists() and path.stat().st_size>offset:
            failed=True
            raise sqlite3.OperationalError('controlled projection failure')
        return original(c,thread,batch)
    monkeypatch.setattr(stream,'project',fail_after_log)
    loop=scope.require(AGENT_LOOP)
    with pytest.raises(CommitUncertain):
        await loop.send_message('accepted before SQL failure','uncertain')
    db=paths.session('session').database_file
    assert rows(db,'SELECT indexed_seq FROM stream_heads')==[(0,)]
    if retry_first:
        await loop.send_message('accepted before SQL failure','uncertain')
        await idle(loop)
        assert model.call_count==1
    assert rows(db,'SELECT indexed_seq FROM stream_heads')[0][0]>=0
    await loop.send_message('next','next')
    await idle(loop)
    assert paths.session('session').thread('main').rollout_file.read_text().count('accepted before SQL failure')==1
    assert rows(db,'SELECT input_id FROM input_ids ORDER BY input_id')==[('next',),('uncertain',)]
    async with asyncio.timeout(5):
        while scope.require(AGENT_LOOP).pending_input_count or scope.require(AGENT_LOOP).busy:
            await asyncio.sleep(.005)
    inputs=[message.id for message in scope.require(LOOP_STATE).history if message.kind=='human_input']
    assert inputs==['uncertain','next']
    assert model.call_count==2
    assert rows(db,'SELECT COUNT(*) FROM inbox')==[(0,)]

async def test_compaction_releases_archived_bodies_and_pages_canonical(opened):
    paths,scope,model=opened
    history=scope.require(LOOP_STATE).history
    from XBotv2.core.api.messages import HumanInputMessage
    from XBotv2.core.api.parts import TextPart
    original=HumanInputMessage(id='old',parts=(TextPart(text='large original'*1000),))
    reference=weakref.ref(original)
    await history.append(original)
    del original
    await history.replace_range(0,1,[CompactionSummaryMessage(id='summary',summary='small')],operation='compact',preserve_transcript=True)
    import gc
    gc.collect()
    assert reference() is None
    assert len(history)==1
    persistence=scope.root.require(ARCHIVE)
    page=await persistence.message_page('session','main',limit=1)
    assert page.items[0].id=='old'
    assert not hasattr(history,'_transcript') and not hasattr(history,'_seen_ids')
    await history.clear()
    assert not (await persistence.message_page('session','main',limit=1)).items

async def test_tail_repair_and_complete_corruption_rejection(opened):
    paths,scope,model=opened
    loop=scope.require(AGENT_LOOP)
    await loop.send_message('kept','kept');await idle(loop)
    manager=scope.root.require(SESSIONS)
    await manager.close_session('session')
    rollout=paths.session('session').thread('main').rollout_file
    size=rollout.stat().st_size
    with rollout.open('ab') as output:
        output.write(b'{"unfinished":')
    resumed=await manager.open(OpenSession('session','main',None,None,'resume',False,model_override=model))
    assert rollout.stat().st_size==size
    await manager.close_session('session')
    with rollout.open('ab') as output:
        output.write(b'{complete bad json}\n')
    with pytest.raises(Exception,match='Invalid rollout'):
        await manager.open(OpenSession('session','main',None,None,'resume',False,model_override=model))
    assert rollout.stat().st_size>size

async def test_fork_all_threads_retires_pending_even_after_rebuild(opened):
    paths,scope,model=opened
    manager=scope.root.require(SESSIONS)
    loop=scope.require(AGENT_LOOP)
    await loop.send_message('committed source','source');await idle(loop)
    await loop.submit_input(InboxItem(id='pending-source',target=InboxTarget.NEXT_TURN,input=HumanInput(content='pending does not execute in fork')),wake=False)
    from XBotv2.session.api.contracts import OpenThread
    child=await manager.open_thread(OpenThread('session','child-1','main',None,None,'new',False,selected_agent='default',model_override=model))
    await child.require(AGENT_LOOP).submit_input(InboxItem(id='child-pending',target=InboxTarget.NEXT_TURN,input=HumanInput(content='child pending')),wake=False)
    target=await manager.fork_session('session')
    persistence=scope.root.require(ARCHIVE)
    assert [value.thread_id for value in (await manager.list_threads(target)).items]==['child-1','main']
    assert rows(paths.session(target).database_file,'SELECT COUNT(*) FROM inbox')==[(0,)]
    assert rows(paths.session('session').database_file,'SELECT COUNT(*) FROM inbox')==[(2,)]
    assert paths.session(target).thread('main').rollout_file.stat().st_ino!=paths.session('session').thread('main').rollout_file.stat().st_ino
    assert paths.session(target).database_file.stat().st_ino!=paths.session('session').database_file.stat().st_ino
    # Destroy only rebuildable projections, preserving SQL authority.
    def reset(c):
        for table in ('records','message_refs','surface','transcript','lineage','inbox','input_ids','open_transactions','turn_starts'):
            c.execute('DELETE FROM '+table)
        c.execute('UPDATE stream_heads SET indexed_seq=0,indexed_offset=0,indexed_mtime_ns=0')
        c.execute('DELETE FROM history_state')
    await scope.root.require(PERSISTENCE).worker.call(paths.session(target).database_file,reset)
    store=persistence._store(target,'main')
    assert not await store.inbox.load()
    page=await persistence.message_page(target,'main',limit=5)
    assert page.items[0].id=='source'
    assert rows(paths.session(target).database_file,"SELECT retired FROM input_ids WHERE input_id='pending-source'")==[(1,)]
    before=model.call_count
    forked=await manager.open(OpenSession(target,'main',None,None,'resume',False,model_override=model))
    await asyncio.sleep(.02)
    assert not forked.require(AGENT_LOOP).busy and model.call_count==before
    assert forked.require(LOOP_STATE).history[0].id=='source'

async def test_task_rows_and_process_workspace_preferences_use_owned_tables(opened):
    paths,scope,model=opened
    from XBotv2.todolist.api.service import GET_TODOS
    from XBotv2.core.api.operations import EmptyRequest
    # The ordinary service/tool path writes structured task rows.
    service=scope.require('todolist')
    outcome=await service.task_create('真实任务','独立表正文')
    assert outcome.kind=='succeeded'
    assert (await service.snapshot()).tasks[0].subject=='真实任务'
    db=paths.session('session').database_file
    assert rows(db,'SELECT subject,description FROM todolist_tasks')==[('真实任务','独立表正文')]
    assert rows(db,"SELECT COUNT(*) FROM plugin_kv WHERE key='snapshot'")==[(0,)]
    from XBotv2.workspaces.api.contracts import WORKSPACES
    registry=scope.root.require(WORKSPACES)
    import inspect
    workspace=await registry.create(str(paths.data_dir.parent/'workspace'))
    assert (paths.data_dir/'plugin-state.sqlite3').exists()
    assert not (paths.data_dir/'state.json').exists()

async def test_stale_metadata_cannot_undo_explicit_session_rename(opened):
    paths,scope,model=opened
    state=scope.require(LOOP_STATE)
    stale=state.metadata.value
    await scope.root.require(SESSIONS).rename_session('session','new authoritative title')
    from XBotv2.core.api.metadata import THREAD_METADATA_CHANGED,ThreadMetadataChanged
    await scope.emit(THREAD_METADATA_CHANGED,ThreadMetadataChanged('session','main',stale,stale.model_copy(update={'provider_override':'old-choice'})))
    assert rows(paths.session('session').database_file,'SELECT title FROM session')==[('new authoritative title',)]
    assert (await scope.root.require(SESSIONS).session_summary('session')).title=='new authoritative title'

async def test_multiple_inputs_start_one_turn_and_restore_one_turn(opened):
    paths,scope,model=opened
    loop=scope.require(AGENT_LOOP)
    from XBotv2.agentloop.api.contracts import RuntimeInput
    await loop.submit_input(InboxItem(id='steer',target=InboxTarget.NEXT_STEP,input=HumanInput(content='steer first')),wake=False)
    await loop.submit_input(InboxItem(id='notice',target=InboxTarget.NEXT_STEP,input=RuntimeInput(source='test',event='notice',content='notice first')),wake=False)
    await loop.send_message('primary','primary');await idle(loop)
    assert scope.require(LOOP_STATE).turn_count==1
    db=paths.session('session').database_file
    assert rows(db,"SELECT COUNT(*) FROM records WHERE kind='input_committed'")==[(3,)]
    assert rows(db,'SELECT turn_count FROM history_state')==[(1,)]
    manager=scope.root.require(SESSIONS)
    await manager.close_session('session')
    resumed=await manager.open(OpenSession('session','main',None,None,'resume',False,model_override=model))
    assert resumed.require(LOOP_STATE).turn_count==1

async def test_canonical_export_import_preserves_surface_and_retires_pending(opened,tmp_path):
    paths,scope,model=opened
    loop=scope.require(AGENT_LOOP)
    await loop.send_message('original archive','source');await idle(loop)
    history=scope.require(LOOP_STATE).history
    await history.replace_range(0,len(history),[CompactionSummaryMessage(id='summary',summary='small surface')],operation='compact',preserve_transcript=True)
    await loop.submit_input(InboxItem(id='not-inherited',target=InboxTarget.NEXT_TURN,input=HumanInput(content='not executed by import')),wake=False)
    persistence=scope.root.require(ARCHIVE)
    output=tmp_path/'export.jsonl'
    await persistence.export_rollout('session','main',output)
    target=await persistence.import_rollout(output)
    store=persistence._store(target,'main')
    assert [message.id for message in await store.history.load_surface()]==['summary']
    assert (await persistence.message_page(target,'main',limit=1)).items[-1].kind=='assistant'
    assert not await store.inbox.load()
    await scope.root.require(SESSIONS).delete_session(target)
    assert not paths.session(target).root.exists()
    bad=tmp_path/'bad.jsonl'
    bad.write_bytes(output.read_bytes()+b'{broken}\n')
    sessions_before={path.name for path in paths.sessions_dir.iterdir()}
    with pytest.raises(ValueError,match='Invalid canonical'):
        await persistence.import_rollout(bad)
    assert {path.name for path in paths.sessions_dir.iterdir()}==sessions_before

async def test_tool_result_projection_failure_recovers_live_working_set_without_reexecution(opened,monkeypatch,tmp_path):
    paths,scope,model=opened
    from XBotv2.agentloop.api.contracts import TOOLS
    from XBotv2.core.api.tools import Tool,succeeded_text
    from XBotv2.interactions.api.contracts import CLIENT_EVENTS
    from XBotv2.permissions.api.models import Allowed,PermissionRequest
    writes=[]
    def write_once():
        writes.append('executed')
        (tmp_path/'side-effect').write_text('executed once')
        return succeeded_text('unique actual tool result')
    scope.require(TOOLS).register(Tool.from_function(write_once),owner=scope,cleanup='fiber')
    model.responses[:]=[{'tool_calls':[{'id':'provider-call','name':'write_once','args':{}}]},{'content':'after recovery'}]
    import XBotv2.agentloop.impl.storage_projection as stream
    original=stream.project
    failed=False
    def fail_result(c,thread,batch):
        nonlocal failed
        record,offset,length=batch[0]
        if not failed and record['kind']=='message' and record['data']['message']['kind']=='tool' and paths.session('session').thread('main').rollout_file.stat().st_size>offset:
            failed=True
            raise sqlite3.OperationalError('tool result projection failed')
        return original(c,thread,batch)
    monkeypatch.setattr(stream,'project',fail_result)
    async def approve():
        async with asyncio.timeout(5):
            while True:
                pending=scope.require(CLIENT_EVENTS).pending_interactions()
                for item in pending:
                    if isinstance(item,PermissionRequest):
                        scope.require(CLIENT_EVENTS).resolve(item.interaction_id,Allowed(scope='once'))
                        return
                await asyncio.sleep(.005)
    approval=asyncio.create_task(approve())
    try:
        loop=scope.require(AGENT_LOOP)
        await loop.send_message('execute one real tool','first')
        await approval;await idle(loop)
        assert failed and writes==['executed']
        assert (tmp_path/'side-effect').read_text()=='executed once'
        canonical=await scope.root.require(ARCHIVE).message_page('session','main',limit=10)
        assert [m.id for m in scope.require(LOOP_STATE).history]==[m.id for m in canonical.items]
        await loop.send_message('continue in same owner','second');await idle(loop)
        assert writes==['executed'] and model.call_count==2
        request=model.request_history[-1]
        assert 'unique actual tool result' in request.model_dump_json()
    finally:
        approval.cancel()
        await asyncio.gather(approval,return_exceptions=True)

@pytest.mark.parametrize('operation',['delete','root-close'])
async def test_storage_close_waits_for_actual_archive_file_reader(opened,monkeypatch,operation):
    paths,scope,model=opened
    loop=scope.require(AGENT_LOOP)
    await loop.send_message('archive reader','source');await idle(loop)
    import threading
    import XBotv2.persistence.impl.stream as stream
    started=threading.Event();release=threading.Event()
    original=stream.read_record
    def blocked(*args):
        started.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(stream,'read_record',blocked)
    persistence=scope.root.require(ARCHIVE)
    reading=asyncio.create_task(persistence.message_page('session','main',limit=1))
    closing=None
    probe=scope.root.require(PERSISTENCE).database('session',owner='agentloop',tables=('history_state',))
    try:
        assert await asyncio.to_thread(started.wait,5)
        closing=asyncio.create_task(scope.root.require(SESSIONS).delete_session('session') if operation=='delete' else scope.root.close())
        async with asyncio.timeout(5):
            while scope.is_active:
                await asyncio.sleep(.005)
        async with asyncio.timeout(5):
            while True:
                probe_task=asyncio.create_task(probe.exists())
                try:await probe_task
                except asyncio.CancelledError:
                    continue
                except RuntimeError as error:
                    assert 'closing' in str(error)
                    break
                await asyncio.sleep(.005)
        assert not closing.done() and not release.is_set()
        if operation=='root-close':
            with pytest.raises(RuntimeError,match='closing'):
                await persistence.message_page('session','main',limit=1)
        assert paths.session('session').database_file.exists()
        release.set()
        await asyncio.wait_for(closing,5)
        with pytest.raises(asyncio.CancelledError):
            await reading
        if operation=='delete':
            assert not paths.session('session').root.exists()
    finally:
        release.set()
        await asyncio.gather(reading,*([closing] if closing is not None else []),return_exceptions=True)

async def test_large_working_set_pages_compacts_and_clears_without_archive_residency(opened):
    paths,scope,model=opened
    from XBotv2.core.api.messages import HumanInputMessage
    from XBotv2.core.api.parts import TextPart
    history=scope.require(LOOP_STATE).history
    await history.extend(HumanInputMessage(id=f'message-{index}',parts=(TextPart(text=f'body-{index}'),)) for index in range(513))
    archive=scope.root.require(ARCHIVE)
    newest=await archive.message_page('session','main',limit=500)
    assert len(newest.items)==500 and newest.older_cursor is not None
    older=await archive.message_page('session','main',limit=500,cursor=newest.older_cursor)
    assert len(older.items)==13 and older.older_cursor is None
    assert len(await archive.surface('session','main'))==513
    await history.replace_range(0,len(history),(CompactionSummaryMessage(id='large-summary',summary='small'),),operation='compact',preserve_transcript=True)
    assert [m.id for m in await archive.surface('session','main')]==['large-summary']
    assert len((await archive.message_page('session','main',limit=500)).items)==500
    await history.clear()
    assert not (await archive.message_page('session','main',limit=500)).items

@pytest.mark.parametrize('mutation',['edit','retarget'])
async def test_uncertain_pending_mutation_restores_latest_version_in_same_inbox(opened,monkeypatch,mutation):
    paths,scope,model=opened
    from XBotv2.agentloop.api.contracts import INBOX
    inbox=scope.require(INBOX)
    await inbox.submit(InboxItem(id='edited',target=InboxTarget.NEXT_TURN,input=HumanInput(content='original version')),wake=False)
    import XBotv2.agentloop.impl.storage_projection as projection
    original=projection.project;failed=False
    def fail(c,thread,batch):
        nonlocal failed
        record,offset,length=batch[0]
        if not failed and record['kind']==('input_edited' if mutation=='edit' else 'input_retargeted') and paths.session('session').thread('main').rollout_file.stat().st_size>offset:
            failed=True
            raise sqlite3.OperationalError('pending mutation projection failure')
        return original(c,thread,batch)
    monkeypatch.setattr(projection,'project',fail)
    async def change():
        return await inbox.edit('edited','latest version') if mutation=='edit' else await inbox.retarget('edited',InboxTarget.NEXT_STEP)
    with pytest.raises(CommitUncertain):await change()
    await change()
    assert len(inbox.pending)==1
    assert inbox.pending[0].input.content==('latest version' if mutation=='edit' else 'original version')
    if mutation=='retarget':assert inbox.pending[0].target is InboxTarget.NEXT_STEP
    await scope.require(AGENT_LOOP).send_message('new primary','primary')
    async with asyncio.timeout(5):
        while scope.require(AGENT_LOOP).busy or scope.require(AGENT_LOOP).pending_input_count:await asyncio.sleep(.005)
    assert model.call_count==(2 if mutation=='edit' else 1)
    inputs=[m for m in scope.require(LOOP_STATE).history if m.kind=='human_input']
    assert [m.id for m in inputs]==['edited','primary']
    assert inputs[0].parts[0].text==('latest version' if mutation=='edit' else 'original version')

async def test_metadata_only_fork_publishes_independent_session_and_stays_blank(opened):
    paths,scope,model=opened
    manager=scope.root.require(SESSIONS)
    await manager.rename_session('session','metadata without chat')
    source=paths.session('session')
    assert not source.thread('main').rollout_file.exists()
    target=await manager.fork_session('session')
    assert (await manager.session_summary(target)).title=='metadata without chat'
    assert (await manager.session_summary(target)).blank
    assert paths.session(target).database_file.exists()
    assert not source.thread('main').rollout_file.exists()
    assert not (await scope.root.require(ARCHIVE).message_page(target,'main')).items

async def test_managed_cas_read_uses_standard_tool_and_readonly_sandbox(tmp_path):
    paths=RuntimePaths.from_data_dir(tmp_path/'data')
    model=MockLLM([{'content':'placeholder'}])
    scope=await thread_scope(paths=paths,session_id='managed',llm_override=model,extra_plugins=[{'id':'caption','config':{'auto':False}},{'id':'sandbox','config':{'external_read':'deny'}}])
    approval=None
    try:
        from XBotv2.core.api.artifacts import ArtifactKind
        from XBotv2.sandbox.api.models import SANDBOX
        from XBotv2.interactions.api.contracts import CLIENT_EVENTS
        from XBotv2.permissions.api.models import Allowed,PermissionRequest
        ref=(await scope.artifacts.put(ArtifactKind.TOOL_RESULT,b'managed CAS original\n'))
        path=scope.artifacts.model_path(ref)
        policy=scope.require(SANDBOX)
        assert not policy.check_filesystem_access('read',{'path':path})
        assert policy.check_filesystem_access('write',{'path':path})
        model.responses[:]=[{'tool_calls':[{'id':'managed-read','name':'read','args':{'path':path}}]},{'content':'done'}]
        async def approve():
            async with asyncio.timeout(5):
                while model.call_count<2:
                    for event in scope.require(CLIENT_EVENTS).pending_interactions():
                        if isinstance(event,PermissionRequest):scope.require(CLIENT_EVENTS).resolve(event.interaction_id,Allowed(scope='once'))
                    await asyncio.sleep(.005)
        approval=asyncio.create_task(approve())
        await scope.require(AGENT_LOOP).send_message('Read managed artifact','managed-input')
        await idle(scope.require(AGENT_LOOP));await approval
        tools=[message for message in scope.require(LOOP_STATE).history if message.kind=='tool']
        assert len(tools)==1 and tools[0].outcome.kind=='succeeded'
        assert 'managed CAS original' in tools[0].model_dump_json()
        assert (await scope.artifacts.read(ref))==b'managed CAS original\n'
    finally:
        if approval is not None:
            approval.cancel();await asyncio.gather(approval,return_exceptions=True)
        await scope.root.close()

async def test_transcript_pages_move_both_directions_from_record_anchor(opened):
    _, scope, _ = opened
    from XBotv2.core.api.messages import HumanInputMessage
    from XBotv2.core.api.parts import TextPart
    history = scope.require(LOOP_STATE).history
    await history.extend(HumanInputMessage(id=f"page-{i}", parts=(TextPart(text=str(i)),)) for i in range(8))
    archive = scope.root.require(ARCHIVE)
    middle = await archive.message_page("session", "main", limit=2, before_id="page-6")
    assert [item.id for item in middle.items] == ["page-4", "page-5"]
    older = await archive.message_page("session", "main", limit=2, cursor=middle.older_cursor)
    assert [item.id for item in older.items] == ["page-2", "page-3"]
    back = await archive.message_page("session", "main", limit=2, cursor=older.newer_cursor)
    assert [item.id for item in back.items] == ["page-4", "page-5"]
    newer = await archive.message_page("session", "main", limit=2, cursor=back.newer_cursor)
    assert [item.id for item in newer.items] == ["page-6", "page-7"]
    assert newer.newer_cursor is None
    anchored = await archive.message_page("session", "main", limit=2, after_id="page-3")
    assert [item.id for item in anchored.items] == ["page-4", "page-5"]
