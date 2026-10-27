"""Canonical acceptance, byte artifacts, recovery and streaming read ownership."""
import asyncio
import hashlib
import sqlite3
import threading
from contextlib import aclosing

import pytest

from XBotv2.agentloop.api.contracts import AGENT_LOOP, INBOX, HumanInput, InboxItem
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.errors import OperationError
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.persistence.api.models import CommitUncertain
from XBotv2.session.api.contracts import SESSIONS
from XBotv2.tests.runtime import thread_scope


async def opened(tmp_path, *, mode="new", policy=None):
    return await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), session_id="s", mode=mode,
        llm_override=MockLLM([{"content": "done"}] * 6),
        extra_plugins=[{"id": "caption", "config": {"auto": False}},
                       {"id": "content_cache", "config": policy or {}}],
    )


def item(content, identity="original"):
    return InboxItem(id=identity, target=InboxTarget.NEXT_TURN, input=HumanInput(content=content))


async def download(archive, session_id, ref):
    payload = await archive.artifact(session_id, "main", ref.id)
    async with aclosing(payload.content):
        return b"".join([chunk async for chunk in payload.content])


async def test_over_record_budget_single_original_edit_retry_restart_and_fork(tmp_path):
    original = "original-start α\n" + "文" * 1800000 + "ONLY_MIDDLE_RAW" + "文" * 1800000 + "\noriginal-end"
    encoded = original.encode("utf-8")
    assert len(encoded) > 8 * 1024 * 1024
    edited = "edited-start\n" + "edited-middle 二" * 12000 + "\nedited-end"
    scope = await opened(tmp_path)
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    try:
        loop = scope.require(AGENT_LOOP)
        await loop.submit_input(item(original), wake=False)
        archive = scope.root.require(ARCHIVE)
        page = scope.require(INBOX).pending
        assert len(page) == 1
        old_ref = page[0].input.artifacts[0]
        assert old_ref.sha256 == hashlib.sha256(encoded).hexdigest()
        assert await download(archive, "s", old_ref) == encoded
        rollout = paths.session("s").thread("main").rollout_file
        assert rollout.stat().st_size < 50000
        assert "ONLY_MIDDLE_RAW" not in rollout.read_text()
        await loop.update_pending_input("original", "edit", edited)
        edit_ref = scope.require(INBOX).pending[0].input.artifacts[0]
        assert edit_ref.id != old_ref.id
        assert await download(archive, "s", old_ref) == encoded
        assert await download(archive, "s", edit_ref) == edited.encode("utf-8")
        before = rollout.read_bytes()
        objects = set(paths.session("s").root.joinpath("objects").iterdir())
        await loop.submit_input(item(original), wake=False)
        assert rollout.read_bytes() == before
        assert scope.require(INBOX).pending[0].input.artifacts[0] == edit_ref
        with pytest.raises(OperationError, match="different content"):
            await loop.submit_input(item(original + "conflict"), wake=False)
        assert set(paths.session("s").root.joinpath("objects").iterdir()) == objects
        await loop.send_message("run pending", "run")
        async with asyncio.timeout(5):
            while loop.busy or loop.pending_input_count:
                await asyncio.sleep(.005)
        before = rollout.read_bytes()
        await loop.submit_input(item(original), wake=False)
        assert rollout.read_bytes() == before
        fork_id = await scope.root.require(SESSIONS).fork_session("s")
        assert await download(archive, fork_id, old_ref) == encoded
        assert await download(archive, fork_id, edit_ref) == edited.encode("utf-8")
        destination = tmp_path / "canonical.jsonl"
        await archive.export_rollout("s", "main", destination)
        imported = await archive.import_rollout(destination)
        assert await download(archive, imported, old_ref) == encoded
        assert await download(archive, imported, edit_ref) == edited.encode("utf-8")
    finally:
        await scope.root.close()
    resumed = await opened(tmp_path, mode="resume", policy={"threshold_chars": 1000000})
    try:
        before = rollout.read_bytes()
        await resumed.require(AGENT_LOOP).submit_input(item(original), wake=False)
        assert rollout.read_bytes() == before
        assert await download(resumed.root.require(ARCHIVE), "s", old_ref) == encoded
    finally:
        await resumed.root.close()


async def test_prelog_failure_removes_unreferenced_publication(tmp_path, monkeypatch):
    import XBotv2.agentloop.impl.storage_projection as projection
    scope = await opened(tmp_path)
    original = projection.project
    def reject(sql, thread, batch):
        if batch[0][0]["kind"] == "input_accepted":
            raise ValueError("reject before append")
        return original(sql, thread, batch)
    monkeypatch.setattr(projection, "project", reject)
    try:
        with pytest.raises(ValueError, match="reject before append"):
            await scope.require(AGENT_LOOP).submit_input(item("source" * 100000), wake=False)
        root = tmp_path / "data" / "sessions" / "s"
        assert list(root.joinpath("objects").iterdir()) == []
        assert list(root.joinpath(".spool").iterdir()) == []
        assert not root.joinpath("threads/main/rollout.jsonl").exists()
    finally:
        await scope.root.close()


