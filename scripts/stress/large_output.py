"""One real shell output artifact, streamed over HTTP and checked in bounded chunks."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
import json
import shlex
from pathlib import Path
import time
import tracemalloc
from urllib.parse import quote

import httpx

from .product import disk_sizes, process_io, sample
from .server import LiveMockServerHarness


async def run(args):
    report = {
        "target_bytes": args.bytes,
        "measurement_scope": "one process: XBot, network mock, HTTP client and observer",
        "limitations": [
            "Process RSS and IO include the observer; shell subprocess IO is not included in process IO.",
            "Disk lengths are current logical sizes, not physical write counts.",
        ],
        "samples": [], "errors": [],
    }
    program = (
        "import sys\n"
        "block = b'x' * 65536\n"
        f"remaining = {args.bytes}\n"
        "while remaining:\n"
        "    chunk = block[:min(remaining, len(block))]\n"
        "    sys.stdout.buffer.write(chunk)\n"
        "    remaining -= len(chunk)\n"
    )
    command = "python3 -c " + shlex.quote(program)
    tracemalloc.start()
    start = time.perf_counter()
    report["samples"].append(sample("process_before", None))
    try:
        async with LiveMockServerHarness(
            120, data_dir=args.data_dir, mock_tools=True, tool_steps=1,
            capture_requests=False, chunk_delay_ms=0, shell_command=command,
        ) as harness:
            client = harness.client
            assert client is not None and harness.data_dir is not None
            data_dir = harness.data_dir
            initial_write_bytes = process_io().get("write_bytes")
            report["samples"].append(sample("server_started", data_dir))
            opened = await client.open_session(session_id="large-output", thread_id="main", mode="new")
            await client.send_message("large-output", "main", "Generate a large streamed output.", request_id="large")
            ref = None
            cursor = opened.data.event_cursor
            async def collect():
                nonlocal ref, cursor
                async with aclosing(client.stream_events("large-output", "main", after=cursor)) as stream:
                    async for event in stream:
                        cursor = max(cursor, event.sequence)
                        if event.kind == "tool_completed":
                            outcome = event.payload.get("outcome", {})
                            if outcome.get("kind") != "succeeded":
                                raise RuntimeError(f"shell output failed: {outcome}")
                            artifacts = outcome.get("output", {}).get("artifacts", [])
                            if len(artifacts) != 1:
                                raise RuntimeError("shell output did not return one artifact")
                            ref = artifacts[0]
                        if event.kind == "error" and event.payload.get("code") == "turn_failed":
                            raise RuntimeError(f"turn failed: {event.payload}")
                        if event.kind == "turn_ended":
                            return
                raise RuntimeError("event stream ended before turn completion")
            async def watch():
                while True:
                    await asyncio.sleep(.25)
                    disk = disk_sizes(data_dir)
                    if disk["total"] > args.max_disk_mib * 1024 * 1024:
                        raise RuntimeError(f"disk budget exceeded: {disk}")
                    written = process_io().get("write_bytes")
                    if (initial_write_bytes is not None and written is not None
                        and written - initial_write_bytes > args.max_write_mib * 1024 * 1024):
                        raise RuntimeError("process write budget exceeded")
            work = asyncio.create_task(collect())
            guard = asyncio.create_task(watch())
            try:
                done, _ = await asyncio.wait((work, guard), timeout=120, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError("large-output turn timed out")
                if guard in done:
                    await guard
                await work
            finally:
                work.cancel()
                guard.cancel()
                await asyncio.gather(work, guard, return_exceptions=True)
            if ref is None or ref.get("size") != args.bytes:
                raise RuntimeError(f"unexpected output artifact: {ref}")
            report["samples"].append(sample("artifact_written", data_dir))
            total = 0
            started = time.perf_counter()
            async with httpx.AsyncClient(base_url=harness.base_url, timeout=120) as raw:
                path = "/sessions/large-output/threads/main/artifacts/" + quote(ref["id"], safe="/")
                async with raw.stream("GET", path) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if chunk.count(b"x") != len(chunk):
                            raise RuntimeError("artifact content differs from generated bytes")
                        total += len(chunk)
            if total != args.bytes:
                raise RuntimeError(f"artifact byte count {total} differs from {args.bytes}")
            report.update({"artifact": ref, "verified_bytes": total,
                           "download_ms": (time.perf_counter() - started) * 1000,
                           "provider_requests": harness.provider.request_count,
                           "process_io": process_io()})
            report["samples"].append(sample("artifact_verified", data_dir))
            await client.close_session("large-output")
            report["samples"].append(sample("session_closed", data_dir))
        report["samples"].append(sample("server_closed", data_dir))
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        report["elapsed_ms"] = (time.perf_counter() - start) * 1000
        tracemalloc.stop()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bytes", type=int, default=200 * 1024 * 1024)
    parser.add_argument("--max-disk-mib", type=int, default=512)
    parser.add_argument("--max-write-mib", type=int, default=2048)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.data_dir is not None and args.data_dir.exists() and any(args.data_dir.iterdir()):
        parser.error("data-dir must be empty to isolate this run")
    if args.bytes < 1 or args.max_disk_mib < 1 or args.max_write_mib < 1:
        parser.error("bytes and write budgets must be positive")
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verified_bytes": result.get("verified_bytes"), "errors": result["errors"]}))
    raise SystemExit(bool(result["errors"]))


if __name__ == "__main__":
    main()
