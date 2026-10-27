"""An independent storage owner uses the backend without an Agent loop."""
import asyncio
import pytest
from xcore.api.context import Context
from XBotv2.loader.api.service import create_loader
from XBotv2.loader.api.contracts import PluginTree,PluginEntry
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.persistence.api.contracts import PERSISTENCE

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