async def test_cancel_during_durable_append_keeps_original_for_same_id_recovery(tmp_path, monkeypatch):
    import XBotv2.persistence.impl.stream as physical
    scope = await opened(tmp_path)
    started, finish = threading.Event(), threading.Event()
    append_file = physical.append_file
    def pause_after_append(*args):
        append_file(*args)
        started.set()
        assert finish.wait(5)
    monkeypatch.setattr(physical, "append_file", pause_after_append)
    body = "original for uncertain cancellation α " * 10000
    task = asyncio.create_task(scope.require(AGENT_LOOP).submit_input(item(body), wake=False))
    try:
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        finish.set()
        with pytest.raises(CommitUncertain):
            await task
        monkeypatch.setattr(physical, "append_file", append_file)
        await scope.require(AGENT_LOOP).submit_input(item(body), wake=False)
        pending = scope.require(INBOX).pending
        assert len(pending) == 1
        ref = pending[0].input.artifacts[0]
        assert await download(scope.root.require(ARCHIVE), "s", ref) == body.encode("utf-8")
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        await scope.root.close()


async def test_root_close_terminates_real_socket_with_unread_artifact_body(tmp_path):
    import httpx
    from urllib.parse import quote
    from XBotv2.server.api.contracts import HTTP_LISTENER

    scope = await opened(tmp_path)
    await scope.require(AGENT_LOOP).submit_input(item("full original " * 1300000), wake=False)
    ref = scope.require(INBOX).pending[0].input.artifacts[0]
    socket = tmp_path / "server.sock"
    server = asyncio.create_task(scope.root.require(HTTP_LISTENER).serve(uds=str(socket)))
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(socket)),
            base_url="http://localhost", timeout=5,
        ) as client:
            async with asyncio.timeout(5):
                while True:
                    try:
                        if (await client.get("/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.01)
            url = "/sessions/s/threads/main/artifacts/" + quote(ref.id, safe="/")
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks = response.aiter_bytes(65536)
                assert len(await anext(chunks)) == 65536
                # Keep the socket open without reading the remaining body.
                # transport.close() alone would wait indefinitely for its
                # unsent buffer even after all physical readers have exited.
                await asyncio.wait_for(scope.root.close(), 3)
                await asyncio.wait_for(server, 3)
    finally:
        await scope.root.close()
        if not server.done():
            server.cancel()
        await asyncio.gather(server, return_exceptions=True)


async def test_uncertain_long_edit_recovery_is_idempotent_on_original_edit_bytes(tmp_path, monkeypatch):
    import XBotv2.agentloop.impl.storage_projection as projection
    scope = await opened(tmp_path)
    original_project = projection.project
    body = "original accepted body " * 10000
    edit = "edited accepted body 二 " * 10000
    failed = False
    rollout = tmp_path / "data/sessions/s/threads/main/rollout.jsonl"
    def fail_after_edit_log(sql, thread, batch):
        nonlocal failed
        record, offset, _ = batch[0]
        if record["kind"] == "input_edited" and not failed and rollout.stat().st_size > offset:
            failed = True
            raise sqlite3.OperationalError("edit confirmation failed")
        return original_project(sql, thread, batch)
    try:
        loop = scope.require(AGENT_LOOP)
        await loop.submit_input(item(body), wake=False)
        monkeypatch.setattr(projection, "project", fail_after_edit_log)
        with pytest.raises(CommitUncertain):
            await loop.update_pending_input("original", "edit", edit)
        before = rollout.read_bytes()
        await loop.update_pending_input("original", "edit", edit)
        assert rollout.read_bytes() == before
        ref = scope.require(INBOX).pending[0].input.artifacts[0]
        assert await download(scope.root.require(ARCHIVE), "s", ref) == edit.encode("utf-8")
        await loop.submit_input(item(body), wake=False)
        assert rollout.read_bytes() == before
    finally:
        await scope.root.close()


async def test_preview_larger_than_policy_threshold_does_not_externalize_itself(tmp_path):
    scope = await opened(tmp_path, policy={
        "threshold_chars": 12, "preview_chars": 8, "tail_chars": 2,
    })
    body = "original raw content " * 100
    try:
        loop = scope.require(AGENT_LOOP)
        await loop.send_message(body, "one-body")
        async with asyncio.timeout(5):
            while loop.busy:
                await asyncio.sleep(.005)
        message = next(message for message in scope.require(LOOP_STATE).history if message.kind == "human_input")
        assert len(message.artifacts) == 1
        assert await download(scope.root.require(ARCHIVE), "s", message.artifacts[0]) == body.encode("utf-8")
        objects = tmp_path / "data/sessions/s/objects"
        assert len(list(objects.iterdir())) == 1
    finally:
        await scope.root.close()
