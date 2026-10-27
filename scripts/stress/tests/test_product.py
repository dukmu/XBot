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
        client = harness.client
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
        assert len(tool_latencies) == 4 and all(value >= 0 for value in tool_latencies)
        assert not harness.provider.requests
        measured = sample("test", tmp_path / "data")
        assert measured["disk"]["jsonl"] > 0
        assert measured["process"]["open_fds"] is not None
        assert measured["active_contexts"] > 0
        assert process_details()["rss_bytes"] is not None
