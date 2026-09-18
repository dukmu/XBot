"""Micro-benchmark for in-process HTTP submission and session events.

Measures the latency from submitting a user message over in-process HTTP to
receiving its terminal ``turn_finished`` event from the authoritative session
event stream. The script uses ``httpx.ASGITransport`` (no real socket) and a
mock LLM, so it is safe to run on CI without external dependencies. It does
not model a long-lived HTTP SSE response.

Run with::

    uv run pytest XBotv2/tests/bench/test_http_latency.py -v -s

The reported numbers are wall-clock seconds across 50 turns. They are
informational; the goal is to detect regressions in the transport
layer, not to assert absolute thresholds.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter
from functools import partial
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import yaml
from XBotv2.core.paths import RuntimePaths
from httpx import ASGITransport

from XBotv2.llm.mock import MockLLM
from XBotv2.application.app import create_agent_application
from XBotv2.application.server import start_server_application


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def http_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "default",
                    "providers": {
                        "default": {
                            "protocol": "openai",
                            "base_url": "http://test",
                            "api_key": "test",
                            "default_model": "test",
                            "models": [
                                {
                                    "model": "test",
                                    "max_context_tokens": 4096,
                                },
                            ],
                        },
                    },
                },
            },
            {
                "id": "config",
                "name": "config",
                "config": {
                    "user": {
                        "user_id": "bench",
                        "user_name": "Bench",
                        "platform": "tui",
                        "session_type": "interactive",
                    },
                },
            },
        ], sort_keys=False),
        encoding="utf-8",
    )
    (data_dir / "config" / "config.yaml").write_text(
        "provider: default\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )
    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(tmp_path),
        no_plugins=True,
    )
    app = server.server
    app.state.manager = server.sessions
    server.sessions.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(responses=[{"content": "bench reply"}] * 200),
    )
    try:
        yield app
    finally:
        await server.stop()


@pytest_asyncio.fixture
async def client(http_app) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=http_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://bench"
    ) as ac:
        yield ac


# ----------------------------------------------------------------------
# Bench
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_turn_latency_distribution(
    client: httpx.AsyncClient,
    http_app,
) -> None:
    """Run 50 turns and report end-to-end latency statistics.

    The test is *informational* — it always passes, but prints a summary so
    the bench report can quote realistic numbers. The reported metric is
    wall-clock from the HTTP submit to the authoritative session event.
    """

    open_resp = await client.post(
        "/sessions", json={"session_id": "bench", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    latencies_ms: list[float] = []
    event_type_counter: Counter[str] = Counter()

    for turn_idx in range(50):
        runtime = await http_app.state.manager.get("bench", "t")
        events = runtime.event_stream.subscribe()
        started = time.perf_counter()
        try:
            response = await client.post(
                "/sessions/bench/threads/t/messages",
                json={"content": f"turn-{turn_idx}", "request_id": f"r-{turn_idx}"},
            )
            assert response.status_code == 202
            async for frame in events:
                event_type_counter[frame.event.type] += 1
                if frame.event.type == "turn_finished":
                    break
            latencies_ms.append((time.perf_counter() - started) * 1000.0)
        finally:
            await events.aclose()

    summary = {
        "count": len(latencies_ms),
        "mean_ms": statistics.mean(latencies_ms),
        "median_ms": statistics.median(latencies_ms),
        "p95_ms": (
            statistics.quantiles(latencies_ms, n=20)[-1]
            if len(latencies_ms) >= 20
            else max(latencies_ms)
        ),
        "min_ms": min(latencies_ms),
        "max_ms": max(latencies_ms),
        "event_counts": dict(event_type_counter),
    }

    # Print in a way that shows up under `pytest -s`.
    print("\n[bench] HTTP submit + session-event delivery latency:")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    # The bench should produce the canonical 4 events per turn.
    expected_per_turn = {
        "message": 1,
        "turn_started": 1,
        "assistant_message": 1,
        "turn_finished": 1,
    }
    for event_type, per_turn in expected_per_turn.items():
        assert event_type_counter[event_type] == 50 * per_turn, (
            f"event {event_type} count mismatch: "
            f"{event_type_counter[event_type]} vs expected {50 * per_turn}"
        )
