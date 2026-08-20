"""Shell execution and session-owned background shell jobs.

The foreground ``shell`` tool executes one command synchronously. Background
shells run as SHELL jobs in the shared JobRegistry through ``ShellRunner``;
``start_shell`` / ``list_shells`` / ``wait_shell`` / ``read_shell`` /
``cancel_shell`` never return bulk output — reading is explicit and bounded.

Like the filesystem tools, all shell tools are stateless module-level values;
per-session state (the JobRegistry and the sandbox policy) arrives through
keyword-only injected parameters at invocation time.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
from typing import Any, Literal

from XBotv2.jobs import (
    Job,
    JobKind,
    JobNotFound,
    JobRegistryClosed,
    JobResult,
    JobRunnerContext,
    JobsPort,
    JobStatus,
    WaitResult,
)
from XBotv2.core.tools import Tool, ToolResult


class ShellCommandError(RuntimeError):
    """Shell failure carrying the process exit code."""

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.code = "command_failed"
        self.detail = f"exit_code={exit_code}" if exit_code is not None else None


_ESCALATION_JUSTIFICATION_REQUIRED = (
    "sandbox_permissions=require_escalated requires a non-empty justification "
    "explaining why the command must run outside the sandbox"
)


class ShellRunner:
    """Runs one background SHELL job through the shared shell executor."""

    def __init__(self, *, sandbox: Any = None) -> None:
        self.sandbox = sandbox

    async def run(self, job: Job, ctx: JobRunnerContext) -> JobResult:
        command = str(job.metadata.get("command") or "")
        cwd_value = job.metadata.get("cwd")
        cwd = str(cwd_value) if cwd_value else None
        escalated = bool(job.metadata.get("escalated"))
        output = ctx.outputs.create_text()
        ctx.primary_output = output
        try:
            text = await run_shell_command(
                command,
                cwd=cwd,
                sandbox=None if escalated else self.sandbox,
                timeout_seconds=0,
            )
        except asyncio.CancelledError:
            raise
        except ShellCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - spawn errors are job errors
            await output.write(f"Failed to start command: {exc}\n")
            raise ShellCommandError(str(exc)) from exc
        await output.write(text)
        return JobResult(
            summary="Exited with code 0",
            output_store=output,
            data={"exit_code": 0},
        )

    async def cancel(self, job: Job) -> None:
        # Task cancellation propagates into run_shell_command, which reaps the
        # process group; nothing extra to release here.
        del job


async def shell(
    command: str,
    cwd: str | None = None,
    background: bool = False,
    name: str | None = None,
    sandbox_permissions: Literal[
        "use_default", "require_escalated"
    ] = "use_default",
    justification: str | None = None,
    *,
    sandbox: Any = None,
    job_registry: JobsPort | None = None,
    default_cwd: str | None = None,
) -> ToolResult:
    """Run a shell command in the foreground, or start one in the background.

    Foreground (default) commands must be non-interactive and return their
    output directly. Tool status follows the final exit code; if a nonzero
    exit is an expected result, the command must verify that condition and
    then exit zero.

    With ``background=true`` the command runs as a session-owned job and the
    tool returns only its job ID; use ``wait_shell`` when later work depends
    on completion, ``list_shells`` to inspect it without waiting, and
    ``read_shell`` to read captured output. Starting a background shell does
    not mean its command succeeded; check the returned status and exit code.

    Commands run in the sandbox by default; writes outside it fail read-only.
    When completing the user's request genuinely requires writing outside it,
    request ``sandbox_permissions=require_escalated`` with a justification so
    the human can approve.

    Args:
        command: Complete shell command to execute.
        cwd: Working directory. Defaults to the session workspace root.
        background: Start the command as a background job instead of waiting.
        name: Optional short label for background jobs.
        sandbox_permissions: ``use_default`` runs inside the configured
            sandbox; ``require_escalated`` requests execution outside it.
        justification: Required explanation when requesting escalation.
    """
    cwd = cwd or default_cwd
    if sandbox_permissions == "require_escalated":
        if not justification or not justification.strip():
            return ToolResult.failure(
                "invalid_sandbox_request",
                _ESCALATION_JUSTIFICATION_REQUIRED,
            )
    if background:
        return await start_shell(
            command,
            cwd=cwd,
            name=name,
            sandbox_permissions=sandbox_permissions,
            justification=justification,
            sandbox=sandbox,
            job_registry=job_registry,
        )
    active_sandbox = (
        None if sandbox_permissions == "require_escalated" else sandbox
    )
    try:
        output = await run_shell_command(
            command,
            cwd=cwd,
            sandbox=active_sandbox,
            timeout_seconds=0,
        )
    except Exception as exc:
        return ToolResult.failure("command_failed", str(exc))
    return ToolResult.success(output)


async def start_shell(
    command: str,
    cwd: str | None = None,
    name: str | None = None,
    sandbox_permissions: Literal[
        "use_default", "require_escalated"
    ] = "use_default",
    justification: str | None = None,
    *,
    sandbox: Any = None,
    job_registry: JobsPort | None = None,
) -> ToolResult:
    """Start a shell command in the background and return its job ID.

    The command runs independently of the current turn. Use ``wait_shell``
    when later work depends on completion, ``list_shells`` to inspect it
    without waiting, and ``read_shell`` to read captured output. Starting a
    background shell does not mean its command succeeded; check the returned
    status and exit code.

    Writes outside the sandbox fail read-only. When completing the user's
    request genuinely requires writing outside it, request
    ``sandbox_permissions=require_escalated`` with a justification.

    Args:
        command: Complete shell command to execute.
        cwd: Working directory. Defaults to the session workspace root.
        name: Optional short label for listing and debugging.
        sandbox_permissions: ``use_default`` runs inside the configured
            sandbox; ``require_escalated`` requests execution outside it.
        justification: Required explanation when requesting escalation.
    """
    if not command.strip():
        return ToolResult.failure("invalid_command", "Command cannot be empty")
    if sandbox_permissions == "require_escalated":
        if not justification or not justification.strip():
            return ToolResult.failure(
                "invalid_sandbox_request",
                _ESCALATION_JUSTIFICATION_REQUIRED,
            )
    if job_registry is None:
        return ToolResult.failure(
            "job_registry_unavailable",
            "Background shells require a live session",
        )
    try:
        job = await job_registry.create(
            kind=JobKind.SHELL,
            metadata={
                "command": command,
                "cwd": cwd or "",
                "name": name,
                "escalated": sandbox_permissions == "require_escalated",
            },
            name=name,
        )
    except JobRegistryClosed:
        return ToolResult.failure("session_closing", "Session is closing")
    runner_sandbox = (
        None if sandbox_permissions == "require_escalated" else sandbox
    )
    job_registry.start(job.id, ShellRunner(sandbox=runner_sandbox))
    return ToolResult.success(
        f"Started {job.id}"
    )


async def list_shells(
    status: str | None = None,
    *,
    job_registry: JobsPort | None = None,
) -> ToolResult:
    """List session-owned background shells with lightweight metadata.

    Never includes command output; use ``read_shell`` for text. Jobs are
    runtime state and do not survive session shutdown.

    Args:
        status: Optional filter: pending, running, completed, failed, cancelled.
    """
    if job_registry is None:
        return ToolResult.failure(
            "job_registry_unavailable", "Background shells require a live session"
        )
    status_filter = _parse_status(status)
    summaries = job_registry.list(kind=JobKind.SHELL, status=status_filter)
    payload = {"shells": [summary.to_dict() for summary in summaries]}
    return ToolResult.success(
        json.dumps(payload, ensure_ascii=False)
    )


async def wait_shell(
    ids: list[str] | None = None,
    mode: Literal["all", "any"] = "all",
    timeout_ms: int | None = None,
    *,
    job_registry: JobsPort | None = None,
) -> ToolResult:
    """Wait for background shells to reach a terminal state.

    Returns only IDs, statuses, and exit codes — never command output. Use
    ``read_shell`` to inspect captured output after waiting.

    Args:
        ids: Shell job IDs to wait for. Omit to wait for any shell owned by
            this session.
        mode: ``all`` waits for every listed job; ``any`` returns on the first.
        timeout_ms: Optional maximum wait time in milliseconds.
    """
    if job_registry is None:
        return ToolResult.failure(
            "job_registry_unavailable", "Background shells require a live session"
        )
    resolved = ids or [
        job.id for job in job_registry.all() if job.kind is JobKind.SHELL
    ]
    if not resolved:
        return ToolResult.failure("shell_not_found", "No shell jobs to wait for")
    try:
        result = await job_registry.wait(
            resolved,
            mode=mode,
            timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
        )
    except JobNotFound:
        return ToolResult.failure("shell_not_found", "Unknown shell job id")
    payload = _wait_payload(result, job_registry)
    return ToolResult.success(
        json.dumps(payload, ensure_ascii=False)
    )


async def read_shell(
    id: str,
    stream: Literal["stdout", "stderr", "combined"] = "combined",
    cursor: int | None = None,
    max_bytes: int = 8000,
    *,
    job_registry: JobsPort | None = None,
) -> ToolResult:
    """Read captured output from one background shell job.

    The shell runner captures combined stdout/stderr; ``stream`` selects the
    requested view (only ``combined`` differs from the raw capture today).
    Continue reading by passing the returned ``next_cursor``.

    Args:
        id: Shell job ID returned by start_shell.
        stream: Output stream to read. Defaults to combined output.
        cursor: Character offset to start reading from.
        max_bytes: Maximum characters to return (default 8000).
    """
    del stream
    if job_registry is None:
        return ToolResult.failure(
            "job_registry_unavailable", "Background shells require a live session"
        )
    job = job_registry.get_or_none(id)
    if job is None or job.kind is not JobKind.SHELL:
        return ToolResult.failure("shell_not_found", f"Unknown shell job: {id}")
    store = job.result.output_store if job.result is not None else None
    if store is None:
        return ToolResult.success(
            "No output captured yet"
        )
    chunk = await store.read(cursor=cursor, max_bytes=max_bytes)
    return ToolResult.success(
        chunk.data # TODO: more detailed output structure with next_cursor, etc.
    )


async def cancel_shell(
    id: str,
    *,
    job_registry: JobsPort | None = None,
) -> ToolResult:
    """Cancel one background shell job (idempotent).

    Args:
        id: Shell job ID returned by start_shell.
    """
    if job_registry is None:
        return ToolResult.failure(
            "job_registry_unavailable", "Background shells require a live session"
        )
    job = job_registry.get_or_none(id)
    if job is None or job.kind is not JobKind.SHELL:
        return ToolResult.failure("shell_not_found", f"Unknown shell job: {id}")
    result = await job_registry.cancel(id)
    return ToolResult.success(
        f"Shell {id} {result.status}"
    )


SHELL_TOOLS: tuple[Tool, ...] = (
    Tool.from_function(shell, name="shell"),
    Tool.from_function(list_shells, name="list_shells"),
    Tool.from_function(wait_shell, name="wait_shell"),
    Tool.from_function(read_shell, name="read_shell"),
    Tool.from_function(cancel_shell, name="cancel_shell"),
)


def _wait_payload(result: WaitResult, registry: JobsPort) -> dict[str, Any]:
    ready: list[dict[str, Any]] = []
    for summary in result.ready:
        item = summary.to_dict()
        if summary.kind == JobKind.SHELL.value:
            job = registry.get_or_none(summary.id)
            if job is not None and job.result is not None and "exit_code" in job.result.data:
                item["exit_code"] = job.result.data["exit_code"]
        ready.append(item)
    return {
        "ready": ready,
        "pending": list(result.pending),
        "timed_out": result.timed_out,
    }


def _parse_status(value: str | None) -> JobStatus | None:
    if value is None:
        return None
    try:
        return JobStatus(value)
    except ValueError:
        return None


async def run_shell_command(
    command: str,
    *,
    cwd: str | None = None,
    sandbox=None,
    timeout_seconds: float | None = 0,
) -> str:
    """Run a shell command with cancellation-safe process cleanup."""
    shell = _default_shell()
    if sandbox is not None and sandbox.enabled:
        return await sandbox.run_shell(
            command,
            shell=shell,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    with tempfile.TemporaryFile() as output_file:
        proc = subprocess.Popen(
            [shell, "/c" if os.name == "nt" else "-lc", command],
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
        raise ShellCommandError(
            f"Command failed with exit code {proc.returncode}: {output.strip()}",
            exit_code=proc.returncode,
        )
    return output


def _default_shell() -> str:
    variable = "COMSPEC" if os.name == "nt" else "SHELL"
    shell = os.environ.get(variable)
    if not shell:
        raise RuntimeError(f"{variable} is not set in the XBot process environment")
    return shell


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


__all__ = [
    "SHELL_TOOLS",
    "ShellCommandError",
    "ShellRunner",
    "run_shell_command",
]
