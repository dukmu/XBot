"""Shell deadlines through the production tool dispatch and job lifecycle."""

from XBotv2.agentloop.api.contracts import TOOLS
import asyncio
import sys
import pytest
from pydantic import ValidationError
from XBotv2.agentloop.impl.tool_registry import ToolRegistry
from XBotv2.agentloop.impl.tool_runtime import execute_tools
from XBotv2.persistence.impl.artifacts import ArtifactStore
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import ToolCall, ToolFailed, ToolSucceeded
from XBotv2.coretools.api.models import CoreToolsConfig
from XBotv2.coretools.impl.shell import shell_tools, wait_shell
from XBotv2.coretools.impl.shell import ShellJobResult, ShellJobSpec, ShellRunner
from XBotv2.core.api.artifacts import ArtifactKind, ArtifactRef
from XBotv2.jobs.impl.registry import JobRegistry


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


@pytest.mark.parametrize("command,timeout,expected_error", [
    ("printf short", 0.15, None),
    ("printf short; exit 7", 0.15, "command_failed"),
    ("printf short; sleep 2", 0.1, "tool_timeout"),
])
async def test_short_foreground_output_is_returned_without_artifact(
    runtime, monkeypatch, command, timeout, expected_error,
):
    registry, _ = runtime
    async def unexpected_adoption(*args, **kwargs):
        pytest.fail("short output must not be adopted")

    monkeypatch.setattr(ArtifactStore, "adopt_file", unexpected_adoption)
    result = await invoke(registry, command=command, timeout_seconds=timeout)
    assert result.output.parts[0].text == "short"
    assert not result.output.artifacts
    if expected_error:
        assert isinstance(result, ToolFailed)
        assert result.error.code == expected_error
    else:
        assert isinstance(result, ToolSucceeded)


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


async def test_wait_returns_artifact_even_when_job_is_evicted_after_wait():
    class EvictingRegistry(JobRegistry):
        async def wait(self, ids=None, **kwargs):
            result = await super().wait(ids, **kwargs)
            for view in result.ready:
                self.remove(view.id)
            return result

    ref = ArtifactRef(id="shell-output", kind=ArtifactKind.TOOL_RESULT)

    class Runner:
        async def run(self, job):
            return ShellJobResult(ref, 7)

        async def cancel(self, job):
            pass

    jobs = EvictingRegistry()
    job = await jobs.create(spec=ShellJobSpec("command", None, False, "command"), owner="test")
    jobs.start(job.id, Runner())
    result = await wait_shell([job.id], timeout_ms=1000, job_registry=jobs)
    assert jobs.get_or_none(job.id) is None
    assert result.output.artifacts == (ref,)
    import json
    ready = json.loads(result.output.parts[0].text)["ready"]
    assert ready[0]["output_artifact_id"] == ref.id
    assert ready[0]["exit_code"] == 7
    await jobs.shutdown()


async def test_shell_result_budget_counts_reference_not_artifact_content(tmp_path):
    jobs = JobRegistry(completed_bytes=5_000)
    artifacts = ArtifactStore(RuntimePaths.from_data_dir(tmp_path / "data").session("s").thread("t"))
    spec = ShellJobSpec(f"{sys.executable} -c 'print(\"x\" * 20000)'", None, False, "output")
    job = await jobs.create(spec=spec, owner="test")
    jobs.start(job.id, ShellRunner(spec, artifacts=artifacts))
    waited = await jobs.wait([job.id], timeout=5)
    result = dict(waited.results)[job.id]
    assert jobs.get_or_none(job.id) is job
    assert await artifacts.read(result.output_ref) == b"x" * 20_000 + b"\n"
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
