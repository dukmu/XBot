"""Shell execution tool. Uses session sandbox capabilities when available."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import tempfile

from xbotv2.api.tools import ToolResult
from xbotv2.api.tools import Tool

async def execute_shell(command: str, cwd: str | None = None, *, sandbox=None) -> ToolResult:
    """Execute a short non-interactive shell command and wait for completion.

    Use this for inspection, tests, and commands that should finish within 30
    seconds. Standard error is merged into standard output. A non-zero exit,
    timeout, or sandbox failure returns a structured Tool error. Large output is
    cached by the common Tool-result pipeline and remains available through its
    session-relative artifact path. Use ``background=true`` for long-running
    processes.

    Args:
        command: Complete shell command to execute.
        cwd: Working directory. Defaults to the session workspace root.
    """
    if cwd is None and sandbox is not None:
        cwd = str(sandbox.workspace_root)
    try:
        return ToolResult.success(
            await run_shell_command(
                command, cwd=cwd, sandbox=sandbox, timeout_seconds=30
            )
        )
    except asyncio.TimeoutError:
        return ToolResult.failure("command_timeout", "Command timed out after 30 seconds")
    except Exception as exc:
        return ToolResult.failure("command_failed", str(exc))


async def run_shell_command(
    command: str,
    *,
    cwd: str | None = None,
    sandbox=None,
    timeout_seconds: float | None = 30,
) -> str:
    """Run a shell command with cancellation-safe process cleanup."""
    if sandbox is not None and sandbox.enabled:
        return await sandbox.run_shell(
            command, cwd=cwd, timeout_seconds=timeout_seconds
        )

    with tempfile.TemporaryFile() as output_file:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            await _wait_process(proc, timeout_seconds)
        except BaseException:
            if proc.poll() is None:
                _signal_process(proc)
            await _wait_process(proc, None)
            raise
        output_file.seek(0)
        output = output_file.read().decode("utf-8", errors="replace")
    output = output or "(no output)"
    if proc.returncode:
        raise RuntimeError(
            f"Command failed with exit code {proc.returncode}: {output.strip()}"
        )
    return output


def _signal_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


async def _wait_process(
    proc: subprocess.Popen[bytes], timeout_seconds: float | None
) -> None:
    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + timeout_seconds
        if timeout_seconds is not None and timeout_seconds > 0
        else None
    )
    while proc.poll() is None:
        if deadline is not None and loop.time() >= deadline:
            raise asyncio.TimeoutError
        await asyncio.sleep(0.05)


shell = Tool.from_function(execute_shell, name="shell")
SHELL_TOOLS = [shell]
