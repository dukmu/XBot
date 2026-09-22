"""PTY-level TUI probes.

The PTY probe deliberately treats terminal output as evidence, not as a
second implementation of Textual.  It catches startup failures, deadlocks,
ANSI corruption, and command latency while the TUI's own integration tests
remain responsible for widget-level assertions.
"""

from __future__ import annotations

import os
import pty
import select
import shlex
import signal
import time
import uuid
from typing import Any

from .config import StressConfig
from .models import ScenarioReport
from .server import real_client


def _run_pty(
    command: str,
    duration: float,
    inputs: list[str] | None = None,
) -> dict[str, Any]:
    master, slave = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(master)
        os.close(slave)
        os.environ.setdefault("TERM", "xterm-256color")
        os.execvp(shlex.split(command)[0], shlex.split(command))
    os.close(slave)
    os.set_blocking(master, False)
    started = time.perf_counter()
    chunks: list[bytes] = []
    scheduled = list(inputs or [])
    # Give Textual time to finish its initial session/event-stream attach;
    # otherwise the first PTY write can be consumed by startup and the probe
    # would measure terminal timing rather than a submitted turn.
    next_input = started + 2.0
    try:
        os.write(master, b"/help\r")
        os.write(master, b"/status\r")
        deadline = started + duration
        while time.perf_counter() < deadline:
            if scheduled and time.perf_counter() >= next_input:
                value = scheduled.pop(0)
                os.write(master, value.encode("utf-8") + b"\r")
                next_input = time.perf_counter() + 1.5
            remaining = max(0.0, deadline - time.perf_counter())
            readable, _, _ = select.select([master], [], [], min(0.1, remaining))
            if not readable:
                continue
            try:
                chunks.append(os.read(master, 65536))
            except (BlockingIOError, OSError):
                break
    finally:
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        os.close(master)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if waited == pid:
                break
            time.sleep(0.02)
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
    output = b"".join(chunks)
    decoded = output.decode("utf-8", errors="replace")
    return {
        "bytes": len(output),
        "text": decoded,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "scheduled_inputs": len(inputs or []),
        "submitted_inputs": len(inputs or []) - len(scheduled),
    }


async def run_tui(config: StressConfig) -> ScenarioReport:
    report = ScenarioReport("tui.pty")
    requested = {value.strip() for value in config.scenario.split(",")}
    inputs: list[str] = []
    if "stream_failure" in requested or "incomplete" in requested:
        inputs = [
            "stress tui injected stream failure",
            "stress tui recovery after stream failure",
        ]
    elif "long_history" in requested:
        inputs = [f"stress tui long history {index}" for index in range(config.history_turns)]
    probe = None
    probe_session_id = ""
    command = config.tui_command
    started = time.perf_counter()
    try:
        if not command:
            # Give the PTY a known session so post-exit API evidence can prove
            # that input was accepted, persisted, and left the thread idle.
            probe_session_id = f"stress-tui-{uuid.uuid4().hex[:10]}"
            probe = real_client(config.server_urls[0], config.timeout)
            await probe.open_session(
                session_id=probe_session_id,
                thread_id="main",
                mode="new",
            )
            command = (
                f".venv/bin/xbot tui --server {shlex.quote(config.server_urls[0])} "
                f"--session {shlex.quote(probe_session_id)}"
            )
        # pty.fork() must run in the process' main thread on some libc
        # implementations; forking it from asyncio's executor can leave the
        # event loop waiting forever even after the child exits.
        duration = config.duration or max(6.0, 2.0 + len(inputs) * 1.5)
        result = _run_pty(command, duration, inputs)
        text = result["text"]
        report.add_sample(
            "pty_session",
            started,
            metadata={
                "bytes": result["bytes"],
                "command": command,
                "scheduled_inputs": result["scheduled_inputs"],
                "submitted_inputs": result["submitted_inputs"],
                "output_tail": text[-2000:],
                "output_contains_stress": "stress" in text.lower(),
            },
        )
        report.invariants["produced_output"] = result["bytes"] > 0
        report.invariants["no_replacement_character"] = "�" not in text
        if inputs:
            report.invariants["all_inputs_submitted"] = (
                result["submitted_inputs"] == len(inputs)
            )
        if "stream_failure" in requested or "incomplete" in requested:
            if probe is not None:
                history = await probe.list_messages(
                    probe_session_id, "main", limit=500
                )
                user_content = {
                    item.content for item in history.messages if item.role == "user"
                }
                threads = await probe.list_threads(probe_session_id)
                thread = next(
                    (item for item in threads.threads if item.thread_id == "main"),
                    None,
                )
                report.invariants["tui_recovery_input_visible"] = (
                    "stress tui recovery after stream failure" in user_content
                )
                report.samples[-1].metadata["persisted_user_inputs"] = sorted(user_content)
                report.invariants["tui_thread_idle_after_failure"] = bool(
                    thread is not None and thread.turn_status == "idle"
                )
            else:
                report.invariants["tui_recovery_input_visible"] = (
                    "stress tui recovery after stream failure" in text
                )
        if "long_history" in requested:
            if probe is not None:
                history = await probe.list_messages(
                    probe_session_id, "main", limit=500
                )
                user_content = {
                    item.content for item in history.messages if item.role == "user"
                }
                report.invariants["tui_long_history_input_visible"] = (
                    f"stress tui long history {config.history_turns - 1}" in user_content
                )
            else:
                report.invariants["tui_long_history_input_visible"] = (
                    f"stress tui long history {config.history_turns - 1}" in text
                )
    except Exception as exc:  # noqa: BLE001
        report.add_sample("pty_session", started, ok=False, error=str(exc))
    finally:
        if probe is not None:
            if probe_session_id:
                await probe.delete_session(probe_session_id)
            await probe.close()
    report.finish()
    return report
