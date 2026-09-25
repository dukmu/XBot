"""Shell execution and session-owned background shell jobs.

The foreground ``shell`` tool executes one command synchronously. Background
shells run as SHELL jobs in the shared JobRegistry through ``ShellRunner``;
``start_shell`` / ``list_shells`` / ``wait_shell`` / ``read_shell`` /
``cancel_shell`` never return bulk output — reading is explicit and bounded.

The plugin builds one Tool set per session. The Tool functions close over the
session's JobRegistry, sandbox policy, and workspace root.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from functools import partial
from typing import Literal

from pydantic import JsonValue

from XBotv2.core.artifacts import ArtifactKind, ArtifactRef, ArtifactStorePort
from XBotv2.jobs import (
    Job,
    JobNotFound,
    JobRegistryClosed,
    JobsPort,
    OutputPage,
    WaitResult,
    parse_job_status,
)
from XBotv2.core.parts import TextPart
from XBotv2.core.tools import Tool, ToolError, ToolFailed, ToolOutput, ToolSucceeded
from XBotv2.sandbox.contracts import SandboxPort

#: Lazily-resolved approval-layer capability. The shell tool discharges
#: escalation only while an approval layer is active; resolving at call time
#: keeps the check independent of plugin-tree order.
ApprovalLayer = Callable[[], object | None] | object | None


def _resolve_approval_layer(approval_layer: ApprovalLayer) -> object | None:
    if callable(approval_layer):
        return approval_layer()
    return approval_layer


_ESCALATION_UNAVAILABLE = (
    "Sandbox escape requires an active approval layer; none is mounted "
    "(enable the permissions plugin)"
)


def _success(text: str) -> ToolSucceeded:
    return ToolSucceeded(output=ToolOutput(parts=(TextPart(text=text),)))


def _failure(code: str, message: str) -> ToolFailed:
    return ToolFailed(error=ToolError(code=code, message=message), output=ToolOutput())


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


@dataclass(frozen=True, slots=True)
class ShellJobSpec:
    command: str
    cwd: str | None
    escalated: bool
    label: str
    kind: str = "shell"


@dataclass(frozen=True, slots=True)
class ShellJobResult:
    output_ref: ArtifactRef
    exit_code: int


class ShellRunner:
    """Runs one background SHELL job through the shared shell executor."""

    def __init__(
        self,
        spec: ShellJobSpec,
        *,
        artifacts: ArtifactStorePort,
        sandbox: SandboxPort | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.spec = spec
        self.artifacts = artifacts

    async def run(self, job: Job) -> ShellJobResult:
        command = self.spec.command
        cwd = self.spec.cwd
        escalated = self.spec.escalated
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
            raise ShellCommandError(str(exc)) from exc
        output_ref = self.artifacts.put(
            ArtifactKind.TOOL_RESULT,
            text.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            name="shell-output.txt",
            suffix=".txt",
        )
        return ShellJobResult(
            output_ref=output_ref,
            exit_code=0,
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
    sandbox: SandboxPort | None = None,
    job_registry: JobsPort,
    artifacts: ArtifactStorePort,
    default_cwd: str | None = None,
    approval_layer: ApprovalLayer = None,
) -> ToolSucceeded | ToolFailed:
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
            return _failure(
                "invalid_sandbox_request",
                _ESCALATION_JUSTIFICATION_REQUIRED,
            )
        if _resolve_approval_layer(approval_layer) is None:
            # Fail closed: without an approval layer nobody can ever approve
            # this escape, so the command must not run unsandboxed.
            return _failure(
                "sandbox_escape_unavailable",
                _ESCALATION_UNAVAILABLE,
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
            artifacts=artifacts,
            approval_layer=approval_layer,
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
        return _failure("command_failed", str(exc))
    return _success(output)


async def start_shell(
    command: str,
    cwd: str | None = None,
    name: str | None = None,
    sandbox_permissions: Literal[
        "use_default", "require_escalated"
    ] = "use_default",
    justification: str | None = None,
    *,
    sandbox: SandboxPort | None = None,
    job_registry: JobsPort,
    artifacts: ArtifactStorePort,
    approval_layer: ApprovalLayer = None,
) -> ToolSucceeded | ToolFailed:
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
        return _failure("invalid_command", "Command cannot be empty")
    if sandbox_permissions == "require_escalated":
        if not justification or not justification.strip():
            return _failure(
                "invalid_sandbox_request",
                _ESCALATION_JUSTIFICATION_REQUIRED,
            )
        if _resolve_approval_layer(approval_layer) is None:
            return _failure(
                "sandbox_escape_unavailable",
                _ESCALATION_UNAVAILABLE,
            )
    try:
        spec = ShellJobSpec(
            command=command,
            cwd=cwd,
            escalated=sandbox_permissions == "require_escalated",
            label=name or command,
        )
        job = await job_registry.create(
            spec=spec,
            owner="coretools.shell",
            name=name,
        )
    except JobRegistryClosed:
        return _failure("session_closing", "Session is closing")
    runner_sandbox = (
        None if sandbox_permissions == "require_escalated" else sandbox
    )
    job_registry.start(
        job.id,
        ShellRunner(spec, artifacts=artifacts, sandbox=runner_sandbox),
    )
    return _success(
        f"Started {job.id}"
    )


async def list_shells(
    status: str | None = None,
    *,
    job_registry: JobsPort,
) -> ToolSucceeded | ToolFailed:
    """List session-owned background shells with lightweight metadata.

    Never includes command output; use ``read_shell`` for text. Jobs are
    runtime state and do not survive session shutdown.

    Args:
        status: Optional filter: pending, running, completed, failed, cancelled.
    """
    status_filter = parse_job_status(status)
    summaries = job_registry.list(kind="shell", status=status_filter)
    payload = {
        "shells": [
            summary.model_dump(mode="json", exclude_none=True)
            for summary in summaries
        ]
    }
    return _success(
        json.dumps(payload, ensure_ascii=False)
    )


async def wait_shell(
    ids: list[str] | None = None,
    mode: Literal["all", "any"] = "all",
    timeout_ms: int | None = None,
    *,
    job_registry: JobsPort,
) -> ToolSucceeded | ToolFailed:
    """Wait for background shells to reach a terminal state.

    Returns only IDs, statuses, and exit codes — never command output. Use
    ``read_shell`` to inspect captured output after waiting.

    Args:
        ids: Shell job IDs to wait for. Omit to wait for any shell owned by
            this session.
        mode: ``all`` waits for every listed job; ``any`` returns on the first.
        timeout_ms: Optional maximum wait time in milliseconds.
    """
    resolved = ids or [
            job.id for job in job_registry.all() if job.kind == "shell"
    ]
    if not resolved:
        return _failure("shell_not_found", "No shell jobs to wait for")
    try:
        result = await job_registry.wait(
            resolved,
            mode=mode,
            timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
        )
    except JobNotFound:
        return _failure("shell_not_found", "Unknown shell job id")
    payload = _wait_payload(result, job_registry)
    return _success(
        json.dumps(payload, ensure_ascii=False)
    )


async def read_shell(
    id: str,
    stream: Literal["stdout", "stderr", "combined"] = "combined",
    cursor: int | None = None,
    max_bytes: int = 8000,
    *,
    job_registry: JobsPort,
    artifacts: ArtifactStorePort,
) -> ToolSucceeded | ToolFailed:
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
    job = job_registry.get_or_none(id)
    if job is None or job.kind != "shell":
        return _failure("shell_not_found", f"Unknown shell job: {id}")
    result = job.result
    if not isinstance(result, ShellJobResult):
        if job.status in {"queued", "running"}:
            return _failure("shell_not_complete", "Shell output is not available yet")
        return _failure("shell_output_unavailable", "Shell job has no captured output")
    text = artifacts.read(result.output_ref).decode("utf-8")
    start = max(0, min(cursor or 0, len(text)))
    end = min(len(text), start + max_bytes)
    page = OutputPage(
        data=text[start:end],
        next_cursor=end if end < len(text) else None,
        eof=end >= len(text),
        truncated=end < len(text),
    )
    return _success(json.dumps(asdict(page), ensure_ascii=False))


async def cancel_shell(
    id: str,
    *,
    job_registry: JobsPort,
) -> ToolSucceeded | ToolFailed:
    """Cancel one background shell job (idempotent).

    Args:
        id: Shell job ID returned by start_shell.
    """
    job = job_registry.get_or_none(id)
    if job is None or job.kind != "shell":
        return _failure("shell_not_found", f"Unknown shell job: {id}")
    result = await job_registry.cancel(id)
    return _success(
        f"Shell {id} {result.status}"
    )


def shell_tools(
    sandbox: SandboxPort | None,
    job_registry: JobsPort,
    default_cwd: str,
    artifacts: ArtifactStorePort,
    approval_layer: ApprovalLayer = None,
) -> tuple[Tool, ...]:
    """Build the shell Tools for one session's runtime services.

    The shell tool declares ``escapes_sandbox`` (its escalating capability);
    the approval layer is resolved lazily at call time so the mount does not
    depend on plugin-tree order.
    """
    bindings = (
        (shell, {
            "sandbox": sandbox,
            "job_registry": job_registry,
            "default_cwd": default_cwd,
            "artifacts": artifacts,
            "approval_layer": approval_layer,
        }),
        (list_shells, {"job_registry": job_registry}),
        (wait_shell, {"job_registry": job_registry}),
        (read_shell, {"job_registry": job_registry, "artifacts": artifacts}),
        (cancel_shell, {"job_registry": job_registry}),
    )
    tools = tuple(
        replace(
            Tool.from_function(
                function,
                excluded_parameters=frozenset(dependencies),
            ),
            function=partial(function, **dependencies),
        )
        for function, dependencies in bindings
    )
    # Owners declare the model-facing category (all shell tools execute) and
    # the arguments that define a session grant's scope.
    return tuple(
        replace(
            tool,
            escapes_sandbox=tool.name == "shell",
            kind="execute",
            grant_selectors=(
                ("command", "cwd", "sandbox_permissions")
                if tool.name == "shell"
                else ()
            ),
        )
        for tool in tools
    )


def _wait_payload(result: WaitResult, registry: JobsPort) -> dict[str, JsonValue]:
    ready: list[dict[str, JsonValue]] = []
    for summary in result.ready:
        item = summary.model_dump(mode="json", exclude_none=True)
        if summary.kind == "shell":
            job = registry.get_or_none(summary.id)
            if job is not None and isinstance(job.result, ShellJobResult):
                item["exit_code"] = job.result.exit_code
        ready.append(item)
    return {
        "ready": ready,
        "pending": list(result.pending),
        "timed_out": result.timed_out,
    }


async def run_shell_command(
    command: str,
    *,
    cwd: str | None = None,
    sandbox: SandboxPort | None = None,
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
    "shell_tools",
    "ShellCommandError",
    "ShellRunner",
    "run_shell_command",
]
