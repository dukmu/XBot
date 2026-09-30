"""Shell deadlines through the production tool dispatch and job lifecycle."""

import asyncio
import sys

import pytest
from pydantic import ValidationError

from XBotv2.agentloop.tool_registry import ToolRegistry
from XBotv2.agentloop.tool_runtime import execute_tools
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import ToolCall, ToolFailed, ToolSucceeded
from XBotv2.coretools.contracts import CoreToolsConfig
from XBotv2.coretools.shell import shell_tools, wait_shell
from XBotv2.jobs.registry import JobRegistry


@pytest.fixture
def runtime(tmp_path):
    jobs = JobRegistry()
    artifacts = ArtifactStore(RuntimePaths.from_data_dir(tmp_path / "data").session("s").thread("t"))
    registry = ToolRegistry()
    for tool in shell_tools(None, jobs, str(tmp_path), artifacts, max_timeout_seconds=0.15):
        registry.register(tool)
    return registry, jobs


async def invoke(registry, **args):
    results = [result async for result in execute_tools(
        [ToolCall(id="test", name="shell", args=args)], registry,
    )]
    return results[0].message.outcome


@pytest.mark.parametrize("timeout", [0, -1, 0.16])
async def test_foreground_invalid_timeout_never_executes(runtime, tmp_path, timeout):
    registry, _ = runtime
    result = await invoke(registry, command="touch must-not-exist", timeout_seconds=timeout)
    assert isinstance(result, ToolFailed)
    assert result.error.code == "invalid_arguments"
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("timeout", [None, 0.05])
async def test_foreground_deadline_kills_command_before_late_write(runtime, tmp_path, timeout):
    registry, _ = runtime
    args = {} if timeout is None else {"timeout_seconds": timeout}
    result = await invoke(registry, command="sleep 0.4; touch late", **args)
    assert isinstance(result, ToolFailed)
    assert result.error.code == "tool_timeout"
    assert result.error.message
    await asyncio.sleep(0.45)
    assert not (tmp_path / "late").exists()


async def test_foreground_accepts_exact_configured_maximum(runtime):
    registry, _ = runtime
    result = await invoke(registry, command="printf ok", timeout_seconds=0.15)
    assert isinstance(result, ToolSucceeded)
    assert result.output.parts[0].text == "ok"


async def test_negative_background_timeout_does_not_create_job(runtime):
    registry, jobs = runtime
    result = await invoke(registry, command="printf unexpected", background=True, timeout_seconds=-1)
    assert isinstance(result, ToolFailed)
    assert result.error.code == "invalid_arguments"
    assert not jobs.all()


@pytest.mark.parametrize("timeout", [None, 0, 1])
async def test_background_ignores_foreground_cap_and_wait_timeout(runtime, tmp_path, timeout):
    registry, jobs = runtime
    args = {} if timeout is None else {"timeout_seconds": timeout}
    result = await invoke(registry, command="sleep 0.25; printf finished", background=True, **args)
    assert isinstance(result, ToolSucceeded)
    job = jobs.all()[0]
    try:
        polled = await wait_shell([job.id], timeout_ms=0, job_registry=jobs)
        assert isinstance(polled, ToolSucceeded)
        assert '"timed_out": true' in polled.output.parts[0].text
        await jobs.wait([job.id], timeout=2)
        assert job.status == "succeeded"
    finally:
        await jobs.shutdown()


async def test_background_deadline_is_failed_not_cancelled(runtime, tmp_path):
    registry, jobs = runtime
    await invoke(registry, command="sleep 0.4; touch late", background=True, timeout_seconds=0.05)
    job = jobs.all()[0]
    await jobs.wait([job.id], timeout=2)
    assert job.status == "failed_running"
    assert job.state.error.code == "tool_timeout"
    await asyncio.sleep(0.45)
    assert not (tmp_path / "late").exists()
    await jobs.shutdown()


@pytest.mark.parametrize("config", [
    {"shell_max_timeout_seconds": 0},
    {"tool_timeout_seconds": -1},
    {"tool_timeouts": {"read": 0}},
    {"shell_max_timeout_seconds": float("inf")},
])
def test_timeout_config_requires_positive_finite_values(config):
    with pytest.raises(ValidationError):
        CoreToolsConfig(**config)


async def test_sandbox_backend_has_no_implicit_deadline(tmp_path, monkeypatch):
    from XBotv2.sandbox.bwrap import BubblewrapBackend

    command = [sys.executable, "-c", "import time; time.sleep(0.1); print('ok')"]
    monkeypatch.setattr(BubblewrapBackend, "process_args", lambda *args: command)
    assert (await BubblewrapBackend(tmp_path).run([], [])).strip() == "ok"


async def test_shell_deadline_cancels_sandbox_process_group(tmp_path, monkeypatch):
    from XBotv2.coretools.shell import run_shell_command, ShellTimeoutError
    from XBotv2.sandbox.bwrap import BubblewrapBackend
    from XBotv2.sandbox.policy import SandboxPolicy

    command = [sys.executable, "-c", f"import time; from pathlib import Path; time.sleep(0.4); Path({str(tmp_path / 'late')!r}).touch()"]
    monkeypatch.setattr(BubblewrapBackend, "process_args", lambda *args: command)
    policy = SandboxPolicy(workspace_root=tmp_path)
    with pytest.raises(ShellTimeoutError):
        await run_shell_command("ignored by test backend", sandbox=policy, timeout_seconds=0.05)
    await asyncio.sleep(0.45)
    assert not (tmp_path / "late").exists()


@pytest.mark.parametrize("tool_timeouts,expected", [({}, 0.1), ({"read": 0.05}, 0.05)])
async def test_plugin_configuration_controls_dispatch_deadline(tmp_path, monkeypatch, tool_timeouts, expected):
    from XBotv2.application.app import start_application
    from XBotv2.coretools import filesystem
    from XBotv2.llm.mock import MockLLM

    cancelled = asyncio.Event()

    async def slow_read(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    monkeypatch.setattr(filesystem, "read", slow_read)
    # Preserve the exported tool identity while substituting slow I/O.
    slow_read.__name__ = "read"
    context = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        workspace_root=tmp_path,
        llm_override=MockLLM(responses=[]),
        extra_plugins=[
            {"id": "coretools", "config": {
                "shell_max_timeout_seconds": 0.15,
                "tool_timeout_seconds": 0.1,
                "tool_timeouts": tool_timeouts,
            }},
            {"id": "permissions", "config": {"default_decision": "allow", "rules": []}},
            {"id": "sandbox", "config": {"enabled": False}},
        ],
    )
    try:
        results = await context.tools.execute_all([
            ToolCall(id="read", name="read", args={}),
            ToolCall(id="shell", name="shell", args={"command": "sleep 0.4"}),
        ])
        for result in results:
            assert isinstance(result.message.outcome, ToolFailed)
            assert result.message.outcome.error.code == "tool_timeout"
        assert f"{expected}s" in results[0].message.outcome.error.message
        assert "0.15s" in results[1].message.outcome.error.message
        assert cancelled.is_set()
    finally:
        await context.destroy()
