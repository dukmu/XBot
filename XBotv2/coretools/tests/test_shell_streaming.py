"""Real registry/approval/sandbox shell output, including failed jobs."""
import asyncio
import hashlib
import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from XBotv2.agentloop.api.contracts import TOOLS, AGENT_LOOP
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import ToolCall, ToolFailed, ToolSucceeded
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.jobs.api.contracts import JOBS
from XBotv2.permissions.api.models import Allowed, PermissionRequest
from XBotv2.tests.runtime import thread_scope
from XBotv2.llm.impl.mock import MockLLM


@pytest.fixture(params=[False, True], ids=["host", "bwrap"])
async def scope(tmp_path, request):
    if request.param and not shutil.which("bwrap"):
        pytest.skip("bubblewrap unavailable")
    context = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), session_id="s",
        llm_override=MockLLM([]), extra_plugins=[
            {"id": "sandbox", "config": {"enabled": request.param}},
            {"id": "permissions", "config": {"default_decision": "allow", "rules": [
                {"tool_pattern": "shell", "decision": "ask"},
            ]}},
        ],
    )
    router = context.require(CLIENT_EVENTS)
    async def client_sink(event):
        waiter = router.waiter_for(event)
        pending = waiter.register(event.interaction_id)
        return await waiter.wait_registered(event.interaction_id, pending, None)
    uninstall = router.install(client_sink)
    try:
        yield context
    finally:
        uninstall()
        await context.root.close()


async def invoke(scope, name, args):
    results = await scope.require(TOOLS).execute_all([ToolCall(id="call", name=name, args=args)])
    return results[0].message.outcome


async def approved_shell(scope, **args):
    call = asyncio.create_task(invoke(scope, "shell", args))
    try:
        async with asyncio.timeout(5):
            while True:
                requests = scope.require(CLIENT_EVENTS).pending_interactions()
                permissions = [event for event in requests if isinstance(event, PermissionRequest)]
                if permissions:
                    assert len(permissions) == 1
                    scope.require(CLIENT_EVENTS).resolve(permissions[0].interaction_id, Allowed(scope="once"))
                    return await call
                if call.done():
                    pytest.fail(f"Shell completed without approval: {call.result()}")
                await asyncio.sleep(.005)
    finally:
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)


async def test_large_foreground_keeps_original_bytes_with_bounded_preview(scope):
    source = "import os; os.write(1,bytes(range(256))*32768)"
    result = await approved_shell(scope, command=f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}")
    assert isinstance(result, ToolSucceeded)
    assert len(result.output.parts[0].text) < 9000
    assert len(result.output.artifacts) == 1
    ref = result.output.artifacts[0]
    payload = bytes(range(256)) * 32768
    assert ref.size == len(payload)
    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    digest = hashlib.sha256()
    async for chunk in scope.artifacts.read_chunks(ref):
        assert len(chunk) <= 256 * 1024
        digest.update(chunk)
    assert digest.hexdigest() == ref.sha256


async def test_background_failed_output_and_byte_cursors(scope):
    source = "import os; os.write(1,b'begin'+bytes(range(256))*1000+b'end'); raise SystemExit(7)"
    result = await approved_shell(scope, command=f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}", background=True)
    assert isinstance(result, ToolSucceeded)
    identity = result.output.parts[0].text.split()[-1]
    waited = await invoke(scope, "wait_shell", {"ids": [identity], "timeout_ms": 5000})
    metadata = json.loads(waited.output.parts[0].text)
    assert metadata["ready"][0]["state"] == "failed_running"
    assert metadata["ready"][0]["exit_code"] == 7
    payload = b"begin" + bytes(range(256)) * 1000 + b"end"
    for offset in [0, 123, len(payload) - 5]:
        result = await invoke(scope, "read_shell", {"id": identity, "cursor": offset, "max_bytes": 500})
        page = json.loads(result.output.parts[0].text)
        end = min(len(payload), offset + 500)
        assert page["data"] == payload[offset:end].decode("utf-8", errors="replace")
        assert page["next_cursor"] == (end if end < len(payload) else None)
    invalid = await invoke(scope, "read_shell", {"id": identity, "max_bytes": 65537})
    assert isinstance(invalid, ToolFailed)
    assert invalid.error.code == "invalid_arguments"


async def test_background_timeout_retains_partial_output_and_close_reaps(scope):
    result = await approved_shell(scope, command="printf partial; sleep 10", background=True, timeout_seconds=.15)
    identity = result.output.parts[0].text.split()[-1]
    waited = await invoke(scope, "wait_shell", {"ids": [identity], "timeout_ms": 5000})
    metadata = json.loads(waited.output.parts[0].text)
    assert metadata["ready"][0]["state"] == "failed_running"
    page = await invoke(scope, "read_shell", {"id": identity})
    assert json.loads(page.output.parts[0].text)["data"] == "partial"
    marker = Path(scope.require(AGENT_LOOP).workspace_root) / "must-not-appear"
    running = await approved_shell(scope, command=f"printf cancelled; sleep .3; touch {shlex.quote(str(marker))}", background=True)
    running_id = running.output.parts[0].text.split()[-1]
    job = scope.require(JOBS).get_or_none(running_id)
    await scope.root.close()
    assert job.status in {"cancelled_before_start", "cancelled_running"}
    await asyncio.sleep(.4)
    assert not marker.exists()
