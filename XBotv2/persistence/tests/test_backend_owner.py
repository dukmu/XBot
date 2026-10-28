"""An independent storage owner uses the backend without an Agent loop."""
import asyncio
import sqlite3
import threading
import pytest
from xcore.api.context import Context
from XBotv2.loader.api.service import create_loader
from XBotv2.loader.api.contracts import PluginTree,PluginEntry
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.persistence.impl.session_lock import acquire_session
from XBotv2.core.api.errors import OperationError

async def test_custom_owner_transaction_rollout_replay_and_lifetime(tmp_path):
    root=Context()
    root.set('runtime_paths',RuntimePaths.from_data_dir(tmp_path/'data'))
    root.set('runtime_log',DEFAULT_RUNTIME_LOG)
    loader=create_loader()
    try:
        await loader.mount(root,PluginTree([PluginEntry('persistence','persistence')]),kind='process',required=(PERSISTENCE,))
        backend=root.require(PERSISTENCE)
        claim=backend.acquire('custom-owner')
        try:
            domain=backend.database('custom-owner',owner='custom',tables=('custom_values',))
            await domain.transaction(lambda sql:sql.execute('CREATE TABLE custom_values(id TEXT PRIMARY KEY,value TEXT)'))
            def project(sql,stream,batch):
                for record,offset,length in batch:
                    sql.execute('INSERT INTO custom_values VALUES(?,?)',(record['id'],record['data']['value']))
            rollout=backend.rollout('custom-owner','ledger',domain=domain,project=project)
            async with rollout.lease(write=True):
                await rollout.initialize()
                await rollout.append('custom.completed',{'value':'普通owner正文'},identity='stable-operation')
                assert (await rollout.flush())[0]==1
                page,cursor,cut=await rollout.read_range(limit=1,max_bytes=4096)
                assert cursor==cut==1 and page[0]['kind']=='custom.completed'
            assert await domain.transaction(lambda sql:sql.execute('SELECT value FROM custom_values'),write=False)==(('普通owner正文',),)
            # Explicitly rebuild this owner's derived rows and the generic
            # location index, then replay its unknown-to-backend event kind.
            await domain.transaction(lambda sql:sql.execute('DELETE FROM custom_values'))
            async with rollout.lease(write=True):
                await rollout.reset()
                await rollout.recover()
            assert await domain.transaction(lambda sql:sql.execute('SELECT value FROM custom_values'),write=False)==(('普通owner正文',),)
            await backend.drain('custom-owner')
            with pytest.raises(RuntimeError,match='closing'):
                await domain.transaction(lambda sql:sql.execute('SELECT value FROM custom_values'),write=False)
        finally:
            claim.release()
        # A target claim conflict must roll back the temporary source claim.
        occupied=backend.acquire('occupied')
        try:
            from XBotv2.core.api.errors import OperationError
            with pytest.raises(OperationError,match='already owned'):
                async with backend.copy_session('custom-owner','occupied'):
                    raise AssertionError('copy must not begin')
            recovered=backend.acquire('custom-owner')
            recovered.release()
        finally:
            occupied.release()
    finally:
        await root.close()
        loader.close()


async def test_delete_waits_for_sql_and_recreate_does_not_read_old_database(tmp_path):
    root = Context()
    paths = RuntimePaths.from_data_dir(tmp_path / 'data')
    root.set('runtime_paths', paths)
    root.set('runtime_log', DEFAULT_RUNTIME_LOG)
    loader = create_loader()
    await loader.mount(root, PluginTree([PluginEntry('persistence', 'persistence')]), kind='process', required=(PERSISTENCE,))
    backend = root.require(PERSISTENCE)
    claim = backend.acquire('same')
    started = threading.Event()
    release = threading.Event()
    reading = deleting = None
    try:
        state = backend.state('same').namespace('test')
        await state.set('value', 'old')
        path = paths.session('same').database_file

        async def read_blocked():
            async with backend.use('same'):
                def blocked(c):
                    started.set()
                    assert release.wait(5)
                    return c.execute('SELECT 1').fetchone()[0]
                return await backend.worker.call(path, blocked)

        reading = asyncio.create_task(read_blocked())
        assert await asyncio.to_thread(started.wait, 5)
        deleting = asyncio.create_task(backend.delete_session('same'))
        await asyncio.sleep(.05)
        assert not deleting.done() and path.exists()
        release.set()
        await asyncio.wait_for(deleting, 5)
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert not path.exists()
        await state.set('value', 'new')
        assert await state.get('value') == 'new'
    finally:
        release.set()
        await asyncio.gather(*(task for task in (reading, deleting) if task), return_exceptions=True)
        await backend.drain('same')
        claim.release()
        await root.close()
        loader.close()


