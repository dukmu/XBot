"""Behavior tests for explicit same-session goals."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError
from xcore.state import StateService

from XBotv2.agentloop import InboxItem, RuntimeInput
from XBotv2.agentloop.events import OnTurnInput, RejectInput, SessionLifecycle, TurnEnded
from XBotv2.application.app import start_application
from XBotv2.core.domain import TokenCounters, UsageSnapshot
from XBotv2.core.messages import RuntimeNoticeMessage
from XBotv2.core.paths import RuntimePaths
from XBotv2.goal.models import ActiveGoal, GoalState
from XBotv2.goal.plugin import GoalService
from XBotv2.llm.mock import MockLLM


class FakeEngine:
    def __init__(self) -> None:
        self.messages = []
        self.pending_inputs: list[InboxItem] = []

    async def submit_input(self, item: InboxItem, *, wake: bool) -> None:
        assert wake is True
        self.pending_inputs.append(item)

    async def remove_input(self, input_id: str) -> InboxItem:
        item = next((item for item in self.pending_inputs if item.id == input_id), None)
        if item is None:
            raise KeyError(input_id)
        self.pending_inputs.remove(item)
        return item


class FakeEvents:
    async def emit(self, _event, *_args):
        return None


class FakeUsage:
    def __init__(self) -> None:
        self.counters = TokenCounters()

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(total_counters=self.counters)


def _service(tmp_path: Path, *, jobs=None, usage=None) -> tuple[GoalService, FakeEngine]:
    engine = FakeEngine()
    store = StateService(path=tmp_path / "state.json").namespace("goal")
    return GoalService(
        store, engine, events=FakeEvents(), usage=usage or FakeUsage(), jobs=jobs,
        now=lambda: 10.0,
    ), engine


def test_goal_state_is_closed_and_uses_canonical_status_names():
    adapter = TypeAdapter(GoalState)
    goal = adapter.validate_python({
        "kind": "active", "goal_id": "g1", "revision": 1,
        "objective": "tests pass", "started_at": 1.0,
    })
    assert isinstance(goal, ActiveGoal)
    with pytest.raises(ValidationError):
        adapter.validate_python({
            "kind": "achieved", "goal_id": "g1", "revision": 1,
            "objective": "tests pass", "started_at": 1.0,
        })


@pytest.mark.asyncio
async def test_command_goal_reserves_one_same_session_continuation(tmp_path: Path):
    service, engine = _service(tmp_path)
    await service.command("tests pass")
    snapshot = await service.snapshot()
    assert snapshot.activation == "armed"
    assert isinstance(snapshot.state, ActiveGoal)
    assert snapshot.state.pending_input_id == engine.pending_inputs[0].id
    assert len(engine.pending_inputs) == 1
    queued = engine.pending_inputs[0]
    assert isinstance(queued.input, RuntimeInput)
    assert (queued.input.source, queued.input.event) == ("goal", "continue")
    assert "update_goal" in queued.input.content


@pytest.mark.asyncio
async def test_admission_counts_round_and_turn_end_queues_only_one_successor(tmp_path: Path):
    service, engine = _service(tmp_path)
    await service.command("tests pass")
    first = engine.pending_inputs.pop()
    assert await service.on_turn_input(OnTurnInput(first, ())) is None
    assert (await service.snapshot()).state.stats.rounds_started == 1
    event = TurnEnded(history=(), stop_reason="completed")
    await service.on_turn_end(event)
    await service.on_turn_end(event)
    assert len(engine.pending_inputs) == 1


@pytest.mark.asyncio
async def test_control_update_is_cas_and_removes_queued_continuation(tmp_path: Path):
    service, engine = _service(tmp_path)
    await service.command("tests pass")
    current = (await service.snapshot()).state
    assert isinstance(current, ActiveGoal)
    stale = await service.update_goal(current.goal_id, 99, "complete", "verified")
    assert stale.kind == "failed"
    assert len(engine.pending_inputs) == 1
    result = await service.update_goal(
        current.goal_id, current.revision, "complete", "pytest passed"
    )
    assert result.kind == "succeeded"
    snapshot = await service.snapshot()
    assert snapshot.state.kind == "complete"
    assert snapshot.state.revision == 2
    assert snapshot.state.reason == "pytest passed"
    assert engine.pending_inputs == []


@pytest.mark.asyncio
async def test_claimed_continuation_is_rejected_after_goal_replacement(tmp_path: Path):
    service, engine = _service(tmp_path)
    await service.command("old objective")
    claimed = engine.pending_inputs.pop()
    await service.command("new objective")
    rejection = await service.on_turn_input(OnTurnInput(claimed, ()))
    assert isinstance(rejection, RejectInput)
    assert (await service.snapshot()).state.objective == "new objective"


@pytest.mark.asyncio
async def test_resume_lifecycle_disarms_without_resetting_statistics(tmp_path: Path):
    service, engine = _service(tmp_path)
    await service.command("tests pass")
    queued = engine.pending_inputs.pop()
    await service.on_turn_input(OnTurnInput(queued, ()))
    before = await service.snapshot()
    await service.on_session_resume(SessionLifecycle())
    after = await service.snapshot()
    assert after.activation == "disarmed"
    assert after.state.goal_id == before.state.goal_id
    assert after.state.revision == before.state.revision
    assert after.state.started_at == before.state.started_at
    assert after.state.stats == before.state.stats
    assert engine.pending_inputs == []


@pytest.mark.asyncio
async def test_background_completion_wakes_deferred_goal_once(tmp_path: Path):
    jobs = SimpleNamespace(views=lambda: [SimpleNamespace(state="running")])
    service, engine = _service(tmp_path, jobs=jobs)
    await service.create_goal("background result verified")
    assert (await service.snapshot()).state.stats.rounds_started == 1
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))
    assert engine.pending_inputs == []

    jobs.views = lambda: [SimpleNamespace(state="succeeded")]
    await service.on_job_completed(SimpleNamespace())
    await service.on_job_completed(SimpleNamespace())
    assert len(engine.pending_inputs) == 1


@pytest.mark.asyncio
async def test_disarmed_goal_does_not_charge_unrelated_turn_usage(tmp_path: Path):
    usage = FakeUsage()
    service, engine = _service(tmp_path, usage=usage)
    await service.command("tests pass")
    queued = engine.pending_inputs.pop()
    await service.on_turn_input(OnTurnInput(queued, ()))
    usage.counters = TokenCounters(input=10, output=2)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="client_interrupt"))
    frozen = (await service.snapshot()).state.stats

    usage.counters = TokenCounters(input=100, output=50)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))
    assert (await service.snapshot()).state.stats == frozen


@pytest.mark.asyncio
async def test_two_goal_rounds_advance_usage_baseline_without_double_counting(tmp_path: Path):
    usage = FakeUsage()
    service, engine = _service(tmp_path, usage=usage)
    await service.command("tests pass")
    await service.on_turn_input(OnTurnInput(engine.pending_inputs.pop(), ()))
    usage.counters = TokenCounters(input=10)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))
    await service.on_turn_input(OnTurnInput(engine.pending_inputs.pop(), ()))
    usage.counters = TokenCounters(input=25)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))
    assert (await service.snapshot()).state.stats.input_tokens == 25


@pytest.mark.asyncio
async def test_replaced_goal_does_not_inherit_old_turn_tools_or_usage(tmp_path: Path):
    usage = FakeUsage()
    service, engine = _service(tmp_path, usage=usage)
    await service.command("old")
    await service.on_turn_input(OnTurnInput(engine.pending_inputs.pop(), ()))
    await service.command("new")
    await service.on_tool_call(None)  # event payload is irrelevant to accounting
    usage.counters = TokenCounters(input=20)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))
    stats = (await service.snapshot()).state.stats
    assert stats.tool_calls == 0
    assert stats.input_tokens == 0


@pytest.mark.asyncio
async def test_terminal_update_tool_is_counted_at_standard_after_tool_boundary(tmp_path: Path):
    usage = FakeUsage()
    service, engine = _service(tmp_path, usage=usage)
    await service.command("tests pass")
    await service.on_turn_input(OnTurnInput(engine.pending_inputs.pop(), ()))
    await service.on_tool_call(None)
    current = (await service.snapshot()).state
    await service.update_goal(current.goal_id, current.revision, "complete", "verified")
    await service.on_tool_call(None)
    usage.counters = TokenCounters(input=12, output=3)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))
    stats = (await service.snapshot()).state.stats
    assert stats.tool_calls == 2
    assert stats.total_tokens == 15


@pytest.mark.asyncio
async def test_pause_command_and_dispose_clear_continuation_reservations(tmp_path: Path):
    service, engine = _service(tmp_path)
    await service.command("tests pass")
    paused = await service.command("pause")
    assert paused.status == "ok"
    assert (await service.snapshot()).state.kind == "paused"
    assert engine.pending_inputs == []

    await service.command("another goal")
    await service.dispose()
    snapshot = await service.snapshot()
    assert snapshot.activation == "disarmed"
    assert snapshot.state.pending_input_id is None
    assert engine.pending_inputs == []


@pytest.mark.asyncio
async def test_pause_resume_accumulates_only_goal_owned_turns(tmp_path: Path):
    usage = FakeUsage()
    service, engine = _service(tmp_path, usage=usage)
    await service.command("ship")
    await service.on_turn_input(OnTurnInput(engine.pending_inputs.pop(), ()))
    usage.counters = TokenCounters(input=10)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))

    await service.command("pause")
    usage.counters = TokenCounters(input=100)
    await service.command("resume")
    await service.on_turn_input(OnTurnInput(engine.pending_inputs.pop(), ()))
    usage.counters = TokenCounters(input=115)
    await service.on_turn_end(TurnEnded(history=(), stop_reason="completed"))
    assert (await service.snapshot()).state.stats.input_tokens == 25


@pytest.mark.asyncio
async def test_hot_unload_removes_reserved_goal_input(tmp_path: Path):
    app = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="goal-unload",
        thread_id="agent",
        workspace_root=tmp_path,
        llm_override=MockLLM(responses=[{"content": "must not run"}]),
    )
    try:
        await app.goal.command("ship")
        assert app.engine.pending_input_count == 1

        handle = next(
            handle for handle in app.registry.handles() if handle.name == "goal"
        )
        await handle.dispose()
        for _ in range(100):
            if app.get("goal", strict=False) is None:
                break
            await asyncio.sleep(0.01)

        assert app.get("goal", strict=False) is None
        assert [event async for event in app.engine.run_pending()] == []
        assert not any(
            isinstance(message, RuntimeNoticeMessage) and message.source == "goal"
            for message in app.engine.messages
        )
    finally:
        await app.destroy()
