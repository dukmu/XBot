"""Bubblewrap execution backend for XBotv2 sandboxing.

The backend receives mount specs from SandboxPolicy and enforces
them via bubblewrap process isolation. It does not inspect tool
arguments or make access decisions.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


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
    max_output_chars: int = 100_000

    async def create_process(
        self,
        payload: list[str],
        mount_specs: Iterable[SandboxMountSpec],
        cwd: str | None = None,
    ):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("Sandbox enabled but bubblewrap (bwrap) is not installed")

        args = [bwrap, *_build_args(mount_specs, self.network, cwd or str(self.workspace_root)), "--", *payload]
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def communicate(self, proc, stdin: str | None = None) -> tuple[str, str]:
        if stdin is None and proc.stdin is not None:
            proc.stdin.close()
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin.encode("utf-8") if stdin is not None else None),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Sandbox command timed out after {self.timeout_seconds}s") from None
        return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    async def run(
        self,
        payload: list[str],
        mount_specs: Iterable[SandboxMountSpec],
        cwd: str | None = None,
        stdin: str | None = None,
    ) -> str:
        proc = await self.create_process(payload, mount_specs, cwd=cwd)
        stdout, stderr = await self.communicate(proc, stdin=stdin)
        return _format_result(stdout, stderr, proc.returncode)


def _format_result(stdout: str, stderr: str, returncode: int) -> str:
    payload = {"stdout": stdout, "stderr": stderr, "exit_code": returncode}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_args(mount_specs: Iterable[SandboxMountSpec], network: bool, cwd: str) -> list[str]:
    args = [
        "--die-with-parent",
        "--unshare-user-try",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--new-session",
        "--clearenv",
        "--setenv", "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
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

    args.extend(_system_mount_args())

    mounted_targets = {mount.target for mount in mount_specs}
    synthetic_parent_dirs: set[Path] = set()
    for mount in mount_specs:
        parent_args, parent_dirs = _parent_dirs(mount.target)
        args.extend(parent_args)
        synthetic_parent_dirs.update(parent_dirs)
        if mount.mask and mount.kind == "dir":
            args.extend(["--tmpfs", str(mount.target)])
            continue
        bind_flag = "--bind-try" if mount.access == "readwrite" else "--ro-bind-try"
        args.extend([bind_flag, str(mount.source), str(mount.target)])

    for parent in sorted(synthetic_parent_dirs, key=lambda p: len(p.parts), reverse=True):
        if _can_chmod(parent, mounted_targets):
            args.extend(["--chmod", "0555", str(parent)])

    args.extend(["--chdir", cwd])
    return args


def _system_mount_args() -> list[str]:
    args: list[str] = []
    usr = Path("/usr")
    if usr.exists():
        args.extend(["--ro-bind-try", str(usr), str(usr)])
    for path_str in ["/bin", "/sbin", "/lib", "/lib64"]:
        path = Path(path_str)
        if path.is_symlink():
            args.extend(["--symlink", os.readlink(path_str), path_str])
        elif path.exists():
            args.extend(["--ro-bind", path_str, path_str])
    # DNS, name service switch, and TLS roots must be reachable
    # inside the sandbox. Without these, ``curl example.com``
    # fails with ``Could not resolve host`` even when
    # ``--share-net`` is on. We bind individual files (not
    # ``/etc``) so unrelated host config (e.g. ``/etc/passwd``)
    # stays invisible to the sandbox.
    for path_str in (
        "/etc/resolv.conf",
        "/etc/nsswitch.conf",
        "/etc/hosts",
        "/etc/host.conf",
        "/etc/services",
        "/etc/protocols",
        "/etc/ssl/certs",
        "/etc/ssl/openssl.cnf",
    ):
        path = Path(path_str)
        if path.is_symlink():
            real = os.path.realpath(path_str)
            if os.path.exists(real):
                # Follow the symlink to the real file and bind
                # that at the expected path.  Without this, WSL
                # symlinks like /etc/resolv.conf → /mnt/wsl/…
                # stay dangling inside the sandbox.
                args.extend(["--ro-bind-try", real, path_str])
            else:
                target = os.readlink(path_str)
                args.extend(["--symlink", target, path_str])
        elif path.exists():
            args.extend(["--ro-bind-try", path_str, path_str])
    return args


def _parent_dirs(path: Path) -> tuple[list[str], list[Path]]:
    args = []
    dirs = []
    current = Path("/")
    for part in path.parts[1:-1]:
        current = current / part
        args.extend(["--dir", str(current)])
        dirs.append(current)
    return args, dirs


def _can_chmod(path: Path, mounted_targets: set[Path]) -> bool:
    if path in {Path("/"), Path("/tmp"), Path("/proc"), Path("/dev")}:
        return False
    if _is_under(path, Path("/tmp")):
        return False
    if any(path == target or _is_under(path, target) for target in mounted_targets):
        return False
    return True


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def backend_available() -> bool:
    return shutil.which("bwrap") is not None
