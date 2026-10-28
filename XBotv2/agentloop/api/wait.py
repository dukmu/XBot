"""Wait for owner-managed targets while observing the canonical inbox."""

import asyncio

from XBotv2.agentloop.api.contracts import RuntimeInput
from XBotv2.core.api.domain import InboxTarget


async def wait_for_targets(targets, *, mode, timeout, inbox, completion_event):
    """Targets map identities to (terminal predicate, completion event)."""
    deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
    observer = inbox.subscribe() if inbox is not None else None
    try:
        while True:
            if observer is not None:
                observer.clear()
                if any(
                    item.target is InboxTarget.NEXT_STEP
                    and not (
                        isinstance(item.input, RuntimeInput)
                        and item.input.event == completion_event
                        and item.input.source in targets
                    )
                    for item in inbox.pending
                ):
                    return "steered"
            if not targets:
                return "empty"
            terminal = [done() for done, _ in targets.values()]
            if (all(terminal) if mode == "all" else any(terminal)):
                return "ready"
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining is not None and remaining <= 0:
                return "timeout"
            events = [event for (done, event) in targets.values() if not done()]
            if observer is not None:
                events.append(observer)
            tasks = [asyncio.create_task(event.wait()) for event in events]
            try:
                await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if observer is not None:
            inbox.unsubscribe(observer)
