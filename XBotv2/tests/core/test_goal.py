"""Behavior tests for the evaluator-driven Goal plugin."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from XBotv2.application import COLLECT_STATUS_SLOTS, StatusSlots
from XBotv2.agentloop import EventContext
from XBotv2.goal.models import GoalConfig, GoalSnapshot
from XBotv2.goal.plugin import GoalPlugin
from XBotv2.llm.mock import MockLLM
from plugin_harness import mount_ctx


class RecordingDriver:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.injected: list[tuple[str, dict[str, object]]] = []
        self.messages: list = []
        #: Continuation marker of the most recent queued turn, kept across
        #: ``requests.clear()`` so turn simulation always uses what the plugin
        #: actually sent.
        self.last_continuation = False

    async def followup(self, content: str, **kwargs: object) -> None:
        self.requests.append((content, kwargs))
        metadata = kwargs.get("metadata")
        self.last_continuation = bool(
            metadata.get("continuation") if isinstance(metadata, dict) else False
        )

    async def inject(self, content: str, **kwargs: object) -> None:
        self.injected.append((content, kwargs))


class FailingDriver(RecordingDriver):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def followup(self, content: str, **kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("inbox closed")
        await super().followup(content, **kwargs)


class FakeJobs:
    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses

    def snapshots(self):
        return [SimpleNamespace(status=status) for status in self._statuses]


def verdict(value: str, reason: str = "") -> dict[str, object]:
    return {"content": json.dumps({"verdict": value, "reason": reason})}


FAST = GoalConfig(
    checkin_seconds=0.01,
    retry_seconds=0.01,
    max_retries=2,
    stall_turns=2,
    max_idle_checkins=1,
)


def make_plugin(state_store, *, responses, config=None, jobs=None):
    ctx = mount_ctx(state_store)
    driver = RecordingDriver()
    ctx.set("engine", driver)
    evaluator = MockLLM(responses=responses)
    ctx.model.replace(evaluator)
    GoalPlugin().apply(ctx, config)
    service = ctx.goal
    if jobs is not None:
        service._jobs = jobs
    return SimpleNamespace(
        service=service,
        ctx=ctx,
        driver=driver,
        evaluator=evaluator,
        model=ctx.model,
        store=ctx.state.namespace("goal"),
    )


async def settle(service) -> None:
    """Wait for the evaluation task the turn-end hook scheduled."""
    task = service._evaluation
    if task is not None:
        await task


async def end_turn(
    harness,
    stop_reason: str = "completed",
    *,
    used_tools: bool = False,
    start: bool = True,
):
    """Simulate a started turn ending, then let the evaluator run.

    ``start`` drives the real ``TURN_START`` listener using the continuation
    flag the plugin itself put on the queued round, so a round that forgot to
    mark itself automatic would latch the scheduler and fail here.
    """
    if start:
        await harness.service.on_turn_start(
            EventContext(continuation=harness.driver.last_continuation)
        )
    harness.service._turn_used_tools = used_tools
    await harness.service.on_turn_end(_Stop(stop_reason))
    await settle(harness.service)


async def set_goal(harness, condition: str = "all tests pass"):
    result = await harness.service.set_condition(condition)
    assert result.status == "success"
    return result


def test_goal_registers_the_minimal_agent_tools(state_store):
    harness = make_plugin(state_store, responses=[])
    names = sorted(entry.tool.name for entry in harness.ctx.tools.registrations())
    # The Agent may create a goal and read it; only the evaluator may end one.
    assert names == ["create_goal", "get_goal"]
    commands = [command.name for command in harness.ctx.commands.all()]
    assert commands == ["goal"]


@pytest.mark.asyncio
async def test_agent_can_create_a_goal_and_read_its_status(state_store):
    harness = make_plugin(state_store, responses=[verdict("not_yet_met", "half done")])
    tool = {entry.tool.name: entry.tool for entry in harness.ctx.tools.registrations()}
    assert tool["create_goal"].parameters["required"] == ["condition"]

    created = await tool["create_goal"].ainvoke({"condition": "ship the API"})
    assert created.status == "success"
    assert "[active] ship the API" in created.content
    content, kwargs = harness.driver.requests[-1]
    assert 'Objective: "ship the API"' in content
    assert "Round: 1/20" in content
    assert content.startswith('<system_reminder source="goal" event="round"')
    assert kwargs["metadata"] == {
        "kind": "round",
        "round": 1,
        "max_rounds": 20,
        "continuation": True,
    }

    await end_turn(harness)
    read = await tool["get_goal"].ainvoke({})
    assert read.status == "success"
    assert "[active] ship the API" in read.content
    assert "Latest: half done" in read.content
    assert "round 2/20" in read.content

    assert (await tool["get_goal"].ainvoke({})).status == "success"
    await harness.service.clear()
    assert (await tool["get_goal"].ainvoke({})).content.startswith("[cleared]")


@pytest.mark.asyncio
async def test_goal_never_injects_a_per_request_projection(state_store):
    from XBotv2.agentloop import EventContext

    harness = make_plugin(state_store, responses=[])
    await set_goal(harness, "ship the API")
    harness.driver.injected.clear()

    # Nothing rebuilds per request: the goal travels in its round prompt, and
    # the per-build component hook is not subscribed at all.
    from XBotv2.context_builder import CONTEXT_COMPONENTS_BUILT
    assert harness.ctx._bus.listener_count(CONTEXT_COMPONENTS_BUILT) == 0

    await harness.service.on_turn_start(EventContext(continuation=False))
    assert harness.driver.injected == []


@pytest.mark.asyncio
async def test_compaction_restates_the_active_goal_once(state_store):
    from XBotv2.session import HistoryChanged

    harness = make_plugin(state_store, responses=[verdict("not_yet_met", "half done")])
    await set_goal(harness, "ship the API")
    await end_turn(harness)
    harness.driver.injected.clear()

    await harness.service.on_compaction(
        HistoryChanged(messages=(), operation="compact:auto", turns=2)
    )

    assert len(harness.driver.injected) == 1
    content, kwargs = harness.driver.injected[0]
    assert content.startswith('<system_reminder source="goal" event="compaction"')
    assert 'Active goal: "ship the API"' in content
    assert "Round: 2/20" in content
    assert "Latest evaluator verdict: half done" in content
    assert kwargs["source"] == "goal"
    assert kwargs["metadata"]["kind"] == "compaction"

    harness.driver.injected.clear()
    await harness.service.on_compaction(
        HistoryChanged(messages=(), operation="undo", turns=1)
    )
    assert harness.driver.injected == []


@pytest.mark.asyncio
async def test_round_cap_pauses_the_goal(state_store):
    from XBotv2.goal.models import GoalConfig

    harness = make_plugin(
        state_store,
        responses=[verdict("not_yet_met", "still going")] * 4,
        config=GoalConfig(max_rounds=2),
    )
    await set_goal(harness)

    await end_turn(harness)
    assert (await harness.service.snapshot()).turns_evaluated == 1
    assert harness.driver.requests[-1][0].count("Round: 2/2") == 1

    harness.driver.requests.clear()
    await end_turn(harness)

    goal = await harness.service.snapshot()
    assert goal.status == "paused"
    assert "Round cap reached (2/2)" in goal.reason
    assert harness.driver.requests == []


@pytest.mark.asyncio
async def test_active_goal_exposes_its_round_slot(state_store):
    harness = make_plugin(state_store, responses=[])
    await set_goal(harness, "ship the API")

    slots = StatusSlots()
    await harness.service.contribute_status(slots)

    assert slots.values["goal"] == "active"
    assert slots.values["goal_objective"] == "ship the API"
    assert slots.values["goal_round"] == "1/20"


def test_goal_diagnostics_describe_the_evaluator(state_store):
    diagnostics = GoalPlugin().diagnostics()
    assert diagnostics["evaluator"] == "auxiliary_model_call"
    assert diagnostics["commands"] == ["/goal", "/goal <condition>", "/goal clear"]


@pytest.mark.asyncio
async def test_setting_a_goal_starts_a_turn_with_the_condition(state_store):
    harness = make_plugin(state_store, responses=[])

    await set_goal(harness, "all tests in test/auth pass")

    goal = await harness.service.snapshot()
    assert goal is not None
    assert goal.status == "active"
    assert goal.condition == "all tests in test/auth pass"
    assert goal.started_at > 0
    content, kwargs = harness.driver.requests[-1]
    assert 'Objective: "all tests in test/auth pass"' in content
    assert "Round: 1/20" in content
    assert kwargs == {
        "source": "goal",
        "metadata": {
            "kind": "round",
            "round": 1,
            "max_rounds": 20,
            "continuation": True,
        },
    }


@pytest.mark.asyncio
async def test_not_yet_met_continues_with_the_evaluator_reason(state_store):
    harness = make_plugin(state_store, responses=[
        verdict("not_yet_met", "test/auth still fails"),
    ])
    await set_goal(harness)
    harness.driver.requests.clear()

    await end_turn(harness)

    goal = await harness.service.snapshot()
    assert goal.status == "active"
    assert goal.turns_evaluated == 1
    assert goal.reason == "test/auth still fails"
    content, kwargs = harness.driver.requests[-1]
    assert kwargs["metadata"] == {
        "kind": "round",
        "round": 2,
        "max_rounds": 20,
        "continuation": True,
    }
    assert 'Objective: "all tests pass"' in content
    assert "Round: 2/20" in content
    assert "Evaluator: test/auth still fails" in content


@pytest.mark.asyncio
async def test_loop_keeps_rounding_through_real_turn_starts(state_store):
    """Two consecutive not-yet-met verdicts must each admit another round."""
    harness = make_plugin(state_store, responses=[
        verdict("not_yet_met", "first"),
        verdict("not_yet_met", "second"),
    ])
    await set_goal(harness)

    await end_turn(harness)
    assert "Round: 2/20" in harness.driver.requests[-1][0]

    await end_turn(harness)

    assert "Round: 3/20" in harness.driver.requests[-1][0]
    assert (await harness.service.snapshot()).turns_evaluated == 2


@pytest.mark.asyncio
async def test_met_achieves_the_goal_and_stops(state_store):
    harness = make_plugin(state_store, responses=[
        verdict("met", "npm test exits 0"),
    ])
    await set_goal(harness)
    harness.driver.requests.clear()

    await end_turn(harness)

    goal = await harness.service.snapshot()
    assert goal.status == "achieved"
    assert goal.reason == "npm test exits 0"
    assert goal.turns_evaluated == 1
    assert goal.finished_at >= goal.started_at
    assert harness.driver.requests == []


@pytest.mark.asyncio
async def test_impossible_fails_the_goal(state_store):
    harness = make_plugin(state_store, responses=[
        verdict("impossible", "the branch was deleted"),
    ])
    await set_goal(harness)

    await end_turn(harness)

    goal = await harness.service.snapshot()
    assert goal.status == "failed"
    assert goal.reason == "the branch was deleted"


@pytest.mark.asyncio
async def test_evaluator_receives_the_conversation_and_a_strict_contract(state_store):
    harness = make_plugin(state_store, responses=[verdict("not_yet_met", "keep going")])
    await set_goal(harness, "ship the API")
    harness.service._engine.messages = [
        SimpleNamespace(role="user", content="please ship", tool_calls=()),
        SimpleNamespace(role="assistant", content="working", tool_calls=()),
    ]

    await end_turn(harness)

    request = harness.evaluator.get_call_messages(0)
    assert request[0].role == "system"
    assert "single JSON object" in request[0].content
    payload = request[-1].content
    assert "ship the API" in payload
    assert "please ship" in payload


@pytest.mark.asyncio
async def test_malformed_verdict_follows_the_retry_policy(state_store):
    harness = make_plugin(state_store, responses=[{"content": "not json at all"}], config=FAST)
    await set_goal(harness)

    await end_turn(harness)

    goal = await harness.service.snapshot()
    assert goal.status == "active"
    assert goal.retries == 1
    assert "Evaluation failed" in goal.reason


@pytest.mark.asyncio
async def test_status_reports_condition_turns_tokens_and_reason(state_store):
    harness = make_plugin(state_store, responses=[verdict("not_yet_met", "half done")])
    await set_goal(harness, "ship the API")
    await end_turn(harness)

    result = await harness.service.command("")
    assert result.status == "ok"
    assert "[active] ship the API" in result.message
    assert "round 2/20" in result.message
    assert "Latest: half done" in result.message


@pytest.mark.asyncio
async def test_clear_aliases_stop_the_goal(state_store):
    harness = make_plugin(state_store, responses=[])
    await set_goal(harness)

    for alias in ("clear", "stop", "off", "reset", "none", "cancel"):
        result = await harness.service.command(alias)
        assert result.status == "ok"
        assert "Goal cleared" in result.message
        assert (await harness.service.snapshot()).status == "cleared"
        await set_goal(harness)

    assert (await harness.service.command("clear")).status == "ok"


@pytest.mark.asyncio
async def test_status_without_a_goal(state_store):
    harness = make_plugin(state_store, responses=[])
    assert (await harness.service.command("")).message == "No goal set."
    await set_goal(harness)
    await harness.store.delete("snapshot")
    assert (await harness.service.command("")).message == "No goal set."


@pytest.mark.asyncio
async def test_interrupt_pauses_the_goal(state_store):
    harness = make_plugin(state_store, responses=[])
    await set_goal(harness)
    harness.driver.requests.clear()

    await harness.service.on_turn_end(_Stop("client_interrupt"))

    goal = await harness.service.snapshot()
    assert goal.status == "paused"
    assert goal.reason == "Interrupted."
    assert harness.driver.requests == []


@pytest.mark.asyncio
async def test_recoverable_error_retries_then_pauses(state_store):
    harness = make_plugin(state_store, responses=[], config=FAST)
    await set_goal(harness)
    harness.driver.requests.clear()
    harness.service._continuation_pending = False

    for _ in range(2):
        await harness.service.on_error(
            EventContext(error=RuntimeError("connection reset"))
        )
    retried = await harness.service.snapshot()
    assert retried.status == "active"
    assert retried.retries == 2

    # The retry timer asks for the next turn after the configured delay.
    await asyncio.sleep(0.05)
    assert harness.driver.requests
    assert "Note: Retry after the previous error." in harness.driver.requests[-1][0]

    await harness.service.on_error(
        EventContext(error=RuntimeError("connection reset"))
    )
    goal = await harness.service.snapshot()
    assert goal.status == "paused"
    assert "Paused after" in goal.reason


@pytest.mark.asyncio
async def test_unrecoverable_error_clears_the_goal(state_store):
    harness = make_plugin(state_store, responses=[], config=FAST)
    await set_goal(harness)

    await harness.service.on_error(EventContext(error=RuntimeError("invalid api key")))

    goal = await harness.service.snapshot()
    assert goal.status == "cleared"
    assert "unrecoverable error" in goal.reason
    assert "Run /goal again" in goal.reason


@pytest.mark.asyncio
async def test_tool_less_turns_stall_the_loop_and_a_human_prompt_resumes_it(state_store):
    harness = make_plugin(state_store, responses=[
        verdict("not_yet_met", "no progress"),
        verdict("not_yet_met", "no progress"),
    ], config=FAST)
    await set_goal(harness)
    harness.service._turn_used_tools = False

    await end_turn(harness)
    assert (await harness.service.snapshot()).tool_less_turns == 1

    harness.driver.requests.clear()
    await end_turn(harness)

    goal = await harness.service.snapshot()
    assert goal.stalled is True
    assert harness.driver.requests == []

    # The next human prompt restarts evaluation.
    await harness.service.on_turn_start(EventContext(continuation=False))
    assert (await harness.service.snapshot()).stalled is False


@pytest.mark.asyncio
async def test_tool_use_resets_the_stall_counter(state_store):
    harness = make_plugin(
        state_store, responses=[verdict("not_yet_met", "keep going")], config=FAST
    )
    await set_goal(harness)
    harness.service._turn_used_tools = False
    await end_turn(harness)

    await end_turn(harness, used_tools=True)

    assert (await harness.service.snapshot()).tool_less_turns == 0


@pytest.mark.asyncio
async def test_background_work_defers_evaluation_and_checks_in(state_store):
    harness = make_plugin(
        state_store, responses=[], config=FAST, jobs=FakeJobs(["running"])
    )
    await set_goal(harness)
    harness.driver.requests.clear()
    harness.service._continuation_pending = False

    await harness.service.on_turn_end(_Stop("completed"))

    # Evaluation is deferred: the evaluator model was never called.
    assert harness.evaluator.call_count == 0
    await asyncio.sleep(0.05)

    goal = await harness.service.snapshot()
    assert goal.status == "active"
    assert goal.idle_checkins == 1
    assert goal.checkin_seconds > 0
    assert harness.driver.requests
    assert "Note: Check-in:" in harness.driver.requests[-1][0]


@pytest.mark.asyncio
async def test_background_finish_evaluates_instead_of_checking_in(state_store):
    jobs = FakeJobs(["running"])
    harness = make_plugin(
        state_store, responses=[verdict("met", "done")], config=FAST, jobs=jobs
    )
    await set_goal(harness)

    await harness.service.on_turn_end(_Stop("completed"))
    jobs._statuses = ["completed"]
    await asyncio.sleep(0.05)
    await settle(harness.service)

    assert (await harness.service.snapshot()).status == "achieved"


@pytest.mark.asyncio
async def test_resume_resets_counters_and_continues(state_store):
    harness = make_plugin(state_store, responses=[verdict("not_yet_met", "keep going")])
    await set_goal(harness)
    await end_turn(harness)
    assert (await harness.service.snapshot()).turns_evaluated == 1

    harness.driver.requests.clear()
    harness.service._continuation_pending = False
    await harness.service.on_session_resume(EventContext())

    goal = await harness.service.snapshot()
    assert goal.status == "active"
    assert goal.turns_evaluated == 0
    assert goal.reason == ""
    assert "Round: 1/20" in harness.driver.requests[-1][0]
    assert goal.condition in harness.driver.requests[-1][0]


@pytest.mark.asyncio
async def test_status_slots_and_client_events_report_the_goal(state_store):
    from XBotv2.application import RUNTIME_EVENT, RuntimeEvent

    harness = make_plugin(state_store, responses=[verdict("met", "verified")])
    published: list[RuntimeEvent] = []
    harness.ctx.on(RUNTIME_EVENT, published.append)
    await set_goal(harness)
    harness.service._pending_tool_calls = 2
    await end_turn(harness, used_tools=True)

    slots = StatusSlots()
    await harness.ctx.emit(COLLECT_STATUS_SLOTS, slots)
    assert slots.values["goal"] == "achieved"
    assert slots.values["goal_reason"] == "verified"
    assert "goal_stats" in slots.values

    assert published[-1].client_event.type == "goal_updated"
    assert published[-1].client_event.data["status"] == "achieved"
    assert published[-1].client_event.data["reason"] == "verified"


@pytest.mark.asyncio
async def test_v2_snapshot_is_migrated_to_the_evaluator_model(state_store):
    harness = make_plugin(state_store, responses=[])
    await harness.store.set("snapshot", {
        "schema_version": 2,
        "objective": "finish the audit",
        "status": "complete",
        "summary": "Audit finished.",
        "stats": {"turns": 2, "tool_calls": 3, "total_tokens": 40,
                  "todo_items": 1, "todo_completed": 1},
    })

    goal = await harness.service.snapshot()

    assert goal.condition == "finish the audit"
    assert goal.status == "achieved"
    assert goal.reason == "Audit finished."
    assert goal.stats.total_tokens == 40


@pytest.mark.asyncio
async def test_condition_bounds_are_enforced(state_store):
    harness = make_plugin(state_store, responses=[])

    empty = await harness.service.set_condition(" ")
    long = await harness.service.set_condition("x" * 4_001)

    assert empty.error.code == "invalid_condition"
    assert long.error.code == "condition_too_long"
    assert await harness.service.snapshot() is None


@pytest.mark.asyncio
async def test_replacing_an_active_goal_reports_the_previous_condition(state_store):
    harness = make_plugin(state_store, responses=[])
    await set_goal(harness, "first condition")

    result = await harness.service.set_condition("second condition")

    assert "Replaced active goal: first condition" in result.content


@pytest.mark.asyncio
async def test_failed_continuation_enqueue_does_not_latch_the_scheduler(state_store):
    harness = make_plugin(state_store, responses=[])
    failing = FailingDriver()
    harness.service._engine = failing

    with pytest.raises(RuntimeError):
        await harness.service.set_condition("keep going")

    assert harness.service._continuation_pending is False
    failing.fail = False
    await harness.service._start_round(await harness.service.snapshot())
    assert failing.requests


@pytest.mark.asyncio
async def test_dispose_cancels_pending_timers(state_store):
    harness = make_plugin(state_store, responses=[], config=FAST)
    await set_goal(harness)
    await harness.service.on_error(EventContext(error=RuntimeError("reset")))
    assert harness.service._timer is not None

    harness.service.dispose()

    assert harness.service._timer is None
    await asyncio.sleep(0.03)
    assert harness.service._evaluation is None


@pytest.mark.asyncio
async def test_completed_goal_keeps_stats_for_the_status_view(state_store):
    harness = make_plugin(state_store, responses=[verdict("met", "done")])
    await set_goal(harness)
    harness.service._pending_tool_calls = 3
    await end_turn(harness)

    goal = await harness.service.snapshot()
    assert goal.stats.tool_calls == 3
    assert goal.finished_at > 0
    assert goal.duration_seconds(now=time.time()) >= 0


class _Stop:
    def __init__(self, stop_reason: str) -> None:
        self.stop_reason = stop_reason


_ = (GoalConfig, GoalSnapshot)
