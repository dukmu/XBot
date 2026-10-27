"""Scope contracts through the same services, event bus, and registry as plugins."""

import asyncio
import gc
import weakref

import pytest

from xcore.api.context import Context
from xcore.api.errors import InactiveEffectError, ServiceNotFoundError
from xcore.api.context import Context as PublicContext
from xcore.api.events import EventKey
from xcore.api.services import ServiceKey

DATABASE: ServiceKey[object] = ServiceKey("database")
SHARED: ServiceKey[object] = ServiceKey("shared")
DONE: EventKey[str] = EventKey("thread.done")


async def test_two_threads_share_parent_services_but_keep_local_bindings_and_events():
    assert PublicContext is Context
    root = Context()
    shared, parent_db = object(), object()
    root.provide(SHARED, shared)
    root.provide(DATABASE, parent_db)
    await root.start()
    session = root.scope("session")
    first = session.scope("first", provides=(DATABASE,))
    second = session.scope("second", provides=(DATABASE,))
    first_db, second_db = object(), object()
    first.provide(DATABASE, first_db)
    second.provide(DATABASE, second_db)
    await session.start()
    assert first.require(SHARED) is second.require(SHARED) is shared
    assert first.require(DATABASE) is first_db
    assert second.require(DATABASE) is second_db
    assert root.require(DATABASE) is parent_db
    missing = session.scope("missing", provides=(DATABASE,))
    await missing.start()
    with pytest.raises(ServiceNotFoundError):
        missing.require(DATABASE)

    observed = []
    root.on(DONE, lambda value: observed.append(("root", value)))
    session.on(DONE, lambda value: observed.append(("session", value)), descendants=True)
    first.on(DONE, lambda value: observed.append(("first", value)))
    second.on(DONE, lambda value: observed.append(("second", value)))
    another = root.scope("another")
    another.on(DONE, lambda value: observed.append(("other", value)), descendants=True)
    await first.emit(DONE, "one")
    await second.emit(DONE, "two")
    assert observed == [("session", "one"), ("first", "one"), ("session", "two"), ("second", "two")]
    await root.close()


async def test_event_declaration_identity_and_dispatch_modes_and_scoped_off():
    root = Context()
    await root.start()
    first, second = root.scope("first"), root.scope("second")
    await first.start()
    await second.start()
    same_name = EventKey("thread.done")
    calls = []
    listener = lambda value: calls.append(value)
    first.on(DONE, listener)
    second.on(DONE, listener)
    assert second.off(DONE, listener)
    assert not second.off(DONE, listener)
    await first.emit(same_name, "wrong identity")
    await second.emit(DONE, "wrong scope")
    await first.emit(DONE, "first")
    assert calls == ["first"]
    first.on("pipe", lambda value: value * 2)
    first.on("pipe", lambda value: value + 1)
    second.on("pipe", lambda value: 0)
    assert await first.chain("pipe", 3) == 7
    first.on("question", lambda: "answer")
    assert await first.serial("question") == "answer"
    with pytest.raises(ValueError, match="runtime root"):
        first.on("internal/status", lambda *args: None)
    await root.close()


async def test_same_named_dependency_refresh_stays_with_actual_binding():
    root = Context()
    await root.start()
    first, second = [root.scope(name, provides=(DATABASE,)) for name in ("first", "second")]
    loads = []

    def consumer(ctx, config):
        loads.append((ctx._scope.name, ctx.require(DATABASE)))

    consumer.inject = (DATABASE,)
    handles = [scope.plugin(consumer) for scope in (first, second)]
    first.provide(DATABASE, "first-db")
    second.provide(DATABASE, "second-db")
    await first.start()
    await second.start()
    await asyncio.gather(*handles)
    assert loads == [("first", "first-db"), ("second", "second-db")]
    first.unset(DATABASE)
    first.provide(DATABASE, "replacement")
    await asyncio.sleep(0)
    await asyncio.gather(*handles)
    assert loads == [("first", "first-db"), ("second", "second-db"), ("first", "replacement")]
    await first.close()
    assert second.require(DATABASE) == "second-db"
    assert handles[1].state.value == "running"
    await root.close()


