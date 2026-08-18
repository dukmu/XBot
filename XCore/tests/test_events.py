"""Event bus tests: registration, dispatch modes, filtering, errors."""

from __future__ import annotations

import asyncio

import pytest

from xcore import Context
from xcore.events import is_bailed


class Session:
    def __init__(self, platform: str = "qq", user: str = "u1") -> None:
        self.platform = platform
        self.user = user


async def test_emit_runs_in_registration_order():
    ctx = Context()
    order: list[str] = []
    ctx.on("evt", lambda s: order.append("a"))
    ctx.on("evt", lambda s: order.append("b"))
    await ctx.emit("evt", None)
    assert order == ["a", "b"]


async def test_prepend_inserts_at_head():
    ctx = Context()
    order: list[str] = []
    ctx.on("evt", lambda s: order.append("a"))
    ctx.on("evt", lambda s: order.append("b"), prepend=True)
    ctx.on("evt", lambda s: order.append("c"), prepend=True)
    await ctx.emit("evt", None)
    assert order == ["c", "b", "a"]


async def test_once_fires_single_time_even_across_dispatches():
    ctx = Context()
    fired: list[int] = []
    ctx.once("evt", lambda s: fired.append(1))
    await ctx.emit("evt", None)
    await ctx.emit("evt", None)
    assert fired == [1]


async def test_once_is_concurrency_safe():
    ctx = Context()
    fired: list[int] = []
    ctx.once("evt", lambda s: fired.append(1))
    await ctx.parallel("evt", None)
    await ctx.parallel("evt", None)
    assert fired == [1]


async def test_off_removes_by_listener_identity():
    ctx = Context()
    calls: list[str] = []
    listener = lambda s: calls.append("x")
    ctx.on("evt", listener)
    assert ctx.off("evt", listener) is True
    assert ctx.off("evt", listener) is False
    await ctx.emit("evt", None)
    assert calls == []


async def test_disposer_is_idempotent():
    ctx = Context()
    calls: list[str] = []
    disposer = ctx.on("evt", lambda s: calls.append("x"))
    assert disposer() is True
    assert disposer() is False
    await ctx.emit("evt", None)
    assert calls == []


async def test_bail_returns_first_bail_value():
    ctx = Context()
    ctx.on("q", lambda s: None)
    ctx.on("q", lambda s: False)
    ctx.on("q", lambda s: "answer")
    ctx.on("q", lambda s: "later")
    assert await ctx.bail("q", None) == "answer"


async def test_bail_edge_values():
    # isBailed: only None and False continue (0/""/[]/{} are bail values)
    for value in (0, "", [], {}, "x", 1):
        assert is_bailed(value) is True
    for value in (None, False):
        assert is_bailed(value) is False


async def test_serial_stops_at_first_bail():
    ctx = Context()
    seen: list[str] = []
    ctx.on("q", lambda s: seen.append("a"))
    ctx.on("q", lambda s: "stop")
    ctx.on("q", lambda s: seen.append("b"))
    assert await ctx.serial("q", None) == "stop"
    assert seen == ["a"]


async def test_chain_threads_value():
    ctx = Context()
    ctx.on("pipe", lambda v: v * 2)
    ctx.on("pipe", lambda v: v + 1)
    assert await ctx.chain("pipe", 10) == 21


async def test_waterfall_composes_around_next():
    ctx = Context()
    seen: list[str] = []

    async def outer(session, next_fn):
        seen.append("outer")
        result = await next_fn()
        return f"outer({result})"

    async def veto(session, next_fn):
        seen.append("veto")
        return "vetoed"

    async def inner(session, next_fn):
        seen.append("inner")
        return await next_fn()

    ctx.on("wf", outer)
    ctx.on("wf", veto)
    ctx.on("wf", inner)
    result = await ctx.waterfall("wf", None, next=lambda: "builtin")
    assert result == "outer(vetoed)"
    assert seen == ["outer", "veto"]


async def test_waterfall_without_next_raises():
    ctx = Context()
    with pytest.raises(TypeError):
        await ctx.waterfall("evt", None)


