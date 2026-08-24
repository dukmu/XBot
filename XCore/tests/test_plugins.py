"""Plugin system tests: shapes, registry, fiber states, failure isolation."""

from __future__ import annotations

import asyncio

import pytest

from xcore import Context, FiberState, S, SchemaValidationError


# ---------------------------------------------------------------------------
# Plugin shapes
# ---------------------------------------------------------------------------


async def test_function_plugin():
    ctx = Context()
    ran: list[str] = []

    def my_plugin(ctx_, config):
        ran.append(config["label"])

    await ctx.start()
    handle = ctx.plugin(my_plugin, {"label": "fn"})
    await handle
    assert ran == ["fn"]
    assert handle.state is FiberState.RUNNING
    assert handle.name == "my_plugin"
    assert handle.config == {"label": "fn"}


async def test_object_plugin():
    ctx = Context()
    ran: list[str] = []

    class MyObject:
        name = "object-plugin"
        inject = ["svc-a"]

        def apply(self, ctx_, config):
            ran.append("object")

    await ctx.start()
    ctx.set("svc-a", object())
    handle = ctx.plugin(MyObject())
    await handle
    assert ran == ["object"]
    assert handle.name == "object-plugin"


async def test_class_plugin():
    ctx = Context()
    events: list[str] = []

    class MyClass:
        def __init__(self, ctx_, config):
            self.ctx = ctx_
            self.config = config
            events.append("constructed")

    await ctx.start()
    handle = ctx.plugin(MyClass, {"x": 1})
    await handle
    assert events == ["constructed"]
    assert handle.config == {"x": 1}


async def test_invalid_plugin_shape_raises_type_error():
    ctx = Context()
    with pytest.raises(TypeError, match="invalid plugin"):
        ctx.plugin(42)


