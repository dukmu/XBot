"""HTTP/session stress scenarios and event-stream correctness checks."""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from typing import Any

import httpx

from XBotv2.client import XBotClient, XBotClientError
from XBotv2.protocol.models import ServerEvent

from .config import StressConfig
from .models import ScenarioReport


def _session_id(prefix: str) -> str:
    return f"stress-{prefix}-{uuid.uuid4().hex[:10]}"


async def _delete_session(
    client: XBotClient,
    session_id: str,
    report: ScenarioReport,
) -> None:
    """Delete a probe session and keep cleanup failures observable."""
    for attempt in range(10):
        try:
            await client.delete_session(session_id)
            return
        except XBotClientError as exc:
            # The public stream can close just before the runtime releases
            # its turn lock.  Deletion is safe to retry for this one
            # transient state; all other protocol errors remain visible.
            if exc.code != "thread_busy" or attempt == 9:
                report.errors.append(f"cleanup {session_id}: {exc}")
                return
            await asyncio.sleep(0.1)
        except Exception as exc:  # noqa: BLE001 - cleanup is part of the probe
            report.errors.append(f"cleanup {session_id}: {exc}")
            return


async def _collect_events(
    client: XBotClient,
    session_id: str,
    thread_id: str,
    *,
    after: int,
    terminal_count: int = 1,
) -> list[ServerEvent]:
    events: list[ServerEvent] = []
    terminal_events = 0
    async with aclosing(
        client.stream_events(session_id, thread_id, after=after)
    ) as stream:
        async for event in stream:
            events.append(event)
            if event.type in {"turn_finished", "turn_cancelled"}:
                terminal_events += 1
                if terminal_events >= terminal_count:
                    return events
            elif (
                event.type == "error"
                and event.data.get("code") == "turn_failed"
            ):
                return events
    return events


async def _submit_and_collect(
    client: XBotClient,
    session_id: str,
    thread_id: str,
    prompt: str,
    *,
    after: int,
    timeout: float,
    request_id: str,
    delivery: str = "steer",
    attachments: list[dict[str, Any]] | None = None,
) -> list[ServerEvent]:
    await client.send_message(
        session_id,
        thread_id,
        prompt,
        request_id=request_id,
        delivery=delivery,
        attachments=attachments,
    )
    async with asyncio.timeout(timeout):
        return await _collect_events(
            client,
            session_id,
            thread_id,
            after=after,
        )


def _record_events(report: ScenarioReport, events: list[ServerEvent]) -> Counter[str]:
    counts: Counter[str] = Counter(event.type for event in events)
    for event_type, amount in counts.items():
        report.count(f"event.{event_type}", amount)
    return counts


def _check_turn_events(report: ScenarioReport, events: list[ServerEvent]) -> None:
    counts = _record_events(report, events)
    sequences = [event.sequence for event in events if event.sequence > 0]
    report.invariants["turn_sequence_strict"] = sequences == sorted(set(sequences))
    report.invariants["turn_sequence_contiguous"] = (
        not sequences
        or sequences == list(range(sequences[0], sequences[0] + len(sequences)))
    )
    request_ids = {event.request_id for event in events if event.request_id}
    report.invariants["turn_request_id_stable"] = len(request_ids) <= 1
    terminal_types = ("turn_finished", "turn_cancelled")
    turn_failed = any(
        event.type == "error" and event.data.get("code") == "turn_failed"
        for event in events
    )
    report.invariants["turn_has_turn_started"] = counts["turn_started"] >= 1
    report.invariants["turn_has_terminal"] = any(
        counts[event_type] >= 1 for event_type in terminal_types
    ) or turn_failed
    if not (
        report.invariants["turn_has_turn_started"]
        and report.invariants["turn_has_terminal"]
    ):
        missing = []
        if not report.invariants["turn_has_turn_started"]:
            missing.append("turn_started")
        if not report.invariants["turn_has_terminal"]:
            missing.append("turn_finished|turn_cancelled|error(code=turn_failed)")
        report.errors.append(
            "turn stream missing required event: " + ", ".join(missing)
        )