async def test_failed_activation_rolls_back_partial_resources_and_nested_fibers():
    root = Context()
    await root.start()
    scope = root.scope("thread", provides=(DATABASE,))
    cleaned = []

    def broken(ctx, config):
        ctx.provide(DATABASE, object())
        ctx.on(DONE, lambda value: None)
        ctx.plugin(lambda child, _: child.on("nested", lambda: None))
        ctx.dispose(lambda: cleaned.append("released"))
        raise ValueError("activation failed")

    handle = scope.plugin(broken)
    with pytest.raises(ValueError, match="activation failed"):
        await handle
    assert cleaned == ["released"]
    assert scope.get(DATABASE) is None
    assert root._bus.listener_count(DONE) == 0
    assert root._bus.listener_count("nested") == 0
    await scope.close()
    assert len(root.registry) == 0
    await root.close()


async def test_close_waits_for_pre_stop_then_async_cleanup_with_services_available():
    root = Context()
    await root.start()
    scope = root.scope("thread", provides=(DATABASE,))
    value = object()
    scope.provide(DATABASE, value)
    await scope.start()
    entered, drain = asyncio.Event(), asyncio.Event()
    effects = []

    async def stop():
        assert scope.require(DATABASE) is value
        entered.set()
        await drain.wait()
        effects.append("drained")

    async def cleanup():
        await asyncio.sleep(0)
        effects.append("released")

    scope.pre_stop(stop)
    scope.dispose(cleanup)
    closing = asyncio.create_task(scope.close())
    await entered.wait()
    assert not closing.done()
    assert scope.require(DATABASE) is value
    with pytest.raises(InactiveEffectError):
        scope.effect(lambda: effects.append("late"))
    drain.set()
    await closing
    assert effects == ["drained", "released"]
    await root.close()


async def test_plugin_pre_stop_rejects_registration_through_retained_views():
    root = Context()
    await root.start()
    entered, drain = asyncio.Event(), asyncio.Event()
    views = []

    def plugin(ctx, config):
        views.append(ctx.extend())

        async def stop():
            entered.set()
            await drain.wait()

        ctx.pre_stop(stop)

    handle = root.plugin(plugin)
    await handle
    closing = asyncio.create_task(handle.dispose())
    await entered.wait()
    with pytest.raises(InactiveEffectError):
        views[0].effect(lambda: None)
    drain.set()
    await closing
    await root.close()


async def test_pre_stop_failure_retains_owner_and_can_retry_without_harming_sibling():
    root = Context()
    await root.start()
    broken, healthy = root.scope("broken", provides=(DATABASE,)), root.scope("healthy")
    value = object()
    broken.provide(DATABASE, value)
    await broken.start()
    await healthy.start()
    attempts = []

    async def stop():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("I/O still running")

    broken.pre_stop(stop)
    with pytest.raises(ExceptionGroup, match="resources remain owned"):
        await broken.close()
    assert broken.require(DATABASE) is value
    assert not broken._destroyed
    await healthy.close()
    await broken.close()
    assert attempts == [1, 1]
    assert broken._destroyed
    await root.close()


async def test_repeated_dynamic_registration_and_close_returns_to_resource_baseline():
    root = Context()
    await root.start()
    baseline = (len(root._children), len(root.fiber._disposers), len(root._bus._exact))
    refs = []
    for index in range(20):
        scope = root.scope(f"thread-{index}", provides=(DATABASE,))
        await scope.start()
        scope.provide(DATABASE, object())
        scope.inject((DATABASE,), lambda ctx: ctx.on("dynamic/*", lambda value: None))
        listener = lambda value: None
        scope.on("dynamic", listener)
        scope.off("dynamic", listener)
        scope.once("once", listener)
        await asyncio.gather(scope.parallel("once", None), scope.parallel("once", None))
        await scope.start()
        refs.append(weakref.ref(scope))
        await scope.close()
        assert len(root.registry) == 0
        assert not root.registry._dependents
        assert not root._bus._wildcards
        assert (len(root._children), len(root.fiber._disposers), len(root._bus._exact)) == baseline
    del scope
    await asyncio.sleep(0)
    gc.collect()
    assert all(ref() is None for ref in refs)
    await root.close()


