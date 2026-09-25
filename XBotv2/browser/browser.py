"""Single-page Playwright session for the Browser plugin."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from XBotv2.browser.contracts import BrowserSessionPolicy, NetworkPolicy
from XBotv2.core import (
    ArtifactKind,
    ArtifactStorePort,
    ToolOutcome,
    failed_text,
    succeeded_text,
)
from XBotv2.sandbox.contracts import SandboxPort

from .network import BrowserProxy, network_disabled, validate_url

logger = logging.getLogger("xbotv2.browser")


_SNAPSHOT_SCRIPT = """
() => {
  document.querySelectorAll('[data-xbot-ref]').forEach(
    element => element.removeAttribute('data-xbot-ref')
  );
  const selector = 'a,button,input,textarea,select,[role="button"],[tabindex]';
  const elements = Array.from(document.querySelectorAll(selector))
    .filter(element => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== 'hidden' && style.display !== 'none' &&
        rect.width > 0 && rect.height > 0;
    })
    .map((element, index) => {
      const ref = `e${index + 1}`;
      element.setAttribute('data-xbot-ref', ref);
      return {
        ref,
        tag: element.tagName.toLowerCase(),
        role: element.getAttribute('role') || '',
        text: (element.tagName.toLowerCase() === 'select'
          ? (element.selectedOptions[0]?.textContent || element.value)
          : (element.innerText || element.value || '')).trim().slice(0, 200),
        label: (element.getAttribute('aria-label') ||
          element.getAttribute('placeholder') || '').trim().slice(0, 200)
      };
    });
  return {text: (document.body?.innerText || '').trim(), elements};
}
"""


@dataclass(frozen=True, slots=True)
class _ClickAction:
    ref: str


@dataclass(frozen=True, slots=True)
class _FillAction:
    ref: str
    text: str


@dataclass(frozen=True, slots=True)
class _SelectAction:
    ref: str
    value: str


_BrowserAction = _ClickAction | _FillAction | _SelectAction


class BrowserSession:
    """Own one isolated Chromium context and its active page."""

    def __init__(
        self,
        *,
        network_policy: NetworkPolicy,
        session_policy: BrowserSessionPolicy,
        artifacts: ArtifactStorePort,
        sandbox: SandboxPort,
    ) -> None:
        self.network_policy = network_policy
        self.session_policy = session_policy
        self.artifacts = artifacts
        self.timeout_ms = int(session_policy.timeout * 1000)
        # Bound at construction: the guard route reads it outside any call.
        self._sandbox = sandbox
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._proxy: BrowserProxy | None = None

    @property
    def active(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def open(self, url: str) -> ToolOutcome:
        try:
            if urlsplit(url.strip()).scheme.lower() != "file":
                if not self._sandbox.network:
                    return network_disabled()
            target = await self._target_url(url)
            page = await self._ensure_page()
            await page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
            return await self.snapshot()
        except Exception as exc:
            return failed_text("browser_open_failed", f"Browser open failed: {exc}")

    async def _target_url(self, url: str) -> str:
        stripped = url.strip()
        if urlsplit(stripped).scheme.lower() == "file":
            return self._file_url(stripped)
        checked = await validate_url(stripped, self.network_policy)
        return checked.request_url

    def _file_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "file":
            raise ValueError("URL scheme must be file")
        if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
            raise ValueError("file:// requires an absolute local path")
        raw = unquote(parsed.path)
        target = self._sandbox.resolve_read_path(raw)
        issues = self._sandbox.check_filesystem_access(
            "read_bytes",
            {"path": raw},
        )
        if any(issue.get("decision") != "allow" for issue in issues):
            raise ValueError("File URL is outside the sandbox-approved paths")
        return "file://" + str(target)

    async def snapshot(self) -> ToolOutcome:
        if not self.active:
            return failed_text("browser_not_open", "Open a page first")
        try:
            state = await self._page.evaluate(_SNAPSHOT_SCRIPT)
            title = await self._page.title()
            lines = [
                "[Untrusted rendered Web page]",
                f"Title: {title}",
                f"URL: {self._page.url}",
                "Interactive elements:",
            ]
            for item in state["elements"]:
                description = item["text"] or item["label"]
                role = item["role"] or item["tag"]
                lines.append(f"[{item['ref']}] {role}: {description}")
            lines.extend(["", "Page text:", state["text"]])
            return succeeded_text("\n".join(lines))
        except Exception as exc:
            return failed_text("browser_snapshot_failed", f"Snapshot failed: {exc}")

    async def click(self, ref: str) -> ToolOutcome:
        return await self._act(_ClickAction(ref))

    async def fill(self, ref: str, text: str) -> ToolOutcome:
        return await self._act(_FillAction(ref, text))

    async def press(self, key: str) -> ToolOutcome:
        if not self.active:
            return failed_text("browser_not_open", "Open a page first")
        try:
            await self._page.keyboard.press(key)
            await self._page.wait_for_timeout(200)
            return await self.snapshot()
        except Exception as exc:
            return failed_text("browser_press_failed", f"Key press failed: {exc}")

    async def select(self, ref: str, value: str) -> ToolOutcome:
        return await self._act(_SelectAction(ref, value))

    async def screenshot(self) -> ToolOutcome:
        if not self.active:
            return failed_text("browser_not_open", "Open a page first")
        name = f"screenshot-{time.time_ns()}.png"
        try:
            payload = await self._page.screenshot(full_page=True)
        except Exception as exc:
            return failed_text("browser_screenshot_failed", f"Screenshot failed: {exc}")
        artifact = self.artifacts.put(
            ArtifactKind.BROWSER,
            payload,
            media_type="image/png",
            name=name,
            suffix=".png",
        )
        model_path = self.artifacts.model_path(artifact)
        return succeeded_text(f"Screenshot saved to {model_path}", artifacts=(artifact,))

    async def close(self) -> ToolOutcome:
        was_active = self.active
        try:
            await self.shutdown()
        except Exception as exc:
            return failed_text("browser_close_failed", f"Browser close failed: {exc}")
        return succeeded_text(
            "Browser closed." if was_active else "Browser is already closed.",
        )

    async def shutdown(self) -> None:
        # Teardown must proceed, but a failed close is a real signal: record
        # it instead of discarding it.
        failures: list[str] = []
        for name, resource in (
            ("context", self._context),
            ("browser", self._browser),
            ("proxy", self._proxy),
        ):
            if resource is not None:
                try:
                    await resource.close()
                except Exception as exc:
                    logger.exception(
                        "browser.resource.close.failed resource=%s",
                        name,
                    )
                    failures.append(f"{name}: {exc}")
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.exception("browser.playwright.stop.failed")
                failures.append(f"playwright: {exc}")
        self._page = self._context = self._browser = self._playwright = None
        self._proxy = None
        if failures:
            raise RuntimeError("; ".join(failures))

    async def _ensure_page(self) -> Any:
        if self.active:
            return self._page
        from playwright.async_api import async_playwright

        self._proxy = BrowserProxy(
            self.network_policy,
            network_enabled=self._sandbox.network,
        )
        try:
            proxy_server = await self._proxy.start()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.session_policy.headless,
                proxy={"server": proxy_server, "bypass": "<-loopback>"},
            )
            self._context = await self._browser.new_context(accept_downloads=False)
            await self._context.route("**/*", self._guard_request)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
            return self._page
        except Exception:
            await self.shutdown()
            raise

    async def _guard_request(self, route: Any, request: Any) -> None:
        scheme = urlsplit(request.url).scheme
        if scheme == "file":
            try:
                self._file_url(request.url)
            except Exception:
                await route.abort("blockedbyclient")
                return
            await route.continue_()
            return
        if scheme in {"about", "blob", "data"}:
            await route.continue_()
            return
        if not self._sandbox.network:
            await route.abort("blockedbyclient")
            return
        try:
            await validate_url(request.url, self.network_policy)
        except Exception:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _act(self, action: _BrowserAction) -> ToolOutcome:
        if not self.active:
            return failed_text("browser_not_open", "Open a page first")
        ref = action.ref
        if not ref or not ref.replace("-", "").isalnum():
            return failed_text("invalid_ref", "Element ref is invalid")
        locator = self._page.locator(f'[data-xbot-ref="{ref}"]')
        try:
            if await locator.count() != 1:
                return failed_text(
                    "stale_ref",
                    "Element ref is missing or stale; take a new snapshot",
                )
            match action:
                case _ClickAction():
                    await locator.click()
                case _FillAction(text=text):
                    await locator.fill(text)
                case _SelectAction(value=value):
                    await locator.select_option(value)
            await self._page.wait_for_timeout(200)
            return await self.snapshot()
        except Exception as exc:
            return failed_text("browser_action_failed", f"Browser action failed: {exc}")
