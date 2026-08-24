"""Service system tests: registry, Service base, isolation, injection."""

from __future__ import annotations

import pytest

from xcore import (
    Context,
    Service,
    ServiceConflictError,
    ServiceNotFoundError,
)


async def test_set_get_has_require():
    ctx = Context()
    ctx.set("greeter", "hello")
    assert ctx.get("greeter") == "hello"
    assert ctx.has("greeter")
    assert ctx.require("greeter") == "hello"
    assert ctx.get("missing") is None
    assert not ctx.has("missing")
    with pytest.raises(ServiceNotFoundError):
        ctx.require("missing")


async def test_attribute_access():
    ctx = Context()
    ctx.set("greeter", "hello")
    assert ctx.greeter == "hello"
    assert hasattr(ctx, "greeter")
    assert not hasattr(ctx, "absent")
    with pytest.raises(AttributeError):
        ctx.absent


async def test_direct_attribute_assignment_rejected():
    ctx = Context()
    with pytest.raises(AttributeError):
        ctx.some_service = object()  # must use ctx.set


async def test_double_provide_conflicts():
    ctx = Context()
    ctx.set("svc", "one")
    with pytest.raises(ServiceConflictError):
        ctx.set("svc", "two")


async def test_release_then_reprovide():
    ctx = Context()
    ctx.set("svc", "one")
    ctx.set("svc", None)  # release
    assert ctx.get("svc") is None
    ctx.set("svc", "two")
    assert ctx.get("svc") == "two"


async def test_set_disposer_releases():
    ctx = Context()
    disposer = ctx.set("svc", "one")
    assert ctx.get("svc") == "one"
    assert disposer() is True
    assert ctx.get("svc") is None


async def test_unset_identity_check():
    ctx = Context()
    value = object()
    ctx.set("svc", value)
    assert ctx.unset("svc", object()) is False  # wrong identity -> no-op
    assert ctx.unset("svc", value) is True
    assert ctx.get("svc") is None


async def test_service_base_registers_on_construction():
    ctx = Context()

    class Counter(Service):
        name = "counter"

        def __init__(self, ctx, *, start=0):
            self.value = start
            super().__init__(ctx)

    counter = Counter(ctx, start=5)
    assert ctx.counter is counter
    assert ctx.get("counter") is counter
    # same name cannot be provided twice
    with pytest.raises(ServiceConflictError):
        Counter(ctx)


async def test_service_default_name_is_snake_case():
    ctx = Context()

    class MyDatabase(Service):
        pass

    service = MyDatabase(ctx)
    assert service.name == "my_database"
    assert ctx.my_database is service


async def test_service_removed_with_owning_fiber():
    ctx = Context()
    seen: list[str] = []

    def provider(ctx_, config):
        class Widget(Service):
            name = "widget"

        Widget(ctx_)
        seen.append("provided")

    await ctx.start()
    handle = ctx.plugin(provider)
    await handle
    assert ctx.has("widget")
    await handle.dispose()
    assert not ctx.has("widget")


async def test_isolate_creates_independent_scope():
    ctx = Context()
    base_label = object()
    scoped = ctx.isolate("db", label=base_label)
    ctx.set("db", "root-db")
    scoped.set("db", "scoped-db")
    assert ctx.get("db") == "root-db"
    assert scoped.get("db") == "scoped-db"
    # same label joins the same scope
    scoped2 = ctx.isolate("db", label=base_label)
    assert scoped2.get("db") == "scoped-db"


async def test_isolate_default_labels_do_not_merge():
    ctx = Context()
    a = ctx.isolate("db")
    b = ctx.isolate("db")
    a.set("db", "a")
    b.set("db", "b")
    assert a.get("db") == "a"
    assert b.get("db") == "b"
    assert ctx.get("db") is None


async def test_inject_wakes_dependent_when_service_appears_in_apply():
    # The A1 scenario from the design review: the service is registered inside
    # apply(); the dependent must load only after the provider is RUNNING.
    ctx = Context()
    loaded: list[str] = []

    def provider(ctx_, config):
        ctx_.set("database", "db-impl")
        loaded.append("provider")

    def consumer(ctx_, config):
        assert ctx_.database == "db-impl"
        loaded.append("consumer")

    consumer.inject = ["database"]

    await ctx.start()
    h1 = ctx.plugin(provider)
    await h1
    h2 = ctx.plugin(consumer)
    await h2
    assert loaded == ["provider", "consumer"]
    assert h1.state.value == "running"
    assert h2.state.value == "running"


async def test_start_resolves_pre_mounted_dependencies_without_row_order():
    ctx = Context()
    loaded: list[str] = []

    def consumer(ctx_, config):
        loaded.append(ctx_.database)

    consumer.inject = ["database"]

    def provider(ctx_, config):
        ctx_.set("database", "db-impl")

    consumer_handle = ctx.plugin(consumer)
    provider_handle = ctx.plugin(provider)

    await ctx.start()

    assert provider_handle.state.value == "running"
    assert consumer_handle.state.value == "running"
    assert loaded == ["db-impl"]


async def test_inject_dependent_rolls_back_when_service_removed():
    ctx = Context()
    states: list[str] = []

    def provider(ctx_, config):
        ctx_.set("database", object())

    def consumer(ctx_, config):
        ctx_.on("internal/status", lambda fiber, old: None)  # noqa
        states.append("consumer-loaded")

    consumer.inject = ["database"]

    await ctx.start()
    h1 = ctx.plugin(provider)
    await h1
    h2 = ctx.plugin(consumer)
    await h2
    assert states == ["consumer-loaded"]
    # remove the service -> consumer rolls back to pending
    ctx.unset("database")
    await asyncio_sleep()
    assert h2.state.value == "pending"


async def test_inject_optional_does_not_gate():
    ctx = Context()
    loaded: list[str] = []

    def consumer(ctx_, config):
        loaded.append("consumer")

    consumer.inject = {"optional": ["missing-service"]}

    await ctx.start()
    handle = ctx.plugin(consumer)
    await handle
    assert loaded == ["consumer"]


async def test_required_plugin_name_dependency_waits():
    # required: list[str] is not part of cordis core; XCore uses inject for
    # service deps. This test guards the koishi-form dict (required/optional).
    ctx = Context()
    loaded: list[str] = []

    def dep(ctx_, config):
        loaded.append("dep")

    def main(ctx_, config):
        loaded.append("main")

    main.inject = {"required": ["dep-service"], "optional": []}

    await ctx.start()

    def dep_provider(ctx_, config):
        ctx_.set("dep-service", object())
        loaded.append("provider")

    h1 = ctx.plugin(dep_provider)
    await h1
    h2 = ctx.plugin(main)
    await h2
    assert "main" in loaded


async def asyncio_sleep():
    import asyncio

    await asyncio.sleep(0.05)
