# `browser`

Live search, page retrieval, and isolated browser control via Chromium.
All browser interactions are sandboxed; network access is gated by the
session's sandbox policy.

- **Import/profile:** `browser`, Agent profile.
- **Source:** `XBotv2/browser/plugin.py`,
  `XBotv2/browser/browser.py`,
  `XBotv2/browser/network.py`.
- **Injects/provides:** `tools`, `session`, `sandbox`, `artifacts` →
  (none directly; registers Tools).
- **Subscribes to events:** none.
- **Config:** `search`, `network`, `browser` sub-configs.

## Config schema (`Config = S.object(...)`)

```python
Config = S.object({
    "search": S.object({
        "backend": S.string().optional(),      # "yandex" (default)
        "region": S.string().optional(),       # "wt-wt" (default)
        "safesearch": S.string().optional(),   # "moderate" (default)
    }).optional(),
    "network": S.object({
        "timeout_seconds": S.number().optional(),       # 20.0 (default)
        "max_response_bytes": S.number().optional(),    # 5_000_000 (default)
        "allow_private": S.boolean().optional(),        # False (default)
    }).optional(),
    "browser": S.object({
        "headless": S.boolean().optional(),        # True (default)
        "timeout_seconds": S.number().optional(),   # 30.0 (default)
    }).optional(),
})
```

## Public data models

### `BrowserSession` (`XBotv2/browser/browser.py`)

```python
class BrowserSession:
    def __init__(
        self,
        *,
        policy: UrlPolicy,
        artifacts: Any,
        headless: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None: ...

    async def open(self, url: str, *, sandbox: Any = None) -> ToolResult: ...
    async def snapshot(self) -> ToolResult: ...
    async def click(self, ref: str) -> ToolResult: ...
    async def fill(self, ref: str, text: str) -> ToolResult: ...
    async def press(self, key: str) -> ToolResult: ...
    async def select(self, ref: str, value: str) -> ToolResult: ...
    async def screenshot(self) -> ToolResult: ...
    async def close(self) -> ToolResult: ...
    async def shutdown(self) -> None: ...

    @property
    def active(self) -> bool: ...
```

`open()` accepts public `http://` / `https://` URLs and sandbox-approved
`file://` URLs. Refs (e.g. `e1`) are from the latest `snapshot()` output
and are ephemeral — they become stale after navigation.

### `WebAccess` (`XBotv2/browser/network.py`)

```python
class WebAccess:
    def __init__(self, options: NetworkOptions) -> None: ...

    async def search(
        self,
        query: str,
        max_results: int = 5,
        freshness: Literal["day", "week", "month", "year"] | None = None,
        backend: str = "yandex",
        region: str = "wt-wt",
        safesearch: str = "moderate",
    ) -> ToolResult: ...

    async def fetch(self, url: str) -> ToolResult: ...

    async def close(self) -> None: ...

@dataclass
class NetworkOptions:
    timeout_seconds: float = 20.0
    max_response_bytes: int = 5_000_000
    allow_private: bool = False
```

`fetch()` HTML is reduced to Markdown; JSON and text remain textual.
Redirect targets are checked against `UrlPolicy`. `search()` returns
structured titles, URLs, snippets, and optional dates.

### `UrlPolicy` (`XBotv2/browser/network.py`)

```python
class UrlPolicy:
    def __init__(self, allow_private: bool = False) -> None: ...

    def check(self, url: str) -> str | None:
        """Return None if allowed, or 'denied' + reason if blocked."""

    def is_allowed(self, url: str) -> bool: ...
```

Blocks `file://` outside sandbox, `ftp://`, `mailto:`, `data:`,
private IPs (unless `allow_private=True`), and URLs with embedded
credentials.

### `network_available` (`XBotv2/browser/network.py`)

```python
def network_available(sandbox: Any) -> ToolResult | None:
    """Check sandbox.network + sandbox.enabled; return error or None."""
```

All browser Tools that make network calls (web_search, web_fetch,
browser_click, browser_fill, browser_press, browser_select) gate
through `network_available(self._sandbox)` first.

## Tools registered by `apply()`

| Tool | Function | Description |
|---|---|---|
| `web_search` | `self.web_search` | Search the live public Web |
| `web_fetch` | `self.web_fetch` | Fetch one public URL |
| `browser_open` | `self.browser_open` | Open URL in Chromium |
| `browser_snapshot` | `self.browser_snapshot` | Read page text + element refs |
| `browser_click` | `self.browser_click` | Click one element ref |
| `browser_fill` | `self.browser_fill` | Replace editable element text |
| `browser_press` | `self.browser_press` | Press Playwright keyboard key |
| `browser_select` | `self.browser_select` | Select option in select element |
| `browser_screenshot` | `self.browser_screenshot` | Capture page to artifacts |
| `browser_close` | `self.browser_close` | Close browser + discard state |

## How `apply()` works

```python
def apply(self, ctx, config=None):
    config = config or {}
    self._search.update(config.get("search") or {})
    network = config.get("network") or {}
    self._network_options = NetworkOptions(
        timeout_seconds=float(network.get("timeout_seconds", 20)),
        max_response_bytes=int(network.get("max_response_bytes", 5_000_000)),
        allow_private=bool(network.get("allow_private", False)),
    )
    self._url_policy = UrlPolicy(allow_private=self._network_options.allow_private)
    self._browser_options.update(config.get("browser") or {})
    ctx.dispose(self._dispose)
    self._artifacts = ctx.artifacts
    self._sandbox = ctx.sandbox
    for function in (
        self.web_search, self.web_fetch, self.browser_open,
        self.browser_snapshot, self.browser_click, self.browser_fill,
        self.browser_press, self.browser_select,
        self.browser_screenshot, self.browser_close,
    ):
        ctx.tools.register(Tool.from_function(function))
```

Lazy initialization: `BrowserSession` is created on first `browser_open`,
`WebAccess` on first `web_search` or `web_fetch`.

## On-disk artifacts

`browser_screenshot()` captures the page into the session's artifacts
directory. `browser_open()` may store the URL policy check result
and screenshot metadata.

## Cross-references

- Depends on: `tools`, `session`, `sandbox`, `artifacts`.
- Depended on by: the Agent (search/fetch/browser Tools).
- Pairs with: `sandbox` (network capability gates),
  `content-cache` (caches fetch results).

## Common pitfalls

- **Using `browser_open` for static content**: always prefer
  `web_fetch` for static HTML/JSON/text. The browser tool starts
  Chromium and is only needed for rendering or interaction.
- **Using stale element refs**: `browser_snapshot` refs (`e1`, `e2`...)
  become stale after any navigation or page update. Always call
  `snapshot` immediately before interacting.
- **`browser_fill` with credentials**: the tool documentation warns
  against entering credentials unless explicitly authorized.
  Use `browser_press` with `Enter` instead of typing passwords.
- **`web_search` with sandbox disabled**: if `sandbox is None`
  or `not sandbox.network`, returns `"network_disabled"`.
- **`browser_open` with private URLs**: blocked by `UrlPolicy`
  unless `network.allow_private=True` and the IP is not private.
- **Not calling `browser_close`**: the Chromium process continues
  until `_dispose()` fires on session cleanup.
- **`browser_screenshot` without active browser**: if no page
  is open, returns an error instead of a blank screenshot.
