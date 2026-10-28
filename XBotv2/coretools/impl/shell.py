"""Shell execution and session-owned background shell jobs.

The foreground ``shell`` tool executes one command and waits for it. Background
shells run as SHELL jobs in the shared JobRegistry through ``ShellRunner``;
``start_shell`` / ``list_shells`` / ``wait_shell`` / ``read_shell`` /
``cancel_shell`` never return bulk output — reading is explicit and bounded.

The plugin builds one Tool set per session. The Tool functions close over the
session's JobRegistry, sandbox policy, and workspace root.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from functools import partial
from typing import Literal
from pydantic import JsonValue
from XBotv2.core.api.artifacts import ArtifactKind, ArtifactRef, ArtifactStorePort
from XBotv2.jobs.api.contracts import (
    JobExecutionError, Job, JobNotFound, JobRegistryClosed, JobsPort, OutputPage,
    WaitResult, parse_job_status,
)
from XBotv2.core.api.parts import TextPart
from XBotv2.core.api.tools import (
    SandboxEscape,
    Tool,
    ToolError,
    ToolFailed,
    ToolOutput,
    ToolSucceeded,
)
from XBotv2.sandbox.api.models import SandboxPort

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


class ShellCommandError(JobExecutionError):
    """Shell failure carrying the process exit code."""

    def __init__(
        self, message: str, *, exit_code: int | None = None, result=None,
    ) -> None:
        super().__init__(message, result=result)
        self.code = "command_failed"
        self.detail = f"exit_code={exit_code}" if exit_code is not None else None


class ShellTimeoutError(JobExecutionError, TimeoutError):
    code = "tool_timeout"

    def __init__(self, seconds: float, *, result=None) -> None:
        super().__init__(f"Shell command timed out after {seconds}s", result=result)


_ESCALATION_JUSTIFICATION_REQUIRED = (
    "sandbox_permissions=require_escalated requires a non-empty justification "
    "explaining why the command must run outside the sandbox"
)


def _sandbox_escape(args) -> SandboxEscape | None:
    if args.get("sandbox_permissions") != "require_escalated":
        return None
    justification = str(args.get("justification") or "").strip()
    reason = "Sandbox escape requires human approval."
    if justification:
        reason = f"Sandbox escape requires human approval: {justification}"
    return SandboxEscape(argument="sandbox_permissions", reason=reason)


@dataclass(frozen=True, slots=True)
class ShellJobSpec:
    command: str
    cwd: str | None
    escalated: bool
    label: str
    timeout_seconds: float = 0
    kind: str = "shell"


@dataclass(frozen=True, slots=True)
class ShellJobResult:
    output_ref: ArtifactRef
    exit_code: int

    def retained_bytes(self) -> int:
        ref = self.output_ref
        return (
            sys.getsizeof(self) + sys.getsizeof(self.exit_code)
            + sys.getsizeof(ref) + sys.getsizeof(ref.__dict__)
            + sum(sys.getsizeof("") + 4 * len(value) for value in (
                ref.id, ref.media_type, ref.name, ref.sha256,
            )) + sys.getsizeof(ref.kind) + sys.getsizeof(ref.size)
        )


class ShellRunner:
    """Runs one background SHELL job through the shared shell executor."""

    def __init__(
        self,
        spec: ShellJobSpec,
        *,
        artifacts: ArtifactStorePort,
        sandbox: SandboxPort | None = None,
        uses=None,
    ) -> None:
        self.sandbox = sandbox
        self.spec = spec
        self.artifacts = artifacts
        self._uses = uses
        self._running = False

    async def run(self, job: Job) -> ShellJobResult:
        self._running = True
        try:
            result = await run_shell_command(
                self.spec.command, cwd=self.spec.cwd,
                sandbox=None if self.spec.escalated else self.sandbox,
                timeout_seconds=self.spec.timeout_seconds, artifacts=self.artifacts,
            )
            if result.exit_code:
                raise ShellCommandError(
                    f"Command failed with exit code {result.exit_code}; output: {result.output_ref.id}",
                    exit_code=result.exit_code, result=result,
                )
            return result
        finally:
            if self._uses is not None:
                await self._uses.aclose()

    async def cancel(self, job: Job) -> None:
        # Running commands reap their process before releasing transferred
        # paths. Pre-start cancellation never enters run's finally block.
        del job
        if not self._running and self._uses is not None:
            await self._uses.aclose()


async def shell(
    command: str,
    cwd: str | None = None,
    background: bool = False,
    name: str | None = None,
    sandbox_permissions: Literal[
        "use_default", "require_escalated"
    ] = "use_default",
    justification: str | None = None,
    timeout_seconds: float | None = None,
    *,
    sandbox: SandboxPort | None = None,
    job_registry: JobsPort,
    artifacts: ArtifactStorePort,
    default_cwd: str | None = None,
    approval_layer: ApprovalLayer = None,
    max_timeout_seconds: float = 120.0,
) -> ToolSucceeded | ToolFailed:
    """Run a shell command in the foreground, or start one in the background.

    Foreground (default) commands must be non-interactive. Short output is
    returned directly; long output includes a bounded preview and complete artifact.
    Tool status follows the final exit code; if a nonzero
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
        timeout_seconds: Command runtime limit in seconds, starting at process
            launch. Foreground: omit to use the configured maximum; must be
            positive and no greater than that maximum. Background: omit or
            use 0 for unlimited runtime; positive values have no foreground cap.
    """
    effective_timeout = (
        (0 if background else max_timeout_seconds)
        if timeout_seconds is None else timeout_seconds
    )
    if (
        not math.isfinite(effective_timeout)
        or effective_timeout < 0
        or (not background and not 0 < effective_timeout <= max_timeout_seconds)
    ):
        return _failure(
            "invalid_arguments",
            "Background timeout_seconds must be finite and non-negative"
            if background else (
                f"Foreground timeout_seconds must be > 0 and <= {max_timeout_seconds}s"
            ),
        )
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
            timeout_seconds=effective_timeout,
        )
    active_sandbox = (
        None if sandbox_permissions == "require_escalated" else sandbox
    )
    try:
        async with _capture_shell_command(
            command, artifacts=artifacts, cwd=cwd, sandbox=active_sandbox,
            timeout_seconds=effective_timeout,
        ) as (path, exit_code, timed_out):
            if path.stat().st_size <= 8000:
                text = path.read_bytes().decode("utf-8", errors="replace") or "(no output)"
                output = ToolOutput(parts=(TextPart(text=text),))
            else:
                ref = await _adopt_output(artifacts, path)
                preview = await _preview(artifacts, ref)
                output = ToolOutput(parts=(TextPart(text=preview),), artifacts=(ref,))
    except Exception as exc:
        return _failure("command_failed", str(exc))
    if timed_out:
        exc = ShellTimeoutError(effective_timeout)
        return ToolFailed(error=ToolError(code=exc.code, message=str(exc)), output=output)
    if exit_code:
        return ToolFailed(error=ToolError(code="command_failed", message=(
            f"Command failed with exit code {exit_code}"
        )), output=output)
    return ToolSucceeded(output=output)


async def start_shell(
    command: str,
    cwd: str | None = None,
    name: str | None = None,
    sandbox_permissions: Literal[
        "use_default", "require_escalated"
    ] = "use_default",
    justification: str | None = None,
    timeout_seconds: float = 0,
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
        timeout_seconds: Runtime limit in seconds; 0 is unlimited.
    """
    if not command.strip():
        return _failure("invalid_command", "Command cannot be empty")
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        return _failure("invalid_arguments", "timeout_seconds must be finite and non-negative")
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
            timeout_seconds=timeout_seconds,
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
    uses = await artifacts.handoff()
    try:
        job_registry.start(
            job.id,
            ShellRunner(spec, artifacts=artifacts, sandbox=runner_sandbox, uses=uses),
        )
    except BaseException:
        await uses.aclose()
        raise
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
    return _success(json.dumps(payload, ensure_ascii=False))


