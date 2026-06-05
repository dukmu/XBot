"""Tests for the tool dispatch timeout (added 2026-06-05).

The user reported that two simple shell commands took 10+ seconds
and the TUI was frozen — the asyncio event loop was blocked by a
synchronous ``tool.invoke(args)`` call inside ``execute_tools``.

These tests pin the fix: sync tools are dispatched in a worker
thread and a hard wall-clock cap (``_TOOL_DISPATCH_TIMEOUT_SECONDS``)
is enforced so the event loop can never be starved by a runaway
tool.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from xbotv2.tools.runtime import (
    _TOOL_DISPATCH_TIMEOUT_SECONDS,
    _invoke_with_timeout,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def test_dispatch_timeout_is_bounded() -> None:
    """The hard cap is short enough to keep the event loop responsive.

    60s matches the shell tool's own subprocess.run(timeout=30)
    and is generous for any reasonable tool. If this drifts much
    higher, the next regression test will silently take longer too.
    """

    assert _TOOL_DISPATCH_TIMEOUT_SECONDS <= 60.0


# ----------------------------------------------------------------------
# Behaviour: sync work runs in a thread, event loop stays alive
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_with_timeout_runs_sync_call_in_thread() -> None:
    """A blocking sync tool does NOT stall the event loop.

    Without ``asyncio.to_thread`` the call below would block the
    event loop for ~0.5s, which is what we are guarding against.
    """

    block_for = 0.5

    def slow_sync() -> str:
        time.sleep(block_for)
        return "done"

    started = time.monotonic()
    result = await _invoke_with_timeout(slow_sync, (), tool_name="slow_sync")
    elapsed = time.monotonic() - started

    assert result == "done"
    # The call must have taken roughly the sleep time (allow 2x
    # for scheduler overhead, but it must NOT be a 60s timeout).
    assert elapsed < 2.0, f"slow sync took {elapsed:.2f}s; should be <2s"
    # And it must have actually run (i.e. the worker thread did
    # block, not the event loop).
    assert elapsed >= block_for * 0.9, (
        f"finished in {elapsed:.2f}s — too fast, sync ran on the event loop?"
    )


@pytest.mark.asyncio
async def test_event_loop_alive_during_sync_tool() -> None:
    """While a sync tool blocks, another coroutine on the same loop
    can still make progress. This is the regression we are pinning.
    """

    block_for = 0.4
    heartbeats: list[float] = []

    async def heartbeat() -> None:
        # Four quick ticks spaced 0.1s apart. Total expected ≈ 0.4s.
        for _ in range(4):
            heartbeats.append(time.monotonic())
            await asyncio.sleep(0.1)

    def slow_sync() -> str:
        time.sleep(block_for)
        return "ok"

    started = time.monotonic()
    await asyncio.gather(
        _invoke_with_timeout(slow_sync, (), tool_name="slow_sync"),
        heartbeat(),
    )
    elapsed = time.monotonic() - started

    # The event loop must have ticked at least 3 times during the
    # 0.4s sync block. If the sync tool ran on the event loop the
    # heartbeats would all be ≈ block_for.
    intervals = [b - a for a, b in zip(heartbeats, heartbeats[1:])]
    assert max(intervals) < block_for * 0.9, (
        f"heartbeats stalled: {intervals!r} (elapsed {elapsed:.2f}s)"
    )


# ----------------------------------------------------------------------
# Timeout enforcement
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_with_timeout_caps_a_hanging_tool() -> None:
    """A tool that never returns gets capped, not awaited forever.

    We monkey-patch the timeout constant to a tiny value so the
    test runs fast, then prove the wrapper returns an error string
    within the cap.
    """

    import xbotv2.tools.runtime as runtime

    original = runtime._TOOL_DISPATCH_TIMEOUT_SECONDS
    runtime._TOOL_DISPATCH_TIMEOUT_SECONDS = 0.2
    try:
        def hang() -> str:
            time.sleep(5)
            return "never returned in time"

        started = time.monotonic()
        result = await _invoke_with_timeout(hang, (), tool_name="hang")
        elapsed = time.monotonic() - started

        assert "exceeded" in str(result).lower(), (
            f"expected timeout error string, got: {result!r}"
        )
        # Must be capped well under the 5s the tool wanted to sleep.
        assert elapsed < 2.0, f"timeout took {elapsed:.2f}s; cap is 0.2s"
    finally:
        runtime._TOOL_DISPATCH_TIMEOUT_SECONDS = original


@pytest.mark.asyncio
async def test_invoke_with_timeout_preserves_async_tool_exception() -> None:
    """An async tool that raises propagates the exception through the
    wrapper rather than swallowing it.
    """

    async def boom() -> str:
        raise RuntimeError("tool exploded")

    with pytest.raises(RuntimeError, match="tool exploded"):
        await _invoke_with_timeout(boom, (), tool_name="boom")
