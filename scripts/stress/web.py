"""Real-browser WebUI smoke and interaction probes."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import httpx

from .config import StressConfig
from .models import ScenarioReport


async def run_web(config: StressConfig) -> ScenarioReport:
    """Exercise the running WebUI without importing its implementation."""
    report = ScenarioReport("web.interaction")
    if not config.web_url:
        report.invariants["configured"] = False
        report.errors.append("--web-url is required for the WebUI layer")
        report.finish()
        return report
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        report.errors.append(f"Playwright is unavailable: {exc}")
        report.finish()
        return report

    screenshot_dir = config.report_path.parent / "stress-web" if config.report_path else Path("stress-web")
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    session_id = f"stress-web-{uuid.uuid4().hex[:10]}"
    secondary_session_id = f"stress-web-{uuid.uuid4().hex[:10]}"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=config.timeout,
            follow_redirects=True,
        ) as probe:
            response = await probe.get(config.web_url)
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - report unavailable WebUI cleanly
        report.add_sample(
            "webui.preflight",
            started,
            ok=False,
            error=f"{type(exc).__name__}: {exc or repr(exc)}",
        )
        report.finish()
        return report
    try:
        async with async_playwright() as playwright:
            browser_type = getattr(playwright, config.browser)
            browser = await asyncio.wait_for(
                browser_type.launch(headless=True),
                timeout=config.timeout,
            )
            try:
                context = await browser.new_context(
                    viewport={"width": 390, "height": 844}
                    if config.mobile
                    else {"width": 1440, "height": 960},
                )
                page = await context.new_page()
                await page.goto(config.web_url, wait_until="domcontentloaded", timeout=int(config.timeout * 1000))
                # Vite keeps an HMR websocket open in development mode, so
                # ``networkidle`` is never a stable readiness signal.  The
                # page-level selectors below are the meaningful application
                # readiness checks; allow the initial React mount to settle
                # without waiting for the transport to become idle.
                await page.wait_for_timeout(250)
                report.add_sample("startup", started)
                session_response = await page.request.post(
                    f"{config.web_url.rstrip('/')}/api/sessions",
                    data={"session_id": session_id, "thread_id": "main", "mode": "new"},
                )
                if not session_response.ok:
                    raise RuntimeError(f"session creation returned {session_response.status}")
                await page.get_by_title(session_id).click()
                composer = page.get_by_role("textbox", name="Message XBot")
                await composer.fill("stress browser turn")
                turn_started = time.perf_counter()
                await composer.press("Enter")
                await page.locator("[data-conversation-scroll]").wait_for(
                    state="visible", timeout=int(config.timeout * 1000)
                )
                await page.wait_for_timeout(250)
                report.add_sample("stream_render", turn_started)
                geometry = await page.locator("[data-conversation-scroll]").evaluate(
                    "element => ({scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})"
                )
                report.invariants["no_horizontal_overflow"] = (
                    geometry["scrollWidth"] <= geometry["clientWidth"] + 1
                )
                report.invariants["timeline_visible"] = await page.locator(
                    ".timeline-node"
                ).count() > 0
                await page.locator(".message-block.assistant").first.wait_for(
                    state="visible", timeout=int(config.timeout * 1000)
                )
                report.invariants["assistant_visible"] = await page.locator(
                    ".message-block.assistant"
                ).count() > 0
                first_node_count = await page.locator(".timeline-node").count()

                # A second real turn catches dropped stream deltas and stale
                # draft replacement that a single startup turn cannot expose.
                await composer.fill("stress browser second turn")
                await composer.press("Enter")
                await page.wait_for_function(
                    "previous => document.querySelectorAll('.timeline-node').length > previous",
                    arg=first_node_count,
                    timeout=int(config.timeout * 1000),
                )
                report.invariants["second_turn_rendered"] = (
                    await page.get_by_text("stress browser second turn", exact=True).count()
                    > 0
                )
                requested = {value.strip() for value in config.scenario.split(",")}
                if "stream_failure" in requested or "incomplete" in requested:
                    status_payload = {}
                    thread_status = {}
                    idle_deadline = time.perf_counter() + config.timeout
                    while time.perf_counter() < idle_deadline:
                        status = await page.request.get(
                            f"{config.web_url.rstrip('/')}/api/sessions/{session_id}/threads"
                        )
                        status_payload = await status.json()
                        thread_status = next(
                            (
                                item for item in status_payload.get("threads", [])
                                if item.get("thread_id") == "main"
                            ),
                            {},
                        )
                        if thread_status.get("turn_status") == "idle":
                            break
                        await page.wait_for_timeout(50)
                    report.invariants["failure_followup_rendered"] = (
                        await page.get_by_text(
                            "stress browser second turn", exact=True
                        ).count()
                        > 0
                    )
                    report.invariants["web_turn_not_stuck_running"] = (
                        thread_status.get("turn_status") == "idle"
                    )
                if "long_history" in requested:
                    baseline_nodes = await page.locator(".timeline-node").count()
                    for turn in range(config.history_turns):
                        append_started = time.perf_counter()
                        prompt = f"stress browser long history {turn}"
                        await composer.fill(prompt)
                        await composer.press("Enter")
                        await page.wait_for_function(
                            "previous => document.querySelectorAll('.timeline-node').length > previous",
                            arg=baseline_nodes,
                            timeout=int(config.timeout * 1000),
                        )
                        current_nodes = await page.locator(".timeline-node").count()
                        report.add_sample(
                            "long_history_append",
                            append_started,
                            metadata={
                                "turn": turn + 1,
                                "timeline_nodes": current_nodes,
                            },
                        )
                        baseline_nodes = current_nodes
                    report.invariants["long_history_rendered"] = (
                        await page.get_by_text(
                            f"stress browser long history {config.history_turns - 1}",
                            exact=True,
                        ).count()
                        > 0
                    )
                geometry_nodes = await page.locator("[data-conversation-scroll]").evaluate(
                    """element => [...element.querySelectorAll('.timeline-node')]
                      .map(node => node.getBoundingClientRect())
                      .filter(rect => rect.width > 0 && rect.height > 0)
                      .sort((left, right) => left.top - right.top)"""
                )
                report.invariants["timeline_nodes_do_not_overlap"] = all(
                    current["top"] >= previous["bottom"] - 1
                    for previous, current in zip(geometry_nodes, geometry_nodes[1:])
                )

                # The composer keeps submitted prompts in local history.
                await composer.press("ArrowUp")
                history_value = await composer.input_value()
                report.invariants["input_history_reachable"] = (
                    history_value in {"stress browser turn", "stress browser second turn"}
                )

                # Exercise the same clipboard path used by the real client;
                # this only verifies attachment capture, not model vision.
                await composer.evaluate(
                    """element => {
                      const transfer = new DataTransfer();
                      const encoded = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
                      const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0));
                      transfer.items.add(new File([bytes], 'stress-clipboard.png', {type: 'image/png'}));
                      element.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: transfer}));
                    }"""
                )
                report.invariants["clipboard_attachment_visible"] = await page.get_by_role(
                    "img", name="stress-clipboard.png"
                ).count() > 0
                await page.get_by_role(
                    "button", name="Remove stress-clipboard.png"
                ).click()

                conversation = page.locator("[data-conversation-scroll]")
                geometry_before = await conversation.evaluate(
                    "element => ({height: element.scrollHeight, client: element.clientHeight})"
                )
                if geometry_before["height"] > geometry_before["client"]:
                    await conversation.evaluate(
                        "element => { element.scrollTop = 0; }"
                    )
                    # Trigger the same intent signal as a user's upward wheel
                    # gesture.  A synthetic scroll alone can be coalesced by
                    # Chromium when the element was already at the top.
                    await page.locator(".timeline").evaluate(
                        "element => element.dispatchEvent(new WheelEvent('wheel', {deltaY: -120, bubbles: true}))"
                    )
                    await page.wait_for_timeout(100)
                    await page.get_by_role(
                        "button", name="Jump to latest activity"
                    ).click()
                    await page.wait_for_timeout(150)
                    distance = await conversation.evaluate(
                        "element => element.scrollHeight - element.scrollTop - element.clientHeight"
                    )
                    report.invariants["latest_button_returns_to_bottom"] = distance <= 2
                else:
                    report.invariants["latest_button_returns_to_bottom"] = True
                await page.screenshot(path=str(screenshot_dir / "webui-smoke.png"), full_page=True)

                # Client commands should remain in the same visible activity
                # flow and must not trigger a hidden history rebuild.
                await composer.fill("/help status")
                await composer.press("Enter")
                help_dialog = page.get_by_role("dialog", name="Commands")
                await help_dialog.wait_for(
                    state="visible", timeout=int(config.timeout * 1000)
                )
                status_button = help_dialog.locator("article button").filter(
                    has_text="/status"
                ).first
                await status_button.click()
                await composer.press("Enter")
                await page.get_by_role("region", name="/status result").wait_for(
                    state="visible", timeout=int(config.timeout * 1000)
                )
                report.invariants["status_command_rendered"] = (
                    await page.get_by_role("region", name="/status result").count() > 0
                    if "stream_failure" in requested or "incomplete" in requested
                    else await page.locator(".notice-row").count() == 0
                )

                second_response = await page.request.post(
                    f"{config.web_url.rstrip('/')}/api/sessions",
                    data={"session_id": secondary_session_id, "thread_id": "main", "mode": "new"},
                )
                if not second_response.ok:
                    raise RuntimeError(f"second session creation returned {second_response.status}")
                await page.get_by_title(secondary_session_id).wait_for(
                    state="visible", timeout=int(config.timeout * 1000)
                )
                await page.get_by_title(secondary_session_id).click()
                await page.get_by_title(session_id).click()
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=int(config.timeout * 1000),
                )
                await page.wait_for_timeout(250)
                await page.get_by_title(session_id).click()
                persisted_prompt = page.get_by_text("stress browser turn", exact=True).first
                try:
                    await persisted_prompt.wait_for(
                        state="visible", timeout=int(config.timeout * 1000)
                    )
                except Exception:  # noqa: BLE001 - preserve a failed invariant
                    report.invariants["session_survives_reload"] = False
                else:
                    report.invariants["session_survives_reload"] = True
                await context.close()
            finally:
                await asyncio.wait_for(browser.close(), timeout=config.timeout)
    except Exception as exc:  # noqa: BLE001 - preserve browser evidence in report
        report.add_sample("webui", started, ok=False, error=str(exc))
    finally:
        try:
            async with async_playwright() as playwright:
                request = await playwright.request.new_context(base_url=config.web_url)
                await request.delete(f"/api/sessions/{session_id}")
                await request.delete(f"/api/sessions/{secondary_session_id}")
                await request.dispose()
        except Exception as exc:  # noqa: BLE001 - preserve cleanup evidence
            report.errors.append(f"browser cleanup: {exc}")
    report.finish()
    return report
