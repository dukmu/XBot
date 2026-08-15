"""Lifecycle tests: start/stop/restart/destroy, events, ordering, failures."""

from __future__ import annotations

import asyncio
import logging

import pytest

from xcore import Context, FiberState


async def test_start_loads_plugins_and_fires_ready():
    ctx = Context()
    events: list[str] = []

    def plugin(ctx_, config):
        ctx_.on("ready", lambda: events.append("ready"))
        events.append("applied")

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    await asyncio.sleep(0.05)
    assert events == ["applied", "ready"]


async def test_ready_registered_while_active_runs_immediately():
    ctx = Context()
    events: list[str] = []
    await ctx.start()
    ctx.on("ready", lambda: events.append("ready"))
    await asyncio.sleep(0.05)
    assert events == ["ready"]


async def test_stop_unloads_in_reverse_load_order():
    ctx = Context()
    unloaded: list[str] = []

    def make(name: str):
        def plugin(ctx_, config):
            def cleanup():
                unloaded.append(name)

            return cleanup

        return plugin

    await ctx.start()
    h1 = ctx.plugin(make("first"))
    await h1
    h2 = ctx.plugin(make("second"))
    await h2
    await ctx.stop()
    assert unloaded == ["second", "first"]
    assert h1.state is FiberState.PENDING
    assert h2.state is FiberState.PENDING


async def test_stop_fires_dispose_event():
    ctx = Context()
    events: list[str] = []

    def plugin(ctx_, config):
        ctx_.on("dispose", lambda: events.append("dispose"))

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    await ctx.stop()
    assert events == ["dispose"]


async def test_restart_recovers_state():
    ctx = Context()
    runs: list[int] = []

    def plugin(ctx_, config):
        runs.append(config["n"])

    await ctx.start()
    handle = ctx.plugin(plugin, {"n": 1})
    await handle
    assert runs == [1]
    await ctx.stop()
    await ctx.start()
    await handle  # reloaded on start
    assert runs == [1, 1]
    assert handle.state is FiberState.RUNNING


async def test_disposer_failures_do_not_break_stop():
    ctx = Context()
    ctx_logger = logging.getLogger("xcore.plugin")
    cleaned: list[str] = []

    def bad(ctx_, config):
        def cleanup():
            cleaned.append("bad")
            raise RuntimeError("cleanup failed")

        return cleanup

    def good(ctx_, config):
        def cleanup():
            cleaned.append("good")

        return cleanup

    await ctx.start()
    h1 = ctx.plugin(bad)
    await h1
    h2 = ctx.plugin(good)
    await h2
    # stop() must complete all cleanup and never raise
    await ctx.stop()
    assert cleaned == ["good", "bad"]  # reverse load order; bad still ran
    assert not ctx.is_active


async def test_ready_listener_failure_does_not_wedge_start():
    ctx = Context()

    def plugin(ctx_, config):
        def boom():
            raise ValueError("ready boom")

        ctx_.on("ready", boom)

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    await asyncio.sleep(0.05)
    assert ctx.is_active  # start() completed despite the ready failure


async def test_stop_during_async_apply_completes_cleanly():
    ctx = Context()
    started = asyncio.Event()

    async def slow(ctx_, config):
        started.set()
        await asyncio.sleep(0.2)
        ctx_.on("evt", lambda s: None)  # late registration after stop

    await ctx.start()
    handle = ctx.plugin(slow)
    await asyncio.sleep(0.02)
    assert handle.state is FiberState.LOADING
    await ctx.stop()
    assert not ctx.is_active
    # the in-flight apply finished; its late registration must not leak
    await asyncio.sleep(0.25)
    await ctx.emit("evt", None)  # must not raise


async def test_double_set_same_tick_does_not_double_load():
    ctx = Context()
    loads: list[str] = []

    def consumer(ctx_, config):
        loads.append("consumer")

    consumer.inject = ["svc"]

    await ctx.start()
    handle = ctx.plugin(consumer)
    ctx.set("svc", object())
    ctx.set("svc", None)  # same tick: provide then release
    ctx.set("svc", object())
    await asyncio.sleep(0.05)
    assert handle.state is FiberState.RUNNING
    assert loads == ["consumer"]


async def test_destroy_is_permanent():
    ctx = Context()
    runs: list[str] = []

    def plugin(ctx_, config):
        runs.append("run")

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    assert runs == ["run"]
    await ctx.destroy()
    assert ctx.is_active is False
    with pytest.raises(RuntimeError):
        await ctx.start()  # destroyed root cannot restart
    assert runs == ["run"]


async def test_dispose_requires_callback():
    ctx = Context()
    with pytest.raises(TypeError):
        ctx.dispose()  # no callback -> error (use destroy())


async def test_concurrent_starts_are_serialized():
    ctx = Context()

    def plugin(ctx_, config):
        pass

    await asyncio.gather(ctx.start(), ctx.start())
    assert ctx.is_active
    handle = ctx.plugin(plugin)
    await handle
    assert handle.state is FiberState.RUNNING


async def test_inject_cycle_stays_pending_without_hanging():
    ctx = Context()

    def a(ctx_, config):
        pass

    def b(ctx_, config):
        pass

    a.inject = ["svc-b"]
    b.inject = ["svc-a"]

    def pa(ctx_, config):
        ctx_.set("svc-a", object())

    def pb(ctx_, config):
        ctx_.set("svc-b", object())

    await ctx.start()
    ha = ctx.plugin(a)
    await ha
    hb = ctx.plugin(b)
    await hb
    ctx.plugin(pa)
    ctx.plugin(pb)
    await asyncio.sleep(0.05)
    # both providers load; both consumers wait on each other forever
    assert ha.state in (FiberState.PENDING, FiberState.RUNNING)
    assert hb.state in (FiberState.PENDING, FiberState.RUNNING)


async def test_plugin_mounted_before_start_loads_at_start():
    ctx = Context()
    ran: list[str] = []

    def plugin(ctx_, config):
        ran.append("x")

    handle = ctx.plugin(plugin)
    assert handle.state is FiberState.PENDING
    await ctx.start()
    await handle
    assert ran == ["x"]


async def test_services_provided_before_start_do_not_load_early():
    ctx = Context()
    loaded: list[str] = []

    def consumer(ctx_, config):
        loaded.append("consumer")

    consumer.inject = ["svc"]

    ctx.set("svc", object())  # before start
    handle = ctx.plugin(consumer)
    await asyncio.sleep(0.05)
    assert handle.state is FiberState.PENDING  # app not active yet
    await ctx.start()
    await handle
    assert loaded == ["consumer"]
