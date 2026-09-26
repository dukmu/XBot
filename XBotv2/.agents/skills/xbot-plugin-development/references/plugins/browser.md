# `browser`

Live search, page retrieval, and isolated browser control through Chromium. All
browser interactions run in an isolated session, and every network-touching Tool
is gated by the sandbox's network flag.

- **Import/profile:** `browser`, Agent profile.
- **Source:** `XBotv2/browser/plugin.py`, `contracts.py`, `browser.py`,
  `network.py`.
- **Injects/provides:** `tools`, `session`, `sandbox`, `artifacts` →
  (none directly; registers Tools).
- **Subscribes to events:** none.
- **Config:** `search`, `network`, `session` sub-configs.
- **Tools:** `web_search`, `web_fetch`, `browser_open`, `browser_snapshot`,
  `browser_click`, `browser_fill`, `browser_press`, `browser_select`,
  `browser_screenshot`, `browser_close`.

## Config schema (`Config = BrowserConfig`)

```python
class SearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = "yandex"
    region: str = "wt-wt"
    safesearch: str = "moderate"


class NetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout: float = Field(default=20.0, gt=0)
    max_bytes: int = Field(default=5_000_000, ge=1)
    private_access: bool = False


class BrowserSessionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = True
    timeout: float = Field(default=30.0, gt=0)


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: SearchPolicy = Field(default_factory=SearchPolicy)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    session: BrowserSessionPolicy = Field(default_factory=BrowserSessionPolicy)
```

The third sub-config is named **`session`** and its type is
`BrowserSessionPolicy`. The policy type names are `SearchPolicy`,
`NetworkPolicy`, and `BrowserSessionPolicy`; there is no `BrowserSearchConfig`,
`BrowserNetworkConfig`, or `BrowserSessionConfig`.

## Registration

```python
class BrowserPlugin:
    inject = ['tools', 'session', 'sandbox', 'artifacts']
    name = "browser"
    Config = BrowserConfig

    def __init__(self) -> None:
        self._config = BrowserConfig()
        self._web: WebAccess | None = None
        self._browser: BrowserSession | None = None

    def apply(self, ctx: Context, config: BrowserConfig) -> None:
        self._config = config
        ctx.dispose(self._dispose)
        self._artifacts = ctx.artifacts
        self._sandbox = ctx.sandbox
        for function in (...):  # each Tool method below
            ctx.tools.register(replace(Tool.from_function(function), kind="fetch"))
```

`plugin = BrowserPlugin()` is the root export. All ten Tools are registered from
their bound methods with `kind="fetch"`, marking them as network-facing. The
`WebAccess` HTTP client and the Chromium session are created lazily on first use
and torn down by the single `ctx.dispose(self._dispose)` callback — no plugin
holds either resource per call.

## Tools

| Tool | Purpose |
|---|---|
| `web_search(query, max_results=5, freshness=None)` | Search the live Web and return structured titles, URLs, snippets, and optional dates |
| `web_fetch(url)` | Retrieve one page's readable content with source metadata |
| `browser_open(url)` | Open a URL, then return a snapshot |
| `browser_snapshot()` | Re-read the current page |
| `browser_click`, `browser_fill`, `browser_press`, `browser_select` | Interact with the current page |
| `browser_screenshot()` | Capture the page as an artifact |
| `browser_close()` | Tear down the current browser session |

`web_search` accepts `freshness` as one of `"day"`, `"week"`, `"month"`, or
`"year"`.

## Network policy

```python
class WebAccess:
    """Own the HTTP client used by read-only Web tools."""

    def __init__(self, policy: NetworkPolicy) -> None:
        self.policy = policy
        ...

    async def close(self) -> None: ...
    async def search(self, ...) -> ToolOutcome: ...


async def validate_url(url: str, policy: NetworkPolicy) -> CheckedUrl: ...
def network_disabled() -> ToolFailed: ...
```

`WebAccess` takes a `NetworkPolicy` — there is no separate `NetworkOptions`
object. URL validation is the module-level `validate_url(url, policy)` returning
a `CheckedUrl`; there is no `UrlPolicy` class.

When the sandbox has no network capability the Tools return the shared
`network_disabled()` failure (`ToolFailed`). This is a runtime gate, not a
config flag: every network-facing Tool checks `self._sandbox.network` before
doing work, so disabling the sandbox network capability disables these Tools
without touching browser configuration.

## `BrowserSession` (`XBotv2/browser/browser.py`)

```python
class BrowserSession:
    def __init__(
        self,
        *,
        network_policy: NetworkPolicy,
        session_policy: BrowserSessionPolicy,
        artifacts: ArtifactStorePort,
        sandbox: SandboxPort,
    ) -> None: ...

    async def open(self, url: str) -> ToolOutcome: ...
```

Every constructor parameter is keyword-only and named `*_policy` for the two
policies. `open` takes only the URL — there is no per-call `sandbox` argument,
because the session already owns its sandbox handle.

`open` re-checks `self._sandbox.network` for non-`file:` URLs and returns
`network_disabled()`, then navigates and returns a snapshot. Failures come back
as `failed_text("browser_open_failed", ...)`.

## Sandbox interaction

Browser network access and Shell network access are the same capability. A
session whose sandbox denies network gets `network_disabled()` from the Web
Tools; it does not get a partially working browser. Do not add a browser-local
network flag — that would create a second, bypassable policy.

## Pitfalls

- Do not assume `netloc`-style validation happens in the Tool. URL safety is
  enforced by `validate_url(url, policy)`, which consults
  `NetworkPolicy.private_access`; a plugin that fetches URLs on its own would
  bypass it.
- Screenshots and fetched pages are stored as artifacts. Resolve them through
  the injected `artifacts` store rather than reading Chromium's temp paths.
- The session is lazy. A failed `browser_open` may mean the sandbox denied
  network, not that Chromium is missing; check the returned failure code before
  changing browser configuration.