async def _send_turn(
    client: XBotClient,
    session_id: str,
    thread_id: str,
    prompt: str,
    report: ScenarioReport,
) -> list[ServerEvent]:
    started = time.perf_counter()
    request_id = f"stress-{uuid.uuid4().hex}"
    try:
        opened = await client.open_session(
            session_id=session_id,
            thread_id=thread_id,
            mode="resume",
        )
        events = await _submit_and_collect(
            client,
            session_id,
            opened.thread_id,
            prompt,
            after=opened.event_cursor,
            timeout=client._timeout,
            request_id=request_id,
        )
        report.add_sample(
            "turn",
            started,
            metadata={"events": len(events)},
        )
        _check_turn_events(report, events)
        return events
    except Exception as exc:  # noqa: BLE001 - report one failed worker, keep load running
        report.add_sample("turn", started, ok=False, error=str(exc))
        return []


async def run_lifecycle(client: XBotClient, config: StressConfig) -> ScenarioReport:
    report = ScenarioReport("server.lifecycle")
    session_id = _session_id("lifecycle")
    started = time.perf_counter()
    try:
        await client.health()
        await client.hello(session_id=session_id, thread_id="main")
        opened = await client.open_session(
            session_id=session_id,
            thread_id="main",
            mode="new",
        )
        await client.list_sessions()
        await client.list_threads(session_id)
        await client.list_messages(session_id, opened.thread_id, limit=20)
        await client.list_trajectory(session_id, opened.thread_id, limit=20)
        report.add_sample("lifecycle", started)
        report.count("sessions_opened")
        report.invariants["session_id_stable"] = opened.session_id == session_id
        resumed = await client.open_session(
            session_id=session_id,
            thread_id=opened.thread_id,
            mode="resume",
        )
        report.invariants["resume_identity_stable"] = resumed.session_id == session_id
    except Exception as exc:  # noqa: BLE001
        report.add_sample("lifecycle", started, ok=False, error=str(exc))
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_streaming(client: XBotClient, config: StressConfig) -> ScenarioReport:
    report = ScenarioReport("server.streaming")
    semaphore = asyncio.Semaphore(config.concurrency)

    async def worker(worker_id: int) -> None:
        async with semaphore:
            session_id = _session_id(f"stream-{worker_id}")
            try:
                opened = await client.open_session(
                    session_id=session_id,
                    thread_id="main",
                    mode="new",
                )
                for turn in range(config.turns):
                    await _send_turn(
                        client,
                        opened.session_id,
                        opened.thread_id,
                        f"stress worker {worker_id} turn {turn}",
                        report,
                    )
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"worker-{worker_id}: {exc}")
            finally:
                await _delete_session(client, session_id, report)

    await asyncio.gather(*(worker(index) for index in range(config.concurrency)))
    report.count("workers", config.concurrency)
    report.count("turns_requested", config.concurrency * config.turns)
    report.finish()
    return report


