"""Range paths survive model/tool boundaries and explicit background ownership."""
import asyncio
import re
import shlex
from pathlib import Path

import pytest
import yaml

from XBotv2.agentloop.api.contracts import AGENT_LOOP, TOOLS
from XBotv2.agentloop.api.events import Events
from XBotv2.agentloop.api.state import LOOP_STATE
from XBotv2.agentloop.api.storage import ARCHIVE
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.stream import ModelCompleted
from XBotv2.core.api.tools import ToolCall, ToolDenied, ToolSucceeded
from XBotv2.jobs.api.contracts import JOBS
from XBotv2.interactions.api.contracts import CLIENT_EVENTS
from XBotv2.permissions.api.models import PermissionRequest, Allowed
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.persistence.api.contracts import PERSISTENCE
from XBotv2.tests.runtime import thread_scope


async def idle(loop, scope=None):
    async with asyncio.timeout(8):
        while loop.busy:
            if scope is not None:
                for event in scope.require(CLIENT_EVENTS).pending_interactions():
                    if isinstance(event, PermissionRequest):
                        scope.require(CLIENT_EVENTS).resolve(event.interaction_id, Allowed(scope="once"))
            await asyncio.sleep(.005)


class RangeModel(MockLLM):
    def __init__(self, workspace):
        super().__init__([])
        self.workspace = workspace
        self.scope = None
        self.path = self.identity = None
        self.expected = None

    async def pressure(self):
        store = self.scope.artifacts
        ref = await store.reference_records("main", record_ids=["input:range-root"])
        async with store.pin(ref) as path:
            assert Path(path).read_bytes()
        if self.path is not None:
            assert Path(self.path).read_bytes() == self.expected

    async def _astream_once(self, request):
        self._state.call_count += 1
        self._state.request_history.append(request)
        step = self.call_count
        if step == 1:
            result = {"tool_calls": [{"id": "archive", "name": "archive_records", "args": {}}]}
        else:
            text = "\n".join(part.text for message in request.messages for part in message.parts if hasattr(part, "text"))
            match = re.search(r'<attachment[^>]*artifact_id="(records/[a-f0-9]+)"[^>]*path="([^"]+)"', text)
            assert match is not None
            self.identity, self.path = match.groups()
            if self.expected is None:
                self.expected = b"".join([chunk async for chunk in self.scope.artifacts.read_chunks(self.identity)])
            assert Path(self.path).read_bytes() == self.expected
            await self.pressure()
            if step == 2:
                result = {"tool_calls": [{"id": "read", "name": "read", "args": {
                    "path": "artifact:" + self.identity, "max_chars": 80,
                }}]}
            elif step == 3:
                gate = self.workspace / "release"
                output = self.workspace / "background-copy"
                command = f"while [ ! -f {shlex.quote(str(gate))} ]; do sleep .01; done; cat {shlex.quote(self.path)} > {shlex.quote(str(output))}"
                self.command = command
                result = {"tool_calls": [{"id": "background", "name": "shell", "args": {
                    "command": command, "background": True,
                }}]}
            else:
                result = {"content": "Background capture started."}
        yield ModelCompleted(response=self.to_response(result))


async def test_model_return_gap_background_handoff_eviction_and_rebuild(tmp_path):
    workspace = tmp_path / "workspace"
    model = RangeModel(workspace)
    config = tmp_path / "data/config"
    config.mkdir(parents=True)
    (config / "plugins.yaml").write_text(yaml.safe_dump([
        {"id": "persistence", "config": {"artifact_cache_bytes": 0}},
    ]))
    scope = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"), session_id="s",
        workspace_root=workspace, llm_override=model, extra_plugins=[
            {"id": "caption", "config": {"auto": False}},
            {"id": "persistence", "config": {"artifact_cache_bytes": 0}},
            {"id": "permissions", "config": {"default_decision": "allow", "rules": []}},
            {"id": "sandbox", "config": {"enabled": True}},
        ],
    )
    model.scope = scope
    async def before_tools(event):
        if model.path is not None:
            # This event runs after model return and before standard guards
            # or tool dispatch. Eviction pressure must not open a path gap.
            await model.pressure()
    remove = scope.on(Events.TOOL_CALLS_OBSERVED, before_tools)
    try:
        await scope.require(AGENT_LOOP).send_message("RANGE_ORIGINAL " * 2000, "range-root")
        await idle(scope.require(AGENT_LOOP), scope)
        messages = tuple(scope.require(LOOP_STATE).history)
        tools = [message for message in messages if message.kind == "tool"]
        assert [message.outcome.kind for message in tools] == ["succeeded"] * 3
        job = scope.require(JOBS).all()[0]
        assert not job.terminal
        assert Path(model.path).read_bytes() == model.expected
        await model.pressure()
        (workspace / "release").touch()
        await scope.require(JOBS).wait([job.id], timeout=5)
        assert job.status == "succeeded"
        assert (workspace / "background-copy").read_bytes() == model.expected
        assert not Path(model.path).exists()
        shell_call = next(part for message in messages if message.kind == "assistant" for part in message.parts if isinstance(part, ToolCall) and part.id == "background")
        assert shell_call.args["command"] == model.command
        resolved = scope.artifacts.resolve_model_path("artifact:" + model.identity)
        assert not Path(resolved).exists()
        read = (await scope.require(TOOLS).execute_all([
            ToolCall(id="read-again", name="read", args={"path": "artifact:" + model.identity, "max_chars": 100}),
        ]))[0].message.outcome
        assert isinstance(read, ToolSucceeded)
        assert not Path(resolved).exists()
    finally:
        remove()
        (workspace / "release").touch()
        await scope.root.close()


