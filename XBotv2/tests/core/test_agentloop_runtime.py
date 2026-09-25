"""Behavioral checks for the composed Agent loop runtime."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from XBotv2.agentloop.contracts import HumanInput, InboxItem, InboxTarget
from XBotv2.agentloop.events import Events, LoopFailure, TurnEnded
from XBotv2.agentloop.protocol import (
    AssistantCompleted,
    AssistantReasoningDelta,
    AssistantTextDelta,
    LoopError,
    LoopTurnEnded,
    ToolCompleted,
    UsageObserved,
)
from XBotv2.application.app import start_application
from XBotv2.core.artifacts import ArtifactKind
from XBotv2.core.messages import AssistantMessage
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.core.provider import ProviderAssistant, ProviderTool, ProviderUser
from XBotv2.core.stream import ModelCompleted, TextDelta
from XBotv2.core.tools import Tool, ToolCall, ToolFailed, ToolSucceeded
from XBotv2.llm.mock import MockLLM
from XBotv2.llm.provider_errors import normalize_sdk_provider_error
from XBotv2.permissions import PermissionPolicy, PermissionRule
from XBotv2.session.records import (
    AssistantRecord,
    HumanInputRecord,
    ToolRecord,
    project_message,
)
from XBotv2.session.contracts import conversation_replay


@pytest.mark.parametrize("boundary", [
    Events.BEFORE_CONTEXT_BUILD,
    Events.BEFORE_MODEL_REQUEST,
])
@pytest.mark.asyncio
async def test_hook_can_complete_a_turn_without_sending_a_model_request(
    temp_data_dir, temp_workspace, tmp_path, boundary
):
    plugin_name = "finish_at_context" if boundary == Events.BEFORE_CONTEXT_BUILD else "finish_at_request"
    plugin_dir = tmp_path / plugin_name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "from XBotv2.agentloop.events import CompleteTurn, Events\n"
        "from XBotv2.agentloop.protocol import LoopError\n"
        "\n"
        "class HookPlugin:\n"
        "    def apply(self, ctx, _config):\n"
        "        async def finish(_event):\n"
        "            return CompleteTurn(LoopError(\n"
        "                code='hook_completed', message='request handled by hook'\n"
        "            ))\n"
        f"        ctx.on(Events.{('BEFORE_CONTEXT_BUILD' if boundary == Events.BEFORE_CONTEXT_BUILD else 'BEFORE_MODEL_REQUEST')}, finish, global_=True)\n"
        "\n"
        "plugin = HookPlugin()\n",
        encoding="utf-8",
    )
    provider = MockLLM(responses=[
        {"content": "session title"},
        {"content": "This model answer must not be requested."},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id=f"runtime-hook-{plugin_name}",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[tmp_path],
        llm_override=provider,
    )
    try:
        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Handle this without a model request"),
            ),
            request_id=f"{plugin_name}-request",
        )]

        error = next(event for event in events if isinstance(event, LoopError))
        assert (error.code, error.message) == (
            "hook_completed",
            "request handled by hook",
        )
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
        expected_calls = (
            0 if boundary == Events.BEFORE_CONTEXT_BUILD else 1
        )
        # The request-boundary case can run the captioner's auxiliary request;
        # neither hook permits the Agent's user-turn request to reach a model.
        assert provider.call_count == expected_calls
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_tool_turn_executes_and_persists_the_complete_conversation(
    temp_data_dir, temp_workspace
):
    calls = []

    async def lookup(term: str) -> str:
        calls.append(term)
        return f"found:{term}"

    provider = MockLLM(responses=[
        {"content": "session title"},
        {
            "tool_calls": [{
                "id": "lookup-1",
                "name": "lookup",
                "args": {"term": "runtime"},
            }],
            "usage_metadata": {"input_tokens": 12, "output_tokens": 3},
        },
        {
            "content": "The result is found:runtime.",
            "usage_metadata": {"input_tokens": 20, "output_tokens": 8},
        },
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-tool-turn",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        services.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        services.engine.tools.register(Tool.from_function(lookup), cleanup="caller")

        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Look up runtime"),
            ),
            request_id="runtime-request",
        )]

        assert calls == ["runtime"]
        assert any(isinstance(event, ToolCompleted) for event in events)
        assert sum(isinstance(event, AssistantCompleted) for event in events) == 2
        assert sum(isinstance(event, UsageObserved) for event in events) == 2
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"

        records = [
            record for message in services.engine.messages
            if (record := project_message(message)) is not None
        ]
        assert [type(record) for record in records] == [
            HumanInputRecord,
            AssistantRecord,
            ToolRecord,
            AssistantRecord,
        ]
        assert isinstance(records[2].outcome, ToolSucceeded)
        assert "found:runtime" in "".join(
            part.text for part in records[2].outcome.output.parts
        )
        assert records[3].content == "The result is found:runtime."

        replay = conversation_replay(services.engine.messages)
        assert [item.kind for item in replay] == [
            "human_input", "assistant", "tool", "assistant",
        ]
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_turn_end_hook_observes_the_complete_durable_conversation(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "session title"},
        {
            "tool_calls": [{
                "id": "lookup-before-end",
                "name": "lookup",
                "args": {"term": "ordering"},
            }],
        },
        {"content": "The durable order is preserved."},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-turn-end-durable-order",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        async def lookup(term: str) -> str:
            return f"found:{term}"

        services.engine.tools.register(Tool.from_function(lookup), cleanup="caller")
        persistence = services.get("thread_persistence")
        assert persistence is not None
        observations = []

        async def inspect_durable_history(event: TurnEnded) -> None:
            durable_messages = persistence.history.load()
            observations.append((
                tuple(message.id for message in event.history),
                tuple(message.id for message in durable_messages),
                tuple(
                    type(project_message(message)).__name__
                    for message in durable_messages
                ),
            ))

        services.on(Events.TURN_END, inspect_durable_history)
        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Check durable ordering"),
            ),
            request_id="durable-order-request",
        )]

        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
        assert observations == [(
            tuple(message.id for message in services.engine.messages),
            tuple(message.id for message in services.engine.messages),
            ("HumanInputRecord", "AssistantRecord", "ToolRecord", "AssistantRecord"),
        )]
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_application_recovers_history_after_owner_process_crashes(
    temp_data_dir, temp_workspace
):
    child_program = """
