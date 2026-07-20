"""Single-page Playwright session for the Browser plugin."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from xbotv2.api import ArtifactRef, ToolResult

from .network import UrlPolicy


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
        text: (element.innerText || element.value || '').trim().slice(0, 200),
        label: (element.getAttribute('aria-label') ||
          element.getAttribute('placeholder') || '').trim().slice(0, 200)
      };
    });
  return {text: (document.body?.innerText || '').trim(), elements};
}
"""


class BrowserSession:
    """Own one isolated Chromium context and its active page."""

    def __init__(
        self,
        *,
        policy: UrlPolicy,
        artifacts_dir: Path,
        headless: bool,
        timeout_seconds: float,
    ) -> None:
        self.policy = policy
        self.artifacts_dir = artifacts_dir
        self.headless = headless
        self.timeout_ms = int(timeout_seconds * 1000)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    @property
    def active(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def open(self, url: str) -> ToolResult:
        try:
            target = await self.policy.check(url)
            page = await self._ensure_page()
            await page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
            return await self.snapshot()
        except Exception as exc:
            return ToolResult.failure("browser_open_failed", f"Browser open failed: {exc}")

    async def snapshot(self) -> ToolResult:
        if not self.active:
            return ToolResult.failure("browser_not_open", "Open a page first")
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
            return ToolResult.success(
                "\n".join(lines),
                data={
                    "url": self._page.url,
                    "title": title,
                    "elements": state["elements"],
                    "untrusted": True,
                },
            )
        except Exception as exc:
            return ToolResult.failure("browser_snapshot_failed", f"Snapshot failed: {exc}")

    async def click(self, ref: str) -> ToolResult:
        return await self._act(ref, "click")

    async def fill(self, ref: str, text: str) -> ToolResult:
        return await self._act(ref, "fill", text)

    async def press(self, key: str) -> ToolResult:
        if not self.active:
            return ToolResult.failure("browser_not_open", "Open a page first")
        try:
            await self._page.keyboard.press(key)
            await self._page.wait_for_timeout(200)
            return await self.snapshot()
        except Exception as exc:
            return ToolResult.failure("browser_press_failed", f"Key press failed: {exc}")

    async def select(self, ref: str, value: str) -> ToolResult:
        return await self._act(ref, "select_option", value)

    async def screenshot(self) -> ToolResult:
        if not self.active:
            return ToolResult.failure("browser_not_open", "Open a page first")
        directory = self.artifacts_dir / "browser"
        directory.mkdir(parents=True, exist_ok=True)
        name = f"screenshot-{time.time_ns()}.png"
        path = directory / name
        try:
            await self._page.screenshot(path=str(path), full_page=True)
        except Exception as exc:
            return ToolResult.failure("browser_screenshot_failed", f"Screenshot failed: {exc}")
        relative = f"browser/{name}"
        return ToolResult(
            content=f"Screenshot saved to session/artifacts/{relative}",
            data={"path": f"session/artifacts/{relative}", "url": self._page.url},
            artifacts=(ArtifactRef(id=relative, media_type="image/png", name=name),),
        )

    async def close(self) -> ToolResult:
        was_active = self.active
        await self.shutdown()
        return ToolResult.success(
            "Browser closed." if was_active else "Browser is already closed.",
            data={"closed": True},
        )

    async def shutdown(self) -> None:
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None

    async def _ensure_page(self) -> Any:
        if self.active:
            return self._page
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._context = await self._browser.new_context(accept_downloads=False)
            await self._context.route("**/*", self._guard_request)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
            return self._page
        except Exception:
            await self.shutdown()
            raise

    async def _guard_request(self, route: Any, request: Any) -> None:
        if urlsplit(request.url).scheme in {"about", "blob", "data"}:
            await route.continue_()
            return
        try:
            await self.policy.check(request.url)
        except Exception:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _act(self, ref: str, method: str, value: str | None = None) -> ToolResult:
        if not self.active:
            return ToolResult.failure("browser_not_open", "Open a page first")
        if not ref or not ref.replace("-", "").isalnum():
            return ToolResult.failure("invalid_ref", "Element ref is invalid")
        locator = self._page.locator(f'[data-xbot-ref="{ref}"]')
        try:
            if await locator.count() != 1:
                return ToolResult.failure(
                    "stale_ref",
                    "Element ref is missing or stale; take a new snapshot",
                )
            action = getattr(locator, method)
            await action() if value is None else await action(value)
            await self._page.wait_for_timeout(200)
            return await self.snapshot()
        except Exception as exc:
            return ToolResult.failure("browser_action_failed", f"Browser action failed: {exc}")