async def test_apply_return_value_is_treated_as_disposer():
    ctx = Context()
    cleaned: list[str] = []

    def plugin(ctx_, config):
        ctx_.on("evt", lambda s: None)

        async def cleanup():
            cleaned.append("cleaned")

        return cleanup

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    await handle.dispose()
    await asyncio.sleep(0.05)
    assert cleaned == ["cleaned"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


async def test_registry_dedup_by_callback_and_inspection():
    ctx = Context()
    await ctx.start()

    def plugin(ctx_, config):
        pass

    h1 = ctx.plugin(plugin)
    await h1
    h2 = ctx.plugin(plugin)  # same callback -> same runtime, new fiber
    await h2
    runtime = ctx.registry.get(plugin)
    assert runtime is not None
    assert len(runtime.fibers) == 2
    assert ctx.registry.has(plugin)
    assert len(ctx.registry) == 1
    names = [runtime.definition.name for runtime in ctx.registry.values()]
    assert names == ["plugin"]


async def test_registry_delete_disposes_all_fibers():
    ctx = Context()
    await ctx.start()
    cleaned: list[str] = []

    def plugin(ctx_, config):
        def cleanup():
            cleaned.append("cleaned")

        return cleanup

    h1 = ctx.plugin(plugin)
    await h1
    h2 = ctx.plugin(plugin)
    await h2
    assert len(ctx.registry.get(plugin).fibers) == 2
    assert ctx.registry.delete(plugin) is True
    await asyncio.sleep(0.05)
    assert not ctx.registry.has(plugin)
    assert len(cleaned) == 2  # both fibers' disposers ran


# ---------------------------------------------------------------------------
# Fiber states and failures
# ---------------------------------------------------------------------------


async def test_plugin_stays_pending_until_start():
    ctx = Context()
    ran: list[str] = []

    def plugin(ctx_, config):
        ran.append("x")

    handle = ctx.plugin(plugin)  # registered before start
    assert handle.state is FiberState.PENDING
    await ctx.start()
    await handle
    assert ran == ["x"]
    assert handle.state is FiberState.RUNNING


async def test_apply_failure_is_isolated_and_awaitable():
    ctx = Context()
    other_ran: list[str] = []

    def failing(ctx_, config):
        raise ValueError("apply exploded")

    def fine(ctx_, config):
        other_ran.append("fine")

    await ctx.start()
    h1 = ctx.plugin(failing)
    with pytest.raises(ValueError, match="apply exploded"):
        await h1
    assert h1.state is FiberState.FAILED
    h2 = ctx.plugin(fine)
    await h2
    assert other_ran == ["fine"]


async def test_bound_effect_and_current_plugin_name():
    from xcore import bound_effect, current_plugin_name

    ctx = Context()
    cleaned: list[str] = []
    names: list[str] = []

    def applier(ctx_, config):
        names.append(current_plugin_name())
        assert bound_effect(lambda: cleaned.append("x")) is True

    await ctx.start()
    handle = ctx.plugin(applier)
    await handle
    assert names == ["applier"]
    # Outside apply both helpers are safe no-ops.
    assert current_plugin_name() == "unknown"
    assert bound_effect(lambda: cleaned.append("y")) is False

    await handle.dispose()
    assert cleaned == ["x"]


async def test_current_fiber_tracks_applying_plugin_and_binds_cleanup():
    from xcore import current_fiber

    ctx = Context()
    seen: list[str | None] = []
    registered: list[str] = []

    def applier(ctx_, config):
        # During apply the current fiber is this plugin's fiber.
        fiber = current_fiber()
        seen.append(fiber is not None)
        if fiber is not None:
            seen.append(fiber.runtime.definition.name)
            fiber.effect(lambda: lambda: registered.append("cleaned"))
        registered.append("setup")

    await ctx.start()
    handle = ctx.plugin(applier)
    await handle
    assert seen == [True, "applier"]
    assert current_fiber() is None  # outside apply there is no current fiber
    assert registered == ["setup"]

    await handle.dispose()
    assert registered == ["setup", "cleaned"]


async def test_partial_effects_rolled_back_on_failure():
    ctx = Context()
    calls: list[str] = []

    def failing(ctx_, config):
        ctx_.on("evt", lambda s: calls.append("leaked"))
        raise RuntimeError("boom")

    await ctx.start()
    handle = ctx.plugin(failing)
    with pytest.raises(RuntimeError):
        await handle
    assert handle.state is FiberState.FAILED
    await ctx.emit("evt", None)
    assert calls == []  # listener registered before the throw was rolled back


async def test_config_validation_failure_is_isolated():
    ctx = Context()
    schema = S.object({"name": S.string()})

    def plugin(ctx_, config):
        pass

    plugin.Config = schema
    await ctx.start()
    handle = ctx.plugin(plugin, {"name": 42})
    with pytest.raises(SchemaValidationError):
        await handle
    assert handle.state is FiberState.FAILED


async def test_plugin_nesting_disposes_children():
    ctx = Context()
    cleaned: list[str] = []

    def child(ctx_, config):
        def cleanup():
            cleaned.append("child")

        return cleanup

    def parent(ctx_, config):
        ctx_.plugin(child)

    await ctx.start()
    handle = ctx.plugin(parent)
    await handle
    await asyncio.sleep(0.05)  # let the child's background load settle
    await handle.dispose()
    await asyncio.sleep(0.05)
    assert cleaned == ["child"]


async def test_handle_restart_reloads():
    ctx = Context()
    loads: list[int] = []

    def plugin(ctx_, config):
        loads.append(config["n"])

    await ctx.start()
    handle = ctx.plugin(plugin, {"n": 1})
    await handle
    assert loads == [1]
    await handle.restart()
    assert loads == [1, 1]


async def test_failed_fiber_retries_on_dependency_change():
    ctx = Context()
    attempts: list[str] = []

    def provider(ctx_, config):
        ctx_.set("db", object())

    def consumer(ctx_, config):
        attempts.append("consumer")

    consumer.inject = ["db"]

    await ctx.start()
    # register consumer first (pending: db missing), then provider
    h2 = ctx.plugin(consumer)
    await asyncio.sleep(0.02)
    h1 = ctx.plugin(provider)
    await h1
    await h2
    assert attempts == ["consumer"]


async def test_effect_after_dispose_raises():
    ctx = Context()

    def plugin(ctx_, config):
        pass

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    await handle.dispose()
    assert handle.uid is None
    with pytest.raises(Exception, match="inactive"):
        handle._fiber.effect(lambda: None)


async def test_inject_dependency_appears_mid_run():
    ctx = Context()
    loaded: list[str] = []

    def consumer(ctx_, config):
        loaded.append("consumer")

    consumer.inject = ["late-service"]

    await ctx.start()
    handle = ctx.plugin(consumer)
    await asyncio.sleep(0.02)
    assert handle.state is FiberState.PENDING
    ctx.set("late-service", object())
    await asyncio.sleep(0.05)
    assert handle.state is FiberState.RUNNING
    assert loaded == ["consumer"]


async def test_settle_waits_for_dependency_reactivation_without_sleep():
    ctx = Context()
    activations: list[str] = []

    def provider(ctx_, config):
        ctx_.set("dependency", config["value"])

    def consumer(ctx_, config):
        activations.append(ctx_.dependency)

    consumer.inject = ["dependency"]
    await ctx.start()
    consumer_handle = ctx.plugin(consumer)
    provider_handle = ctx.plugin(provider, {"value": "first"})

    await ctx.settle()

    assert provider_handle.state is FiberState.RUNNING
    assert consumer_handle.state is FiberState.RUNNING
    assert activations == ["first"]


async def test_settle_rejects_reentrant_plugin_apply():
    ctx = Context()

    async def plugin(ctx_, config):
        await ctx_.settle()

    await ctx.start()
    handle = ctx.plugin(plugin)
    with pytest.raises(RuntimeError, match="cannot run inside plugin apply"):
        await handle