@pytest.mark.parametrize('retry', ['prepare', 'close'])
async def test_failed_staging_release_preserves_directory_until_connection_closes(tmp_path, monkeypatch, retry):
    root = Context()
    paths = RuntimePaths.from_data_dir(tmp_path / 'data')
    root.set('runtime_paths', paths)
    root.set('runtime_log', DEFAULT_RUNTIME_LOG)
    loader = create_loader()
    await loader.mount(root, PluginTree([PluginEntry('persistence', 'persistence')]), kind='process', required=(PERSISTENCE,))
    backend = root.require(PERSISTENCE)
    connect = sqlite3.connect
    fail_close = threading.Event()
    staging = None

    class BrokenClose(sqlite3.Connection):
        def close(self):
            if fail_close.is_set():
                raise RuntimeError('release fault')
            super().close()

    def controlled(*args, **kwargs):
        return connect(*args, **kwargs, factory=BrokenClose)

    monkeypatch.setattr(sqlite3, 'connect', controlled)
    try:
        with pytest.raises(RuntimeError, match='release fault'):
            async with backend.prepare_session('target') as prepared:
                staging = prepared.staging
                await backend.state('target').namespace('test').set('value', 'staged')
                fail_close.set()
        assert staging.database_file.exists()
        assert not paths.session('target').root.exists()
        for locked in (paths.session('target').root, staging.root):
            with pytest.raises(OperationError, match='already owned'):
                acquire_session(locked, label='competing runtime')
        with pytest.raises(RuntimeError, match='release fault'):
            async with backend.prepare_session('other'):
                pytest.fail('must retry the failed cleanup before preparing')
        assert staging.database_file.exists()
        fail_close.clear()
        if retry == 'prepare':
            async with backend.prepare_session('other'):
                assert not staging.database_file.exists()
        else:
            await root.close()
        assert not staging.database_file.exists()
        assert not staging.root.exists()
        assert not staging.runtime.data_dir.exists()
        owner = acquire_session(paths.session('target').root, label='after cleanup')
        owner.release()
    finally:
        fail_close.clear()
        await root.close()
        loader.close()


async def test_fork_source_release_failure_retains_claims_until_cleanup(tmp_path, monkeypatch):
    root = Context()
    paths = RuntimePaths.from_data_dir(tmp_path / 'data')
    root.set('runtime_paths', paths)
    root.set('runtime_log', DEFAULT_RUNTIME_LOG)
    loader = create_loader()
    await loader.mount(root, PluginTree([PluginEntry('persistence', 'persistence')]), kind='process', required=(PERSISTENCE,))
    backend = root.require(PERSISTENCE)
    claim = backend.acquire('source')
    await backend.state('source').namespace('test').set('value', 'source')
    await backend.worker.release(paths.session('source').database_file)
    claim.release()
    original = backend.worker.release
    fail = True

    async def release(path=None):
        if fail and path == paths.session('source').database_file:
            raise RuntimeError('source release fault')
        await original(path)

    monkeypatch.setattr(backend.worker, 'release', release)
    try:
        with pytest.raises(RuntimeError, match='source release fault'):
            async with backend.copy_session('source', 'target'):
                pass
        for sid in ('source', 'target'):
            with pytest.raises(OperationError, match='already owned'):
                acquire_session(paths.session(sid).root, label='competing runtime')
        fail = False
        async with backend.prepare_session('other'):
            for sid in ('source', 'target'):
                owner = acquire_session(paths.session(sid).root, label='after cleanup')
                owner.release()
    finally:
        fail = False
        await root.close()
        loader.close()