async def test_state_attribute_respects_a_scoped_declaration():
    root = Context()
    shared, private = object(), object()
    root.set("state", shared)
    await root.start()
    scope = root.scope("thread", provides=(ServiceKey("state"),))
    await scope.start()
    with pytest.raises(ServiceNotFoundError):
        _ = scope.state
    scope.set("state", private)
    assert scope.extend().state is private
    assert root.state is shared
    await root.close()


async def test_concurrent_close_drains_owner_once_and_waits_for_released_async_effects():
    root = Context()
    await root.start()
    scope = root.scope("thread")
    await scope.start()
    entered, drain = asyncio.Event(), asyncio.Event()
    calls = []

    async def stop():
        calls.append("stop")
        entered.set()
        await drain.wait()

    async def cleanup():
        await asyncio.sleep(0)
        calls.append("cleanup")

    scope.pre_stop(stop)
    release = scope.dispose(cleanup)
    release()
    one = asyncio.create_task(scope.close())
    await entered.wait()
    two = asyncio.create_task(scope.close())
    drain.set()
    await asyncio.gather(one, two)
    assert calls.count("stop") == 1
    assert calls.count("cleanup") == 1
    assert len(root.registry) == 0
    await root.close()


async def test_ready_waits_for_complete_graph_and_stays_in_its_scope():
    root = Context()
    calls = []
    root.on("ready", lambda: calls.append("root"), global_=True, descendants=True)
    def first(ctx, config):
        ctx.on("ready", lambda: calls.append(ctx.require("later")))
    first.inject = ()
    first.provide = ()
    def later(ctx, config):
        ctx.set("later", "graph complete")
    later.inject = ()
    later.provide = ("later",)
    root.plugin(first)
    root.plugin(later)
    try:
        await root.start()
        assert calls == ["root", "graph complete"]
        child = root.scope("child", provides=("later",))
        child.plugin(first)
        child.plugin(later)
        await child.start()
        assert calls == ["root", "graph complete", "graph complete"]
    finally:
        await root.close()


async def test_ready_dispatch_boundary_expires_in_inherited_background_tasks():
    from xcore.api.plugins import FiberState
    root = Context()
    await root.start()
    scope = root.scope("child")
    release = asyncio.Event()
    tasks = []
    def plugin(ctx, config):
        def fail():
            raise ValueError("ordinary event failure")
        ctx.on("ordinary", fail)
        async def background():
            await release.wait()
            await ctx.emit("ordinary")
        def ready():
            tasks.append(asyncio.create_task(background()))
        ctx.on("ready", ready)
    plugin.inject = ()
    plugin.provide = ()
    handle = scope.plugin(plugin)
    try:
        await scope.start()
        release.set()
        with pytest.raises(ValueError, match="ordinary event failure"):
            await tasks[0]
        assert handle.state is FiberState.RUNNING
        assert not handle.activation_errors
    finally:
        await root.close()


async def test_late_ready_callback_is_cancelled_and_awaited_before_disposal():
    root = Context()
    await root.start()
    entered, stopping, release, finished = (asyncio.Event() for _ in range(4))
    def plugin(ctx, config):
        async def ready():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopping.set()
                await release.wait()
                finished.set()
        ctx.on("ready", ready)
    plugin.inject = ()
    plugin.provide = ()
    await root.plugin(plugin)
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(root.close())
    try:
        await asyncio.wait_for(stopping.wait(), 5)
        assert not closing.done()
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(closing, 5)
        assert finished.is_set()
    finally:
        release.set()
        await root.close()
