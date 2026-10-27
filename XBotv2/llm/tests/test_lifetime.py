import asyncio

import pytest

from XBotv2.llm.api.contracts import LlmConfig, ModelConfig, ProviderConfig
from XBotv2.llm.impl.plugin import ProviderDirectories
from XBotv2.llm.impl.service import ModelService


class BlockingProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.closed = 0

    async def astream(self, request):
        self.started.set()
        await self.finish.wait()
        yield request

    async def close(self):
        self.closed += 1


def config(name="one"):
    return LlmConfig(default_provider="test", providers={"test": ProviderConfig(
        protocol="fake", default_model=name,
        models=[ModelConfig(model=name)],
    )})


@pytest.mark.asyncio
async def test_shared_bindings_streams_churn_and_shutdown():
    directories = ProviderDirectories()
    made = []

    def factory(provider, model):
        if model.model == "bad":
            raise ValueError("creation failed")
        client = BlockingProvider()
        made.append(client)
        return client

    directories._factories = {"fake": factory}
    first = config()
    service = directories.acquire(first)
    assert directories.acquire(first) is service
    a, b = ModelService(), ModelService()
    await a.replace(service.acquire(first.providers["test"]))
    await b.replace(service.acquire(first.providers["test"]))
    assert len(made) == 1
    old = made[0]
    stream = a.astream("ok")
    task = asyncio.create_task(anext(stream))
    await old.started.wait()
    await a.close()
    await directories.release(first, service)
    assert old.closed == 0
    other = config("two")
    await b.replace(service.acquire(other.providers["test"]))
    assert old.closed == 0
    old.finish.set()
    assert await task == "ok"
    await stream.aclose()
    assert old.closed == 1
    with pytest.raises(ValueError, match="creation failed"):
        service.acquire(config("bad").providers["test"])
    assert made[1].closed == 0
    await b.close()
    assert made[1].closed == 1
    await directories.release(first, service)
    for i in range(5):
        cfg = config(str(i))
        item = directories.acquire(cfg)
        model = ModelService()
        await model.replace(item.acquire(cfg.providers["test"]))
        await model.close()
        await directories.release(cfg, item)
    assert not directories._catalogs
    assert all(client.closed == 1 for client in made)

    item = directories.acquire(first)
    model = ModelService()
    await model.replace(item.acquire(first.providers["test"]))
    active = made[-1]
    stream = model.astream("done")
    request = asyncio.create_task(anext(stream))
    await active.started.wait()
    shutdown = asyncio.create_task(directories.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await shutdown
    await model.close()
    assert active.closed == 1


@pytest.mark.asyncio
async def test_loader_releases_only_closed_thread(tmp_path, monkeypatch):
    from XBotv2.core.api.paths import RuntimePaths
    from XBotv2.llm.api.contracts import LLM, MODEL
    from XBotv2.main import open_runtime
    from XBotv2.session.api.contracts import OpenSession, SESSIONS

    monkeypatch.setenv("MINIMAX_API_TOKEN", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    made = []
    def factory(provider, model):
        client = BlockingProvider()
        made.append(client)
        return client
    root.require(LLM)._factories["anthropic"] = factory
    sessions = root.require(SESSIONS)
    def request(name):
        return OpenSession(name, "main", str(tmp_path), "minimax", "new", True)
    try:
        a = await sessions.open(request("a"))
        b = await sessions.open(request("b"))
        assert len(made) == 1
        await sessions.close_session("a")
        assert made[0].closed == 0
        stream = b.require(MODEL).astream("ok")
        task = asyncio.create_task(anext(stream))
        await made[0].started.wait()
        made[0].finish.set()
        assert await task == "ok"
        await stream.aclose()
        await sessions.close_session("b")
        assert made[0].closed == 1
    finally:
        await root.close()
    assert made[0].closed == 1


@pytest.mark.asyncio
async def test_close_failure_does_not_skip_other_clients():
    directories = ProviderDirectories()
    clients = []

    class FailingProvider(BlockingProvider):
        async def close(self):
            await super().close()
            raise ValueError("close failed")

    def factory(provider, model):
        client = FailingProvider() if model.model == "bad-close" else BlockingProvider()
        clients.append(client)
        return client

    directories._factories = {"fake": factory}
    first, second = config("bad-close"), config("good")
    a, b = directories.acquire(first), directories.acquire(second)
    a.acquire(first.providers["test"])
    b.acquire(second.providers["test"])
    with pytest.raises(ValueError, match="close failed"):
        await directories.close()
    assert [client.closed for client in clients] == [1, 1]


@pytest.mark.asyncio
async def test_direct_use_keeps_stream_alive_after_release():
    directories = ProviderDirectories()
    client = BlockingProvider()
    directories._factories = {"fake": lambda provider, model: client}
    cfg = config()
    service = directories.acquire(cfg)
    use = service.acquire(cfg.providers["test"])
    stream = use.astream("ok")
    request = asyncio.create_task(anext(stream))
    await client.started.wait()
    await use.close()
    assert client.closed == 0
    client.finish.set()
    assert await request == "ok"
    await stream.aclose()
    assert client.closed == 1
    await directories.release(cfg, service)
    await directories.close()


@pytest.mark.asyncio
async def test_cancelled_selection_finishes_old_close_and_updates_binding(tmp_path, monkeypatch):
    from XBotv2.agents.api.contracts import AGENT_RUNTIME
    from XBotv2.core.api.paths import RuntimePaths
    from XBotv2.llm.api.contracts import LLM, MODEL
    from XBotv2.main import open_runtime
    from XBotv2.session.api.contracts import OpenSession, SESSIONS

    monkeypatch.setenv("MINIMAX_API_TOKEN", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    clients = []
    entered, finish = asyncio.Event(), asyncio.Event()

    class SlowClose(BlockingProvider):
        async def close(self):
            entered.set()
            await finish.wait()
            await super().close()

    def factory(provider, model):
        client = SlowClose() if not clients else BlockingProvider()
        clients.append(client)
        return client

    root.require(LLM)._factories["anthropic"] = factory
    sessions = root.require(SESSIONS)
    try:
        thread = await sessions.open(OpenSession("a", "main", str(tmp_path), "minimax", "new", True))
        runtime = thread.require(AGENT_RUNTIME)
        selection = asyncio.create_task(runtime.select_provider("minimax", "Minimax-M2.7"))
        await entered.wait()
        selection.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await selection
        assert runtime.current_selection().model == "Minimax-M2.7"
        stream = thread.require(MODEL).astream("new")
        clients[1].finish.set()
        assert await anext(stream) == "new"
        await stream.aclose()
        await sessions.close_session("a")
        assert [client.closed for client in clients] == [1, 1]
    finally:
        finish.set()
        await root.close()


@pytest.mark.asyncio
async def test_early_stream_close_waits_for_provider_cleanup():
    entered, finish = asyncio.Event(), asyncio.Event()

    class CleanupProvider(BlockingProvider):
        async def astream(self, request):
            try:
                yield request
            finally:
                entered.set()
                await finish.wait()

    client = CleanupProvider()
    directories = ProviderDirectories()
    directories._factories = {"fake": lambda provider, model: client}
    cfg = config()
    service = directories.acquire(cfg)
    use = service.acquire(cfg.providers["test"])
    stream = use.astream("ok")
    assert await anext(stream) == "ok"
    await use.close()
    closing = asyncio.create_task(stream.aclose())
    try:
        await entered.wait()
        assert not closing.done()
        assert client.closed == 0
    finally:
        finish.set()
        await closing
        await directories.release(cfg, service)
        await directories.close()
    assert client.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_openai_stream_cleanup_precedes_release_with_shared_use(cancel):
    from types import SimpleNamespace
    from openai.types.chat import ChatCompletionChunk
    from XBotv2.llm.impl.openai import OpenAICompatibleProvider
    from XBotv2.llm.tests.test_provider_runtime import request

    entered, finish, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = []
    chunk = ChatCompletionChunk.model_validate({
        "id": "completion", "object": "chat.completion.chunk", "created": 1,
        "model": "test-model", "choices": [{"index": 0,
            "delta": {"content": "hello"}, "finish_reason": None}],
    })

    class Stream:
        def __init__(self, first):
            self.first = first
            self.iterator = self.chunks()

        async def chunks(self):
            try:
                yield chunk
                if self.first:
                    waiting.set()
                    await asyncio.Event().wait()
            finally:
                if self.first:
                    entered.set()
                    await finish.wait()
                closed.append("stream")

        def __aiter__(self):
            return self.iterator

        async def close(self):
            await self.iterator.aclose()

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            return Stream(self.calls == 1)

        async def close(self):
            closed.append("client")

    provider = OpenAICompatibleProvider(api_key="test", base_url=None)
    await provider.client.close()
    provider.client = Client()
    directories = ProviderDirectories()
    directories._factories = {"fake": lambda *_: provider}
    cfg = config()
    service = directories.acquire(cfg)
    first = service.acquire(cfg.providers["test"])
    second = service.acquire(cfg.providers["test"])
    stream = first.astream(request())
    assert (await anext(stream)).text == "hello"
    await first.close()
    if cancel:
        closing = asyncio.create_task(anext(stream))
        await waiting.wait()
        closing.cancel()
    else:
        closing = asyncio.create_task(stream.aclose())
    try:
        await entered.wait()
        assert not closing.done()
        assert closed == []
        other = second.astream(request())
        assert (await anext(other)).text == "hello"
        await other.aclose()
        assert closed == ["stream"]
        await second.close()
        assert closed == ["stream"]
    finally:
        finish.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
        await directories.release(cfg, service)
        await directories.close()
    assert closed == ["stream", "stream", "client"]
