"""Browser plugin: live search, page retrieval, and isolated browser control."""

from __future__ import annotations

from dataclasses import replace

from typing import Literal
from pydantic import JsonValue

from XBotv2.core import (
    ArtifactStorePort,
    Tool,
    ToolOutcome,
)
from XBotv2.sandbox.contracts import SandboxPort
from xcore import Context

from .browser import BrowserSession
from .contracts import BrowserConfig
from .network import WebAccess, network_disabled


class BrowserPlugin:
    inject = ['tools', 'session', 'sandbox', 'artifacts']
    name = "browser"
    Config = BrowserConfig

    def __init__(self) -> None:
        self._config = BrowserConfig()
        self._web: WebAccess | None = None
        self._browser: BrowserSession | None = None
        self._artifacts: ArtifactStorePort
        self._sandbox: SandboxPort

    def apply(self, ctx: Context, config: BrowserConfig) -> None:
        self._config = config
        ctx.dispose(self._dispose)
        self._artifacts = ctx.artifacts
        self._sandbox = ctx.sandbox
        for function in (
            self.web_search,
            self.web_fetch,
            self.browser_open,
            self.browser_snapshot,
            self.browser_click,
            self.browser_fill,
            self.browser_press,
            self.browser_select,
            self.browser_screenshot,
            self.browser_close,
        ):
            # Owners declare the model-facing category (web access).
            ctx.tools.register(replace(Tool.from_function(function), kind="fetch"))

    async def web_search(
        self,
        query: str,
        max_results: int = 5,
        freshness: Literal["day", "week", "month", "year"] | None = None,
    ) -> ToolOutcome:
        """Search the live public Web and return concise source results.

        The configured search backend returns structured titles, URLs,
        snippets, and optional dates.

        Args:
            query: Focused search query, including site: filters when useful.
            max_results: Number of results from 1 to 10; defaults to 5.
            freshness: Optional day, week, month, or year recency filter.
        """
        if not self._sandbox.network:
            return network_disabled()
        return await self._web_access().search(
            query,
            max_results=max_results,
            freshness=freshness,
            backend=self._config.search.backend,
            region=self._config.search.region,
            safesearch=self._config.search.safesearch,
        )

    async def web_fetch(self, url: str) -> ToolOutcome:
        """Fetch one public URL and extract readable content with source metadata.

        HTML is reduced to Markdown; JSON and text remain textual. Fetches are
        bounded by timeout and byte size, and redirect targets are checked
        against the same URL policy.

        Args:
            url: Absolute public http or https URL without embedded credentials.
        """
        if not self._sandbox.network:
            return network_disabled()
        return await self._web_access().fetch(url)

    async def browser_open(self, url: str) -> ToolOutcome:
        """Open a public or sandbox-approved local URL in the browser.

        Starts Chromium lazily. Use web_fetch for static reading and this Tool
        only when rendering or interaction is required.

        Args:
            url: Absolute public http/https URL, or absolute file:// URL within
                the sandbox-approved filesystem scope.
        """
        return await self._browser_session().open(url)

    async def browser_snapshot(self) -> ToolOutcome:
        """Read the active page text and refresh its interactive element refs.

        Refs are temporary and may become stale after navigation or page updates.
        """
        return await self._browser_session().snapshot()

    async def browser_click(self, ref: str) -> ToolOutcome:
        """Click one element ref from the latest browser snapshot.

        Args:
            ref: Element identifier such as e1 from browser_snapshot.
        """
        if not self._sandbox.network:
            return network_disabled()
        return await self._browser_session().click(ref)

    async def browser_fill(self, ref: str, text: str) -> ToolOutcome:
        """Replace the value of one editable element from the latest snapshot.

        Never enter credentials or sensitive data unless the human explicitly
        provided and authorized it for this destination.

        Args:
            ref: Editable element identifier from browser_snapshot.
            text: Exact text to place in the element.
        """
        if not self._sandbox.network:
            return network_disabled()
        return await self._browser_session().fill(ref, text)

    async def browser_press(self, key: str) -> ToolOutcome:
        """Press one Playwright keyboard key on the active page.

        Args:
            key: Key name or chord such as Enter or Control+A.
        """
        if not self._sandbox.network:
            return network_disabled()
        return await self._browser_session().press(key)

    async def browser_select(self, ref: str, value: str) -> ToolOutcome:
        """Select one option value in a select element from the latest snapshot.

        Args:
            ref: Select element identifier from browser_snapshot.
            value: Exact option value to select.
        """
        if not self._sandbox.network:
            return network_disabled()
        return await self._browser_session().select(ref, value)

    async def browser_screenshot(self) -> ToolOutcome:
        """Capture the full active page into the thread's session artifacts."""
        return await self._browser_session().screenshot()

    async def browser_close(self) -> ToolOutcome:
        """Close the active isolated browser and discard its temporary state."""
        return await self._browser_session().close()

    def _browser_session(self) -> BrowserSession:
        if self._browser is None:
            self._browser = BrowserSession(
                network_policy=self._config.network,
                session_policy=self._config.session,
                artifacts=self._artifacts,
                sandbox=self._sandbox,
            )
        return self._browser

    def _web_access(self) -> WebAccess:
        if self._web is None:
            self._web = WebAccess(self._config.network)
        return self._web

    def diagnostics(self) -> dict[str, JsonValue]:
        return {
            "status": "ready",
            "search_backend": self._config.search.backend,
            "browser_active": bool(self._browser and self._browser.active),
        }


    async def _dispose(self) -> None:
        if self._browser is not None:
            await self._browser.shutdown()
        if self._web is not None:
            await self._web.close()
        self._browser = None
        self._web = None


plugin = BrowserPlugin()