@pytest.mark.parametrize("permit_cache", [False, True])
async def test_logical_reference_guard_and_actual_read_share_path_intent(tmp_path, permit_cache):
    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    allowed = data if permit_cache else workspace
    config = data / "config"
    config.mkdir(parents=True)
    (config / "plugins.yaml").write_text(yaml.safe_dump([
        {"id": "persistence", "config": {"artifact_cache_bytes": 0}},
    ]))
    scope = await thread_scope(
        paths=RuntimePaths.from_data_dir(data), session_id="s", workspace_root=workspace,
        llm_override=MockLLM([]), extra_plugins=[
            {"id": "persistence", "config": {"artifact_cache_bytes": 0}},
            {"id": "permissions", "config": {"default_decision": "deny", "rules": [
                {"tool_pattern": "read", "decision": "allow", "path_scope": re.escape(str(allowed)) + "/.*"},
            ]}},
            {"id": "sandbox", "config": {"enabled": True, "external_read": "deny"}},
        ],
    )
    try:
        ref = await scope.root.require(ARCHIVE).record_selection("s", "main", record_ids=[])
        call = ToolCall(id="read", name="read", args={"path": "artifact:" + ref.id})
        result = (await scope.require(TOOLS).execute_all([call]))[0].message.outcome
        assert isinstance(result, ToolSucceeded if permit_cache else ToolDenied)
        assert call.args == {"path": "artifact:" + ref.id}
        assert not Path(scope.artifacts.resolve_model_path(call.args["path"])).exists()
    finally:
        await scope.root.close()


@pytest.mark.parametrize("rebuild", [False, True], ids=["retry-existing-request", "replace-candidate"])
async def test_request_retry_retains_pins_and_rebuild_releases_old_candidate(tmp_path, rebuild):
    from XBotv2.agentloop.api.events import RetryRequest
    from XBotv2.core.api.messages import HumanInputMessage, CompactionSummaryMessage
    from XBotv2.core.api.parts import TextPart
    data = tmp_path / "data"
    data.joinpath("config").mkdir(parents=True)
    data.joinpath("config/plugins.yaml").write_text(yaml.safe_dump([
        {"id": "persistence", "config": {"artifact_cache_bytes": 0}},
    ]))
    class Model(MockLLM):
        path = None
        def __init__(self):
            super().__init__([])
        async def _astream_once(self, request):
            self._state.call_count += 1
            self._state.request_history.append(request)
            if rebuild:
                assert self.path is not None and not Path(self.path).exists()
            else:
                text = "\n".join(part.text for message in request.messages for part in message.parts if hasattr(part, "text"))
                path = re.search(r'<attachment[^>]*path="([^"]+)"', text).group(1)
                assert Path(path).exists()
                self.path = path
                if self.call_count == 1:
                    raise RuntimeError("controlled retry")
            yield ModelCompleted(response=self.to_response({"content": "done"}))
    model = Model()
    scope = await thread_scope(
        paths=RuntimePaths.from_data_dir(data), session_id="s", llm_override=model,
        extra_plugins=[{"id": "caption", "config": {"auto": False}}],
    )
    try:
        ref = await scope.root.require(ARCHIVE).record_selection("s", "main", record_ids=[])
        await scope.require(LOOP_STATE).history.append(HumanInputMessage(
            id="anchor", parts=(TextPart(text="archived attachment"),), artifacts=(ref,),
        ))
        if rebuild:
            async def replace(event):
                if model.path is not None:
                    return
                text = "\n".join(part.text for message in event.request.messages for part in message.parts if hasattr(part, "text"))
                model.path = re.search(r'<attachment[^>]*path="([^"]+)"', text).group(1)
                assert Path(model.path).exists()
                await scope.require(LOOP_STATE).history.replace_range(
                    0, 1, [CompactionSummaryMessage(id="summary", summary="small")],
                    operation="compact", preserve_transcript=True,
                )
            scope.on(Events.MODEL_REQUEST_READY, replace)
        else:
            async def retry(event):
                pressure = await scope.artifacts.reference_records("main", record_ids=[])
                async with scope.artifacts.pin(pressure):
                    assert Path(model.path).exists()
                assert Path(model.path).exists()
                return RetryRequest(event.request)
            scope.on(Events.MODEL_REQUEST_ERROR, retry)
        await scope.require(AGENT_LOOP).send_message("Continue.", "request")
        await idle(scope.require(AGENT_LOOP), scope)
        assert model.call_count == (1 if rebuild else 2)
        assert not Path(model.path).exists()
        assert scope.require(LOOP_STATE).history[-1].kind == "assistant"
    finally:
        await scope.root.close()