import asyncio
import os
import sys
from pathlib import Path
from XBotv2.agentloop.contracts import HumanInput, InboxItem, InboxTarget
from XBotv2.agentloop.protocol import LoopTurnEnded
from XBotv2.application.app import start_application
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM
from XBotv2.session.records import project_message

async def main():
    app = await start_application(
        paths=RuntimePaths.from_data_dir(Path(sys.argv[1])),
        session_id='crash-recovery',
        thread_id='agent',
        workspace_root=Path(sys.argv[2]),
        plugin_dirs=[],
        llm_override=MockLLM(responses=[
            {'content': 'session title'},
            {'content': 'durable answer before crash'},
        ]),
    )
    events = [event async for event in app.engine.run_turn(
        InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content='persist this before abrupt exit'),
        ),
        request_id='crash-recovery-first-turn',
    )]
    assert isinstance(events[-1], LoopTurnEnded)
    assert events[-1].outcome.kind == 'finished'
    persistence = app.get('thread_persistence')
    assert persistence is not None
    records = [
        record for message in persistence.history.load()
        if (record := project_message(message)) is not None
    ]
    assert [record.kind for record in records] == ['human_input', 'assistant']
    os.write(1, b'durable-turn-recorded\\n')

asyncio.run(main())
os._exit(0)
"""
    repository_root = Path(__file__).resolve().parents[3]
    python_path = os.pathsep.join(filter(None, (
        str(repository_root / "XBotv2"),
        os.environ.get("PYTHONPATH", ""),
    )))
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            child_program,
            str(temp_data_dir),
            str(temp_workspace),
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONPATH": python_path},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "durable-turn-recorded"

    provider = MockLLM(responses=[{"content": "recovered follow-up"}])
    resumed = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="crash-recovery",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        stored = [
            record for message in resumed.engine.messages
            if (record := project_message(message)) is not None
        ]
        assert [record.kind for record in stored] == ["human_input", "assistant"]
        assert stored[0].content == "persist this before abrupt exit"
        assert stored[1].content == "durable answer before crash"

        events = [event async for event in resumed.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="continue after process crash"),
            ),
            request_id="crash-recovery-follow-up",
        )]
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
        user_text = [
            part.text
            for message in provider.request_history[-1].messages
            if isinstance(message, ProviderUser)
            for part in message.parts
            if isinstance(part, TextPart)
        ]
        assistant_text = [
            part.text
            for message in provider.request_history[-1].messages
            if isinstance(message, ProviderAssistant)
            for part in message.parts
            if isinstance(part, TextPart)
        ]
        assert any("persist this before abrupt exit" in text for text in user_text)
        assert any("durable answer before crash" in text for text in assistant_text)
    finally:
        await resumed.stop()


@pytest.mark.asyncio
async def test_multiple_tool_calls_keep_order_and_continue_after_tool_failure(
    temp_data_dir, temp_workspace
):
    calls = []

    async def fail_lookup(term: str) -> str:
        calls.append(("fail", term))
        raise RuntimeError("lookup service unavailable")

    async def read_status(term: str) -> str:
        calls.append(("status", term))
        return f"status:{term}:ready"

    provider = MockLLM(responses=[
        {"content": "session title"},
        {
            "tool_calls": [
                {
                    "id": "lookup-failed",
                    "name": "fail_lookup",
                    "args": {"term": "runtime"},
                },
                {
                    "id": "status-ready",
                    "name": "read_status",
                    "args": {"term": "runtime"},
                },
            ],
        },
        {"content": "The lookup failed, but status is ready."},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-multiple-tools",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        services.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        services.engine.tools.register(
            Tool.from_function(fail_lookup), cleanup="caller",
        )
        services.engine.tools.register(
            Tool.from_function(read_status), cleanup="caller",
        )

        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Check runtime"),
            ),
            request_id="runtime-multiple-tools-request",
        )]

        tool_events = [event for event in events if isinstance(event, ToolCompleted)]
        assert calls == [("fail", "runtime"), ("status", "runtime")]
        assert [event.execution.message.call.id for event in tool_events] == [
            "lookup-failed",
            "status-ready",
        ]
        failed, succeeded = [event.execution.message.outcome for event in tool_events]
        assert isinstance(failed, ToolFailed)
        assert failed.error.message == "lookup service unavailable"
        assert isinstance(succeeded, ToolSucceeded)
        assert succeeded.output.parts == (
            TextPart(text="status:runtime:ready"),
        )
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
        assert provider.call_count == 3
        assert isinstance(services.engine.messages[-1], AssistantMessage)
        assert services.engine.messages[-1].parts == (
            TextPart(text="The lookup failed, but status is ready."),
        )
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_full_agent_tool_arguments_reach_standard_handler_unchanged(
    temp_data_dir, temp_workspace
):
    expected = {
        "text": "start λ " + ("opaque payload words " * 6000) + "end 二",
        "groups": [["first", "middle"], ["last"]],
        "enabled": True,
    }
    received = []

    async def inspect_payload(
        text: str, groups: list[list[str]], enabled: bool
    ) -> str:
        received.append({"text": text, "groups": groups, "enabled": enabled})
        return f"received {len(text)} characters"

    provider = MockLLM(responses=[
        {"content": "session title"},
        {
            "tool_calls": [{
                "id": "inspect-payload-1",
                "name": "inspect_payload",
                "args": expected,
            }],
        },
        {"content": f"Handled {len(expected['text'])} characters."},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-full-tool-args",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        services.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        services.engine.tools.register(
            Tool.from_function(inspect_payload), cleanup="caller"
        )

        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Inspect this complete payload"),
            ),
            request_id="full-tool-args-request",
        )]

        assert received == [expected]
        recorded_calls = [
            part
            for message in services.engine.messages
            if isinstance(message, AssistantMessage)
            for part in message.parts
            if isinstance(part, ToolCall)
        ]
        assert len(recorded_calls) == 1
        assert recorded_calls[0].args == expected
        assert any(isinstance(event, ToolCompleted) for event in events)
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_human_attachment_keeps_logical_id_and_compiles_thread_absolute_path(
    temp_data_dir, temp_workspace
):
    attachment_bytes = b"attachment bytes remain the original artifact"
    provider = MockLLM(responses=[
        {"content": "session title"},
        {"content": "I can inspect the attached notes."},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-artifact-path",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        artifact = services.artifacts.put(
            ArtifactKind.ATTACHMENT,
            attachment_bytes,
            media_type="text/plain",
            name="notes.txt",
        )
        model_path = services.artifacts.model_path(artifact)

        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(
                    content="Inspect the attached notes",
                    artifacts=(artifact,),
                ),
            ),
            request_id="attachment-path-request",
        )]

        stored_input = next(
            message
            for message in services.engine.messages
            if message.kind == "human_input"
        )
        assert stored_input.artifacts == (artifact,)
        assert artifact.id in stored_input.model_dump_json()
        assert model_path not in stored_input.model_dump_json()
        assert services.artifacts.read(artifact) == attachment_bytes

        provider_text = [
            part.text
            for request in provider.request_history
            for message in request.messages
            if isinstance(message, ProviderUser)
            for part in message.parts
            if isinstance(part, TextPart)
            and "<attachments>" in part.text
        ]
        assert provider_text
        assert any(
            f'path="{model_path}"' in text
            and 'name="notes.txt"' in text
            for text in provider_text
        )
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_content_cache_externalizes_user_and_tool_text_through_agent_loop(
    temp_data_dir, temp_workspace
):
    long_user_text = (
        "user-start α\n"
        + ("user body that must be externalized " * 1800)
        + "\nUSER_MIDDLE_MUST_NOT_REACH_MODEL\n"
        + ("user tail material " * 600)
        + "\nuser-end 二"
    )
    long_tool_text = (
        "tool-start α\n"
        + ("tool body that must be externalized " * 1800)
        + "\nTOOL_MIDDLE_MUST_NOT_REACH_MODEL\n"
        + ("tool tail material " * 600)
        + "\ntool-end 二"
    )
    provider = MockLLM(responses=[
        {"content": "session title"},
        {
            "tool_calls": [{
                "id": "read-large-result-1",
                "name": "read_large_result",
                "args": {},
            }],
        },
        {"content": "I reviewed both complete sources."},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-content-cache",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        async def read_large_result() -> str:
            return long_tool_text

        services.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        services.engine.tools.register(
            Tool.from_function(read_large_result), cleanup="caller"
        )

        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content=long_user_text),
            ),
            request_id="content-cache-request",
        )]
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"

        final_request = provider.request_history[-1]
        provider_user_text = [
            "\n".join(
                part.text
                for part in message.parts
                if isinstance(part, TextPart)
            )
            for message in final_request.messages
            if isinstance(message, ProviderUser)
        ]
        provider_tool_text = [
            "\n".join(
                part.text
                for part in message.parts
                if isinstance(part, TextPart)
            )
            for message in final_request.messages
            if isinstance(message, ProviderTool)
        ]
        cached_user = next(
            text for text in provider_user_text if "user-start α" in text
        )
        cached_tool = next(
            text for text in provider_tool_text if "tool-start α" in text
        )
        assert "USER_MIDDLE_MUST_NOT_REACH_MODEL" not in cached_user
        assert "TOOL_MIDDLE_MUST_NOT_REACH_MODEL" not in cached_tool

        user_path = re.search(r'path="([^"]+)"', cached_user)
        tool_path = re.search(r'path="([^"]+)"', cached_tool)
        assert user_path is not None
        assert tool_path is not None
        user_model_path = Path(user_path.group(1))
        tool_model_path = Path(tool_path.group(1))
        thread_paths = RuntimePaths.from_data_dir(temp_data_dir).session(
            "runtime-content-cache"
        ).thread("agent")
        user_artifact_id = str(
            user_model_path.relative_to(thread_paths.artifacts_dir)
        )
        tool_artifact_id = str(
            tool_model_path.relative_to(thread_paths.artifacts_dir)
        )
        assert user_artifact_id.startswith("context/")
        assert tool_artifact_id.startswith("tool_results/")
        assert services.artifacts.model_path(user_artifact_id) == str(user_model_path)
        assert services.artifacts.model_path(tool_artifact_id) == str(tool_model_path)
        assert services.artifacts.read(user_artifact_id) == long_user_text.encode("utf-8")
        assert services.artifacts.read(tool_artifact_id) == long_tool_text.encode("utf-8")

        human = next(
            message for message in services.engine.messages
            if message.kind == "human_input"
        )
        assert human.parts[0].text.startswith("user-start α")
        assert "USER_MIDDLE_MUST_NOT_REACH_MODEL" not in human.parts[0].text
        assert len(human.artifacts) == 1
        assert str(human.artifacts[0].id) == user_artifact_id
        assert services.artifacts.read(human.artifacts[0]) == long_user_text.encode(
            "utf-8"
        )
        assert str(user_model_path) not in human.model_dump_json()
        tool_message = next(
            message for message in services.engine.messages
            if message.kind == "tool"
        )
        tool_preview = tool_message.outcome.output.parts[0].text
        assert tool_preview.startswith("tool-start α")
        assert "TOOL_MIDDLE_MUST_NOT_REACH_MODEL" not in tool_preview
        assert tool_artifact_id in tool_message.model_dump_json()
        assert str(tool_model_path) not in tool_message.model_dump_json()
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_content_cache_keeps_prior_long_user_input_externalized_on_next_turn(
    temp_data_dir, temp_workspace
):
    long_user_text = (
        "history-start α\n"
        + ("history body that must stay externalized " * 1600)
        + "\nHISTORY_MIDDLE_MUST_NOT_REACH_MODEL\n"
        + ("history tail " * 800)
        + "\nhistory-end 二"
    )
    provider = MockLLM(responses=[
        {"content": "session title"},
        {"content": "first turn answer"},
        {"content": "follow-up answer"},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-content-cache-follow-up",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        for content, request_id in (
            (long_user_text, "long-input-request"),
            ("A short follow-up", "follow-up-request"),
        ):
            events = [event async for event in services.engine.run_turn(
                InboxItem(
                    target=InboxTarget.NEXT_TURN,
                    input=HumanInput(content=content),
                ),
                request_id=request_id,
            )]
            assert isinstance(events[-1], LoopTurnEnded)
            assert events[-1].outcome.kind == "finished"

        follow_up_request = provider.request_history[-1]
        history_user_text = [
            "\n".join(
                part.text for part in message.parts if isinstance(part, TextPart)
            )
            for message in follow_up_request.messages
            if isinstance(message, ProviderUser)
        ]
        prior_input = next(
            text for text in history_user_text if "history-start α" in text
        )
        assert "HISTORY_MIDDLE_MUST_NOT_REACH_MODEL" not in prior_input

        stored_input = next(
            message for message in services.engine.messages
            if message.kind == "human_input"
            and message.parts[0].text.startswith("history-start α")
        )
        assert len(stored_input.artifacts) == 1
        artifact = stored_input.artifacts[0]
        assert services.artifacts.read(artifact) == long_user_text.encode("utf-8")
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_content_cache_artifact_resolves_after_application_restart(
    temp_data_dir, temp_workspace
):
    long_user_text = (
        "recovered-start α\n"
        + ("recovered history body " * 4000)
        + "\nRECOVERED_MIDDLE_MUST_NOT_REACH_MODEL\n"
        + ("recovered tail " * 1500)
        + "\nrecovered-end 二"
    )
    first_provider = MockLLM(responses=[
        {"content": "session title"},
        {"content": "first answer"},
    ])
    paths = RuntimePaths.from_data_dir(temp_data_dir)
    services = await start_application(
        paths=paths,
        session_id="runtime-content-cache-recovery",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=first_provider,
    )
    try:
        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content=long_user_text),
            ),
            request_id="long-input-before-restart",
        )]
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
        stored = next(
            message for message in services.engine.messages
            if message.kind == "human_input"
        )
        assert len(stored.artifacts) == 1
        artifact_id = stored.artifacts[0].id
        assert services.artifacts.read(stored.artifacts[0]) == long_user_text.encode(
            "utf-8"
        )
    finally:
        await services.stop()

    resumed_provider = MockLLM(responses=[{"content": "after restart"}])
    resumed = await start_application(
        paths=paths,
        session_id="runtime-content-cache-recovery",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=resumed_provider,
    )
    try:
        restored = next(
            message for message in resumed.engine.messages
            if message.kind == "human_input"
        )
        assert len(restored.artifacts) == 1
        assert restored.artifacts[0].id == artifact_id
        assert resumed.artifacts.read(restored.artifacts[0]) == long_user_text.encode(
            "utf-8"
        )

        events = [event async for event in resumed.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Continue after restart"),
            ),
            request_id="follow-up-after-restart",
        )]
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"

        request_text = [
            "\n".join(
                part.text for part in message.parts if isinstance(part, TextPart)
            )
            for message in resumed_provider.request_history[-1].messages
            if isinstance(message, ProviderUser)
        ]
        recovered_input = next(
            text for text in request_text if "recovered-start α" in text
        )
        assert "RECOVERED_MIDDLE_MUST_NOT_REACH_MODEL" not in recovered_input
        resolved_path = re.search(r'path="([^"]+)"', recovered_input)
        assert resolved_path is not None
        assert Path(resolved_path.group(1)).is_file()
    finally:
        await resumed.stop()


@pytest.mark.asyncio
async def test_content_cache_runs_after_skill_input_expansion(
    temp_data_dir, temp_workspace
):
    skill_dir = temp_workspace / ".agents" / "skills" / "large-review"
    skill_dir.mkdir(parents=True)
    skill_text = (
        "Review instruction start.\n"
        + ("Review prefix " * 1200)
        + "\nSKILL_MIDDLE_MUST_BE_EXTERNALIZED\n"
        + ("Review suffix " * 3000)
        + "\nReview instruction end."
    )
    (skill_dir / "SKILL.md").write_text(
        "---\nname: large-review\ndescription: Large review instructions\n---\n"
        + skill_text,
        encoding="utf-8",
    )
    provider = MockLLM(responses=[
        {"content": "session title"},
        {"content": "reviewed"},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-content-cache-skill",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="/large-review inspect this change"),
            ),
            request_id="large-review-request",
        )]
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"

        accepted = next(
            message for message in services.engine.messages
            if message.kind == "human_input"
        )
        assert "large-review" in accepted.parts[0].text
        assert "SKILL_MIDDLE_MUST_BE_EXTERNALIZED" not in accepted.parts[0].text
        assert len(accepted.artifacts) == 1
        original = services.artifacts.read(accepted.artifacts[0]).decode("utf-8")
        assert "SKILL_MIDDLE_MUST_BE_EXTERNALIZED" in original
        assert "inspect this change" in original

        provider_user_text = [
            "\n".join(
                part.text for part in message.parts if isinstance(part, TextPart)
            )
            for message in provider.request_history[-1].messages
            if isinstance(message, ProviderUser)
        ]
        expanded_input = next(
            text for text in provider_user_text if "large-review" in text
        )
        assert "SKILL_MIDDLE_MUST_BE_EXTERNALIZED" not in expanded_input
        assert re.search(r'path="([^"]+)"', expanded_input) is not None
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_streaming_turn_exposes_reasoning_and_usage_and_keeps_final_answer(
    temp_data_dir, temp_workspace
):
    provider = MockLLM(responses=[
        {"content": "session title"},
        {
            "reasoning": "check the request",
            "content": "The answer is 42.",
            "chunks": [
                {"reasoning": "check the request"},
                {"content": "The answer is 42."},
            ],
            "usage_metadata": {"input_tokens": 7, "output_tokens": 4},
        },
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-streaming-turn",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="What is the answer?"),
            ),
            request_id="streaming-request",
        )]

        reasoning_index = next(
            index for index, event in enumerate(events)
            if isinstance(event, AssistantReasoningDelta)
        )
        answer_index = next(
            index for index, event in enumerate(events)
            if isinstance(event, AssistantTextDelta)
        )
        usage = next(event.usage for event in events if isinstance(event, UsageObserved))
        final = next(
            event.message for event in events if isinstance(event, AssistantCompleted)
        )
        assert reasoning_index < answer_index
        assert usage.counters.input == 7
        assert usage.counters.output == 4
        assert "".join(
            part.text for part in final.parts if isinstance(part, TextPart)
        ) == "The answer is 42."
        assert "".join(
            part.text for part in final.parts if isinstance(part, ReasoningPart)
        ) == "check the request"
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_failed_provider_turn_is_durable_and_same_runtime_recovers(
    temp_data_dir, temp_workspace
):
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-failed-turn",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        # The caption call succeeds; the actual turn request then exhausts.
        llm_override=MockLLM(responses=[{"content": "session title"}]),
    )
    try:
        persistence = services.get("thread_persistence")
        assert persistence is not None
        failure_observations = []

        async def inspect_provider_failure(event: LoopFailure) -> None:
            durable_messages = persistence.history.load()
            failure_observations.append((
                tuple(message.id for message in event.history),
                tuple(message.id for message in durable_messages),
                tuple(
                    type(project_message(message)).__name__
                    for message in durable_messages
                ),
            ))

        services.on(Events.ON_ERROR, inspect_provider_failure)
        failed = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="request that will fail"),
            ),
            request_id="failed-request",
        )]
        error = next(event for event in failed if isinstance(event, LoopError))
        assert error.code == "engine_error"
        assert isinstance(failed[-1], LoopTurnEnded)
        assert failed[-1].outcome.kind == "finished"
        assert failed[-1].outcome.stop_reason == "error"
        assert isinstance(project_message(services.engine.messages[0]), HumanInputRecord)
        assert failure_observations == [(
            (services.engine.messages[0].id,),
            (services.engine.messages[0].id,),
            ("HumanInputRecord",),
        )]

        services.engine.configure(
            model_client=MockLLM(responses=[{"content": "recovered answer"}])
        )
        recovered = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="retry in the same runtime"),
            ),
            request_id="recovery-request",
        )]
        assert not any(isinstance(event, LoopError) for event in recovered)
        assert isinstance(recovered[-1], LoopTurnEnded)
        assert recovered[-1].outcome.kind == "finished"
        assert [
            record.kind
            for message in services.engine.messages
            if (record := project_message(message)) is not None
        ] == ["human_input", "human_input", "assistant"]
        assert services.engine.pending_input_count == 0
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_agent_turn_retries_transient_provider_failure_before_output(
    temp_data_dir, temp_workspace
):
    class TransientOnceMockLLM(MockLLM):
        def __init__(self):
            super().__init__(responses=[
                {"content": "session title"},
                {"content": "answer after retry"},
            ])
            self.max_retries = 1
            self.retry_backoff_factor = 0
            self.attempts = []

        def normalize_provider_error(self, error):
            return normalize_sdk_provider_error(error)

        async def _astream_once(self, request):
            self.attempts.append(None)
            if len(self.attempts) == 2:
                raise ConnectionError("temporary provider disconnect")
            async for event in super()._astream_once(request):
                yield event

    provider = TransientOnceMockLLM()
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-provider-retry",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        events = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="Retry before output"),
            ),
            request_id="provider-retry-request",
        )]

        assert len(provider.attempts) == 3
        assert provider.call_count == 2
        assert not any(isinstance(event, LoopError) for event in events)
        assert sum(isinstance(event, AssistantCompleted) for event in events) == 1
        assert isinstance(events[-1], LoopTurnEnded)
        assert events[-1].outcome.kind == "finished"
        assert isinstance(services.engine.messages[-1], AssistantMessage)
        assert services.engine.messages[-1].parts == (
            TextPart(text="answer after retry"),
        )
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_cancelled_stream_keeps_only_user_input_and_same_runtime_recovers(
    temp_data_dir, temp_workspace
):
    partial_streamed = asyncio.Event()
    release_stream = asyncio.Event()

    class BlockingMockLLM(MockLLM):
        async def _astream_once(self, request):
            if self.call_count != 1:
                async for event in super()._astream_once(request):
                    yield event
                return

            response = self.next_response()
            self._state.request_history.append(request)
            yield TextDelta(text="partial answer")
            partial_streamed.set()
            await release_stream.wait()
            yield ModelCompleted(response=self.to_response(response))

    provider = BlockingMockLLM(responses=[
        {"content": "session title"},
        {"content": "complete answer"},
    ])
    services = await start_application(
        paths=RuntimePaths.from_data_dir(temp_data_dir),
        session_id="runtime-cancelled-stream",
        thread_id="agent",
        workspace_root=temp_workspace,
        plugin_dirs=[],
        llm_override=provider,
    )
    try:
        interrupted_events = []

        async def consume_interrupted_turn():
            try:
                async for event in services.engine.run_turn(
                    InboxItem(
                        target=InboxTarget.NEXT_TURN,
                        input=HumanInput(content="answer this request"),
                    ),
                    request_id="cancelled-stream-request",
                ):
                    interrupted_events.append(event)
            except asyncio.CancelledError:
                pass

        turn = asyncio.create_task(consume_interrupted_turn())
        await asyncio.wait_for(partial_streamed.wait(), timeout=2)
        turn.cancel()
        await asyncio.wait_for(turn, timeout=2)

        assert any(
            isinstance(event, AssistantTextDelta)
            and event.text == "partial answer"
            for event in interrupted_events
        )
        assert isinstance(interrupted_events[-1], LoopTurnEnded)
        assert interrupted_events[-1].outcome.kind == "cancelled"
        assert services.engine.pending_input_count == 0
        assert [
            record.kind
            for message in services.engine.messages
            if (record := project_message(message)) is not None
        ] == ["human_input"]

        services.engine.configure(
            model_client=MockLLM(responses=[{"content": "recovered answer"}])
        )
        recovered = [event async for event in services.engine.run_turn(
            InboxItem(
                target=InboxTarget.NEXT_TURN,
                input=HumanInput(content="continue after cancellation"),
            ),
            request_id="recovery-after-cancel-request",
        )]
        assert not any(isinstance(event, LoopError) for event in recovered)
        assert isinstance(recovered[-1], LoopTurnEnded)
        assert recovered[-1].outcome.kind == "finished"
        assert [
            record.kind
            for message in services.engine.messages
            if (record := project_message(message)) is not None
        ] == ["human_input", "human_input", "assistant"]
        assert services.engine.pending_input_count == 0
    finally:
        release_stream.set()
        await services.stop()
