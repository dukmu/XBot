"""Bounded-observer product workload against the real HTTP/provider path.

Run with ``python -m scripts.stress.product --short --output /tmp/report.json``.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from contextlib import AsyncExitStack, aclosing
import gc
import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
import statistics
import subprocess
import time
import tracemalloc

import httpx
from xcore.api.context import Context

from .models import percentile
from .resources import snapshot
from .server import LiveMockServerHarness


def disk_sizes(root: Path) -> dict[str, int]:
    totals = {"jsonl": 0, "sqlite": 0, "wal": 0, "other": 0}
    directories = [root]
    while directories:
        directory = directories.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
                    name = entry.name
                    category = "wal" if name.endswith("-wal") else (
                        "sqlite" if name.endswith(".sqlite3") else
                        "jsonl" if name.endswith(".jsonl") else "other"
                    )
                    totals[category] += size
        except FileNotFoundError:
            continue
    totals["total"] = sum(totals.values())
    return totals


def process_io() -> dict[str, int]:
    try:
        return {
            key: int(value) for key, value in
            (line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines())
            if key in {"rchar", "wchar", "read_bytes", "write_bytes"}
        }
    except OSError:
        return {}


def process_details() -> dict:
    details = {"rss_bytes": None, "child_processes": None}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                details["rss_bytes"] = int(line.split()[1]) * 1024
                break
        children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
        details["child_processes"] = len(children)
    except OSError:
        pass
    return details


def source_state() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[2],
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            check=True, capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[2],
        ).stdout
        return {"commit": commit, "tracked_dirty": bool(status.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "tracked_dirty": None}


def sample(label: str, data_dir: Path | None) -> dict:
    current, peak = tracemalloc.get_traced_memory()
    objects = gc.get_objects()
    types = Counter(
        type(obj).__name__ for obj in objects
        if type(obj).__name__ in {
            "Context", "Task", "Future", "Lock", "Event", "ReplaySubscriber",
            "_SessionSubscriber", "_SessionEventSubscription", "ReplaySubscription",
            "InteractionWaiter", "Fiber", "RootFiber",
        }
    )
    active_contexts = sum(
        1 for obj in objects if isinstance(obj, Context) and obj.is_active
    )
    active_subscribers = sum(
        1 for obj in objects
        if type(obj).__name__ == "_SessionSubscriber" and not obj.closed
    )
    del objects
    return {
        "label": label, "process": snapshot(), "process_io": process_io(),
        "process_details": process_details(),
        "tracemalloc_current_bytes": current, "tracemalloc_peak_bytes": peak,
        "tracked_object_counts": dict(types),
        "active_contexts": active_contexts,
        "active_session_subscribers": active_subscribers,
        "asyncio_tasks": sum(not task.done() for task in asyncio.all_tasks()),
        "system_load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "disk": disk_sizes(data_dir) if data_dir is not None else None,
    }


async def consume_until(client, session_id, cursor, terminal, counts, tool_latencies=None):
    async with aclosing(client.stream_events(session_id, "main", after=cursor)) as stream:
        async for event in stream:
            cursor = max(cursor, event.sequence)
            counts[event.kind] += 1
            if event.kind == "tool_completed":
                outcome = event.payload.get("outcome", {})
                if not isinstance(outcome, dict) or outcome.get("kind") != "succeeded":
                    raise RuntimeError(f"tool did not succeed: {outcome}")
                counts["tool_succeeded"] += 1
                call = event.payload.get("call", {})
                name = call.get("name", "") if isinstance(call, dict) else ""
                if name:
                    counts[f"tool.{name}"] += 1
                if name in {"team_publish", "team_latest"}:
                    output = outcome.get("output", {})
                    artifacts = output.get("artifacts", []) if isinstance(output, dict) else []
                    if not artifacts or not all(
                        isinstance(ref, dict) and ref.get("kind") == "context"
                        and str(ref.get("id", "")).startswith("records/")
                        for ref in artifacts
                    ):
                        raise RuntimeError(f"{name} did not return a record-range artifact")
                    counts["team_range_artifacts"] += len(artifacts)
                timing = event.payload.get("timing", {})
                if tool_latencies is not None and isinstance(timing, dict):
                    duration = timing.get("duration_ms")
                    if isinstance(duration, (int, float)):
                        tool_latencies.append(float(duration))
            if event.kind == "error" and event.payload.get("code") == "turn_failed":
                raise RuntimeError(f"turn failed: {event.payload}")
            if event.kind == "compaction_failed":
                raise RuntimeError(f"compaction failed: {event.payload}")
            if event.kind == terminal:
                return cursor
    raise RuntimeError(f"event stream ended before {terminal}")


def allocation(total, workers, index):
    return total // workers + (index < total % workers)


async def run(args):
    turns = 8 if args.short else 512
    compactions = 2 if args.short else 20
    children = 4 if args.short else 64
    n = args.concurrency
    report = {
        "parameters": {"turns": turns, "tool_calls_per_turn": 7,
                       "compactions": compactions, "child_lifecycles": children,
                       "concurrency": n, "slow_sse": args.slow_sse,
                       "max_disk_mib": args.max_disk_mib,
                       "max_write_mib": args.max_write_mib,
                       "context_window_tokens": 32768,
                       "model_max_output_tokens": 4096,
                       "compact_keep_recent_turns": 1},
        "measurement_scope": "one process: XBot server, network mock and stress harness",
        "environment": {"started_utc": datetime.now(timezone.utc).isoformat(),
                        "platform": platform.platform(), "python": platform.python_version(),
                        "pid": os.getpid(), "cwd": os.getcwd(),
                        "cpu_count": os.cpu_count(), "source": source_state()},
        "limitations": [
            "RSS and process IO include the mock provider and harness, not only XBot; "
            "tracemalloc and GC inspection add observer overhead.",
            "Process IO excludes shell subprocess IO; child processes are counted only at sample times.",
            "File sizes are current logical file lengths; process write_bytes is not "
            "attributed per file or device.",
            "Object counts cover Python GC-tracked instances by class name, not all native resources.",
            "The matrix does not include 200 MiB output, PTY or browser; large output is a separate case.",
        ],
        "samples": [], "counters": {}, "errors": [],
    }
    tracemalloc.start()
    events = Counter()
    latencies = deque(maxlen=2048)
    tool_latencies = deque(maxlen=2048)
    startup_latencies = deque(maxlen=16)
    compact_latencies = deque(maxlen=512)
    sessions = [f"product-{i}" for i in range(n)]
    cursors = [0] * n
    task_ids = [[] for _ in range(n)]
    completed_turns = 0
    completed_compactions = 0
    completed_children = 0
    harness = None
    report["samples"].append(sample("process_before", None))
    started = time.perf_counter()
    try:
        async with LiveMockServerHarness(
            60, data_dir=args.data_dir, mock_tools=True, tool_steps=7,
            capture_requests=False, compact_recent=1, chunk_delay_ms=0,
            product_workload=True,
        ) as harness, AsyncExitStack() as cleanup:
            server_start_ms = (time.perf_counter() - started) * 1000
            client = harness.client
            assert client is not None and harness.data_dir is not None
            data_dir = harness.data_dir
            from XBotv2.persistence.api.contracts import PERSISTENCE
            from XBotv2.session.api.replay import REPLAY_POOL
            backend = harness.application.require(PERSISTENCE)
            replay = harness.application.require(REPLAY_POOL)
            report["parameters"].update({
                "record_cache_budget_bytes": backend.record_cache_stats["budget_bytes"],
                "artifact_cache_budget_bytes": backend.artifact_cache.budget,
                "replay_budget_bytes": replay.stats["budget_bytes"],
            })

            initial_write_bytes = process_io().get("write_bytes")
            if initial_write_bytes is None:
                report["limitations"].append("Process write-byte budget unavailable on this platform.")
            started_sample = sample("server_started", data_dir)
            started_sample["record_cache"] = backend.record_cache_stats
            started_sample["event_replay"] = replay.stats
            report["samples"].append(started_sample)
            print(json.dumps({"phase": "server_started", "data_dir": str(data_dir),
                              "concurrency": n}), flush=True)
            observer = await cleanup.enter_async_context(httpx.AsyncClient(
                base_url=harness.base_url, timeout=30,
            ))
            for i, identity in enumerate(sessions):
                opened_at = time.perf_counter()
                opened = await client.open_session(session_id=identity, thread_id="main", mode="new")
                startup_latencies.append((time.perf_counter() - opened_at) * 1000)
                cursors[i] = opened.data.event_cursor
                membership = await observer.put(
                    f"/sessions/{identity}/threads/main/teams/stress/members/main"
                )
                membership.raise_for_status()
                if not membership.json()["changed"]:
                    raise RuntimeError(f"team membership not created for {identity}")
            if args.slow_sse:
                slow_client = await cleanup.enter_async_context(httpx.AsyncClient(timeout=None))
                slow_response = await cleanup.enter_async_context(slow_client.stream(
                    "GET", f"{harness.base_url}/sessions/{sessions[0]}/threads/main/events",
                    params={"after": cursors[0]},
                ))
                slow_response.raise_for_status()
                slow_lines = slow_response.aiter_lines()
                report["slow_sse"] = {
                    "socket_connected": True, "initial_cursor": cursors[0],
                    "initial_event_read": False,
                }
            async def record_stage(label):
                threads = []
                for identity in sessions:
                    thread = await client.get_thread(identity, "main")
                    base = f"/sessions/{identity}/threads/main"
                    todo_response = await observer.get(base + "/todos")
                    todo_response.raise_for_status()
                    board_response = await observer.get(base + "/teams/stress/count")
                    board_response.raise_for_status()
                    todos = todo_response.json()
                    board = board_response.json()
                    threads.append({
                        "session_id": identity, "status": thread.status,
                        "working_messages": thread.message_count,
                        "active_todos": len(todos["tasks"]),
                        "completed_todos": todos["completed_count"],
                        "board_messages": board["count"],
                    })
                value = sample(label, data_dir)
                value["record_cache"] = backend.record_cache_stats
                value["event_replay"] = replay.stats
                value.update({
                    "user_turns_completed": completed_turns,
                    "compactions_committed": completed_compactions,
                    "active_threads": threads,
                })
                report["samples"].append(value)

            await record_stage("sessions_open")
            for i, identity in enumerate(sessions):
                count = allocation(children, n, i)
                for start in range(0, count, 4):
                    batch = [f"worker-{j}" for j in range(start, min(start + 4, count))]
                    for child in batch:
                        await client.open_thread(identity, thread_id=child, parent_thread_id="main", mode="new")
                    for child in batch:
                        result = await client.close_thread(identity, child)
                        if result.status != "closed":
                            raise RuntimeError(f"child {child} did not close")
                        completed_children += 1
                    if completed_children % 8 == 0 or completed_children == children:
                        print(json.dumps({
                            "phase": "children", "completed": completed_children,
                            "target": children, "disk": disk_sizes(data_dir),
                        }), flush=True)
                    if disk_sizes(data_dir)["total"] > args.max_disk_mib * 1024 * 1024:
                        raise RuntimeError("disk budget exceeded during child lifecycles")
            await record_stage("children_closed")
            quotas = [allocation(turns, n, i) for i in range(n)]
            compact_quotas = [allocation(compactions, n, i) for i in range(n)]
            if any(c and t < 2 * c for t, c in zip(quotas, compact_quotas)):
                raise ValueError("too few turns per session for requested compactions")

            async def turn(i, number):
                nonlocal completed_turns, completed_compactions
                started = time.perf_counter()
                prompt = f"Product workload turn {number}"
                stride = 2 if args.short else 8
                if number in (stride, 2 * stride, 3 * stride, 4 * stride):
                    target = task_ids[i][number // stride]
                    prompt += f" complete_task={target} read_board=1"
                await client.send_message(
                    sessions[i], "main", prompt, request_id=f"turn-{i}-{number}",
                )
                cursors[i] = await asyncio.wait_for(
                    consume_until(client, sessions[i], cursors[i], "turn_ended", events, tool_latencies), 60,
                )
                latencies.append((time.perf_counter() - started) * 1000)
                completed_turns += 1
                if number == 1:
                    response = await observer.get(f"/sessions/{sessions[i]}/threads/main/todos")
                    response.raise_for_status()
                    by_subject = {task["subject"]: task["id"] for task in response.json()["tasks"]}
                    task_ids[i] = [by_subject[f"Workload task {item}"] for item in range(1, 6)]
                count = compact_quotas[i]
                if count and any(number == (k * quotas[i] // count) for k in range(1, count + 1)):
                    compact_started = time.perf_counter()
                    result = await client.run_command(sessions[i], "main", raw="/compact")
                    if result.data.status != "ok" or "history" not in result.data.effects:
                        raise RuntimeError(f"compact not committed: {result.data}")
                    cursors[i] = await asyncio.wait_for(
                        consume_until(client, sessions[i], cursors[i], "compaction_completed", events), 60,
                    )
                    completed_compactions += 1
                    compact_latencies.append((time.perf_counter() - compact_started) * 1000)

            async def watch_disk():
                while True:
                    await asyncio.sleep(0.25)
                    disk = disk_sizes(data_dir)
                    if disk["total"] > args.max_disk_mib * 1024 * 1024:
                        raise RuntimeError(f"disk budget exceeded: {disk}")
                    written = process_io().get("write_bytes")
                    if (initial_write_bytes is not None and written is not None
                        and written - initial_write_bytes > args.max_write_mib * 1024 * 1024):
                        raise RuntimeError(f"process write budget exceeded: {written - initial_write_bytes} bytes")

            async def stop_task(task):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            guard = asyncio.create_task(watch_disk(), name="product-disk-guard")
            cleanup.push_async_callback(stop_task, guard)
            waves = max(quotas)
            marks = {max(1, waves * k // 4) for k in range(1, 5)}
            last_progress = time.monotonic()
            for number in range(1, waves + 1):
                work = asyncio.gather(*(turn(i, number) for i in range(n) if number <= quotas[i]))
                done, _ = await asyncio.wait((work, guard), return_when=asyncio.FIRST_COMPLETED)
                if guard in done:
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
                    await guard
                await work
                if args.slow_sse and number == 1:
                    async with asyncio.timeout(10):
                        async for line in slow_lines:
                            if line.startswith("data:"):
                                report["slow_sse"]["initial_event_read"] = True
                                break
                disk = disk_sizes(data_dir)
                if disk["total"] > args.max_disk_mib * 1024 * 1024:
                    raise RuntimeError(f"disk budget exceeded: {disk}")
                if number in marks:
                    await record_stage(f"turn_wave_{number}")
                if time.monotonic() - last_progress >= 20 or number == waves:
                    print(json.dumps({
                        "phase": "progress", "wave": number, "waves": waves,
                        "turns": completed_turns, "compactions": completed_compactions,
                        "agent_requests": harness.provider.agent_requests,
                        "tool_successes": events["tool_succeeded"], "disk": disk,
                        "process_io": process_io(), "process": snapshot(),
                        "process_details": process_details(),
                        "record_cache": backend.record_cache_stats,
                        "event_replay": replay.stats,
                        "tracemalloc_current_bytes": tracemalloc.get_traced_memory()[0],
                    }), flush=True)
                    last_progress = time.monotonic()
            if args.slow_sse:
                async with httpx.AsyncClient(timeout=10) as check:
                    async with check.stream(
                        "GET", f"{harness.base_url}/sessions/{sessions[0]}/threads/main/events",
                        params={"after": report["slow_sse"]["initial_cursor"]},
                    ) as response:
                        report["slow_sse"]["old_cursor_http_status"] = response.status_code
                        if response.status_code == 409:
                            report["slow_sse"]["old_cursor_error"] = (
                                await response.aread()
                            ).decode("utf-8")[:1000]
                if report["slow_sse"]["old_cursor_http_status"] != 409:
                    if not args.short:
                        raise RuntimeError("slow SSE old cursor did not expire")
                    report["limitations"].append(
                        "Slow socket remained open, but old-cursor expiry was not demonstrated."
                    )
            report["counters"] = {
                "user_turns": completed_turns,
                "provider_requests": harness.provider.request_count,
                "agent_model_requests": harness.provider.agent_requests,
                "auxiliary_model_requests": harness.provider.auxiliary_requests,
                "provider_tool_calls": harness.provider.tool_calls,
                "provider_tool_responses": harness.provider.tool_responses,
                "provider_requests_with_runtime_notice": harness.provider.product_notice_requests,
                "compactions_committed": completed_compactions,
                "child_lifecycles_closed": completed_children,
                "events": dict(events),
            }
            report["timing"] = {
                "turn_samples": len(latencies), "turn_p50_ms": percentile(list(latencies), .5),
                "turn_p95_ms": percentile(list(latencies), .95),
                "turn_mean_ms": statistics.mean(latencies) if latencies else None,
                "server_start_ms": server_start_ms,
                "session_open_p50_ms": percentile(list(startup_latencies), .5),
                "session_open_p95_ms": percentile(list(startup_latencies), .95),
                "tool_execution_samples": len(tool_latencies),
                "tool_execution_sampling": "most recent 2048 completed tools",
                "tool_execution_p50_ms": percentile(list(tool_latencies), .5),
                "tool_execution_p95_ms": percentile(list(tool_latencies), .95),
                "compact_samples": len(compact_latencies),
                "compact_p50_ms": percentile(list(compact_latencies), .5),
                "compact_p95_ms": percentile(list(compact_latencies), .95),
            }
            if (completed_turns != turns or harness.provider.agent_requests < turns * 8
                or harness.provider.tool_responses < turns * 7
                or events["tool_succeeded"] < turns * 7):
                raise RuntimeError("workload did not complete the requested model/tool round trips")
            if completed_compactions != compactions or completed_children != children:
                raise RuntimeError("workload did not complete compaction or child targets")
            if (events["tool.task_create"] != n * 5
                or events["tool.task_update"] != n * 4
                or events["tool.team_publish"] != turns
                or events["tool.team_latest"] < n * 4):
                raise RuntimeError("todo/team workload did not meet its targets")
            for i, identity in enumerate(sessions):
                base = f"/sessions/{identity}/threads/main"
                todos_response = await observer.get(base + "/todos")
                todos_response.raise_for_status()
                todos = todos_response.json()
                if (len(todos["tasks"]) != 1 or todos["tasks"][0]["id"] != task_ids[i][0]
                    or todos["tasks"][0]["status"] != "pending"
                    or todos["completed_count"] != 4):
                    raise RuntimeError(f"unexpected todo state for {identity}: {todos}")
                board_response = await observer.get(base + "/teams/stress/count")
                board_response.raise_for_status()
                if board_response.json()["count"] != quotas[i]:
                    raise RuntimeError(f"unexpected board count for {identity}")
            for identity in sessions:
                await client.close_session(identity)
            closed_sample = sample("sessions_closed", data_dir)
            closed_sample["record_cache"] = backend.record_cache_stats
            closed_sample["event_replay"] = replay.stats
            report["samples"].append(closed_sample)
        closed_sample = sample("server_closed", data_dir)
        closed_sample["record_cache"] = backend.record_cache_stats
        closed_sample["event_replay"] = replay.stats
        report["samples"].append(closed_sample)
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        report["counters"].update({"user_turns": completed_turns,
            "compactions_committed": completed_compactions,
            "child_lifecycles_closed": completed_children, "events": dict(events)})
        if harness is not None:
            report["counters"].update({
                "provider_requests": harness.provider.request_count,
                "agent_model_requests": harness.provider.agent_requests,
                "auxiliary_model_requests": harness.provider.auxiliary_requests,
                "provider_tool_calls": harness.provider.tool_calls,
                "provider_tool_responses": harness.provider.tool_responses,
                "provider_requests_with_runtime_notice": harness.provider.product_notice_requests,
            })
    finally:
        tracemalloc.stop()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--short", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=(1, 4, 16), default=1)
    parser.add_argument("--slow-sse", action="store_true")
    parser.add_argument("--max-disk-mib", type=int, default=256)
    parser.add_argument("--max-write-mib", type=int, default=2048)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.data_dir is not None and args.data_dir.exists() and any(args.data_dir.iterdir()):
        parser.error("data-dir must be empty to isolate this run")
    if args.max_disk_mib < 1 or args.max_write_mib < 1:
        parser.error("disk and write budgets must be positive")
    if args.short and args.concurrency != 1:
        parser.error("short mode requires concurrency 1 for its four staged todo completions")
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"counters": report["counters"], "errors": report["errors"]}, ensure_ascii=False))
    raise SystemExit(1 if report["errors"] else 0)


if __name__ == "__main__":
    main()