async def run_replay(client: XBotClient, config: StressConfig) -> ScenarioReport:
    report = ScenarioReport("server.replay")
    session_id = _session_id("replay")
    try:
        opened = await client.open_session(
            session_id=session_id,
            thread_id="main",
            mode="new",
        )
        cursor = opened.event_cursor
        first_events = await _submit_and_collect(
            client,
            session_id,
            opened.thread_id,
            "replay one",
            after=cursor,
            timeout=config.timeout,
            request_id=f"stress-{uuid.uuid4().hex}",
        )
        _check_turn_events(report, first_events)
        first_sequences = [event.sequence for event in first_events]
        report.invariants["first_sequence_strict"] = first_sequences == sorted(set(first_sequences))
        if first_sequences:
            cursor = first_sequences[-1]

        second_events = await _submit_and_collect(
            client,
            session_id,
            opened.thread_id,
            "replay two",
            after=cursor,
            timeout=config.timeout,
            request_id=f"stress-{uuid.uuid4().hex}",
        )
        _check_turn_events(report, second_events)
        second_sequences = [event.sequence for event in second_events]
        report.invariants["second_sequence_strict"] = second_sequences == sorted(set(second_sequences))
        report.invariants["reconnect_received_events"] = bool(second_events)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_persistence(client: XBotClient, config: StressConfig) -> ScenarioReport:
    report = ScenarioReport("server.persistence")
    session_id = _session_id("persistence")
    forked_session = ""
    try:
        opened = await client.open_session(
            session_id=session_id,
            thread_id="main",
            mode="new",
        )
        for turn in range(max(2, config.turns)):
            await _send_turn(
                client,
                session_id,
                opened.thread_id,
                f"persist turn {turn}",
                report,
            )
        messages = await client.list_messages(session_id, opened.thread_id, limit=500)
        trajectory = await client.list_trajectory(session_id, opened.thread_id, limit=500)
        report.count("messages_loaded", len(messages.messages))
        report.count("trajectory_items_loaded", len(trajectory.items))
        report.invariants["messages_nonempty"] = bool(messages.messages)
        report.invariants["trajectory_nonempty"] = bool(trajectory.items)
        fork = await client.fork_session(session_id)
        forked_session = fork.session_id
        report.count("forks")
        await client.undo_history(session_id, opened.thread_id, count=1)
        report.count("undo_operations")
        await client.clear_history(session_id, opened.thread_id)
        report.count("clear_operations")
        resumed = await client.open_session(
            session_id=session_id,
            thread_id=opened.thread_id,
            mode="resume",
        )
        report.invariants["resume_after_mutation"] = resumed.session_id == session_id
        report.invariants["mutation_completed"] = True
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    finally:
        for value in (session_id, forked_session):
            if value:
                await _delete_session(client, value, report)
    report.finish()
    return report


