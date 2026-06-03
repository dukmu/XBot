"""Tests for HookManager and HookStage lifecycle."""

import pytest

from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookStage, HookContext, SessionInfo


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------

class TestHookRegistration:
    """Hook registration tests."""

    def test_register_by_enum(self, hook_manager):
        """Hooks can be registered by HookStage enum."""
        called = []

        async def my_hook(ctx):
            called.append(1)

        hook_manager.register(HookStage.BEFORE_AGENT, my_hook)
        assert hook_manager.count(HookStage.BEFORE_AGENT) == 1
        assert hook_manager.count() == 1

    def test_register_by_string(self, hook_manager):
        """Hooks can be registered by stage string."""
        called = []

        async def my_hook(ctx):
            called.append(1)

        hook_manager.register("before_agent", my_hook)
        assert hook_manager.count(HookStage.BEFORE_AGENT) == 1

    def test_register_invalid_stage_raises(self, hook_manager):
        """Invalid stage names raise ValueError."""

        async def my_hook(ctx):
            pass

        with pytest.raises(ValueError, match="Unknown hook stage"):
            hook_manager.register("nonexistent_stage", my_hook)

    def test_register_many(self, hook_manager):
        """Batch register works."""
        called = []

        async def hook1(ctx):
            called.append(1)

        async def hook2(ctx):
            called.append(2)

        hook_manager.register_many([
            (HookStage.BEFORE_AGENT, hook1),
            (HookStage.AFTER_AGENT, hook2),
        ])
        assert hook_manager.count() == 2
        assert hook_manager.count(HookStage.BEFORE_AGENT) == 1
        assert hook_manager.count(HookStage.AFTER_AGENT) == 1

    def test_clear_stage(self, hook_manager):
        """Clear a specific stage."""
        async def hook(ctx):
            pass

        hook_manager.register(HookStage.BEFORE_AGENT, hook)
        hook_manager.register(HookStage.AFTER_AGENT, hook)
        assert hook_manager.count() == 2

        hook_manager.clear(HookStage.BEFORE_AGENT)
        assert hook_manager.count(HookStage.BEFORE_AGENT) == 0
        assert hook_manager.count(HookStage.AFTER_AGENT) == 1

    def test_clear_all(self, hook_manager):
        """Clear all hooks."""
        async def hook(ctx):
            pass

        hook_manager.register(HookStage.BEFORE_AGENT, hook)
        hook_manager.clear()
        assert hook_manager.count() == 0


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
            session=SessionInfo(session_id="s", thread_id="t", personality_id="p"),
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
    """All 17 stages are defined and work."""

    def test_all_stages_exist(self):
        """Verify all 17 HookStage values."""
        stages = list(HookStage)
        assert len(stages) == 17
        stage_values = {s.value for s in stages}

        expected = {
            "on_session_init", "on_session_start", "on_session_resume", "on_session_close",
            "on_turn_start", "on_turn_end",
            "before_context", "after_context", "before_agent", "after_agent",
            "before_tools", "after_tools",
            "on_user_message", "on_assistant_message", "on_tool_message",
            "on_error", "on_config_reload",
        }
        assert stage_values == expected

    def test_short_circuit_stages(self):
        """Only loop stages permit short-circuit."""
        from xbotv2.hooks.types import SHORT_CIRCUIT_STAGES
        assert HookStage.BEFORE_CONTEXT in SHORT_CIRCUIT_STAGES
        assert HookStage.AFTER_TOOLS in SHORT_CIRCUIT_STAGES
        assert HookStage.ON_SESSION_START not in SHORT_CIRCUIT_STAGES
        assert HookStage.ON_ERROR not in SHORT_CIRCUIT_STAGES
