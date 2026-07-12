"""Tests for HookManager and HookStage lifecycle."""

import asyncio
import re
from pathlib import Path

import pytest
from xbotv2.api import HookAction, HookDecision

from xbotv2.hooks.manager import HookManager
from xbotv2.api.hooks import (
    SHORT_CIRCUIT_STAGES,
    STRICT_FAILURE_STAGES,
    HookStage,
    HookContext,
    SessionInfo,
)


def _hook_stage_matrix_rows() -> dict[str, list[str]]:
    matrix = Path(__file__).parents[2] / "docsv2" / "hook_stage_matrix.md"
    rows: dict[str, list[str]] = {}
    for line in matrix.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\| `([^`]+)` \| (.+) \|$", line)
        if not match:
            continue
        rows[match.group(1)] = [cell.strip() for cell in match.group(2).split("|")]
    return rows


def test_hook_stage_matrix_covers_every_stage():
    rows = _hook_stage_matrix_rows()

    assert set(rows) == {stage.value for stage in HookStage}


def test_hook_stage_matrix_matches_short_and_strict_sets():
    rows = _hook_stage_matrix_rows()
    short_stages = {stage.value for stage in SHORT_CIRCUIT_STAGES}
    strict_stages = {stage.value for stage in STRICT_FAILURE_STAGES}

    for stage, cells in rows.items():
        short = cells[1]
        strict = cells[2]
        if stage in short_stages:
            assert short == "default"
        assert (strict == "yes") == (stage in strict_stages)


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------

class TestHookRegistration:

    def test_register_by_enum(self, hook_manager):
        called = []

        async def my_hook(ctx):
            called.append(1)

        hook_manager.register(HookStage.BEFORE_AGENT, my_hook)

    def test_unregister_hook_removes_one_registration(self, hook_manager):
        async def hook(ctx):
            pass

        hook_manager.register(HookStage.BEFORE_AGENT, hook)
        hook_manager.register(HookStage.BEFORE_AGENT, hook)

        assert hook_manager.unregister(HookStage.BEFORE_AGENT, hook) is True
        assert hook_manager.unregister(HookStage.BEFORE_AGENT, hook) is True
        assert hook_manager.unregister(HookStage.BEFORE_AGENT, hook) is False


# ------------------------------------------------------------------
# Execution
# ------------------------------------------------------------------

class TestHookExecution:
    """Hook execution behavior tests."""

    @pytest.mark.asyncio
    async def test_hooks_run_in_order(self, hook_manager, hook_context):
        """Hooks run in registration order."""
        order = []

        async def hook1(ctx):
            order.append(1)

        async def hook2(ctx):
            order.append(2)

        async def hook3(ctx):
            order.append(3)

        hook_manager.register(HookStage.ON_SESSION_START, hook1)
        hook_manager.register(HookStage.ON_SESSION_START, hook2)
        hook_manager.register(HookStage.ON_SESSION_START, hook3)

        hook_context.stage = HookStage.ON_SESSION_START
        await hook_manager.run(HookStage.ON_SESSION_START, hook_context, short_circuit=False)
        assert order == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_short_circuit_loop_hook(self, hook_manager, hook_context):
        """Loop hooks short-circuit on first truthy return."""
        order = []

        async def hook1(ctx):
            order.append(1)
            return "short_circuit"

        async def hook2(ctx):
            order.append(2)  # Should NOT run

        hook_manager.register(HookStage.BEFORE_AGENT, hook1)
        hook_manager.register(HookStage.BEFORE_AGENT, hook2)

        result = await hook_manager.run(HookStage.BEFORE_AGENT, hook_context)
        assert result == "short_circuit"
        assert order == [1]  # hook2 never ran

    @pytest.mark.asyncio
    async def test_continue_decision_does_not_short_circuit(self, hook_manager, hook_context):
        order = []

        async def continue_hook(ctx):
            order.append("continue")
            return HookDecision(HookAction.CONTINUE)

        async def next_hook(ctx):
            order.append("next")

        hook_manager.register(HookStage.BEFORE_TOOL_CALL, continue_hook)
        hook_manager.register(HookStage.BEFORE_TOOL_CALL, next_hook)

        assert await hook_manager.run(HookStage.BEFORE_TOOL_CALL, hook_context) is None
        assert order == ["continue", "next"]

    @pytest.mark.asyncio
    async def test_deny_decision_short_circuits_with_reason(self, hook_manager, hook_context):
        decision = HookDecision(HookAction.DENY, "blocked by policy")

        async def deny_hook(ctx):
            return decision

        hook_manager.register(HookStage.BEFORE_TOOL_CALL, deny_hook)

        assert await hook_manager.run(HookStage.BEFORE_TOOL_CALL, hook_context) is decision

    @pytest.mark.asyncio
    async def test_reused_context_clears_previous_short_circuit_result(
        self,
        hook_manager,
        hook_context,
    ):
        decision = HookDecision(HookAction.STOP, "stop once")

        async def stop_hook(ctx):
            return decision

        hook_manager.register(HookStage.BEFORE_AGENT, stop_hook)

        assert await hook_manager.run(HookStage.BEFORE_AGENT, hook_context) is decision
        assert hook_context.short_circuit_result is decision

        await hook_manager.run(
            HookStage.ON_SESSION_START,
            hook_context,
            short_circuit=False,
        )

        assert hook_context.stage is HookStage.ON_SESSION_START
        assert hook_context.short_circuit_result is None

    @pytest.mark.asyncio
    async def test_no_short_circuit_for_lifecycle_hook(self, hook_manager, hook_context):
        """Session/turn/message hooks run ALL callbacks regardless of return."""
        order = []

        async def hook1(ctx):
            order.append(1)
            return "ignored"

        async def hook2(ctx):
            order.append(2)

        hook_manager.register(HookStage.ON_SESSION_START, hook1)
        hook_manager.register(HookStage.ON_SESSION_START, hook2)

        hook_context.stage = HookStage.ON_SESSION_START
        await hook_manager.run(HookStage.ON_SESSION_START, hook_context, short_circuit=False)
        assert order == [1, 2]

    @pytest.mark.asyncio
    async def test_hook_error_in_lifecycle_does_not_stop_others(self, hook_manager, hook_context):
        """An error in one lifecycle hook doesn't prevent others."""
        order = []

        async def failing_hook(ctx):
            order.append("fail")
            raise RuntimeError("test error")

        async def good_hook(ctx):
            order.append("good")

        hook_manager.register(HookStage.ON_TURN_START, failing_hook)
        hook_manager.register(HookStage.ON_TURN_START, good_hook)

        hook_context.stage = HookStage.ON_TURN_START
        # Should not raise — error is logged, good_hook still runs
        result = await hook_manager.run(
            HookStage.ON_TURN_START, hook_context, short_circuit=False
        )
        assert result is None
        assert order == ["fail", "good"]

    @pytest.mark.asyncio
    async def test_observer_cancellation_propagates_without_running_later_hooks(
        self,
        hook_manager,
        hook_context,
    ):
        order = []

        async def cancelled_hook(ctx):
            order.append("cancelled")
            raise asyncio.CancelledError()

        async def later_hook(ctx):
            order.append("later")

        hook_manager.register(HookStage.ON_TURN_START, cancelled_hook)
        hook_manager.register(HookStage.ON_TURN_START, later_hook)

        with pytest.raises(asyncio.CancelledError):
            await hook_manager.run(
                HookStage.ON_TURN_START,
                hook_context,
                short_circuit=False,
            )

        assert order == ["cancelled"]

    @pytest.mark.asyncio
    async def test_strict_lifecycle_hook_errors_run_all_then_raise(
        self, hook_manager, hook_context
    ):
        """Critical lifecycle stages run all callbacks, then expose failures."""
        order = []

        async def failing_hook(ctx):
            order.append("fail")
            raise RuntimeError("persist failed")

        async def good_hook(ctx):
            order.append("good")

        hook_manager.register(HookStage.BEFORE_STATE_PERSIST, failing_hook)
        hook_manager.register(HookStage.BEFORE_STATE_PERSIST, good_hook)

        hook_context.stage = HookStage.BEFORE_STATE_PERSIST
        with pytest.raises(ExceptionGroup, match="before_state_persist") as exc_info:
            await hook_manager.run(
                HookStage.BEFORE_STATE_PERSIST,
                hook_context,
                short_circuit=False,
            )

        assert order == ["fail", "good"]
        assert len(exc_info.value.exceptions) == 1
        assert isinstance(exc_info.value.exceptions[0], RuntimeError)

    @pytest.mark.asyncio
    async def test_hook_error_in_loop_raises(self, hook_manager, hook_context):
        """An error in a loop hook propagates (short_circuit=True)."""
        async def failing_hook(ctx):
            raise RuntimeError("test error")

        hook_manager.register(HookStage.BEFORE_AGENT, failing_hook)

        with pytest.raises(RuntimeError, match="test error"):
            await hook_manager.run(HookStage.BEFORE_AGENT, hook_context)


# ------------------------------------------------------------------
# Context passing
# ------------------------------------------------------------------

class TestHookContext:
    """HookContext carries the right data."""

    @pytest.mark.asyncio
    async def test_context_has_stage(self, hook_manager, hook_context):
        """HookContext.stage is set correctly."""
        received = []

        async def checker(ctx):
            received.append(ctx.stage)

        hook_manager.register(HookStage.BEFORE_AGENT, checker)
        await hook_manager.run(HookStage.BEFORE_AGENT, hook_context)
        assert received == [HookStage.BEFORE_AGENT]

    @pytest.mark.asyncio
    async def test_context_has_session_info(self, hook_manager, hook_context, session_info):
        """HookContext carries session info."""
        received = []

        async def checker(ctx):
            received.append(ctx.session.session_id)
            received.append(ctx.session.thread_id)

        hook_manager.register(HookStage.BEFORE_AGENT, checker)
        await hook_manager.run(HookStage.BEFORE_AGENT, hook_context)
        assert received == ["test-session", "test-thread"]

    @pytest.mark.asyncio
    async def test_context_stage_specific_fields(self, hook_manager):
        """Stage-specific fields are accessible."""
        ctx = HookContext(
            stage=HookStage.ON_USER_MESSAGE,
            user_input="hello",
            session=SessionInfo(
                session_id="s",
                thread_id="t",
                workspace_root="/workspace",
                provider="p",
            ),
        )

        received = []

        async def checker(c):
            received.append(c.user_input)

        hook_manager.register(HookStage.ON_USER_MESSAGE, checker)
        await hook_manager.run(HookStage.ON_USER_MESSAGE, ctx, short_circuit=False)
        assert received == ["hello"]


# ------------------------------------------------------------------
# Stage coverage
# ------------------------------------------------------------------

class TestAllStages:
    """All hook stages are defined and work."""

    def test_all_stages_exist(self):
        """Verify all HookStage values."""
        stages = list(HookStage)
        assert len(stages) == 41
        stage_values = {s.value for s in stages}

        expected = {
            "on_session_init", "on_session_start", "on_session_resume", "on_session_close",
            "on_turn_start", "on_turn_end", "on_stop", "on_stop_failure",
            "before_user_message_accept", "after_user_message_accept",
            "before_context", "pre_compact", "post_compact",
            "before_context_build", "after_context",
            "after_context_components_build", "after_context_build",
            "before_agent", "before_tool_schema_bind", "after_tool_schema_bind",
            "before_model_request", "after_model_response", "on_model_request_error",
            "after_agent",
            "before_tools", "after_tools",
            "on_user_message", "on_assistant_message", "on_tool_message",
            "on_tool_calls_parsed", "on_permission_request", "on_permission_denied",
            "before_tool_call", "after_tool_call", "on_tool_call_failure",
            "post_tool_batch",
            "on_tool_denied", "on_client_event",
            "before_state_persist", "after_state_persist",
            "on_error",
        }
        assert stage_values == expected

    def test_short_circuit_stages(self):
        """Only loop stages permit short-circuit."""
        from xbotv2.api.hooks import SHORT_CIRCUIT_STAGES
        assert HookStage.BEFORE_CONTEXT in SHORT_CIRCUIT_STAGES
        assert HookStage.PRE_COMPACT in SHORT_CIRCUIT_STAGES
        assert HookStage.BEFORE_CONTEXT_BUILD in SHORT_CIRCUIT_STAGES
        assert HookStage.BEFORE_TOOL_SCHEMA_BIND in SHORT_CIRCUIT_STAGES
        assert HookStage.BEFORE_TOOL_CALL in SHORT_CIRCUIT_STAGES
        assert HookStage.AFTER_TOOLS in SHORT_CIRCUIT_STAGES
        assert HookStage.BEFORE_MODEL_REQUEST in SHORT_CIRCUIT_STAGES
        assert HookStage.AFTER_CONTEXT_BUILD not in SHORT_CIRCUIT_STAGES
        assert HookStage.ON_STOP not in SHORT_CIRCUIT_STAGES
        assert HookStage.POST_TOOL_BATCH not in SHORT_CIRCUIT_STAGES
        assert HookStage.ON_SESSION_START not in SHORT_CIRCUIT_STAGES
        assert HookStage.ON_ERROR not in SHORT_CIRCUIT_STAGES