async def wait_shell(
    ids: list[str] | None = None,
    mode: Literal["all", "any"] = "any",
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
    try:
        result = await job_registry.wait(
            ids or [],
            mode=mode,
            timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
            kind="shell",
        )
    except JobNotFound:
        return _failure("shell_not_found", "Unknown shell job id")
    payload = _wait_payload(result)
    artifacts = tuple(
        output.output_ref
        for _, output in result.results
        if isinstance(output, ShellJobResult)
    )
    return ToolSucceeded(output=ToolOutput(
        parts=(TextPart(text=json.dumps(payload, ensure_ascii=False)),),
        artifacts=artifacts,
    ))


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
        cursor: Byte offset to start reading from.
        max_bytes: Maximum raw bytes to read (1..65536; default 8000).
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
    if not 1 <= max_bytes <= 65536 or (cursor is not None and cursor < 0):
        return _failure(
            "invalid_arguments", "cursor must be non-negative; max_bytes must be 1..65536",
        )
    start = min(cursor or 0, result.output_ref.size)
    data = b"".join([
        chunk async for chunk in artifacts.read_chunks(
            result.output_ref, offset=start, limit=max_bytes,
        )
    ])
    end = start + len(data)
    page = OutputPage(
        data=data.decode("utf-8", errors="replace"),
        next_cursor=end if end < result.output_ref.size else None,
        eof=end >= result.output_ref.size,
        truncated=end < result.output_ref.size,
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
    *,
    max_timeout_seconds: float = 120.0,
) -> tuple[Tool, ...]:
    """Build the shell Tools for one session's runtime services.

    The shell tool declares its sandbox-escape predicate;
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
            "max_timeout_seconds": max_timeout_seconds,
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
    tools[0].parameters["properties"]["timeout_seconds"] = {
        "anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}],
        "description": (
            f"Foreground: > 0, <= {max_timeout_seconds}s; omitted/null uses this maximum. "
            "Background: 0 or omitted/null is unlimited."
        ),
    }
    # Owners declare the model-facing category (all shell tools execute) and
    # the arguments that define a session grant's scope.
    return tuple(
        replace(
            tool,
            sandbox_escape=_sandbox_escape if tool.name == "shell" else None,
            kind="execute",
            grant_selectors=(
                ("command", "cwd", "sandbox_permissions")
                if tool.name == "shell"
                else ()
            ),
        )
        for tool in tools
    )


def _wait_payload(result: WaitResult) -> dict[str, JsonValue]:
    ready: list[dict[str, JsonValue]] = []
    results = dict(result.results)
    for summary in result.ready:
        item = summary.model_dump(mode="json", exclude_none=True)
        if summary.kind == "shell":
            output = results.get(summary.id)
            if isinstance(output, ShellJobResult):
                item["exit_code"] = output.exit_code
                item["output_artifact_id"] = output.output_ref.id
        ready.append(item)
    return {
        "ready": ready,
        "pending": list(result.pending),
        "timed_out": result.timed_out,
        "reason": result.reason,
    }


@asynccontextmanager
async def _capture_shell_command(command, *, artifacts, cwd, sandbox, timeout_seconds):
    """Keep the spool alive until its reaped output has been consumed."""
    deadline = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    async with artifacts.spool_file() as output:
        timed_out = False
        try:
            async with asyncio.timeout(deadline):
                exit_code = await _run_shell_process(command, output, cwd=cwd, sandbox=sandbox)
        except TimeoutError:
            timed_out = True
            exit_code = -signal.SIGKILL
        yield output, exit_code, timed_out


async def _adopt_output(artifacts, output):
    return await artifacts.adopt_file(
        ArtifactKind.TOOL_RESULT, output, media_type="text/plain; charset=utf-8",
        name="Shell output", suffix=".txt",
    )


async def run_shell_command(
    command: str,
    *,
    artifacts: ArtifactStorePort,
    cwd: str | None = None,
    sandbox: SandboxPort | None = None,
    timeout_seconds: float | None = 0,
) -> ShellJobResult:
    """Run a background command and retain its complete output."""
    async with _capture_shell_command(
        command, artifacts=artifacts, cwd=cwd, sandbox=sandbox,
        timeout_seconds=timeout_seconds,
    ) as (output, exit_code, timed_out):
        ref = await _adopt_output(artifacts, output)
        result = ShellJobResult(output_ref=ref, exit_code=exit_code)
        if timed_out:
            raise ShellTimeoutError(timeout_seconds, result=result)
        return result

async def _preview(artifacts, ref):
    head_size, tail_size = 6000, 2000
    head = b"".join([chunk async for chunk in artifacts.read_chunks(ref, limit=head_size)])
    if ref.size <= head_size:
        return head.decode("utf-8", errors="replace") or "(no output)"
    tail = b"".join([
        chunk async for chunk in artifacts.read_chunks(
            ref, offset=max(head_size, ref.size - tail_size), limit=tail_size,
        )
    ])
    omitted = ref.size - len(head) - len(tail)
    marker = (
        f"\n[… {omitted} bytes omitted; complete output: {ref.id} …]\n"
        if omitted else ""
    )
    return (
        head.decode("utf-8", errors="replace")
        + marker
        + tail.decode("utf-8", errors="replace")
    )


async def _run_shell_process(command, output, *, cwd, sandbox) -> int:
    shell = _default_shell()
    if sandbox is not None and sandbox.enabled:
        return await sandbox.run_shell_to_file(command, output, shell=shell, cwd=cwd)
    with output.open("wb") as writer:
        proc = subprocess.Popen(
            [shell, "/c" if os.name == "nt" else "-lc", command], cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=writer, stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            await _wait_process(proc)
        except BaseException:
            if proc.poll() is None:
                _signal_process(proc)
            await _wait_process(proc)
            raise
    return proc.returncode


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


async def _wait_process(proc: subprocess.Popen[bytes]) -> None:
    while proc.poll() is None:
        await asyncio.sleep(0.05)


__all__ = [
    "shell_tools",
    "ShellCommandError",
    "ShellRunner",
    "run_shell_command",
]
