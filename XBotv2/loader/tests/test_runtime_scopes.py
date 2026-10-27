"""Acceptance through the production loader and session lifecycle API."""
import asyncio
import json
import threading
from pathlib import Path

import pytest
from xcore.api.context import Context
from xcore.api.services import ServiceKey
from XBotv2.main import open_runtime
from XBotv2.loader.api.contracts import LoadError, PluginEntry, PluginTree
from XBotv2.loader.impl.runtime import Loader
from XBotv2.session.api.contracts import OpenSession, SESSIONS
from XBotv2.agentloop.api.contracts import TOOLS, AGENT_LOOP
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import Tool, ToolCall
from XBotv2.core.api.errors import OperationError
from XBotv2.llm.api.contracts import MODEL, LLM
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.permissions.api.models import PERMISSIONS, PermissionPolicy
from XBotv2.jobs.api.contracts import JOBS, JobNotFound


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_TOKEN", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")


def request(sid, workspace, *, mode="new", plugins=True, configs=None):
    return OpenSession(sid, "main", str(workspace), "minimax", mode, not plugins, model_override=MockLLM(), plugin_configs=configs)


async def test_shared_root_isolation_and_repeated_close(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = await open_runtime(paths=paths)
    sessions = root.require(SESSIONS)
    baseline = len(root.registry.handles())
    baseline_tasks = asyncio.all_tasks()
    descriptors = Path("/proc/self/fd")
    baseline_fds = len(list(descriptors.iterdir())) if descriptors.exists() else None
    assert not paths.sessions_dir.exists()
    try:
        for index in range(3):
            a, b = await asyncio.gather(sessions.open(request(f"a{index}", tmp_path, plugins=False)), sessions.open(request(f"b{index}", tmp_path, plugins=False)))
            assert a.root is b.root is root
            assert a is not b
            assert a.require(LLM) is b.require(LLM)
            assert a.require(MODEL) is not b.require(MODEL)
            assert a.require(TOOLS) is not b.require(TOOLS)
            assert await sessions.locate(f"a{index}", "main") is a
            subscription = a.require("thread_events").subscribe()
            await sessions.close_session(f"a{index}")
            with pytest.raises(StopAsyncIteration):
                await anext(subscription)
            assert not b.require(AGENT_LOOP).busy
            await sessions.close_session(f"b{index}")
            assert len(root.registry.handles()) == baseline
            await asyncio.sleep(0)
            assert asyncio.all_tasks() == {task for task in baseline_tasks if not task.done()}
            if baseline_fds is not None:
                assert len(list(descriptors.iterdir())) == baseline_fds
            assert not list(paths.sessions_dir.glob("*"))
    finally:
        await root.close()
    assert not root.registry.handles()


async def test_concurrent_open_shields_cancelled_waiter(tmp_path, monkeypatch):
    name = "scope_gate_extension"
    pkg = tmp_path / name / "impl"
    pkg.mkdir(parents=True)
    (pkg / "plugin.py").write_text('''import asyncio
entered = asyncio.Event()
release = asyncio.Event()
async def mount_thread(ctx, config):
    entered.set()
    await release.wait()
mount_thread.inject = ()
mount_thread.provide = ()
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / ".xbot").mkdir()
    (tmp_path / ".xbot/plugins.yaml").write_text(f"- id: gate\n  name: {name}.impl.plugin\n")
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    first = asyncio.create_task(sessions.open(request("shared", tmp_path, plugins=True)))
    try:
        import sys
        while f"{name}.impl.plugin" not in sys.modules:
            await asyncio.sleep(0)
        module = sys.modules[f"{name}.impl.plugin"]
        await asyncio.wait_for(module.entered.wait(), 10)
        second = asyncio.create_task(sessions.open(request("shared", tmp_path, plugins=True)))
        another = tmp_path / "another-workspace"
        another.mkdir()
        wrong = asyncio.create_task(sessions.open(request("shared", another, plugins=True)))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        module.release.set()
        opened = await asyncio.wait_for(second, 10)
        with pytest.raises(OperationError) as conflict:
            await asyncio.wait_for(wrong, 10)
        assert conflict.value.code == "workspace_conflict"
        assert await sessions.locate("shared", "main") is opened
        assert len(await sessions.scopes("shared")) == 1
    finally:
        await root.close()


async def test_independent_runtime_cannot_share_session_owner(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    first, second = await open_runtime(paths=paths), await open_runtime(paths=paths)
    try:
        await first.require(SESSIONS).open(request("exclusive", tmp_path, plugins=False))
        with pytest.raises(OperationError, match="already owned"):
            await second.require(SESSIONS).open(request("exclusive", tmp_path, plugins=False))
        await second.require(SESSIONS).open(request("different", tmp_path, plugins=False))
        await first.require(SESSIONS).close_session("exclusive")
        await second.require(SESSIONS).open(request("exclusive", tmp_path, plugins=False))
    finally:
        await first.close()
        await second.close()


async def test_required_guard_failure_rolls_back_only_new_owner(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = await open_runtime(paths=paths)
    baseline = len(root.registry.handles())
    (tmp_path / ".xbot").mkdir()
    (tmp_path / ".xbot/plugins.yaml").write_text("- id: permissions\n  disabled: true\n")
    sentinel = paths.session("existing").root / "accepted-input.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("keep accepted input")
    try:
        with pytest.raises(LoadError, match="permissions"):
            await root.require(SESSIONS).open(request("existing", tmp_path, plugins=False))
        assert sentinel.read_text() == "keep accepted input"
        assert len(root.registry.handles()) == baseline
        assert not await root.require(SESSIONS).scopes("existing")
    finally:
        await root.close()


async def test_optional_import_isolated_and_undeclared_provide_rejected(tmp_path, monkeypatch):
    root = Context()
    await root.start()
    loader = Loader()
    bad = tmp_path / "bad_scope_extension.py"
    bad.write_text('''def mount_thread(ctx, config):
    ctx.set("undeclared", object())
mount_thread.inject = ()
mount_thread.provide = ()
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = PluginTree([PluginEntry("optional", "missing_scope_extension", optional=True), PluginEntry("bad", "bad_scope_extension")])
    scope = loader.scope(root, "bad", kind="thread", tree=tree)
    try:
        with pytest.raises(LoadError, match="undeclared service"):
            await loader.mount(scope, tree, kind="thread")
        assert not root.has("undeclared")
        assert not root.registry.handles()
    finally:
        await root.close()


async def test_close_waits_for_real_tool_worker_and_lock(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = await open_runtime(paths=paths)
    competitor = await open_runtime(paths=paths)
    sessions = root.require(SESSIONS)
    scope = await sessions.open(request("worker", tmp_path, plugins=False, configs={"permissions": {"default_decision": "allow", "rules": []}}))
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocking_io():
        started.set()
        release.wait()
        (tmp_path / "worker-result.txt").write_text("finished")
        finished.set()
        return "finished"
    from XBotv2.interactions.api.contracts import CLIENT_EVENTS
    from XBotv2.permissions.api.models import Allowed, PermissionRequest
    approvals = []
    async def approve(event):
        assert isinstance(event, PermissionRequest)
        approvals.append(event)
        return Allowed(scope="once")
    remove_sink = scope.require(CLIENT_EVENTS).install(approve)
    scope.require(TOOLS).register(Tool.from_function(blocking_io), owner=scope)
    invocation = asyncio.create_task(scope.require(TOOLS).execute_all([ToolCall(id="worker-call", name="blocking_io", args={})]))
    try:
        assert await asyncio.to_thread(started.wait, 10), await invocation if invocation.done() else "worker did not start"
        assert len(approvals) == 1
        close = asyncio.create_task(sessions.close_session("worker"))
        async with asyncio.timeout(5):
            while scope.is_active:
                await asyncio.sleep(0)
        assert not finished.is_set()
        assert not close.done()
        with pytest.raises(OperationError, match="already owned"):
            await competitor.require(SESSIONS).open(request("worker", tmp_path, plugins=False))
        release.set()
        await asyncio.wait_for(close, 10)
        assert finished.is_set()
        assert (tmp_path / "worker-result.txt").read_text() == "finished"
        await competitor.require(SESSIONS).open(request("worker", tmp_path, plugins=False))
        await asyncio.gather(invocation, return_exceptions=True)
    finally:
        remove_sink()
        release.set()
        await root.close()
        await competitor.close()


async def test_jobs_share_registry_but_cannot_cross_thread_authority(tmp_path):
    from dataclasses import dataclass
    from XBotv2.jobs.api.contracts import JobError
    @dataclass
    class Spec:
        kind: str = "acceptance"
        label: str = "job"
    class Runner:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
        async def run(self, job):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()
        async def cancel(self, job):
            pass
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    try:
        a, b = await asyncio.gather(sessions.open(request("jobs-a", tmp_path, plugins=False)), sessions.open(request("jobs-b", tmp_path, plugins=False)))
        a_jobs, b_jobs = a.require(JOBS), b.require(JOBS)
        first = await a_jobs.create(spec=Spec(), owner="test")
        second = await b_jobs.create(spec=Spec(), owner="test")
        assert first.id != second.id
        ra, rb = Runner(), Runner()
        a_jobs.start(first.id, ra)
        b_jobs.start(second.id, rb)
        await asyncio.wait_for(asyncio.gather(ra.started.wait(), rb.started.wait()), 5)
        assert [view.id for view in a_jobs.views()] == [first.id]
        assert [view.id for view in b_jobs.views()] == [second.id]
        assert a_jobs.get_or_none(second.id) is None
        with pytest.raises(JobNotFound):
            await a_jobs.cancel(second.id)
        await sessions.close_session("jobs-a")
        assert ra.stopped.is_set()
        assert a_jobs.closing
        from XBotv2.jobs.api.contracts import JobRegistryClosed
        with pytest.raises(JobRegistryClosed):
            await a_jobs.create(spec=Spec(), owner="stale")
        reopened = await sessions.open(request("jobs-a", tmp_path, plugins=False))
        fresh_jobs = reopened.require(JOBS)
        fresh = await fresh_jobs.create(spec=Spec(), owner="fresh")
        assert not fresh_jobs.closing
        with pytest.raises(JobRegistryClosed):
            a_jobs.get_or_none(fresh.id)
        with pytest.raises(JobRegistryClosed):
            await a_jobs.cancel(fresh.id)
        assert fresh_jobs.get_or_none(fresh.id) is fresh
        assert not rb.stopped.is_set()
        await root.close()
        assert rb.stopped.is_set()
    finally:
        await root.close()


async def test_workspace_provider_overlays_share_only_identical_configs(tmp_path):
    from XBotv2.llm.api.contracts import LLM
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    first, second = tmp_path / "first", tmp_path / "second"
    for directory, model in [(first, "first-model"), (second, "second-model")]:
        (directory / ".xbot").mkdir(parents=True)
        (directory / ".xbot/plugins.yaml").write_text(f'''- id: llm
  config:
    default_provider: local
    providers:
      local:
        protocol: mock
        default_model: {model}
        models:
          - model: {model}
            max_context_tokens: 8192
            max_output_tokens: 2048
''')
    session_config = paths.session("overlay-session").config_file
    session_config.parent.mkdir(parents=True)
    session_config.write_text("- id: llm\n  config:\n    providers:\n      local:\n        default_model: session-model\n        models:\n          - model: session-model\n            max_context_tokens: 8192\n            max_output_tokens: 2048\n")
    root = await open_runtime(paths=paths)
    created = []
    def build(provider, model):
        created.append(model.model)
        return MockLLM()
    root.require(LLM).unregister("mock")
    root.require(LLM).register("mock", build)
    try:
        sessions = root.require(SESSIONS)
        a, b, twin, selected = await asyncio.gather(*(sessions.open(OpenSession(sid, "main", str(workspace), None, "new", True)) for sid, workspace in [("overlay-a", first), ("overlay-b", second), ("overlay-twin", first), ("overlay-session", first)]))
        assert a.require(LLM) is twin.require(LLM)
        assert a.require(LLM) is not b.require(LLM)
        assert a.require(LLM).default_name() == b.require(LLM).default_name() == "local"
        assert a.require(LLM).provider_config("local").default_model == "first-model"
        assert b.require(LLM).provider_config("local").default_model == "second-model"
        assert selected.require(LLM).provider_config("local").default_model == "session-model"
        assert selected.require(LLM) is not a.require(LLM)
        assert sorted(created) == ["first-model", "second-model", "session-model"]
        provider = a.require(LLM).provider_config("local")
        assert a.require(LLM).create(provider) is twin.require(LLM).create(provider)
        from XBotv2.agentloop.api.state import LOOP_STATE
        assert a.require(LOOP_STATE).metadata.value.runtime_selection.model.route.model == "first-model"
        assert b.require(LOOP_STATE).metadata.value.runtime_selection.model.route.model == "second-model"
        assert selected.require(LOOP_STATE).metadata.value.runtime_selection.model.route.model == "session-model"
    finally:
        await root.close()


async def test_external_impl_plugin_is_discovered_and_mounted_in_both_scopes(tmp_path):
    plugins = tmp_path / "extensions"
    implementation = plugins / "external_scope_contract" / "impl"
    implementation.mkdir(parents=True)
    (implementation / "plugin.py").write_text('''def mount_process(ctx, config):
    ctx.set("extension.shared", object())
mount_process.inject = ()
mount_process.provide = ("extension.shared",)

def mount_thread(ctx, config):
    ctx.set("extension.private", (ctx.require("extension.shared"), object()))
mount_thread.inject = ("extension.shared",)
mount_thread.provide = ("extension.private",)
''')
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"), plugin_dirs=[plugins])
    try:
        sessions = root.require(SESSIONS)
        a, b = await asyncio.gather(sessions.open(request("external-a", tmp_path)), sessions.open(request("external-b", tmp_path)))
        assert a.require("extension.private")[0] is b.require("extension.private")[0] is root.require("extension.shared")
        assert a.require("extension.private")[1] is not b.require("extension.private")[1]
        assert not root.has("extension.private")
    finally:
        await root.close()


async def test_failed_pre_stop_retains_scope_and_lock_until_retry(tmp_path):
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root, competitor = await open_runtime(paths=paths), await open_runtime(paths=paths)
    sessions = root.require(SESSIONS)
    scope = await sessions.open(request("retry-close", tmp_path, plugins=False))
    fail = True
    async def stop():
        if fail:
            raise RuntimeError("controlled drain failure")
    scope.pre_stop(stop)
    try:
        with pytest.raises(ExceptionGroup, match="remain owned"):
            await sessions.close_session("retry-close")
        assert await sessions.locate("retry-close", "main") is scope
        assert not scope.is_active
        with pytest.raises(OperationError, match="closing"):
            await sessions.open(request("retry-close", tmp_path, plugins=False))
        with pytest.raises(OperationError, match="already owned"):
            await competitor.require(SESSIONS).open(request("retry-close", tmp_path, plugins=False))
        fail = False
        await sessions.close_session("retry-close")
        reopened = await sessions.open(request("retry-close", tmp_path, plugins=False))
        assert reopened is not scope
    finally:
        fail = False
        await root.close()
        await competitor.close()


async def test_nested_component_cannot_publish_outside_facet_plan(tmp_path, monkeypatch):
    (tmp_path / "nested_scope_extension.py").write_text('''def child(ctx, config):
    ctx.set("escaped", object())
child.inject = ()
child.provide = ("escaped",)

def mount_thread(ctx, config):
    ctx.plugin(child)
mount_thread.inject = ()
mount_thread.provide = ()
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    root = Context()
    await root.start()
    loader = Loader()
    tree = PluginTree([PluginEntry("nested", "nested_scope_extension")])
    scope = loader.scope(root, "nested", kind="thread", tree=tree)
    try:
        with pytest.raises(LoadError, match="undeclared service"):
            await loader.mount(scope, tree, kind="thread")
        assert not root.has("escaped")
        assert not root.registry.handles()
    finally:
        await root.close()


async def test_reopen_rebinds_current_definition_and_preserves_explicit_provider(tmp_path):
    from XBotv2.agentloop.api.state import LOOP_STATE
    from XBotv2.session.api.contracts import THREAD_EVENTS
    from XBotv2.agents.api.contracts import AGENT_RUNTIME
    definitions = tmp_path / ".agents"
    definitions.mkdir()
    source = definitions / "reviewer.md"
    source.write_text("---\ndescription: Review\nmode: primary\nlimits:\n  max_turns: 2\ntool_policy:\n  enabled: [read]\n---\nFirst definition\n")
    (tmp_path / ".xbot").mkdir()
    (tmp_path / ".xbot/plugins.yaml").write_text("- id: llm\n  config:\n    providers:\n      selected:\n        protocol: mock\n        default_model: explicit-model\n        models:\n          - model: explicit-model\n            max_context_tokens: 8192\n            max_output_tokens: 2048\n")
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    try:
        scope = await sessions.open(OpenSession("definition", "main", str(tmp_path), None, "new", True, selected_agent="reviewer", model_override=MockLLM()))
        events = scope.require(THREAD_EVENTS).subscribe()
        await scope.require(AGENT_LOOP).send_message("hello", "definition-input")
        async with asyncio.timeout(5):
            async for frame in events:
                if frame.event.kind == "turn_ended":
                    break
        await events.aclose()
        await scope.require(AGENT_RUNTIME).select_provider("selected", model="explicit-model")
        await sessions.close_session("definition")
        source.write_text("---\ndescription: Review\nmode: primary\nlimits:\n  max_turns: 3\ntool_policy:\n  enabled: [search]\n---\nSecond definition\n")
        resumed = await sessions.open(OpenSession("definition", "main", str(tmp_path), None, "resume", True, model_override=MockLLM()))
        metadata = resumed.require(LOOP_STATE).metadata.value
        assert metadata.runtime_selection.agent_name == "reviewer"
        assert "Second definition" in metadata.runtime_selection.prompt
        assert "First definition" not in metadata.runtime_selection.prompt
        assert metadata.runtime_selection.limits.max_turns == 3
        assert metadata.runtime_selection.enabled_tools == ("search",)
        assert metadata.runtime_selection.model.route.provider == "selected"
        assert metadata.runtime_selection.model.route.model == "explicit-model"
        assert metadata.provider_override == "selected"
        assert metadata.model_override == "explicit-model"
    finally:
        await root.close()


async def test_disabled_thread_facet_retains_private_service_boundary(tmp_path):
    from XBotv2.commands.api.contracts import COMMANDS
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    commands = root.require(COMMANDS)
    baseline = commands.all()
    (tmp_path / ".xbot").mkdir()
    (tmp_path / ".xbot/plugins.yaml").write_text("- id: commands\n  disabled: true\n")
    try:
        with pytest.raises(LoadError, match="commands"):
            await root.require(SESSIONS).open(request("disabled-commands", tmp_path, plugins=False))
        assert commands.all() == baseline
        assert not await root.require(SESSIONS).scopes("disabled-commands")
    finally:
        await root.close()


async def test_resume_uses_saved_workspace_and_rejects_explicit_conflict(tmp_path):
    from XBotv2.session.api.contracts import THREAD_INFO, THREAD_EVENTS
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    workspace, other = tmp_path / "workspace", tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    root = await open_runtime(paths=paths)
    sessions = root.require(SESSIONS)
    try:
        scope = await sessions.open(OpenSession("workspace-owner", "main", str(workspace), None, "new", True, model_override=MockLLM([{"content": "saved"}])))
        events = scope.require(THREAD_EVENTS).subscribe()
        await scope.require(AGENT_LOOP).send_message("save input", "workspace-input")
        async with asyncio.timeout(5):
            async for frame in events:
                if frame.event.kind == "turn_ended":
                    break
        await events.aclose()
        await sessions.close_session("workspace-owner")
        before = await sessions.session_summary("workspace-owner")
        with pytest.raises(OperationError) as conflict:
            await sessions.open(OpenSession("workspace-owner", "main", str(other), None, "resume", True))
        assert conflict.value.code == "workspace_conflict"
        assert await sessions.session_summary("workspace-owner") == before
        resumed = await sessions.open(OpenSession("workspace-owner", "main", None, None, "resume", True, model_override=MockLLM()))
        assert resumed.require(THREAD_INFO).workspace_root == workspace
        with pytest.raises(OperationError) as conflict:
            await sessions.open(OpenSession("workspace-owner", "main", str(other), None, "resume", True))
        assert conflict.value.code == "workspace_conflict"
        assert await sessions.locate("workspace-owner", "main") is resumed
    finally:
        await root.close()


async def test_children_with_same_provider_call_id_have_independent_interactions(tmp_path):
    from XBotv2.core.api.stream import ModelCompleted
    from XBotv2.core.api.tools import ToolSucceeded, ToolDenied
    from XBotv2.interactions.api.contracts import INTERACTIONS, CLIENT_EVENTS
    from XBotv2.interactions.api.models import UserInputRequest, Answered
    from XBotv2.permissions.api.models import PermissionRequest, Allowed, Denied
    from XBotv2.session.api.contracts import THREAD_EVENTS, OpenThread

    class WaitingProvider(MockLLM):
        def __init__(self):
            super().__init__()
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def _astream_once(self, request):
            self.started.set()
            await self.release.wait()
            yield ModelCompleted(response=self.to_response({"content": "done"}))

    provider = WaitingProvider()
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    sessions = root.require(SESSIONS)
    tasks = []
    try:
        parent = await sessions.open(OpenSession("interactions", "main", str(tmp_path), None, "new", True, model_override=provider))
        children = await asyncio.gather(*(sessions.open_thread(OpenThread("interactions", name, "main", None, None, "new", True, model_override=MockLLM())) for name in ("first", "second")))
        async def child_tool(value: str):
            return value
        for child in children:
            child.require(TOOLS).register(Tool.from_function(child_tool), owner=child)
        events = parent.require(THREAD_EVENTS).subscribe()
        await parent.require(AGENT_LOOP).send_message("wait for children", "parent-input")
        await asyncio.wait_for(provider.started.wait(), 5)
        for child, value in zip(children, ("first", "second")):
            tasks.append(asyncio.create_task(child.require(TOOLS).execute_all([ToolCall(id="same-provider-call", name="child_tool", args={"value": value})])))
            tasks.append(asyncio.create_task(child.require(INTERACTIONS).request_user_input(value, tool_call_id="same-provider-call")))
        pending = []
        async with asyncio.timeout(5):
            async for frame in events:
                if isinstance(frame.event, (PermissionRequest, UserInputRequest)):
                    pending.append(frame.event)
                if len(pending) == 4:
                    break
        assert len({event.interaction_id for event in pending}) == 4
        for event in pending:
            if isinstance(event, PermissionRequest):
                assert event.subject.tool_call.id == "same-provider-call"
                value = event.subject.tool_call.args["value"]
                resolution = Allowed(scope="once") if value == "first" else Denied(reason="second child denied")
            else:
                assert event.tool_call_id == "same-provider-call"
                resolution = Answered(answer=event.question + " answer")
            parent.require(CLIENT_EVENTS).resolve(event.interaction_id, resolution)
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert isinstance(results[0][0].message.outcome, ToolSucceeded)
        assert results[1] == Answered(answer="first answer")
        assert isinstance(results[2][0].message.outcome, ToolDenied)
        assert results[3] == Answered(answer="second answer")
        await events.aclose()
    finally:
        provider.release.set()
        await root.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_close_cancels_and_awaits_initializing_session_owner(tmp_path, monkeypatch):
    import importlib
    plugins = tmp_path / "extensions"
    implementation = plugins / "session_owner_gate" / "impl"
    implementation.mkdir(parents=True)
    (implementation / "plugin.py").write_text('''import asyncio
entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
async def mount_session(ctx, config):
    entered.set()
    try:
        await release.wait()
    finally:
        finished.set()
mount_session.inject = ()
mount_session.provide = ()
''')
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    root = await open_runtime(paths=paths, plugin_dirs=[plugins])
    competitor = await open_runtime(paths=paths)
    sessions = root.require(SESSIONS)
    baseline = len(root.registry.handles())
    module = importlib.import_module("session_owner_gate.impl.plugin")
    opening = asyncio.create_task(sessions.open(request("initializing", tmp_path)))
    try:
        await asyncio.wait_for(module.entered.wait(), 5)
        with pytest.raises(OperationError, match="already owned"):
            await competitor.require(SESSIONS).open(request("initializing", tmp_path, plugins=False))
        await asyncio.wait_for(sessions.close_session("initializing"), 5)
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert module.finished.is_set()
        module.release.set()
        await asyncio.sleep(0)
        assert not await sessions.scopes("initializing")
        assert len(root.registry.handles()) == baseline
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "open-session:initializing"]
        await competitor.require(SESSIONS).open(request("initializing", tmp_path, plugins=False))
    finally:
        module.release.set()
        await root.close()
        await competitor.close()
        await asyncio.gather(opening, return_exceptions=True)


@pytest.mark.parametrize("optional", [False, True])
async def test_ready_failure_is_attributed_and_optional_owner_isolated(tmp_path, monkeypatch, optional):
    module = tmp_path / "ready_failing_extension.py"
    module.write_text('''def mount_thread(ctx, config):
    ctx.set("ready.partial", "must roll back")
    def fail():
        raise ValueError("ready failed")
    ctx.on("ready", fail)
mount_thread.inject = ()
mount_thread.provide = ("ready.partial",)
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    root = Context()
    await root.start()
    loader = Loader()
    tree = PluginTree([PluginEntry("broken", "ready_failing_extension", optional=optional)])
    scope = loader.scope(root, "ready", kind="thread", tree=tree)
    try:
        if optional:
            await loader.mount(scope, tree, kind="thread")
            assert scope.is_active
        else:
            with pytest.raises(LoadError, match="ready failed"):
                await loader.mount(scope, tree, kind="thread")
        assert not scope.has("ready.partial")
        assert len(root.registry.handles()) == (1 if optional else 0)
    finally:
        await root.close()


async def test_optional_initialization_failure_allows_later_owner_and_final_tool_projection(tmp_path):
    plugins = tmp_path / "extensions"
    for name, body in [
        ("a_failed_init", '''def broken_tool(): return "broken"
def mount_thread(ctx, config):
    ctx.set("ready.partial", object())
    def initialize(event):
        ctx.require(TOOLS).register(Tool.from_function(broken_tool), owner=ctx)
        raise ValueError("optional initialization failed")
    ctx.on(THREAD_INITIALIZED, initialize)
mount_thread.inject = (TOOLS,)
mount_thread.provide = ("ready.partial",)
'''),
        ("z_later_init", '''def later_tool(): return "ready"
def mount_thread(ctx, config):
    def initialize(event):
        ctx.require(TOOLS).register(Tool.from_function(later_tool), owner=ctx)
        ctx.set("ready.later", True)
    ctx.on(THREAD_INITIALIZED, initialize)
mount_thread.inject = (TOOLS,)
mount_thread.provide = ("ready.later",)
''')]:
        implementation = plugins / name / "impl"
        implementation.mkdir(parents=True)
        (implementation / "plugin.py").write_text('''from XBotv2.agentloop.api.contracts import TOOLS
from XBotv2.agentloop.api.events import THREAD_INITIALIZED
from XBotv2.core.api.tools import Tool
''' + body)
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"), plugin_dirs=[plugins])
    try:
        scope = await root.require(SESSIONS).open(request("ready-initialization", tmp_path))
        from XBotv2.agentloop.api.state import LOOP_STATE
        assert scope.require("ready.later") is True
        assert not scope.has("ready.partial")
        assert scope.require(TOOLS).resolve("broken_tool") is None
        assert scope.require(TOOLS).resolve("later_tool") is not None
        selected = scope.require(LOOP_STATE).metadata.value.runtime_selection.enabled_tools
        assert "later_tool" in selected
        assert "broken_tool" not in selected
    finally:
        await root.close()