async def run_payload(client: XBotClient, config: StressConfig) -> ScenarioReport:
    """Exercise long UTF-8 input and an attachment through the public API."""
    report = ScenarioReport("server.payload")
    session_id = _session_id("payload")
    content = ("压测 payload / markdown **content** ✓ " *
               max(1, config.payload_size // 34))[:config.payload_size]
    try:
        opened = await client.open_session(
            session_id=session_id,
            thread_id="main",
            mode="new",
        )
        payload = base64.b64encode(b"xbot stress attachment\n" * 32).decode()
        events = await _submit_and_collect(
            client,
            session_id,
            opened.thread_id,
            content,
            after=opened.event_cursor,
            timeout=config.timeout,
            request_id=f"stress-{uuid.uuid4().hex}",
            attachments=[
                {
                    "data": payload,
                    "media_type": "text/plain",
                    "name": "stress.txt",
                }
            ],
        )
        _check_turn_events(report, events)
        history = await client.list_messages(session_id, opened.thread_id, limit=500)
        report.count("input_chars", len(content))
        report.count("attachment_base64_chars", len(payload))
        report.invariants["history_returned"] = bool(history.messages)
        report.invariants["utf8_request_completed"] = not report.errors
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_delivery(client: XBotClient, config: StressConfig) -> ScenarioReport:
    """Exercise queue insertion while another turn is active."""
    report = ScenarioReport("server.delivery")
    session_id = _session_id("delivery")
    try:
        opened = await client.open_session(
            session_id=session_id,
            thread_id="main",
            mode="new",
        )
        cursor = opened.event_cursor
        first_request = f"stress-{uuid.uuid4().hex}"
        second_request = f"stress-{uuid.uuid4().hex}"
        await client.send_message(
            session_id,
            opened.thread_id,
            "active turn",
            request_id=first_request,
            delivery="steer",
        )
        queued_started = time.perf_counter()
        await client.send_message(
            session_id,
            opened.thread_id,
            "queued turn",
            request_id=second_request,
            delivery="queue",
        )
        report.add_sample("queue_insert", queued_started)
        async with asyncio.timeout(config.timeout):
            events = await _collect_events(
                client,
                session_id,
                opened.thread_id,
                after=cursor,
                terminal_count=2,
            )
        counts = _record_events(report, events)
        terminal_count = sum(
            counts[event_type] for event_type in ("turn_finished", "turn_cancelled")
        )
        report.invariants["queue_request_completed"] = terminal_count >= 2
        report.invariants["both_requests_terminal"] = terminal_count >= 2
        history = await client.list_messages(session_id, opened.thread_id, limit=500)
        user_contents = {
            message.content
            for message in history.messages
            if message.role == "user"
        }
        report.invariants["both_inputs_persisted"] = {
            "active turn",
            "queued turn",
        }.issubset(user_contents)
        pending = await client.list_pending_inputs(session_id, opened.thread_id)
        report.invariants["queue_endpoint_readable"] = isinstance(pending.items, list)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_same_session_concurrency(
    client: XBotClient,
    config: StressConfig,
    *,
    observe_session_events: bool = False,
) -> ScenarioReport:
    """Exercise one session's single turn driver with concurrent inputs.

    A session owns one active engine driver.  Additional inputs are not a
    second driver: they are routed through the declared ``steer``/``queue``
    delivery policy.  This probe sends both forms and requires a complete
    lifecycle on the authoritative session event stream plus durable history
    for both user messages.
    """
    del observe_session_events
    report = ScenarioReport("server.same_session_concurrency")
    session_id = _session_id("same-session")
    try:
        opened = await client.open_session(
            session_id=session_id,
            thread_id="main",
            mode="new",
        )
        cursor = opened.event_cursor
        first_request = f"stress-{uuid.uuid4().hex}"
        second_request = f"stress-{uuid.uuid4().hex}"
        await client.send_message(
            session_id,
            opened.thread_id,
            "same session primary turn",
            request_id=first_request,
            delivery="steer",
        )
        await client.send_message(
            session_id,
            opened.thread_id,
            "same session concurrent input",
            request_id=second_request,
            delivery="queue",
        )
        async with asyncio.timeout(config.timeout):
            events = await _collect_events(
                client,
                session_id,
                opened.thread_id,
                after=cursor,
                terminal_count=2,
            )
        counts = _record_events(report, events)
        sequences = [event.sequence for event in events if event.sequence > 0]
        terminal_count = sum(
            counts[event_type] for event_type in ("turn_finished", "turn_cancelled")
        )
        report.invariants["shared_sequence_strict"] = sequences == sorted(set(sequences))
        report.invariants["shared_sequence_contiguous"] = (
            not sequences
            or sequences == list(range(sequences[0], sequences[0] + len(sequences)))
        )
        report.count("requests", 2)
        report.invariants["both_requests_terminal"] = terminal_count >= 2
        history = await client.list_messages(session_id, opened.thread_id, limit=500)
        user_contents = {
            message.content
            for message in history.messages
            if message.role == "user"
        }
        report.invariants["both_inputs_persisted"] = {
            "same session primary turn",
            "same session concurrent input",
        }.issubset(user_contents)
        turn_numbers = [
            event.data.get("turn")
            for event in events
            if event.type == "turn_started"
        ]
        report.invariants["turn_numbers_unique"] = len(turn_numbers) == len(
            set(turn_numbers)
        )
        report.invariants["shared_has_turn_started"] = counts["turn_started"] >= 1
        report.invariants["shared_has_turn_finished"] = (
            counts["turn_finished"] >= 1
        )
        report.invariants["shared_has_activity"] = bool(events)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_multi_server(
    clients: list[XBotClient],
    config: StressConfig,
) -> ScenarioReport:
    """Probe ownership when independent servers share one session store.

    A distributed deployment must not create two active engine drivers for the
    same session merely because requests land on different workers.  The
    invariant is based on the public turn number: a queued second request must
    advance the turn, while two independent ``turn=1`` streams are evidence
    of split-brain ownership.  The report keeps both streams and errors so a
    failing deployment is diagnosable instead of being hidden by cleanup.
    """
    report = ScenarioReport("server.multi_server")
    if len(clients) < 2:
        report.invariants["skipped"] = True
        report.invariants["requires_two_servers"] = True
        report.finish()
        return report
    session_id = _session_id("multi-server")
    thread_id = "main"
    try:
        opened = await clients[0].open_session(
            session_id=session_id,
            thread_id=thread_id,
            mode="new",
        )
        # Prime every server's session lookup, then race two independent
        # requests against the shared session.
        resumed = await clients[1].open_session(
            session_id=session_id,
            thread_id=thread_id,
            mode="resume",
        )
        opened_sessions = (opened, resumed)
        cursors = tuple(item.event_cursor for item in opened_sessions)
        first_request = f"stress-{uuid.uuid4().hex}"
        second_request = f"stress-{uuid.uuid4().hex}"
        requests = [
            _submit_and_collect(
                clients[0],
                session_id,
                opened.thread_id,
                "multi server primary",
                after=cursors[0],
                timeout=config.timeout,
                request_id=first_request,
                delivery="steer",
            ),
            _submit_and_collect(
                clients[1],
                session_id,
                opened.thread_id,
                "multi server secondary",
                after=cursors[1],
                timeout=config.timeout,
                request_id=second_request,
                delivery="queue",
            ),
        ]
        streams = await asyncio.gather(*requests)
        for events in streams:
            _record_events(report, events)
        turns = [
            event.data.get("turn")
            for events in streams
            for event in events
            if event.type == "turn_started"
        ]
        report.count("servers", len(clients))
        report.count("requests", len(streams))
        report.invariants["both_requests_terminal"] = all(
            any(
                event.type in {"turn_finished", "turn_cancelled"}
                for event in events
            )
            for events in streams
        )
        report.invariants["single_owner_turn_order"] = len(turns) == len(set(turns))
        history = await clients[0].list_messages(session_id, thread_id, limit=500)
        contents = {
            message.content
            for message in history.messages
            if message.role == "user"
        }
        report.invariants["both_inputs_persisted"] = {
            "multi server primary",
            "multi server secondary",
        }.issubset(contents)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    finally:
        for client in clients:
            await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_tools(client: XBotClient, config: StressConfig) -> ScenarioReport:
    """Verify the complete tool-call -> tool-result -> answer chain."""
    report = ScenarioReport("server.tools")
    session_id = _session_id("tools")
    try:
        opened = await client.open_session(
            session_id=session_id,
            thread_id="main",
            mode="new",
        )
        events = await _submit_and_collect(
            client,
            session_id,
            opened.thread_id,
            "stress tool call",
            after=opened.event_cursor,
            timeout=config.timeout,
            request_id=f"stress-{uuid.uuid4().hex}",
        )
        counts = _record_events(report, events)
        report.invariants["tool_call_started"] = counts["tool_calls_started"] > 0
        report.invariants["tool_result_received"] = counts["tool_result"] > 0
        report.invariants["tool_result_success"] = any(
            event.type == "tool_result" and event.data.get("status") == "success"
            for event in events
        )
        if not report.invariants["tool_call_started"]:
            report.invariants["tool_chain_configured"] = False
            report.invariants["skipped"] = True
        else:
            report.invariants["tool_chain_configured"] = True
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_errors(client: XBotClient, config: StressConfig) -> ScenarioReport:
    """Verify representative errors are observable and classified."""
    report = ScenarioReport("server.errors")
    try:
        started = time.perf_counter()
        try:
            await client.get_session(_session_id("missing"))
        except XBotClientError as exc:  # expected protocol error
            report.add_sample("missing_session", started, metadata={"error_type": type(exc).__name__})
            report.count("expected_errors")
            report.invariants["missing_session_rejected"] = exc.status_code == 404
        else:
            report.add_sample("missing_session", started, ok=False, error="request unexpectedly succeeded")
            report.invariants["missing_session_rejected"] = False
    except Exception as exc:  # noqa: BLE001
        report.errors.append(str(exc))
    report.finish()
    return report


async def run_stream_failure(
    client: XBotClient,
    config: StressConfig,
    *,
    live_sse: bool = True,
) -> ScenarioReport:
    """Break one provider/HTTP stream, then prove the session remains usable."""
    report = ScenarioReport(
        "server.incomplete_http_sse"
        if config.failure_mode == "http_truncate"
        else "server.stream_failure"
    )
    if config.failure_mode == "http_truncate" and not live_sse:
        report.invariants["skipped"] = True
        report.invariants["requires_real_http"] = True
        report.errors.append("incomplete HTTP/SSE requires the live HTTP harness")
        report.finish()
        return report
    session_id = _session_id("stream-failure")
    thread_id = "main"
    try:
        opened = await client.open_session(
            session_id=session_id, thread_id=thread_id, mode="new"
        )
        first_started = time.perf_counter()
        first_events: list[ServerEvent] = []
        first_error = ""
        first_request = f"stress-{uuid.uuid4().hex}"
        if config.failure_mode == "provider":
            await client.send_message(
                session_id,
                opened.thread_id,
                "stress injected stream failure",
                request_id=first_request,
            )
            async with asyncio.timeout(config.timeout):
                first_events = await _collect_events(
                    client,
                    session_id,
                    opened.thread_id,
                    after=opened.event_cursor,
                )
        else:
            try:
                await client.send_message(
                    session_id,
                    opened.thread_id,
                    "stress injected stream failure",
                    request_id=first_request,
                )
                async with asyncio.timeout(config.timeout):
                    first_events = await _collect_events(
                        client,
                        session_id,
                        opened.thread_id,
                        after=opened.event_cursor,
                    )
            except (httpx.HTTPError, ConnectionError) as exc:
                first_error = f"{type(exc).__name__}: {exc}"
        report.add_sample(
            "failed_turn",
            first_started,
            ok=bool(first_events or first_error),
            error=first_error,
            metadata={
                "mode": config.failure_mode,
                "events": len(first_events),
                "transport_error": bool(first_error),
            },
        )
        provider_error_codes = {
            str(event.data.get("code"))
            for event in first_events
            if event.type == "error"
        }
        if config.failure_mode == "provider":
            allowed_error_codes = {"engine_error", "turn_failed"}
            report.invariants["provider_error_observed"] = bool(
                provider_error_codes
            )
            report.invariants["provider_error_code_valid"] = bool(
                provider_error_codes
            ) and provider_error_codes <= allowed_error_codes
        if first_events:
            if config.failure_mode == "provider":
                _check_turn_events(report, first_events)
            else:
                _record_events(report, first_events)
            report.invariants["failure_was_observable"] = (
                bool(provider_error_codes)
                if config.failure_mode == "provider"
                else bool(first_error)
            )
        else:
            report.invariants["failure_was_observable"] = (
                bool(first_error) if config.failure_mode == "http_truncate" else False
            )

        idle_started = time.perf_counter()
        thread = None
        idle_deadline = time.perf_counter() + config.timeout
        while time.perf_counter() < idle_deadline:
            status_after_failure = await client.list_threads(session_id)
            thread = next(
                (item for item in status_after_failure.threads if item.thread_id == thread_id),
                None,
            )
            if thread is not None and thread.turn_status == "idle":
                break
            await asyncio.sleep(0.05)
        report.add_sample(
            "failure_to_idle",
            idle_started,
            ok=thread is not None and thread.turn_status == "idle",
            metadata={"turn_status": thread.turn_status if thread is not None else None},
        )
        report.invariants["turn_not_stuck_running"] = bool(
            thread is not None and thread.turn_status == "idle"
        )
        recovery_started = time.perf_counter()
        recovery_events: list[ServerEvent] = []
        recovery_error = ""
        try:
            recovery_opened = await client.open_session(
                session_id=session_id,
                thread_id=thread_id,
                mode="resume",
            )
            recovery_events = await _submit_and_collect(
                client,
                session_id,
                recovery_opened.thread_id,
                "stress recovery turn after stream failure",
                after=recovery_opened.event_cursor,
                timeout=config.timeout,
                request_id=f"stress-{uuid.uuid4().hex}",
            )
        except Exception as exc:  # noqa: BLE001 - recovery is the invariant
            recovery_error = f"{type(exc).__name__}: {exc}"
        report.add_sample(
            "recovery_turn",
            recovery_started,
            ok=bool(recovery_events),
            error=recovery_error,
            metadata={"events": len(recovery_events)},
        )
        _check_turn_events(report, recovery_events)
        report.invariants["followup_input_usable"] = bool(
            any(
                event.type in {"turn_finished", "turn_cancelled"}
                for event in recovery_events
            )
        )
        status_after_recovery = await client.list_threads(session_id)
        thread = next(
            (item for item in status_after_recovery.threads if item.thread_id == thread_id),
            None,
        )
        report.invariants["recovery_not_stuck_running"] = bool(
            thread is not None and thread.turn_status == "idle"
        )
    except Exception as exc:  # noqa: BLE001 - retain protocol evidence
        report.errors.append(f"stream failure probe: {type(exc).__name__}: {exc}")
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_long_history(
    client: XBotClient,
    config: StressConfig,
    *,
    live_sse: bool = True,
) -> ScenarioReport:
    """Measure append latency and authoritative event delivery as history grows."""
    del live_sse
    report = ScenarioReport("server.long_history")
    session_id = _session_id("long-history")
    thread_id = "main"
    shared_events: list[ServerEvent] = []
    try:
        opened = await client.open_session(
            session_id=session_id, thread_id=thread_id, mode="new"
        )

        cursor = opened.event_cursor
        for turn in range(config.history_turns):
            before = time.perf_counter()
            events = await _submit_and_collect(
                client,
                session_id,
                opened.thread_id,
                f"long history turn {turn} / {config.history_turns}",
                after=cursor,
                timeout=config.timeout,
                request_id=f"stress-{uuid.uuid4().hex}",
            )
            elapsed = time.perf_counter() - before
            _check_turn_events(report, events)
            shared_events.extend(events)
            if events:
                cursor = events[-1].sequence
            history = await client.list_messages(session_id, thread_id, limit=500)
            report.add_sample(
                "append",
                before,
                metadata={
                    "turn": turn + 1,
                    "history_messages": len(history.messages),
                    "history_chars": sum(len(item.content) for item in history.messages),
                    "events": len(events),
                    "elapsed_ms_exact": round(elapsed * 1000, 3),
                },
            )
        messages = await client.list_messages(session_id, thread_id, limit=500)
        trajectory = await client.list_trajectory(session_id, thread_id, limit=500)
        threads = await client.list_threads(session_id)
        report.count("turns", config.history_turns)
        report.count("messages", len(messages.messages))
        report.count("trajectory_items", len(trajectory.items))
        report.count("authoritative_events", len(shared_events))
        report.invariants["history_grew"] = len(messages.messages) >= config.history_turns * 2
        report.invariants["trajectory_grew"] = len(trajectory.items) >= config.history_turns
        report.invariants["api_event_stream_complete"] = (
            sum(item.type == "turn_finished" for item in shared_events)
            >= config.history_turns
        )
        current = next((item for item in threads.threads if item.thread_id == thread_id), None)
        report.invariants["long_session_idle_after_append"] = bool(
            current is not None and current.turn_status == "idle"
        )
    except Exception as exc:  # noqa: BLE001 - latency samples remain in report
        report.errors.append(f"long history probe: {type(exc).__name__}: {exc}")
    finally:
        await _delete_session(client, session_id, report)
    report.finish()
    return report


async def run_llama_cpp(client: XBotClient, config: StressConfig) -> ScenarioReport:
    """Probe an optional remote llama.cpp OpenAI-compatible endpoint.

    This is deliberately independent of the local MockLLM server.  Without a
    configured endpoint the report is an explicit skip; it never presents the
    local mock as evidence about llama.cpp context or compaction behavior.
    """
    del client
    report = ScenarioReport("llama_cpp.context_compact")
    if not config.llama_cpp_url:
        report.invariants["skipped"] = True
        report.invariants["remote_configured"] = False
        report.errors.append(
            "skipped: set --llama-cpp-url or XBOT_LLAMA_CPP_URL to run the remote probe"
        )
        report.finish()
        return report
    model = config.llama_cpp_model
    try:
        async with httpx.AsyncClient(timeout=config.timeout) as probe:
            health_started = time.perf_counter()
            health = await probe.get(f"{config.llama_cpp_url}/health")
            report.add_sample("health", health_started, ok=health.is_success, error=health.text[:200])
            models = await probe.get(f"{config.llama_cpp_url}/v1/models")
            if not models.is_success:
                raise RuntimeError(f"/v1/models returned {models.status_code}: {models.text[:200]}")
            model_items = models.json().get("data", [])
            if not model:
                model = str(model_items[0].get("id", "")) if model_items else ""
            if not model:
                raise RuntimeError("remote /v1/models returned no model id")
            context = "\n".join(
                f"history item {index}: preserve this token xbot-{index:04d}"
                for index in range(max(64, config.history_turns * 4))
            )
            request_started = time.perf_counter()
            response = await probe.post(
                f"{config.llama_cpp_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Answer with OK."},
                        {"role": "user", "content": context},
                    ],
                    "max_tokens": 8,
                    "stream": False,
                },
            )
            report.add_sample(
                "context_request",
                request_started,
                ok=response.is_success,
                error=response.text[:300] if not response.is_success else "",
                metadata={"model": model, "input_chars": len(context)},
            )
            if not response.is_success:
                raise RuntimeError(f"chat completion returned {response.status_code}")
            body = response.json()
            report.invariants["remote_reachable"] = True
            report.invariants["context_request_completed"] = bool(body.get("choices"))
            report.invariants["compact_exercised"] = False
            report.errors.append(
                "limitation: direct llama.cpp probe does not exercise XBot compaction; "
                "configure XBot's remote provider for that integration test"
            )
    except Exception as exc:  # noqa: BLE001 - remote failures are evidence
        report.errors.append(f"llama.cpp probe: {type(exc).__name__}: {exc}")
    report.finish()
    return report


