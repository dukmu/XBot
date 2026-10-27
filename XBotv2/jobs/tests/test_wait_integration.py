"""Wait behavior through the production loader, tools, and model loop."""

import asyncio
import json

import pytest

from XBotv2.agentloop.api.contracts import AGENT_LOOP, HumanInput, InboxItem
from XBotv2.core.api.domain import InboxTarget
from XBotv2.core.api.messages import HumanInputMessage, ToolMessage
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.provider import ProviderUser
from XBotv2.coretools.impl.shell import ShellJobSpec
from XBotv2.jobs.api.contracts import JOBS
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.permissions.api.models import PermissionPolicy, PermissionRule
from XBotv2.session.api.contracts import THREAD_EVENTS
from XBotv2.tests.runtime import engine_of, thread_scope


@pytest.mark.asyncio
async def test_model_wait_yields_to_pending_steer_and_consumes_it(tmp_path):
    generating = asyncio.Event()
    finish_generation = asyncio.Event()

    class Provider(MockLLM):
        async def _astream_once(self, request):
            if self.call_count == 1:
                generating.set()
                await finish_generation.wait()
            async for event in super()._astream_once(request):
                yield event

    provider = Provider(responses=[
        {"content": "title"},
        {"tool_calls": [{"id": "wait", "name": "wait_shell", "args": {}}]},
        {"content": "acknowledged"},
    ])
    scope = await thread_scope(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        workspace_root=tmp_path / "workspace", llm_override=provider,
    )

    class Runner:
        async def run(self, job):
            await asyncio.Event().wait()

        async def cancel(self, job):
            pass

    try:
        scope.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        jobs = scope.require(JOBS)
        job = await jobs.create(spec=ShellJobSpec("long", None, False, "long"), owner="test")
        jobs.start(job.id, Runner())
        runtime = scope.require(AGENT_LOOP)
        events = scope.require(THREAD_EVENTS).subscribe()
        await runtime.send_message("start", "start")
        async with asyncio.timeout(5):
            await generating.wait()
            await runtime.submit_input(InboxItem(
                id="steer-wait", target=InboxTarget.NEXT_STEP,
                input=HumanInput(content="change direction"),
            ), wake=False)
            finish_generation.set()
            async for frame in events:
                if frame.event.kind == "turn_ended":
                    break
        await events.aclose()
        messages = engine_of(scope).messages
        result = next(message for message in messages if isinstance(message, ToolMessage))
        payload = json.loads(result.outcome.output.parts[0].text)
        assert payload["reason"] == "steered"
        assert payload["pending"] == [job.id]
        assert job.status == "running"
        assert sum(isinstance(message, HumanInputMessage) and message.id == "steer-wait" for message in messages) == 1
        assert any(
            isinstance(message, ProviderUser)
            and any(isinstance(part, TextPart) and part.text == "change direction" for part in message.parts)
            for message in provider.request_history[-1].messages
        )
    finally:
        finish_generation.set()
        await scope.root.close()
