import asyncio
import base64

import pytest

from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.agentloop.api.models import AttachmentInput, SendMessage
from XBotv2.core.api.artifacts import ArtifactKind
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.persistence.api.models import CommitUncertain
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.tests.runtime import thread_scope


def upload(data):
    return AttachmentInput(data=base64.b64encode(data).decode(), name="file")


def request(id, *items, content="hello"):
    return SendMessage(session_id="s", thread_id="main", content=content,
                       request_id=id, delivery="queue", attachments=items)


async def test_upload_acceptance_owns_objects(tmp_path, monkeypatch):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(paths=paths, session_id="s", llm_override=MockLLM([]))
    loop = scope.require(AGENT_LOOP)
    store = scope.artifacts
    inbox = loop._engine.inbox
    await loop._turn_lock.acquire()
    try:
        await loop.submit_message(request("one", upload(b"existing")))
        existing = inbox.pending[0].input.artifacts[0]
        assert await store.read(existing) == b"existing"
        await loop.submit_message(request("one", upload(b"existing")))
        with pytest.raises(OperationError):
            await loop.submit_message(request("one", upload(b"conflict")))
        assert len(list(paths.session("s").root.joinpath("objects").rglob("*"))) == 1
        with pytest.raises(OperationError):
            await loop.submit_message(request("bad", upload(b"new"), AttachmentInput(data="!", name="bad")))
        assert await store.exists(existing)
        assert len(list(paths.session("s").root.joinpath("objects").rglob("*"))) == 1

        original = inbox._sink.append
        async def fail(change):
            raise OSError("append failed")
        monkeypatch.setattr(inbox._sink, "append", fail)
        with pytest.raises(OSError):
            await loop.submit_message(request("failed", upload(b"failed"), upload(b"existing")))
        assert await store.exists(existing)
        assert len(list(paths.session("s").root.joinpath("objects").rglob("*"))) == 1
        async def cancel(change):
            raise asyncio.CancelledError()
        monkeypatch.setattr(inbox._sink, "append", cancel)
        with pytest.raises(asyncio.CancelledError):
            await loop.submit_message(request("cancel", upload(b"cancel")))
        assert len(list(paths.session("s").root.joinpath("objects").rglob("*"))) == 1

        async def uncertain(change):
            await original(change)
            raise CommitUncertain("confirmation lost")
        monkeypatch.setattr(inbox._sink, "append", uncertain)
        with pytest.raises(CommitUncertain):
            await loop.submit_message(request("uncertain", upload(b"uncertain")))
        monkeypatch.setattr(inbox._sink, "append", original)
        await loop.submit_message(request("uncertain", upload(b"uncertain")))
        assert await store.read(inbox.pending[-1].input.artifacts[0]) == b"uncertain"

        text = "long text " * 6000
        async with asyncio.timeout(5):
            await loop.submit_message(request("long", upload(b"same"), upload(b"same"), content=text))
        long_item = inbox.pending[-1]
        assert text.encode() in [await store.read(ref) for ref in long_item.input.artifacts]

        async def observer(change):
            raise RuntimeError("observer failed")
        monkeypatch.setattr(inbox, "_record", observer)
        with pytest.raises(RuntimeError):
            await loop.submit_message(request("committed", upload(b"committed")))
        assert await store.read(inbox.pending[-1].input.artifacts[0]) == b"committed"
    finally:
        loop._turn_lock.release()
        await scope.root.close()


