"""Micro-benchmark for the HTTP/SSE transport.

Measures the round-trip latency for sending a user message and
receiving the ``turn_finished`` event. The script spins up a FastAPI
app in-process via ``httpx.ASGITransport`` (no real socket) and a
mock LLM, so it is safe to run on CI without external dependencies.

Run with::

    uv run pytest XBotv2/tests/bench/test_http_latency.py -v -s

The reported numbers are wall-clock seconds across 50 turns. They are
informational; the goal is to detect regressions in the transport
layer, not to assert absolute thresholds.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from xbotv2.llm.mock import MockLLM
from xbotv2.protocol.http_server import create_app, set_llm_override


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def http_app(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "providers.yaml").write_text(
        "default:\n  provider: openai\n  model: test\n  base_url: http://test\n  api_key: test\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "user.yaml").write_text(
        "user_id: bench\nuser_name: Bench\nplatform: tui\nsession_type: interactive\n",
        encoding="utf-8",
    )
    (data_dir / "config" / "system.yaml").write_text(
        "agent_name: BenchBot\nagent_role: bench\nprovider: default\n"
        "max_context_tokens: 4096\ntools: []\nplugins: {}\nhooks: []\n"
        "sandbox:\n  enabled: false\n  resources: []\n",
        encoding="utf-8",
    )
    app = create_app(
        provider_name="default",
        data_dir=str(data_dir),
        no_plugins=True,
    )
    set_llm_override(
        app, MockLLM(responses=[{"content": "bench reply"}] * 200)
    )
    yield app


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
async def test_http_turn_latency_distribution(client: httpx.AsyncClient) -> None:
    """Run 50 turns and report end-to-end latency statistics.

    The test is *informational* — it always passes, but prints a
    summary so the bench report can quote realistic numbers. The
    reported metric is wall-clock from "first byte of request" to
    "turn_finished event observed".
    """

    open_resp = await client.post(
        "/sessions", json={"session_id": "bench", "thread_id": "t"}
    )
    assert open_resp.status_code == 200

    latencies_ms: list[float] = []
    event_type_counter: Counter[str] = Counter()

    for turn_idx in range(50):
        started = time.perf_counter()
        async with client.stream(
            "POST",
            "/sessions/bench/messages",
            json={"content": f"turn-{turn_idx}", "request_id": f"r-{turn_idx}"},
        ) as response:
            assert response.status_code == 200
            body = "".join([chunk async for chunk in response.aiter_text()])

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        latencies_ms.append(elapsed_ms)

        # Parse and count event types
        for raw_frame in body.split("\n\n"):
            if not raw_frame.strip():
                continue
            for line in raw_frame.splitlines():
                if line.startswith("data:"):
                    text = line.split(":", 1)[1].strip()
                    if text:
                        try:
                            event = json.loads(text)
                            event_type_counter[event.get("type", "?")] += 1
                        except json.JSONDecodeError:
                            pass

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
    print("\n[bench] HTTP turn latency summary:")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    # The bench should produce the canonical 4 events per turn.
    expected_per_turn = {
        "turn_started": 1,
        "assistant_message": 1,
        "turn_finished": 1,
        "end": 1,
    }
    for event_type, per_turn in expected_per_turn.items():
        assert event_type_counter[event_type] == 50 * per_turn, (
            f"event {event_type} count mismatch: "
            f"{event_type_counter[event_type]} vs expected {50 * per_turn}"
        )
