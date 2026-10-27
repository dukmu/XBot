"""Shell deadlines through the production tool dispatch and job lifecycle."""

from XBotv2.agentloop.api.contracts import TOOLS
import asyncio
import sys
import pytest
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import ToolCall, ToolFailed


async def test_sandbox_backend_has_no_implicit_deadline(tmp_path, monkeypatch):
    from XBotv2.sandbox.impl.bwrap import BubblewrapBackend

    command = [sys.executable, "-c", "import time; time.sleep(0.1); print('ok')"]
    monkeypatch.setattr(BubblewrapBackend, "process_args", lambda *args: command)
    assert (await BubblewrapBackend(tmp_path).run([], [])).strip() == "ok"


async def test_shell_deadline_cancels_sandbox_process_group(tmp_path, monkeypatch):
    from XBotv2.coretools.impl.shell import run_shell_command, ShellTimeoutError
    from XBotv2.sandbox.impl.bwrap import BubblewrapBackend
    from XBotv2.sandbox.impl.policy import SandboxPolicy

    command = [sys.executable, "-c", f"import time; from pathlib import Path; time.sleep(0.4); Path({str(tmp_path / 'late')!r}).touch()"]
    monkeypatch.setattr(BubblewrapBackend, "process_args", lambda *args: command)
    policy = SandboxPolicy(workspace_root=tmp_path)
    with pytest.raises(ShellTimeoutError):
        await run_shell_command("ignored by test backend", sandbox=policy, timeout_seconds=0.05)
    await asyncio.sleep(0.45)
    assert not (tmp_path / "late").exists()


@pytest.mark.parametrize("tool_timeouts,expected", [({}, 0.1), ({"read": 0.05}, 0.05)])
async def test_plugin_configuration_controls_dispatch_deadline(tmp_path, monkeypatch, tool_timeouts, expected):
    from XBotv2.tests.runtime import thread_scope
    from XBotv2.coretools.impl import filesystem
    from XBotv2.llm.impl.mock import MockLLM

    cancelled = asyncio.Event()

    async def slow_read(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    monkeypatch.setattr(filesystem, "read", slow_read)
    # Preserve the exported tool identity while substituting slow I/O.
    slow_read.__name__ = "read"
    context = await thread_scope(
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
        results = await context.require(TOOLS).execute_all([
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
        await context.root.close()