async def test_publications_allow_duplicate_and_reverse_order(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(paths=paths, session_id="s", llm_override=MockLLM([]))
    store = scope.artifacts
    other = scope.root.require(PERSISTENCE).artifacts("s", "other")
    async def chunks(data):
        yield data
    async def publish(target, values):
        from contextlib import AsyncExitStack
        async with target.publication(), AsyncExitStack() as stack:
            refs = [await stack.enter_async_context(target.prepare_stream(ArtifactKind.ATTACHMENT, chunks(v))) for v in values]
            return refs
    try:
        async with asyncio.timeout(3):
            refs, _ = await asyncio.gather(publish(store, [b"a", b"a", b"b"]), publish(other, [b"b", b"a"]))
        assert [await store.read(ref) for ref in refs] == [b"a", b"a", b"b"]
    finally:
        await scope.root.close()


async def test_observer_publication_and_direct_input_lock_order(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from XBotv2.agentloop.api.contracts import HumanInput, InboxItem
    from XBotv2.core.api.domain import InboxTarget

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    scope = await thread_scope(paths=paths, session_id="s", llm_override=MockLLM([]))
    loop = scope.require(AGENT_LOOP)
    inbox = loop._engine.inbox
    store = scope.artifacts
    entered, proceed = asyncio.Event(), asyncio.Event()
    attempted = {name: asyncio.Event() for name in ("direct", "cancelled")}
    original_publication = store.publication
    original_record = inbox._record

    @asynccontextmanager
    async def tracked_publication():
        name = asyncio.current_task().get_name()
        if name in attempted:
            attempted[name].set()
        async with original_publication():
            yield

    async def observer(change):
        if getattr(getattr(change, "item", None), "id", None) == "upload":
            entered.set()
            await proceed.wait()
            ref = await store.put(ArtifactKind.ATTACHMENT, b"observer")
            assert await store.read(ref) == b"observer"
        await original_record(change)

    monkeypatch.setattr(store, "publication", tracked_publication)
    monkeypatch.setattr(inbox, "_record", observer)
    await loop._turn_lock.acquire()
    tasks = []
    try:
        async with asyncio.timeout(5):
            first = asyncio.create_task(loop.submit_message(request("upload", upload(b"upload"))))
            tasks.append(first)
            await entered.wait()
            def direct_input(id):
                return InboxItem(id=id, target=InboxTarget.NEXT_TURN, input=HumanInput(content=id))
            direct = asyncio.create_task(loop.submit_input(direct_input("direct"), wake=False), name="direct")
            cancelled = asyncio.create_task(loop.submit_input(direct_input("cancelled"), wake=False), name="cancelled")
            tasks.extend((direct, cancelled))
            await asyncio.gather(*(event.wait() for event in attempted.values()))
            await asyncio.sleep(0)
            assert not direct.done() and not cancelled.done()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            proceed.set()
            await asyncio.gather(first, direct)
            assert {item.id for item in inbox.pending} == {"upload", "direct"}
    finally:
        proceed.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        loop._turn_lock.release()
        await scope.root.close()


async def test_retry_wakes_input_after_postcommit_observer_failure(tmp_path, monkeypatch):
    from XBotv2.agentloop.api.state import LOOP_STATE
    from XBotv2.core.api.messages import AssistantMessage, HumanInputMessage
    from XBotv2.core.api.parts import TextPart

    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    llm = MockLLM([{"content": "recovered answer"}])
    scope = await thread_scope(
        paths=paths, session_id="s", llm_override=llm,
        extra_plugins=[{"id": "caption", "config": {"auto": False}}],
    )
    loop = scope.require(AGENT_LOOP)
    inbox = loop._engine.inbox
    original = inbox._record
    async def failed(change):
        raise RuntimeError("observer failed")
    monkeypatch.setattr(inbox, "_record", failed)
    message = request("retry", upload(b"original"))
    try:
        with pytest.raises(RuntimeError, match="observer failed"):
            await loop.submit_message(message)
        ref = inbox.pending[0].input.artifacts[0]
        assert await scope.artifacts.read(ref) == b"original"
        monkeypatch.setattr(inbox, "_record", original)
        async with asyncio.timeout(5):
            await loop.submit_message(message)
            wakeup = loop.wakeup_task
            assert wakeup is not None
            await wakeup
        history = scope.require(LOOP_STATE).history.snapshot()
        assert len([item for item in history if isinstance(item, HumanInputMessage)]) == 1
        replies = [item for item in history if isinstance(item, AssistantMessage)]
        assert len(replies) == 1
        assert any(isinstance(part, TextPart) and part.text == "recovered answer" for part in replies[0].parts)
        assert not inbox.pending
        assert await scope.artifacts.read(ref) == b"original"
        await loop.submit_message(message)
        assert loop.wakeup_task is None
        assert llm.call_count == 1
        assert len(scope.require(LOOP_STATE).history.snapshot()) == len(history)
    finally:
        await scope.root.close()
