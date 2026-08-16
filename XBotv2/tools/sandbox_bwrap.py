"""Bubblewrap execution backend for XBotv2 sandboxing.

The backend receives mount specs from SandboxPolicy and enforces
them via bubblewrap process isolation. It does not inspect tool
arguments or make access decisions.
"""

from __future__ import annotations

import asyncio
import os
import signal
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Literal


@dataclass(frozen=True)
class SandboxMountSpec:
    source: Path
    target: Path
    access: Literal["readonly", "readwrite"]
    kind: Literal["file", "dir"]
    mask: bool = False


@dataclass(frozen=True)
class BubblewrapBackend:
    workspace_root: Path
    timeout_seconds: float = 60.0
    network: bool = True

    def process_args(
        self,
        payload: list[str],
        mount_specs: Iterable[SandboxMountSpec],
        cwd: str | None = None,
    ) -> list[str]:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("Sandbox enabled but bubblewrap (bwrap) is not installed")

        return [
            bwrap,
            *_build_args(
                mount_specs,
                self.network,
                cwd or str(self.workspace_root),
            ),
            "--",
            *payload,
        ]

    async def run(
        self,
        payload: list[str],
        mount_specs: Iterable[SandboxMountSpec],
        cwd: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        with (
            tempfile.TemporaryFile() as stdin_file,
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            if stdin is not None:
                stdin_file.write(stdin.encode("utf-8"))
                stdin_file.seek(0)
            proc = subprocess.Popen(
                self.process_args(payload, mount_specs, cwd),
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=os.name == "posix",
            )
            effective_timeout = (
                self.timeout_seconds if timeout_seconds is None else timeout_seconds
            )
            try:
                await _wait_process(proc, effective_timeout)
            except TimeoutError:
                _kill_process_group(proc)
                await _wait_process(proc, None)
                raise RuntimeError(
                    f"Sandbox command timed out after {effective_timeout}s"
                ) from None
            except BaseException:
                if proc.poll() is None:
                    _kill_process_group(proc)
                await _wait_process(proc, None)
                raise
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = _read_output(stdout_file)
            stderr = _read_output(stderr_file)
        if proc.returncode:
            detail = stderr.strip() or stdout.strip() or "no error output"
            raise RuntimeError(f"Sandbox command failed with exit code {proc.returncode}: {detail}")
        return stdout


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
            raise TimeoutError
        await asyncio.sleep(0.05)


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def _read_output(file: BinaryIO) -> str:
    return file.read().decode("utf-8", errors="replace")


def _build_args(
    mount_specs: Iterable[SandboxMountSpec],
    network: bool,
    cwd: str,
) -> list[str]:
    args = [
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--new-session",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--bind", "/tmp", "/tmp",
    ]
    if network:
        # Share the host network namespace so DNS and TCP egress
        # work inside the sandbox. Without this, --unshare-net is
        # the default and any HTTP/curl/etc. fails with
        # ``Connection timed out`` (see issue from session
        # 20260609-170727-7449 where the model spent 12 turns
        # trying to reach the internet before falling back).
        args.append("--share-net")
    else:
        args.append("--unshare-net")

    mounts = sorted(mount_specs, key=lambda mount: len(mount.target.parts))
    for mount in mounts:
        if mount.target == Path("/") and mount.access == "readonly":
            continue
        if mount.mask and mount.kind == "dir":
            args.extend(["--tmpfs", str(mount.target)])
            continue
        bind_flag = "--bind-try" if mount.access == "readwrite" else "--ro-bind-try"
        args.extend([bind_flag, str(mount.source), str(mount.target)])

    args.extend(["--chdir", cwd])
    return args


def backend_available() -> bool:
    return shutil.which("bwrap") is not None