async def run_server_scenarios(
    client: XBotClient,
    config: StressConfig,
    *,
    live_sse: bool = True,
    multi_clients: list[XBotClient] | None = None,
) -> list[ScenarioReport]:
    requested = {value.strip() for value in config.scenario.split(",") if value.strip()}
    if "all" in requested:
        requested = {
            "lifecycle", "streaming", "replay", "persistence",
            "payload", "delivery", "same_session", "multi_server", "errors",
            "tools",
        }
    runners: dict[str, Callable[[XBotClient, StressConfig], Awaitable[ScenarioReport]]] = {
        "lifecycle": run_lifecycle,
        "streaming": run_streaming,
        "replay": run_replay,
        "persistence": run_persistence,
        "payload": run_payload,
        "delivery": run_delivery,
        "same_session": run_same_session_concurrency,
        "errors": run_errors,
        "tools": run_tools,
        "stream_failure": lambda current, current_config: run_stream_failure(
            current, current_config, live_sse=live_sse
        ),
        "incomplete": lambda current, current_config: run_stream_failure(
            current, current_config, live_sse=live_sse
        ),
        "long_history": lambda current, current_config: run_long_history(
            current, current_config, live_sse=live_sse
        ),
        "llama_cpp": run_llama_cpp,
    }
    reports: list[ScenarioReport] = []
    event_stream_scenarios = {
        "streaming",
        "replay",
        "persistence",
        "payload",
        "delivery",
        "same_session",
        "multi_server",
        "tools",
        "stream_failure",
        "incomplete",
        "long_history",
    }
    for name in (
        "lifecycle", "streaming", "replay", "persistence",
        "payload", "delivery", "same_session", "multi_server", "errors",
        "tools",
        "stream_failure", "incomplete", "long_history", "llama_cpp",
    ):
        if name in requested:
            if not live_sse and name in event_stream_scenarios:
                report_name = (
                    "server.incomplete_http_sse"
                    if name == "incomplete" and config.failure_mode == "http_truncate"
                    else f"server.{name}"
                )
                report = ScenarioReport(report_name)
                report.invariants["skipped"] = True
                report.invariants["requires_live_http"] = True
                report.finish()
                reports.append(report)
                continue
            if name == "multi_server":
                if multi_clients is None:
                    report = ScenarioReport("server.multi_server")
                    report.invariants["skipped"] = True
                    report.invariants["requires_multiple_servers"] = True
                    report.finish()
                    reports.append(report)
                else:
                    reports.append(await run_multi_server(multi_clients, config))
                continue
            if name == "same_session":
                reports.append(
                    await run_same_session_concurrency(
                        client,
                        config,
                        observe_session_events=live_sse,
                    )
                )
                continue
            reports.append(await runners[name](client, config))
    unknown = requested - (set(runners) | {"multi_server"})
    if unknown:
        report = ScenarioReport("server.configuration")
        report.errors.append(f"unknown scenarios: {', '.join(sorted(unknown))}")
        report.finish()
        reports.append(report)
    return reports