async def test_parallel_aggregates_all_failures():
    ctx = Context()

    async def boom_a():
        raise ValueError("a")

    async def boom_b():
        raise RuntimeError("b")

    ctx.on("par", boom_a)
    ctx.on("par", boom_b)
    with pytest.raises(ExceptionGroup) as excinfo:
        await ctx.parallel("par")
    kinds = {type(exc) for exc in excinfo.value.exceptions}
    assert kinds == {ValueError, RuntimeError}


async def test_parallel_propagates_cancellation():
    ctx = Context()

    async def cancel_self():
        raise asyncio.CancelledError()

    ctx.on("par", cancel_self)
    with pytest.raises(asyncio.CancelledError):
        await ctx.parallel("par")


async def test_emit_propagates_listener_error():
    ctx = Context()

    def boom(s):
        raise ValueError("boom")

    ctx.on("evt", boom)
    with pytest.raises(ValueError, match="boom"):
        await ctx.emit("evt", None)


async def test_wildcard_segment_matching():
    ctx = Context()
    caught: list[str] = []
    ctx.on("foo/*", lambda s: caught.append("foo-star"))
    ctx.on("foo/bar", lambda s: caught.append("foo-bar"))
    ctx.on("*", lambda s: caught.append("all"))
    await ctx.emit("foo/bar", None)
    # registration order governs across exact and wildcard matches
    assert caught == ["foo-star", "foo-bar", "all"]
    caught.clear()
    await ctx.emit("foo/baz", None)
    assert caught == ["foo-star", "all"]


async def test_wildcard_mid_segment_rejected():
    ctx = Context()
    with pytest.raises(ValueError):
        ctx.on("fo*o", lambda s: None)


async def test_filters_snapshot_at_registration():
    ctx = Context()
    captured: list[str] = []

    def predicate(session):
        return session.platform == "qq"

    ctx.on("evt", lambda s: captured.append("unfiltered"))
    ctx.filter(predicate)
    ctx.on("evt", lambda s: captured.append("filtered"))
    await ctx.emit("evt", Session(platform="discord"))
    assert captured == ["unfiltered"]
    await ctx.emit("evt", Session(platform="qq"))
    assert captured == ["unfiltered", "unfiltered", "filtered"]


async def test_global_listener_skips_filters():
    ctx = Context()
    captured: list[str] = []
    ctx.filter(lambda s: False)
    ctx.on("evt", lambda s: captured.append("normal"))
    ctx.on("evt", lambda s: captured.append("global"), global_=True)
    await ctx.emit("evt", Session())
    assert captured == ["global"]


async def test_select_scopes_listeners():
    ctx = Context()
    captured: list[str] = []
    scoped = ctx.select("platform", "qq")
    scoped.on("evt", lambda s: captured.append(s.platform))
    await ctx.emit("evt", Session(platform="qq"))
    await ctx.emit("evt", Session(platform="discord"))
    assert captured == ["qq"]


async def test_listeners_removed_with_owning_fiber():
    ctx = Context()
    captured: list[str] = []

    def plugin(ctx_, config):
        ctx_.on("evt", lambda s: captured.append("x"))

    await ctx.start()
    handle = ctx.plugin(plugin)
    await handle
    await ctx.emit("evt", None)
    assert captured == ["x"]
    await handle.dispose()
    await ctx.emit("evt", None)
    assert captured == ["x"]


async def test_before_sugar_prepends_by_default():
    ctx = Context()
    order: list[str] = []
    # `before` sugar = on("before-x", ..., prepend=not append)
    ctx.before("x", lambda s: order.append("b1"))
    ctx.before("x", lambda s: order.append("b2"), append=True)
    ctx.on("x", lambda s: order.append("x"))
    await ctx.emit("before-x", None)
    assert order == ["b1", "b2"]  # default prepend first, append=True later
    await ctx.emit("x", None)
    assert order == ["b1", "b2", "x"]


async def test_internal_dispatch_diagnostic_event():
    ctx = Context()
    seen: list[tuple[str, str]] = []

    def observer(mode, name, args):
        seen.append((mode, name))

    ctx.on("internal/dispatch", observer, global_=True)
    await ctx.emit("user/event", None)
    assert ("emit", "user/event") in seen
