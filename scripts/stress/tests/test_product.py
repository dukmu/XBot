"""Focused checks for the product workload's real provider and evidence."""
from collections import Counter

import pytest

from scripts.stress.product import consume_until, process_details, sample
from scripts.stress.server import LiveMockServerHarness


@pytest.mark.asyncio
async def test_real_provider_tool_steps_and_compaction(tmp_path):
    async with LiveMockServerHarness(
        30, data_dir=tmp_path / "data", mock_tools=True, tool_steps=2,
        compact_recent=1, capture_requests=False, chunk_delay_ms=0,
    ) as harness:
        from XBotv2.persistence.api.contracts import PERSISTENCE
        from XBotv2.session.api.replay import REPLAY_POOL
        client = harness.client
        backend = harness.application.require(PERSISTENCE)
        replay = harness.application.require(REPLAY_POOL)
        assert backend.record_cache_stats["budget_bytes"] > 0
        assert replay.stats["budget_bytes"] > 0
        opened = await client.open_session(session_id="s", thread_id="main", mode="new")
        cursor = opened.data.event_cursor
        events = Counter()
        tool_latencies = []
        for turn in range(2):
            await client.send_message("s", "main", f"turn {turn}", request_id=f"r{turn}")
            cursor = await consume_until(client, "s", cursor, "turn_ended", events, tool_latencies)
        result = await client.run_command("s", "main", raw="/compact")
        assert result.data.status == "ok" and "history" in result.data.effects
        await consume_until(client, "s", cursor, "compaction_completed", events)
        assert harness.provider.agent_requests == 6
        assert harness.provider.auxiliary_requests == 1
        assert harness.provider.tool_responses == events["tool_succeeded"] == 4
        assert events["compaction_completed"] == 1
        assert replay.stats["frames"] > 0 and replay.stats["resident_bytes"] > 0 and replay.stats["windows"] > 0
        assert len(tool_latencies) == 4 and all(value >= 0 for value in tool_latencies)
        assert not harness.provider.requests
        measured = sample("test", tmp_path / "data")
        assert measured["disk"]["jsonl"] > 0
        assert measured["process"]["open_fds"] is not None
        assert measured["active_contexts"] > 0
        assert process_details()["rss_bytes"] is not None


@pytest.mark.asyncio
async def test_product_todo_team_and_range_artifact(tmp_path):
    import asyncio
    import httpx
    async with LiveMockServerHarness(
        60, data_dir=tmp_path / "data", mock_tools=True, tool_steps=7,
        compact_recent=1, capture_requests=False, chunk_delay_ms=0,
        product_workload=True,
    ) as harness:
        client = harness.client
        opened = await client.open_session(session_id="s", thread_id="main", mode="new")
        cursor = opened.data.event_cursor
        async with httpx.AsyncClient(base_url=harness.base_url) as observer:
            member = await observer.put("/sessions/s/threads/main/teams/stress/members/main")
            assert member.status_code == 200 and member.json()["changed"]
        events = Counter()
        await client.send_message("s", "main", "Product workload turn 1", request_id="first")
        cursor = await asyncio.wait_for(consume_until(client, "s", cursor, "turn_ended", events), 30)
        async with httpx.AsyncClient(base_url=harness.base_url) as observer:
            first = await observer.get("/sessions/s/threads/main/todos")
            first.raise_for_status()
            tasks = first.json()["tasks"]
            assert len(tasks) == 5
            target = next(task["id"] for task in tasks if task["subject"] == "Workload task 2")
            for number in (2, 3, 4):
                await client.send_message(
                    "s", "main", f"Product workload turn {number}", request_id=f"r{number}",
                )
                cursor = await asyncio.wait_for(
                    consume_until(client, "s", cursor, "turn_ended", events), 30,
                )
            await client.send_message(
                "s", "main", f"Product workload turn 8 complete_task={target} read_board=1",
                request_id="second",
            )
            await asyncio.wait_for(consume_until(client, "s", cursor, "turn_ended", events), 30)
            result = await observer.get("/sessions/s/threads/main/todos")
            assert result.json()["completed_count"] == 1
            assert len(result.json()["tasks"]) == 4
            board = await observer.get("/sessions/s/threads/main/teams/stress/count")
            assert board.json()["count"] == 5
        assert harness.provider.agent_requests == 40
        assert harness.provider.product_notice_requests > 0
        assert events["tool.task_create"] == 5
        assert events["tool.task_update"] == 1
        assert events["tool.team_publish"] == 5
        assert events["tool.team_latest"] == 1
        assert events["team_range_artifacts"] == 6


@pytest.mark.asyncio
async def test_slow_socket_connects_before_first_turn(tmp_path):
    import asyncio
    import httpx
    async with LiveMockServerHarness(
        30, data_dir=tmp_path / "data", mock_tools=True, tool_steps=1,
        capture_requests=False, chunk_delay_ms=0,
    ) as harness:
        client = harness.client
        opened = await client.open_session(session_id="s", thread_id="main", mode="new")
        async with httpx.AsyncClient(base_url=harness.base_url, timeout=None) as raw:
            async with raw.stream(
                "GET", "/sessions/s/threads/main/events",
                params={"after": opened.data.event_cursor},
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                await client.send_message("s", "main", "socket probe", request_id="one")
                async with asyncio.timeout(15):
                    async for line in lines:
                        if line.startswith("data:"):
                            break
                events = Counter()
                await asyncio.wait_for(
                    consume_until(client, "s", opened.data.event_cursor, "turn_ended", events), 15,
                )
                assert events["turn_ended"] == 1


@pytest.mark.asyncio
async def test_matching_execution_end_before_compaction_with_todo_reminder(tmp_path):
    import asyncio
    import httpx

    from scripts.stress.measure import WorkloadMeasurements
    with WorkloadMeasurements(single_session=True) as measurements:
        async with LiveMockServerHarness(
            30, data_dir=tmp_path / "data", mock_tools=True, tool_steps=1,
            compact_recent=1, capture_requests=False, chunk_delay_ms=0,
            product_workload=True,
        ) as harness:
            client = harness.client
            opened = await client.open_session(session_id="s", thread_id="main", mode="new")
            cursor = opened.data.event_cursor
            events = Counter()
            async with httpx.AsyncClient(base_url=harness.base_url) as observer:
                member = await observer.put("/sessions/s/threads/main/teams/stress/members/main")
                member.raise_for_status()
                for number in range(1, 5):
                    input_id = f"input-{number}"
                    await client.send_message(
                        "s", "main", f"Product workload turn {number}", request_id=input_id,
                    )
                    cursor = await asyncio.wait_for(
                        consume_until(
                            client, "s", cursor, "execution_ended", events, input_id=input_id,
                        ), 30,
                    )
                result = await client.run_command("s", "main", raw="/compact")
                assert result.data.status == "ok" and "history" in result.data.effects
                await asyncio.wait_for(
                    consume_until(client, "s", cursor, "compaction_completed", events), 30,
                )
            assert events["turn_ended"] == events["execution_ended"] == 4
            assert events["tool_succeeded"] == 4
            assert events["compaction_completed"] >= 1
            assert harness.provider.product_notice_requests > 0
            assert harness.provider.tool_responses == 4
        report = measurements.report()
        assert report["counts"]["model_json_encodes"] >= 8
        assert report["model_json_cpu_ns"] > 0
